"""
Outil de diagnostic ponctuel (pas un script du pipeline quotidien) : liste
tous les bookmakers que The Odds API renvoie réellement pour le baseball_mlb
ce soir, pour savoir ce qui est disponible sans deviner.

Usage :
    python check_bookmakers.py
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

    # L'API exige AU MOINS "regions" ou "bookmakers" (sinon erreur 422) -- on
    # met toutes les régions valides pour voir vraiment tout ce qui existe.
    params = {"apiKey": ODDS_API_KEY, "regions": "us,uk,eu,au", "markets": "h2h", "oddsFormat": "decimal"}
    events = get_json_with_retries(f"{ODDS_API_BASE}/sports/baseball_mlb/odds", params)

    if not events:
        print("Aucun match retourné par l'API pour l'instant (pas encore ouvert ?).")
        return

    all_bookmakers = set()
    for event in events:
        for bookmaker in event.get("bookmakers", []):
            all_bookmakers.add(bookmaker.get("key"))

    print(f"{len(events)} match(s) MLB trouvé(s).")
    print(f"Bookmakers présents (toutes régions confondues) : {sorted(all_bookmakers)}")

    if "betclic" in all_bookmakers:
        print("\n✅ Betclic EST présent pour au moins un match ce soir.")
    else:
        print("\n❌ Betclic n'apparaît sur AUCUN match MLB ce soir, tous bookmakers confondus.")
        print("   Confirmation que ce n'est pas un souci de paramètre : Betclic ne semble")
        print("   simplement pas intégré au flux MLB de cette API.")


if __name__ == "__main__":
    main()
