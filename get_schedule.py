"""
Étape 1 du projet : récupérer le programme MLB du jour, avec les pitchers
probables annoncés, et envoyer la liste sur Telegram.

Source des données : l'API publique et gratuite de MLB (statsapi.mlb.com).
Aucune clé n'est nécessaire pour cet endpoint.

Usage :
    python get_schedule.py                  -> matchs d'aujourd'hui
    python get_schedule.py 2026-07-21        -> matchs d'une date précise
"""

import sys
import datetime
import requests

from telegram_bot import send_message

MLB_SCHEDULE_URL = "https://statsapi.mlb.com/api/v1/schedule"
SPORT_ID_MLB = 1


def get_games_for_date(date: str | None = None) -> list[dict]:
    """Récupère la liste des matchs MLB pour une date donnée (format YYYY-MM-DD).

    Si aucune date n'est fournie, on utilise la date du jour.
    """
    if date is None:
        date = datetime.date.today().isoformat()

    params = {
        "sportId": SPORT_ID_MLB,
        "date": date,
        # "hydrate" demande à l'API d'inclure des infos supplémentaires :
        # le pitcher probable et le stade, en plus des infos de base.
        "hydrate": "team,probablePitcher,venue",
    }

    response = requests.get(MLB_SCHEDULE_URL, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    games = []
    for day in data.get("dates", []):
        games.extend(day.get("games", []))
    return games


def format_game(game: dict) -> str:
    """Transforme les données brutes d'un match en texte lisible pour Telegram."""
    home_team = game["teams"]["home"]["team"]["name"]
    away_team = game["teams"]["away"]["team"]["name"]

    home_pitcher = (
        game["teams"]["home"].get("probablePitcher", {}).get("fullName")
        or "Titulaire non encore annoncé"
    )
    away_pitcher = (
        game["teams"]["away"].get("probablePitcher", {}).get("fullName")
        or "Titulaire non encore annoncé"
    )

    venue = game.get("venue", {}).get("name", "Stade inconnu")

    # game["gameDate"] est au format ISO UTC (ex: 2026-07-20T23:10:00Z)
    # On la garde brute pour l'instant, on gérera la conversion de fuseau
    # horaire proprement à une étape suivante.
    game_time_utc = game.get("gameDate", "")

    return (
        f"⚾ <b>{away_team} @ {home_team}</b>\n"
        f"🏟 {venue}\n"
        f"🕐 {game_time_utc} (UTC)\n"
        f"Titulaire {away_team} : {away_pitcher}\n"
        f"Titulaire {home_team} : {home_pitcher}\n"
    )


def chunk_message(text: str, limit: int = 3800) -> list[str]:
    """Découpe un message trop long en plusieurs morceaux.

    Telegram refuse les messages de plus de 4096 caractères ; on garde une
    marge de sécurité avec une limite à 3800.
    """
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def main() -> None:
    # On peut passer une date en argument, sinon on prend aujourd'hui
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None

    games = get_games_for_date(date_arg)

    if not games:
        send_message("Aucun match MLB programmé pour cette date.")
        print("Aucun match trouvé.")
        return

    header = f"📋 <b>Programme MLB</b> — {len(games)} match(s)\n\n"
    body = "\n".join(format_game(game) for game in games)
    full_message = header + body

    for chunk in chunk_message(full_message):
        send_message(chunk)

    print(f"{len(games)} match(s) envoyé(s) sur Telegram.")


if __name__ == "__main__":
    main()
