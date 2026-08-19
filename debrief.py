"""
Débrief automatique. Compare les prédictions loggées par get_value_bets.py
un soir donné aux résultats réels des matchs (récupérés sur l'API MLB),
règle les picks avec mise, met à jour une bankroll de suivi persistante,
et envoie le résumé sur Telegram. Plus besoin de recopier les messages du
bot à la main pour faire le point.

Système d'unités : 1u = 1% de la bankroll de suivi (départ 100). C'est un
suivi du MODÈLE, pas forcément identique à tes mises réelles -- à toi de
comparer les deux dans ton propre débrief.

Depuis le 19 août : le marché Total (over/under) a sa PROPRE bankroll,
entièrement séparée (bankroll_totals.json) -- jamais mélangée à celle du
moneyline (bankroll.json). Section à part dans le message, réglée à
partir du total de runs RÉEL du match (pas juste qui a gagné).

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
BANKROLL_TOTALS_PATH = DATA_DIR / "bankroll_totals.json"
STARTING_BANKROLL = 100.0
VALUE_TIER_MODEREE = 0.03  # même seuil que dans get_value_bets.py
EUR_PER_UNIT = 5.0  # même grille que get_value_bets.py (0.5u=2.5€, 1u=5€, 2u=10€)


def compute_fair_odds(prob: float | None) -> float | None:
    """Même formule que dans get_value_bets.py -- dupliquée ici pour que
    les deux scripts restent indépendants (pas d'import croisé)."""
    if not prob or prob <= 0:
        return None
    return 1 / prob


def _short_tier_label(reasons: list[str]) -> str:
    """Étiquette courte extraite des raisons de mise -- même logique que
    get_value_bets.py, dupliquée pour la même raison d'indépendance."""
    if not reasons:
        return ""
    text = reasons[0]
    if "suspect" in text:
        return "suspect"
    if "neutre" in text:
        return "signal neutre"
    if "seul facteur" in text:
        return "1 seul facteur"
    if "facteurs indépendants" in text:
        return "confirmé"
    return ""


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


def get_actual_total_runs(date_str: str) -> dict:
    """Renvoie {game_pk: total_de_runs_reel} -- absent si le match n'est
    pas encore terminé. Appel séparé de get_actual_results (même
    structure de requête, mais currency différente : le nombre total de
    runs, pas qui a gagné) -- utilisé UNIQUEMENT pour régler les mises sur
    le Total, jamais pour le moneyline.
    """
    params = {"sportId": 1, "date": date_str, "hydrate": "linescore"}
    data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)

    totals = {}
    for day in data.get("dates", []):
        for game in day.get("games", []):
            game_pk = game.get("gamePk")
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            linescore_teams = game.get("linescore", {}).get("teams", {})
            home_runs = linescore_teams.get("home", {}).get("runs")
            away_runs = linescore_teams.get("away", {}).get("runs")
            if home_runs is not None and away_runs is not None:
                totals[game_pk] = home_runs + away_runs
    return totals


# ---------------------------------------------------------------------------
# Bilan complet -- TOUS les matchs (pas seulement ceux avec mise), groupés
# par palier de confiance, avec le résultat réel de chacun. Miroir du
# récap de fin de soirée dans get_value_bets.py (9 août) : sert à voir si
# le seuil de 3 pts fait rater des victoires évidentes, pas seulement à
# auditer les mises déjà prises.
# ---------------------------------------------------------------------------

def build_full_debrief(games: list[dict], results: dict, bankroll_history_today: list[dict]) -> dict:
    pnl_by_pk = {h["game_pk"]: h for h in bankroll_history_today}
    tiers: dict[float, list[dict]] = {2.0: [], 1.0: [], 0.5: [], 0.0: []}
    not_final = 0
    no_pinnacle = 0

    for g in games:
        winner_side = results.get(g.get("game_pk"))
        if winner_side is None:
            not_final += 1
            continue

        side = g.get("best_side")
        if side is None:
            no_pinnacle += 1
            continue

        units = g.get("stake_units") or 0
        team = g["home_team"] if side == "home" else g["away_team"]
        won = side == winner_side

        settled_entry = pnl_by_pk.get(g.get("game_pk"))
        if settled_entry is not None:
            price = settled_entry["price"]
            pnl = settled_entry["pnl"]
        else:
            prob = g["p_home"] if side == "home" else 1 - g["p_home"]
            price = compute_fair_odds(prob)
            pnl = None

        entry = {
            "team": team,
            "edge": g.get("best_edge") or 0.0,
            "units": units,
            "won": won,
            "price": price,
            "pnl": pnl,
            "tier_label": _short_tier_label(g.get("stake_reasons", [])),
            "hypothetical": units <= 0,  # pas de vraie mise -- juste informatif
        }
        tiers.setdefault(units, []).append(entry)

    return {"tiers": tiers, "not_final": not_final, "no_pinnacle": no_pinnacle}


# ---------------------------------------------------------------------------
# Bankroll de suivi (persistante entre les soirs) -- MONEYLINE
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
# Bankroll de suivi séparée -- TOTAL (over/under), ajoutée le 19 août.
# Même principe que ci-dessus, mais entièrement indépendante : fichier à
# part, réglée à partir du total de runs RÉEL (pas du vainqueur), et gère
# le cas du "push" (ligne entière tombant exactement juste -- rare avec
# des lignes à 0.5, mais possible si un book propose une ligne entière).
# ---------------------------------------------------------------------------

def load_bankroll_totals() -> dict:
    if not BANKROLL_TOTALS_PATH.exists():
        return {"balance": STARTING_BANKROLL, "history": []}
    with open(BANKROLL_TOTALS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_bankroll_totals(bankroll: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    with open(BANKROLL_TOTALS_PATH, "w", encoding="utf-8") as f:
        json.dump(bankroll, f, indent=2, ensure_ascii=False)


def settle_totals_bets(date_str: str, games: list[dict], actual_totals: dict, bankroll: dict) -> list[dict]:
    """Règle les mises sur le Total -- même principe que settle_bets,
    mais complètement séparé (bankroll, historique, et résultat basé sur
    le total de runs réel, pas le vainqueur du match).
    """
    already_settled = {h["game_pk"] for h in bankroll["history"] if h["date"] == date_str}
    settled = []

    for g in games:
        totals_info = g.get("totals") or {}
        units = totals_info.get("units") or 0
        if units <= 0:
            continue

        game_pk = g.get("game_pk")
        if game_pk in already_settled:
            continue

        real_total = actual_totals.get(game_pk)
        if real_total is None:
            continue  # pas encore terminé

        line = totals_info.get("line")
        side = totals_info.get("side")
        price = totals_info.get("price")
        if line is None or side is None:
            continue

        if real_total == line:
            won = None  # push -- remboursé, ni gagné ni perdu (ligne entière)
        elif side == "over":
            won = real_total > line
        else:
            won = real_total < line

        pnl = 0.0 if won is None else None
        if won is not None and price:
            pnl = units * (price - 1) if won else -units
            bankroll["balance"] += pnl

        matchup = f"{g['away_team']} @ {g['home_team']}"
        entry = {
            "date": date_str,
            "game_pk": game_pk,
            "matchup": matchup,
            "side": side,
            "line": line,
            "real_total": real_total,
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

def format_debrief_message(
    date_str: str, debrief: dict, bankroll: dict, bankroll_history_today: list[dict], collection_errors: list[dict] = None
) -> str:
    collection_errors = collection_errors or []
    tiers = debrief["tiers"]
    total_known = sum(len(v) for v in tiers.values())

    if total_known == 0:
        extra = []
        if debrief["not_final"]:
            extra.append(f"{debrief['not_final']} match(s) pas encore terminé(s)")
        if debrief["no_pinnacle"]:
            extra.append(f"{debrief['no_pinnacle']} match(s) sans cotes Pinnacle")
        extra_text = f" ({', '.join(extra)})" if extra else ""
        return f"📋 <b>Débrief {date_str}</b>\n\nAucun match analysable trouvé pour cette date{extra_text}.\n"

    lines = [f"📋 <b>Débrief {date_str}</b>\n"]

    def render_tier(label: str, marker: str, group: list[dict]) -> None:
        if not group:
            return
        wins = sum(1 for e in group if e["won"])
        lines.append(f"\n{marker} <b>{label}</b> — {wins}/{len(group)}\n")
        for e in sorted(group, key=lambda x: -x["edge"]):
            icon = "✅" if e["won"] else "❌"
            tag = f" ({e['tier_label']})" if e["tier_label"] else ""
            if e["hypothetical"]:
                detail = f" (aurait rapporté @{e['price']:.2f})" if e["won"] and e["price"] else ""
                lines.append(f"{icon} {e['team']}{tag}{detail}\n")
            else:
                pnl_text = f" → {e['pnl']:+.2f}u" if e["pnl"] is not None else " (prix inconnu -- non chiffré)"
                lines.append(f"{icon} {e['team']}{tag} @{e['price'] or '?'}{pnl_text}\n")

    render_tier("CONFIANCE FORTE (2u)", "🟢", tiers[2.0])
    render_tier("CONFIANCE MODÉRÉE (1u)", "🔵", tiers[1.0])
    render_tier("MÉFIANCE (0.5u)", "🟠", tiers[0.5])
    render_tier("AUCUNE SÉLECTION", "⚪", tiers[0.0])

    if debrief["not_final"] or debrief["no_pinnacle"] or collection_errors:
        lines.append(
            f"\n({debrief['not_final']} pas encore terminé(s), {debrief['no_pinnacle']} sans cotes Pinnacle, "
            f"{len(collection_errors)} ignoré(s) la veille à la collecte)\n"
        )

    if collection_errors:
        lines.append("\n<b>Matchs ignorés hier soir (échec de collecte) :</b>\n")
        for err in collection_errors:
            lines.append(f"⚠️ {err['matchup']} — {err['reason']}\n")

    if bankroll_history_today:
        total_pnl = sum(h["pnl"] for h in bankroll_history_today if h["pnl"] is not None)
        lines.append(f"\nP&L moneyline de la soirée : <b>{total_pnl:+.2f}u</b> ({total_pnl*EUR_PER_UNIT:+.2f}€)\n")
        lines.append(f"Bankroll moneyline : <b>{bankroll['balance']:.2f}</b> (départ 100)\n")
    else:
        lines.append(f"\nAucun pick moneyline avec mise ce soir-là. Bankroll moneyline inchangée : <b>{bankroll['balance']:.2f}</b>\n")

    return "\n".join(lines)


def format_totals_debrief_section(totals_history_today: list[dict], bankroll_totals: dict) -> str:
    """Section Total (over/under) du débrief -- ENTIÈREMENT séparée de la
    section moneyline ci-dessus, avec sa propre bankroll. N'affiche rien
    du tout si aucune mise Total n'a été réglée ce jour-là (pas de bruit
    inutile).
    """
    if not totals_history_today:
        return ""

    wins = sum(1 for s in totals_history_today if s["won"] is True)
    losses = sum(1 for s in totals_history_today if s["won"] is False)
    pushes = sum(1 for s in totals_history_today if s["won"] is None)
    settled_count = wins + losses

    push_note = f" ({pushes} push)" if pushes else ""
    lines = [f"\n🎯 <b>TOTAL (OVER/UNDER)</b> — {wins}/{settled_count}{push_note}\n"]

    for s in totals_history_today:
        if s["won"] is None:
            icon = "➖"
        else:
            icon = "✅" if s["won"] else "❌"
        side_label = "Over" if s["side"] == "over" else "Under"
        pnl_text = f" → {s['pnl']:+.2f}u" if s["pnl"] is not None else " (prix inconnu -- non chiffré)"
        lines.append(
            f"{icon} {s['matchup']} — {side_label} {s['line']} @{s['price'] or '?'} "
            f"(réel : {s['real_total']}){pnl_text}\n"
        )

    total_pnl = sum(s["pnl"] for s in totals_history_today if s["pnl"] is not None)
    lines.append(f"\nP&L Total de la soirée : <b>{total_pnl:+.2f}u</b> ({total_pnl*EUR_PER_UNIT:+.2f}€)\n")
    lines.append(f"Bankroll Total (séparée du moneyline) : <b>{bankroll_totals['balance']:.2f}</b> (départ 100)\n")

    return "".join(lines)


def chunk_message(text: str, limit: int = 3800) -> list[str]:
    """Découpe un message trop long en plusieurs morceaux -- UNIQUEMENT
    entre deux lignes complètes, jamais au milieu d'une ligne (voir
    get_value_bets.py pour le détail du bug corrigé le 11 août)."""
    if len(text) <= limit:
        return [text]

    chunks = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        line_with_newline = line + "\n"
        if current_len + len(line_with_newline) > limit and current:
            chunks.append("".join(current))
            current = []
            current_len = 0
        current.append(line_with_newline)
        current_len += len(line_with_newline)
    if current:
        chunks.append("".join(current))
    return chunks


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
    actual_totals = get_actual_total_runs(date_str)
    games = predictions.get("games", [])
    collection_errors = predictions.get("collection_errors", [])

    bankroll = load_bankroll()
    settled = settle_bets(date_str, games, results, bankroll)
    if settled:
        save_bankroll(bankroll)

    bankroll_totals = load_bankroll_totals()
    settled_totals = settle_totals_bets(date_str, games, actual_totals, bankroll_totals)
    if settled_totals:
        save_bankroll_totals(bankroll_totals)

    # L'historique COMPLET de cette date (pas juste ce qui vient d'être réglé
    # à cet instant) -- important si le débrief est relancé plusieurs fois
    # pour la même date (ex: après un rattrapage de données).
    bankroll_history_today = [h for h in bankroll["history"] if h["date"] == date_str]
    totals_history_today = [h for h in bankroll_totals["history"] if h["date"] == date_str]

    selection_summary = build_full_debrief(games, results, bankroll_history_today)

    message = format_debrief_message(date_str, selection_summary, bankroll, bankroll_history_today, collection_errors)
    message += format_totals_debrief_section(totals_history_today, bankroll_totals)

    for chunk in chunk_message(message):
        send_message(chunk)

    total_known = sum(len(v) for v in selection_summary["tiers"].values())
    print(
        f"Débrief envoyé pour {date_str} : {total_known} match(s) analysé(s) (moneyline), "
        f"{selection_summary['not_final']} pas encore terminé(s), {len(settled)} pick(s) moneyline réglé(s), "
        f"{len(settled_totals)} pick(s) Total réglé(s) ce lancement."
    )


if __name__ == "__main__":
    main()
