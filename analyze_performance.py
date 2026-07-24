"""
Analyse de performance du modèle. Agrège TOUS les logs de prédictions
(data/*.json) avec les résultats réels, pour identifier quelles
composantes/quels réglages sont le plus corrélés avec des picks gagnants.

À lancer quand tu as accumulé assez d'historique -- quelques semaines,
pas quelques jours. Les logs antérieurs à l'enrichissement (composantes
détaillées) sont comptés dans les totaux globaux mais ignorés dans la
ventilation par composante, faute de détail conservé à l'époque.

Usage :
    python analyze_performance.py
"""

import json
from pathlib import Path
from collections import defaultdict

from get_context_factors import get_json_with_retries, MLB_API_BASE, _winner_side

DATA_DIR = Path(__file__).parent / "data"
SIGNIFICANT_THRESHOLD = 0.02  # 2 points -- même seuil que le "Détail" vulgarisé


def load_all_logs() -> list[dict]:
    logs = []
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name == "bankroll.json":
            continue
        with open(path, "r", encoding="utf-8") as f:
            logs.append(json.load(f))
    return logs


def get_results_for_date(date_str: str) -> dict:
    params = {"sportId": 1, "date": date_str, "hydrate": "linescore"}
    data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)
    results = {}
    for day in data.get("dates", []):
        for game in day.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            results[game.get("gamePk")] = _winner_side(game)
    return results


def _new_bucket() -> dict:
    return {"correct": 0, "total": 0}


def _pct(bucket: dict) -> str:
    if bucket["total"] == 0:
        return "0/0 (--)"
    return f"{bucket['correct']}/{bucket['total']} ({bucket['correct']/bucket['total']*100:.0f}%)"


def analyze() -> None:
    logs = load_all_logs()
    if not logs:
        print("Aucun fichier de log trouvé dans data/. Lance get_value_bets.py quelques soirs d'abord.")
        return

    favori = _new_bucket()
    value = _new_bucket()
    dominant_component = defaultdict(_new_bucket)   # composante la plus significative -> favori correct ?
    stake_tier = defaultdict(_new_bucket)             # unités de mise -> value correcte ?
    books_agree = defaultdict(_new_bucket)            # accord de books -> value correcte ?
    lineup_used = defaultdict(_new_bucket)            # line-up réel utilisé -> value correcte ?
    nights_analyzed = 0

    for log in logs:
        date_str = log.get("date")
        games = log.get("games", [])
        if not date_str or not games:
            continue

        results = get_results_for_date(date_str)
        nights_analyzed += 1

        for g in games:
            winner_side = results.get(g.get("game_pk"))
            if winner_side is None:
                continue  # pas encore terminé, ou match introuvable

            favori_side = "home" if g["p_home"] >= 0.5 else "away"
            favori_ok = favori_side == winner_side
            favori["total"] += 1
            favori["correct"] += int(favori_ok)

            components = g.get("components")
            if components:
                dominant_name, dominant_val = max(components.items(), key=lambda kv: abs(kv[1]))
                if abs(dominant_val) >= SIGNIFICANT_THRESHOLD:
                    dominant_component[dominant_name]["total"] += 1
                    dominant_component[dominant_name]["correct"] += int(favori_ok)

            units = g.get("stake_units") or 0
            if units > 0 and g.get("best_side"):
                value_ok = g["best_side"] == winner_side
                value["total"] += 1
                value["correct"] += int(value_ok)

                stake_tier[units]["total"] += 1
                stake_tier[units]["correct"] += int(value_ok)

                books_agree[g.get("books_agree")]["total"] += 1
                books_agree[g.get("books_agree")]["correct"] += int(value_ok)

                used_lineup = bool(g.get("home_lineup_used") or g.get("away_lineup_used"))
                lineup_used[used_lineup]["total"] += 1
                lineup_used[used_lineup]["correct"] += int(value_ok)

    print(f"=== {nights_analyzed} soirée(s) analysée(s) ===\n")
    print(f"Favori (modèle) global : {_pct(favori)}")
    print(f"Value (picks avec mise) globale : {_pct(value)}\n")

    print("--- Par composante dominante du match (favori correct ?) ---")
    print("(uniquement les matchs où cette composante était la plus significative, >= 2 pts)\n")
    for name, bucket in sorted(dominant_component.items(), key=lambda kv: -kv[1]["total"]):
        print(f"  {name}: {_pct(bucket)}")

    print("\n--- Par palier de mise (value correcte ?) ---")
    for tier, bucket in sorted(stake_tier.items()):
        print(f"  {tier}u: {_pct(bucket)}")

    print("\n--- Accord Betclic/Unibet vs Pinnacle (value correcte ?) ---")
    for key, bucket in books_agree.items():
        label = "d'accord" if key else "pas d'accord / non disponible"
        print(f"  {label}: {_pct(bucket)}")

    print("\n--- Line-up réel confirmé (value correcte ?) ---")
    for key, bucket in lineup_used.items():
        label = "line-up réel" if key else "OPS de saison (repli)"
        print(f"  {label}: {_pct(bucket)}")

    print(
        "\nRappel : sous ~100 mises par catégorie, ces chiffres restent indicatifs, "
        "pas des conclusions statistiquement fiables. Utile pour repérer des tendances "
        "à surveiller, pas pour trancher définitivement."
    )


if __name__ == "__main__":
    analyze()
