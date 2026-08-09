"""
Débrief automatique. Compare les prédictions loggées par get_value_bets.py
un soir donné aux résultats réels des matchs (récupérés sur l'API MLB),
règle les picks avec mise, met à jour une bankroll de suivi persistante,
et envoie le résumé sur Telegram. Plus besoin de recopier les messages du
bot à la main pour faire le point.

Système d'unités : 1u = 1% de la bankroll de suivi (départ 100). C'est un
suivi du MODÈLE, pas forcément identique à tes mises réelles -- à toi de
comparer les deux dans ton propre débrief.

Usage :
    python debrief.py              # débrief de la date d'hier
    python debrief.py 2026-07-20   # débrief d'une date précise
"""

import sys
import json
import datetime
from pathlib import Path

from telegram_bot import send_message
from get_context_factors import get_json_with_retries, MLB_API_BASE, _winner_side

DATA_DIR = Path(__file__).parent / "data"
BANKROLL_PATH = DATA_DIR / "bankroll.json"
STARTING_BANKROLL = 100.0
VALUE_TIER_MODEREE = 0.03  # même seuil que dans get_value_bets.py


# ---------------------------------------------------------------------------
# Chargement des prédictions loggées + résultats réels
# ---------------------------------------------------------------------------

def load_predictions(date_str: str) -> dict | None:
    path = DATA_DIR / f"{date_str}.json"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_actual_results(date_str: str) -> dict:
    """Renvoie {game_pk: 'home'/'away'/None} -- None si le match n'est pas
    encore terminé (ou report, pluie, etc.).
    """
    params = {"sportId": 1, "date": date_str, "hydrate": "linescore"}
    data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)

    results = {}
    for day in data.get("dates", []):
        for game in day.get("games", []):
            game_pk = game.get("gamePk")
            if game.get("status", {}).get("abstractGameState") != "Final":
                results[game_pk] = None
                continue
            results[game_pk] = _winner_side(game)
    return results


# ---------------------------------------------------------------------------
# Bilan des SÉLECTIONS réelles (picks avec mise) -- PAS du favori du
# modèle (p_home >= 0.5). Ce concept a été retiré de get_value_bets.py le
# 6 août au profit d'une sélection unique ; ce fichier n'avait jamais été
# mis à jour en conséquence -- corrigé le 9 août.
# ---------------------------------------------------------------------------

def build_selection_summary(games: list[dict], results: dict) -> dict:
    total = 0
    hits = 0
    not_final = 0
    no_selection = 0
    details = []

    for g in games:
        winner_side = results.get(g.get("game_pk"))
        if winner_side is None:
            not_final += 1
            continue

        units = g.get("stake_units") or 0
        if units <= 0 or not g.get("best_side"):
            no_selection += 1
            continue

        total += 1
        team = g["home_team"] if g["best_side"] == "home" else g["away_team"]
        correct = g["best_side"] == winner_side
        hits += int(correct)

        detail = f"{'✅' if correct else '❌'} {g['away_team']} @ {g['home_team']} — sélection : {team} ({units}u)"
        details.append(detail)

    return {"total": total, "hits": hits, "not_final": not_final, "no_selection": no_selection, "details": details}


# ---------------------------------------------------------------------------
# Bankroll de suivi (persistante entre les soirs)
# ---------------------------------------------------------------------------

def load_bankroll() -> dict:
    if not BANKROLL_PATH.exists():
        return {"balance": STARTING_BANKROLL, "history": []}
    with open(BANKROLL_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_bankroll(bankroll: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(BANKROLL_PATH, "w", encoding="utf-8") as f:
        json.dump(bankroll, f, indent=2, ensure_ascii=False)


def settle_bets(date_str: str, games: list[dict], results: dict, bankroll: dict) -> list[dict]:
    """Règle les picks avec mise (stake_units > 0) du jour donné et met à
    jour la bankroll. Idempotent : ignore les matchs déjà réglés pour cette
    date si le débrief est relancé plusieurs fois.
    """
    already_settled = {h["game_pk"] for h in bankroll["history"] if h["date"] == date_str}
    settled = []

    for g in games:
        units = g.get("stake_units") or 0
        if units <= 0:
            continue

        game_pk = g.get("game_pk")
        if game_pk in already_settled:
            continue

        winner_side = results.get(game_pk)
        if winner_side is None:
            continue  # pas encore terminé -- on le réglera à un prochain lancement

        won = g["best_side"] == winner_side
        price = g.get("stake_price")

        pnl = None
        if price:
            pnl = units * (price - 1) if won else -units
            bankroll["balance"] += pnl

        team = g["home_team"] if g["best_side"] == "home" else g["away_team"]
        entry = {
            "date": date_str,
            "game_pk": game_pk,
            "team": team,
            "units": units,
            "price": price,
            "won": won,
            "pnl": pnl,
            "balance_after": bankroll["balance"],
        }
        bankroll["history"].append(entry)
        settled.append(entry)

    return settled


# ---------------------------------------------------------------------------
# Message final
# ---------------------------------------------------------------------------

def _pct(hits: int, total: int) -> str:
    return f"{hits}/{total} ({hits/total*100:.0f}%)" if total else "0/0"


def format_debrief_message(
    date_str: str, selection_summary: dict, settled: list[dict], bankroll: dict, collection_errors: list[dict] = None
) -> str:
    collection_errors = collection_errors or []

    if selection_summary["total"] == 0:
        extra = []
        if selection_summary["not_final"]:
            extra.append(f"{selection_summary['not_final']} match(s) pas encore terminé(s)")
        if selection_summary["no_selection"]:
            extra.append(f"{selection_summary['no_selection']} match(s) sans sélection ce soir-là")
        extra_text = f" ({', '.join(extra)})" if extra else ""
        return f"📋 <b>Débrief {date_str}</b>\n\nAucune sélection réglable trouvée pour cette date{extra_text}.\n"

    lines = [f"📋 <b>Débrief {date_str}</b>\n"]
    lines.append(f"Sélection correcte : {_pct(selection_summary['hits'], selection_summary['total'])}\n")

    if selection_summary["not_final"] or selection_summary["no_selection"] or collection_errors:
        lines.append(
            f"({selection_summary['not_final']} match(s) pas encore terminé(s), "
            f"{selection_summary['no_selection']} sans sélection ce soir-là, "
            f"{len(collection_errors)} match(s) ignoré(s) la veille à la collecte)\n"
        )

    lines.append("\n<b>Détail des sélections :</b>\n")
    lines.extend(f"{d}\n" for d in selection_summary["details"])

    if collection_errors:
        lines.append("\n<b>Matchs ignorés hier soir (échec de collecte) :</b>\n")
        for err in collection_errors:
            lines.append(f"⚠️ {err['matchup']} — {err['reason']}\n")

    if settled:
        wins = sum(1 for s in settled if s["won"])
        total_pnl = sum(s["pnl"] for s in settled if s["pnl"] is not None)
        lines.append(f"\n<b>Picks avec mise réglés ce soir</b> : {wins}/{len(settled)}\n")
        for s in settled:
            icon = "✅" if s["won"] else "❌"
            pnl_text = f"{s['pnl']:+.2f}u" if s["pnl"] is not None else "prix inconnu -- non chiffré"
            lines.append(f"{icon} {s['team']} — {s['units']}u @ {s['price'] or '?'} → {pnl_text}\n")
        lines.append(f"\nP&L de la soirée : <b>{total_pnl:+.2f}u</b>\n")
        lines.append(f"Bankroll de suivi : <b>{bankroll['balance']:.2f}</b> (départ 100)\n")
    else:
        lines.append(f"\nAucun pick avec mise ce soir-là. Bankroll de suivi inchangée : <b>{bankroll['balance']:.2f}</b>\n")

    return "\n".join(lines)




def chunk_message(text: str, limit: int = 3800) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def parse_date_arg(raw: str) -> str:
    """Accepte les dates avec des '/' ou des '-' (ex: 2026/07/21 ou
    2026-07-21) pour éviter une erreur silencieuse sur un simple détail
    de syntaxe.
    """
    normalized = raw.replace("/", "-")
    datetime.date.fromisoformat(normalized)  # lève une erreur claire si le format est vraiment invalide
    return normalized


def main() -> None:
    date_str = parse_date_arg(sys.argv[1]) if len(sys.argv) > 1 else (datetime.date.today() - datetime.timedelta(days=1)).isoformat()

    predictions = load_predictions(date_str)
    if predictions is None:
        send_message(f"Pas de prédictions loggées trouvées pour le {date_str}. As-tu lancé get_value_bets.py ce soir-là ?")
        print(f"Aucun fichier data/{date_str}.json trouvé.")
        return

    print(f"Récupération des résultats réels du {date_str}...")
    results = get_actual_results(date_str)
    games = predictions.get("games", [])
    collection_errors = predictions.get("collection_errors", [])

    selection_summary = build_selection_summary(games, results)

    bankroll = load_bankroll()
    settled = settle_bets(date_str, games, results, bankroll)
    if settled:
        save_bankroll(bankroll)

    message = format_debrief_message(date_str, selection_summary, settled, bankroll, collection_errors)
    for chunk in chunk_message(message):
        send_message(chunk)

    print(
        f"Débrief envoyé pour {date_str} : {selection_summary['total']} sélection(s) analysée(s), "
        f"{selection_summary['not_final']} pas encore terminé(s), {len(settled)} pick(s) réglé(s)."
    )


if __name__ == "__main__":
    main()
