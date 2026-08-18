"""
Outil de diagnostic ponctuel : vérifie si le marché Total (over/under) est
réellement disponible pour le baseball_mlb, et chez quels bookmakers.

La doc officielle de The Odds API indique que les marchés totals/spreads
sont "surtout disponibles pour les sports et bookmakers US" -- on vérifie
ici concrètement avant de construire tout un modèle dessus.

Usage :
    python check_totals_market.py
"""

import os
from dotenv import load_dotenv
from get_context_factors import get_json_with_retries

load_dotenv()

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"


def main() -> None:
    if not ODDS_API_KEY:
        print("ODDS_API_KEY manquant dans le .env.")
        return

    print("=== Test 1 : région eu (Pinnacle/Betclic/Unibet) ===")
    params_eu = {"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "totals", "oddsFormat": "decimal"}
    events_eu = get_json_with_retries(f"{ODDS_API_BASE}/sports/baseball_mlb/odds", params_eu)
    _report(events_eu)

    print("\n=== Test 2 : région us (pour comparaison) ===")
    params_us = {"apiKey": ODDS_API_KEY, "regions": "us", "markets": "totals", "oddsFormat": "decimal"}
    events_us = get_json_with_retries(f"{ODDS_API_BASE}/sports/baseball_mlb/odds", params_us)
    _report(events_us)


def _report(events: list[dict]) -> None:
    if not events:
        print("Aucun match retourné (pas encore ouvert, ou marché absent).")
        return

    books_with_totals = set()
    sample_line = None

    for event in events:
        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") == "totals":
                    books_with_totals.add(bookmaker.get("key"))
                    if sample_line is None:
                        outcomes = market.get("outcomes", [])
                        sample_line = (
                            f"{event.get('home_team')} @ {event.get('away_team')} -- "
                            f"{bookmaker.get('key')} : {outcomes}"
                        )

    print(f"{len(events)} match(s) trouvé(s).")
    if books_with_totals:
        print(f"✅ Bookmakers proposant le marché Total : {sorted(books_with_totals)}")
        print(f"Exemple de ligne : {sample_line}")
    else:
        print("❌ Aucun bookmaker de cette région ne propose le marché Total pour l'instant.")


if __name__ == "__main__":
    main()