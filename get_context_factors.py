"""
Étape 3 : pour chaque match du jour, ajoute le contexte autour du duel
pitcher vs lineup construit à l'étape 2 :
- Park factor (tendance du stade, favorable lanceurs ou frappeurs)
- Météo au moment du match (température, vent, humidité)
- Repos de chaque équipe avant ce match (et détection des back-to-back)
- Historique des confrontations (H2H) entre les deux équipes cette saison

Sources : uniquement l'API officielle MLB (statsapi.mlb.com, déjà utilisée
dans get_schedule.py et get_matchup_stats.py) + Open-Meteo (météo, gratuite
et sans clé). Aucune nouvelle dépendance à installer.

Usage :
    python get_context_factors.py
    python get_context_factors.py 2026-07-21
"""

import sys
import time
import datetime
import requests

from get_schedule import get_games_for_date
from telegram_bot import send_message

MLB_API_BASE = "https://statsapi.mlb.com/api/v1"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
CURRENT_SEASON = datetime.date.today().year
SEASON_START = f"{CURRENT_SEASON}-03-01"  # assez tôt pour couvrir tout le calendrier régulier

MAX_RETRIES = 4
BASE_RETRY_DELAY_SECONDS = 2

# "Connection: close" évite de réutiliser la même connexion TCP/TLS d'un
# appel à l'autre. Sur certains PC Windows, un antivirus/pare-feu qui
# inspecte le trafic HTTPS coupe les connexions réutilisées trop souvent
# (erreur WinError 10054) — forcer une connexion neuve à chaque fois évite
# ce problème.
_REQUEST_HEADERS = {"Connection": "close"}


def get_json_with_retries(url: str, params: dict) -> dict:
    """Fait un GET avec tentatives automatiques en cas de souci réseau.

    Centralise la même logique de robustesse que telegram_bot.send_message,
    pour les appels vers l'API MLB et vers Open-Meteo.
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, params=params, headers=_REQUEST_HEADERS, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as exc:
            last_error = exc
            delay = BASE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
            print(f"    (requête réseau échouée, tentative {attempt}/{MAX_RETRIES} : {exc} — réessai dans {delay}s)")
            time.sleep(delay)

    raise RuntimeError(f"Échec de la requête réseau après {MAX_RETRIES} tentatives : {last_error}")

# ---------------------------------------------------------------------------
# Données statiques par équipe : coordonnées du stade (pour la météo), si le
# stade a un toit (fixe ou rétractable -> météo peu pertinente), et une
# tendance de park factor très approximative.
#
# IMPORTANT : ce sont des données figées, à rafraîchir toi-même de temps en
# temps (une fois par saison suffit largement, les tendances de park factor
# bougent peu). Échelle du park factor : -2 très favorable aux lanceurs,
# 0 neutre, +2 très favorable aux frappeurs.
#
# Cas particuliers à surveiller : les Athletics jouent temporairement à
# Sutter Health Park (Sacramento) en attendant leur stade de Las Vegas, et
# les Rays ont pu jouer temporairement hors de Tropicana Field après des
# dégâts de toit. Si l'un de ces déménagements a changé depuis, corrige
# juste la ligne correspondante ci-dessous.
# ---------------------------------------------------------------------------
STADIUM_INFO = {
    "Arizona Diamondbacks":  {"lat": 33.4455, "lon": -112.0667, "roofed": True,  "park_factor_tier": 0},
    "Atlanta Braves":        {"lat": 33.8907, "lon": -84.4677,  "roofed": False, "park_factor_tier": 0},
    "Baltimore Orioles":     {"lat": 39.2839, "lon": -76.6217,  "roofed": False, "park_factor_tier": 1},
    "Boston Red Sox":        {"lat": 42.3467, "lon": -71.0972,  "roofed": False, "park_factor_tier": 1},
    "Chicago Cubs":          {"lat": 41.9484, "lon": -87.6553,  "roofed": False, "park_factor_tier": 0},
    "Chicago White Sox":     {"lat": 41.8299, "lon": -87.6338,  "roofed": False, "park_factor_tier": 1},
    "Cincinnati Reds":       {"lat": 39.0975, "lon": -84.5061,  "roofed": False, "park_factor_tier": 2},
    "Cleveland Guardians":   {"lat": 41.4962, "lon": -81.6852,  "roofed": False, "park_factor_tier": 0},
    "Colorado Rockies":      {"lat": 39.7559, "lon": -104.9942, "roofed": False, "park_factor_tier": 2},
    "Detroit Tigers":        {"lat": 42.3390, "lon": -83.0485,  "roofed": False, "park_factor_tier": -1},
    "Houston Astros":        {"lat": 29.7570, "lon": -95.3555,  "roofed": True,  "park_factor_tier": 0},
    "Kansas City Royals":    {"lat": 39.0517, "lon": -94.4803,  "roofed": False, "park_factor_tier": 0},
    "Los Angeles Angels":    {"lat": 33.8003, "lon": -117.8827, "roofed": False, "park_factor_tier": 0},
    "Los Angeles Dodgers":   {"lat": 34.0739, "lon": -118.2400, "roofed": False, "park_factor_tier": 1},
    "Miami Marlins":         {"lat": 25.7781, "lon": -80.2196,  "roofed": True,  "park_factor_tier": 0},
    "Milwaukee Brewers":     {"lat": 43.0280, "lon": -87.9712,  "roofed": True,  "park_factor_tier": 1},
    "Minnesota Twins":       {"lat": 44.9817, "lon": -93.2776,  "roofed": False, "park_factor_tier": 0},
    "New York Mets":         {"lat": 40.7571, "lon": -73.8458,  "roofed": False, "park_factor_tier": -1},
    "New York Yankees":      {"lat": 40.8296, "lon": -73.9262,  "roofed": False, "park_factor_tier": 1},
    "Athletics":             {"lat": 38.5766, "lon": -121.5286, "roofed": False, "park_factor_tier": 0},  # Sutter Health Park (temporaire)
    "Philadelphia Phillies": {"lat": 39.9061, "lon": -75.1665,  "roofed": False, "park_factor_tier": 1},
    "Pittsburgh Pirates":    {"lat": 40.4468, "lon": -80.0057,  "roofed": False, "park_factor_tier": -1},
    "San Diego Padres":      {"lat": 32.7073, "lon": -117.1566, "roofed": False, "park_factor_tier": -1},
    "San Francisco Giants":  {"lat": 37.7786, "lon": -122.3893, "roofed": False, "park_factor_tier": -2},
    "Seattle Mariners":      {"lat": 47.5914, "lon": -122.3325, "roofed": True,  "park_factor_tier": -2},
    "St. Louis Cardinals":   {"lat": 38.6226, "lon": -90.1928,  "roofed": False, "park_factor_tier": -1},
    "Tampa Bay Rays":        {"lat": 27.9803, "lon": -82.5065,  "roofed": False, "park_factor_tier": 0},  # stade temporaire, à vérifier
    "Texas Rangers":         {"lat": 32.7473, "lon": -97.0842,  "roofed": True,  "park_factor_tier": 0},
    "Toronto Blue Jays":     {"lat": 43.6414, "lon": -79.3894,  "roofed": True,  "park_factor_tier": 0},
    "Washington Nationals":  {"lat": 38.8730, "lon": -77.0074,  "roofed": False, "park_factor_tier": 0},
}

PARK_TIER_LABELS = {
    -2: "très favorable aux lanceurs",
    -1: "plutôt favorable aux lanceurs",
    0: "neutre",
    1: "plutôt favorable aux frappeurs",
    2: "très favorable aux frappeurs",
}


def get_stadium_info(team_name: str) -> dict | None:
    """Cherche les infos de stade d'une équipe, avec un repli si le nom ne
    correspond pas exactement (ex: changement de dénomination officielle).
    """
    if team_name in STADIUM_INFO:
        return STADIUM_INFO[team_name]

    last_word = team_name.split()[-1]
    for key, info in STADIUM_INFO.items():
        if key.endswith(last_word):
            return info
    return None


def get_weather_for_game(lat: float, lon: float, game_time_utc: str) -> dict | None:
    """Récupère la météo prévue à l'heure du match (Open-Meteo, gratuit, sans clé)."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,relative_humidity_2m",
        "timezone": "UTC",
        "forecast_days": 3,
    }
    data = get_json_with_retries(OPEN_METEO_URL, params)

    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    if not times:
        return None

    target_hour = game_time_utc[:13]  # ex: "2026-07-20T23"
    idx = next((i for i, t in enumerate(times) if t.startswith(target_hour)), None)
    if idx is None:
        return None

    return {
        "temp_c": hourly["temperature_2m"][idx],
        "wind_kmh": hourly["wind_speed_10m"][idx],
        "humidity_pct": hourly["relative_humidity_2m"][idx],
    }


def get_days_rest(team_id: int, before_date_str: str) -> int | None:
    """Nombre de jours de repos d'une équipe avant la date donnée (YYYY-MM-DD).

    0 = back-to-back (a joué la veille). None = aucun match trouvé dans la
    fenêtre de recherche (ex: tout début de saison ou sortie de pause).
    """
    game_date = datetime.date.fromisoformat(before_date_str)
    window_start = game_date - datetime.timedelta(days=8)

    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": window_start.isoformat(),
        "endDate": (game_date - datetime.timedelta(days=1)).isoformat(),
        "gameType": "R",
    }
    data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)

    last_game_date = None
    for day in data.get("dates", []):
        for game in day.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            played_date = datetime.date.fromisoformat(day["date"])
            if last_game_date is None or played_date > last_game_date:
                last_game_date = played_date

    if last_game_date is None:
        return None

    return max((game_date - last_game_date).days - 1, 0)


def _winner_side(game: dict) -> str | None:
    """Retourne 'home' ou 'away' selon qui a gagné ce match, ou None si on
    ne peut pas le déterminer (match pas encore joué, données incomplètes).
    """
    home = game["teams"]["home"]
    away = game["teams"]["away"]

    if home.get("isWinner"):
        return "home"
    if away.get("isWinner"):
        return "away"

    linescore_teams = game.get("linescore", {}).get("teams", {})
    home_runs = linescore_teams.get("home", {}).get("runs")
    away_runs = linescore_teams.get("away", {}).get("runs")
    if home_runs is not None and away_runs is not None and home_runs != away_runs:
        return "home" if home_runs > away_runs else "away"

    return None


def get_h2h_record(team_a_id: int, team_b_id: int, before_date_str: str) -> dict:
    """Bilan des confrontations entre deux équipes depuis le début de la
    saison en cours et jusqu'à la date donnée (exclue si pas encore jouée).
    """
    params = {
        "sportId": 1,
        "teamId": team_a_id,
        "opponentId": team_b_id,
        "startDate": SEASON_START,
        "endDate": before_date_str,
        "gameType": "R",
        "hydrate": "linescore",
    }
    data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)

    wins_a = 0
    wins_b = 0
    games_played = 0

    for day in data.get("dates", []):
        for game in day.get("games", []):
            winner = _winner_side(game)
            if winner is None:
                continue

            winner_team_id = game["teams"][winner]["team"]["id"]
            if winner_team_id == team_a_id:
                wins_a += 1
            elif winner_team_id == team_b_id:
                wins_b += 1
            games_played += 1

    return {"games_played": games_played, "wins_a": wins_a, "wins_b": wins_b}


def _format_rest(days_off: int | None) -> str:
    if days_off is None:
        return "inconnu"
    if days_off == 0:
        return "back-to-back (0 jour de repos)"
    return f"{days_off} jour(s) de repos"


def format_context_block(game: dict) -> str:
    home_team = game["teams"]["home"]["team"]["name"]
    away_team = game["teams"]["away"]["team"]["name"]
    home_team_id = game["teams"]["home"]["team"]["id"]
    away_team_id = game["teams"]["away"]["team"]["id"]
    game_time_utc = game.get("gameDate", "")
    game_date = game_time_utc[:10]

    lines = [f"⚾ <b>{away_team} @ {home_team}</b>\n"]

    # Park factor
    stadium = get_stadium_info(home_team)
    if stadium:
        tier_label = PARK_TIER_LABELS.get(stadium["park_factor_tier"], "neutre")
        lines.append(f"🏟 Park factor : {tier_label}\n")
    else:
        lines.append("🏟 Park factor : stade non reconnu (à vérifier dans STADIUM_INFO)\n")

    # Météo (ignorée si stade avec toit)
    if stadium and stadium["roofed"]:
        lines.append("🏠 Stade avec toit : effet météo neutralisé/incertain\n")
    elif stadium:
        weather = get_weather_for_game(stadium["lat"], stadium["lon"], game_time_utc)
        if weather:
            lines.append(
                f"🌤 Météo : {weather['temp_c']}°C | vent {weather['wind_kmh']} km/h | "
                f"humidité {weather['humidity_pct']}%\n"
            )
        else:
            lines.append("🌤 Météo : indisponible\n")

    # Repos
    home_rest = get_days_rest(home_team_id, game_date)
    away_rest = get_days_rest(away_team_id, game_date)
    lines.append(f"😴 Repos {home_team} : {_format_rest(home_rest)}\n")
    lines.append(f"😴 Repos {away_team} : {_format_rest(away_rest)}\n")

    # H2H saison en cours
    h2h = get_h2h_record(home_team_id, away_team_id, game_date)
    if h2h["games_played"] > 0:
        lines.append(
            f"🤝 H2H {CURRENT_SEASON} : {home_team} {h2h['wins_a']} - {h2h['wins_b']} {away_team} "
            f"({h2h['games_played']} match(s) joué(s))\n"
        )
    else:
        lines.append(f"🤝 H2H {CURRENT_SEASON} : aucun match encore joué entre ces deux équipes\n")

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

    matchs_envoyes = 0
    matchs_echoues = 0

    for game in games:
        try:
            context_text = format_context_block(game)
            for chunk in chunk_message(context_text):
                send_message(chunk)
            matchs_envoyes += 1
        except Exception as exc:
            # On isole l'échec à ce seul match : mieux vaut rater un contexte
            # que perdre les 14 autres à cause d'une coupure réseau isolée.
            home = game["teams"]["home"]["team"]["name"]
            away = game["teams"]["away"]["team"]["name"]
            print(f"  ⚠️ Match ignoré ({away} @ {home}) après erreur : {exc}")
            matchs_echoues += 1
        time.sleep(0.3)  # petite pause entre deux matchs, pour rester sympa avec les API gratuites

    print(f"{matchs_envoyes} contexte(s) de match envoyé(s) sur Telegram, {matchs_echoues} ignoré(s).")


if __name__ == "__main__":
    main()
