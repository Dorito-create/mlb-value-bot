"""
Étape 2 : pour chaque match du jour, récupère les stats du titulaire
probable de chaque équipe (ERA, WHIP, K, BB, HR, IP + un FIP calculé
nous-mêmes), ainsi que l'offense de l'équipe adverse (AVG/OBP/SLG/OPS).

Pourquoi pas FanGraphs (via pybaseball) ? Depuis mi-2025, FanGraphs
bloque les accès automatisés (erreur 403), un problème connu et non
résolu à ce jour par les mainteneurs de pybaseball. On utilise donc
uniquement l'API officielle MLB (statsapi.mlb.com) : mêmes serveurs que
get_schedule.py, pas de scraping, pas de blocage.

Autre avantage : le programme du jour nous donne déjà l'ID de chaque
titulaire et de chaque équipe, donc on n'a plus besoin de deviner les
noms — on interroge l'API directement avec ces IDs.

Usage :
    python get_matchup_stats.py
    python get_matchup_stats.py 2026-07-21
"""

import sys
import datetime
import requests

from get_schedule import get_games_for_date
from telegram_bot import send_message

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
CURRENT_SEASON = datetime.date.today().year

# Constante standard pour ramener le FIP sur l'échelle de l'ERA (~3.10
# la plupart des saisons). Voir https://library.fangraphs.com/pitching/era-fip-xfip/
FIP_CONSTANT = 3.10


def innings_to_float(ip_value) -> float:
    """Convertit un nombre de manches lancées au format baseball en décimal.

    Piège classique : "142.1" ne veut PAS dire 142.1 manches en décimal,
    mais 142 manches et 1/3 (le chiffre après le point compte des tiers
    de manche : .1 = 1/3, .2 = 2/3). Cette fonction fait la conversion
    correctement.
    """
    if ip_value is None:
        return 0.0

    text = str(ip_value)
    if "." not in text:
        return float(text)

    whole_part, third_part = text.split(".")
    thirds = {"0": 0.0, "1": 1 / 3, "2": 2 / 3}
    return float(whole_part) + thirds.get(third_part, 0.0)


def get_pitcher_season_stats(pitcher_id: int, season: int) -> dict | None:
    """Récupère les stats saison d'un pitcher via son ID MLBAM."""
    url = f"{MLB_API_BASE}/people/{pitcher_id}/stats"
    params = {"stats": "season", "group": "pitching", "season": season}

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    stats_groups = data.get("stats", [])
    if not stats_groups or not stats_groups[0].get("splits"):
        return None  # pas encore de stats cette saison (ex: tout juste rappelé des ligues mineures)

    stat = stats_groups[0]["splits"][0]["stat"]
    ip = innings_to_float(stat.get("inningsPitched"))
    hr = int(stat.get("homeRuns", 0) or 0)
    bb = int(stat.get("baseOnBalls", 0) or 0)
    hbp = int(stat.get("hitByPitch", 0) or 0)
    so = int(stat.get("strikeOuts", 0) or 0)

    fip = None
    if ip > 0:
        fip = round(((13 * hr) + (3 * (bb + hbp)) - (2 * so)) / ip + FIP_CONSTANT, 2)

    return {
        "era": stat.get("era"),
        "whip": stat.get("whip"),
        "ip": stat.get("inningsPitched"),
        "so": so,
        "bb": bb,
        "hr": hr,
        "fip": fip,
    }


def get_team_season_offense(team_id: int, season: int) -> dict | None:
    """Récupère les stats offensives saison d'une équipe via son ID MLB."""
    url = f"{MLB_API_BASE}/teams/{team_id}/stats"
    params = {"stats": "season", "group": "hitting", "season": season}

    response = requests.get(url, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()

    stats_groups = data.get("stats", [])
    if not stats_groups or not stats_groups[0].get("splits"):
        return None

    stat = stats_groups[0]["splits"][0]["stat"]

    # Approche au bâton (K%/BB%) et pression sur les bases (SB) -- calculés
    # nous-mêmes à partir des compteurs bruts, pas de stat "style de jeu"
    # toute faite côté API.
    plate_appearances = stat.get("plateAppearances")
    strikeouts = stat.get("strikeOuts")
    walks = stat.get("baseOnBalls")
    k_pct = strikeouts / plate_appearances if plate_appearances else None
    bb_pct = walks / plate_appearances if plate_appearances else None

    # Runs par match -- ajouté le 18 août pour le marché Total (get_value_bets.py)
    games_played = stat.get("gamesPlayed")
    runs = stat.get("runs")
    runs_per_game = runs / games_played if games_played else None

    return {
        "avg": stat.get("avg"),
        "obp": stat.get("obp"),
        "slg": stat.get("slg"),
        "ops": stat.get("ops"),
        "hr": stat.get("homeRuns"),
        "k_pct": k_pct,
        "bb_pct": bb_pct,
        "stolen_bases": stat.get("stolenBases"),
        "runs_per_game": runs_per_game,
    }


def format_pitcher_block(label: str, stats: dict | None) -> str:
    if stats is None:
        return f"{label} : stats insuffisantes cette saison\n"
    return (
        f"{label} :\n"
        f"   ERA {stats['era']} | WHIP {stats['whip']} | FIP (calculé) {stats['fip']}\n"
        f"   IP {stats['ip']} | K {stats['so']} | BB {stats['bb']} | HR {stats['hr']}\n"
    )


def format_offense_block(label: str, stats: dict | None) -> str:
    if stats is None:
        return f"{label} : stats insuffisantes cette saison\n"
    return f"{label} : AVG {stats['avg']} | OBP {stats['obp']} | SLG {stats['slg']} | OPS {stats['ops']}\n"


def format_matchup(game: dict, season: int) -> str:
    home_team = game["teams"]["home"]["team"]["name"]
    away_team = game["teams"]["away"]["team"]["name"]
    home_team_id = game["teams"]["home"]["team"]["id"]
    away_team_id = game["teams"]["away"]["team"]["id"]

    home_pitcher_info = game["teams"]["home"].get("probablePitcher", {})
    away_pitcher_info = game["teams"]["away"].get("probablePitcher", {})

    home_pitcher_name = home_pitcher_info.get("fullName", "Titulaire non encore annoncé")
    away_pitcher_name = away_pitcher_info.get("fullName", "Titulaire non encore annoncé")

    home_pitcher_stats = (
        get_pitcher_season_stats(home_pitcher_info["id"], season) if home_pitcher_info.get("id") else None
    )
    away_pitcher_stats = (
        get_pitcher_season_stats(away_pitcher_info["id"], season) if away_pitcher_info.get("id") else None
    )

    home_offense = get_team_season_offense(home_team_id, season)
    away_offense = get_team_season_offense(away_team_id, season)

    lines = [
        f"⚾ <b>{away_team} @ {home_team}</b>\n",
        format_pitcher_block(f"Titulaire {away_team} : {away_pitcher_name}", away_pitcher_stats),
        format_pitcher_block(f"Titulaire {home_team} : {home_pitcher_name}", home_pitcher_stats),
        format_offense_block(f"Attaque {home_team} (face au titulaire {away_team})", home_offense),
        format_offense_block(f"Attaque {away_team} (face au titulaire {home_team})", away_offense),
    ]
    return "\n".join(lines)


def chunk_message(text: str, limit: int = 3800) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def main() -> None:
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    games = get_games_for_date(date_arg)

    if not games:
        send_message("Aucun match MLB programmé pour cette date.")
        print("Aucun match trouvé.")
        return

    for game in games:
        matchup_text = format_matchup(game, CURRENT_SEASON)
        for chunk in chunk_message(matchup_text):
            send_message(chunk)

    print(f"{len(games)} matchup(s) envoyé(s) sur Telegram.")


if __name__ == "__main__":
    main()