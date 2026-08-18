"""
Outil de diagnostic ponctuel : vérifie la couverture RÉELLE de PMU
(pmu_fr) sur le marché Total pour le baseball_mlb -- pas juste un match
isolé, mais combien de matchs du jour sont vraiment couverts.

Usage :
    python check_pmu_totals_coverage.py
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

    params = {"apiKey": ODDS_API_KEY, "regions": "eu", "markets": "totals", "oddsFormat": "decimal"}
    events = get_json_with_retries(f"{ODDS_API_BASE}/sports/baseball_mlb/odds", params)

    if not events:
        print("Aucun match retourné (pas encore ouvert, ou marché absent).")
        return

    print(f"{len(events)} match(s) MLB trouvé(s) aujourd'hui.\n")

    covered = 0
    not_covered = []

    for event in events:
        matchup = f"{event.get('away_team')} @ {event.get('home_team')}"
        pmu_market = None
        pinnacle_market = None

        for bookmaker in event.get("bookmakers", []):
            if bookmaker.get("key") == "pmu_fr":
                for market in bookmaker.get("markets", []):
                    if market.get("key") == "totals":
                        pmu_market = market.get("outcomes", [])
            if bookmaker.get("key") == "pinnacle":
                for market in bookmaker.get("markets", []):
                    if market.get("key") == "totals":
                        pinnacle_market = market.get("outcomes", [])

        if pmu_market:
            covered += 1
            pin_note = "Pinnacle aussi dispo" if pinnacle_market else "Pinnacle absent sur ce match"
            print(f"✅ {matchup} -- PMU : {pmu_market} ({pin_note})")
        else:
            not_covered.append(matchup)

    print(f"\n=== Résumé ===")
    print(f"PMU couvre {covered}/{len(events)} match(s) ({covered/len(events)*100:.0f}%)")
    if not_covered:
        print(f"Non couverts par PMU : {', '.join(not_covered)}")


if __name__ == "__main__":
    main()