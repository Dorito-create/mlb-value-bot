"""
Étape 4 : le cœur du bot.

1. Construit une probabilité de victoire par équipe, purement statistique :
   - Base : méthode Log5 (Bill James) sur le bilan DOMICILE de l'équipe qui
     reçoit et le bilan EXTÉRIEUR de l'équipe qui visite -> intègre
     naturellement l'avantage du terrain, sans constante arbitraire.
   - Qualité du titulaire : écart de FIP vs moyenne de la ligue.
   - Adversité : cet avantage est modulé par la force de la ligne de
     batteurs affrontée CE SOIR -- en priorité le line-up réel si publié
     (souvent seulement 1-3h avant le match), sinon l'OPS de saison.
   - Forme récente : bilan des 10 derniers matchs de chaque équipe (en plus
     du bilan de saison utilisé dans le Log5, pas à sa place).
   - Repos (back-to-back) et fatigue du bullpen (lancers des relievers sur
     les 2-3 derniers jours).
   - H2H de la saison, avec un poids qui grossit avec le nombre de matchs
     déjà joués entre les deux équipes (jusqu'à 12 -- pertinent en MLB, où
     les rivaux de division se recroisent 13 à 19 fois par saison).
2. Récupère les cotes MLB sur The Odds API (region=eu -> couvre Pinnacle et
   Betclic), dévigore le prix Pinnacle pour obtenir une proba de marché
   "sharp" de référence, et compare à notre modèle.
3. Envoie un verdict par match sur Telegram, façon matchup-tennis.fr.

IMPORTANT : tous les poids d'ajustement sont un point de départ raisonnable,
pas un résultat validé. Le marché moneyline MLB est assez efficient — ne
considère ce signal comme fiable qu'après plusieurs semaines de suivi
montrant une calibration correcte. Ceci n'est pas un conseil de paris,
juste un outil statistique perso.

NOTE SUR LE LINE-UP RÉEL : dépend du champ "lineups" de l'API MLB, qui n'a
pas pu être testé en direct pendant l'écriture de ce script. Si le line-up
réel ne remonte jamais (le modèle retombe alors sur l'OPS de saison, sans
planter), dis-le et on ajustera le nom du champ ensemble.

Prérequis : une clé gratuite sur https://the-odds-api.com (PAS
theoddsapi.com, un service différent et plus restrictif) dans ton .env :
    ODDS_API_KEY=ta_clé

Usage :
    python get_value_bets.py
    python get_value_bets.py 2026-07-21
"""

import os
import sys
import time
import json
import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from telegram_bot import send_message
from get_matchup_stats import get_pitcher_season_stats, get_team_season_offense, innings_to_float
from get_context_factors import (
    get_json_with_retries,
    get_days_rest,
    get_h2h_record,
    get_stadium_info,
    get_weather_for_game,
    MLB_API_BASE,
)
import pitch_arsenal

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
CURRENT_SEASON = datetime.date.today().year

LEAGUE_AVG_FIP = 4.20  # approximation courante, à ajuster si besoin (voir README)
STARTER_SENSITIVITY = 0.065  # % de proba par run de FIP d'écart -- heuristique de départ
LEAGUE_AVG_OPS = 0.720  # approximation courante, à ajuster si besoin
OFFENSE_SENSITIVITY = 0.08   # module l'avantage du titulaire selon la force de la ligne adverse

RECENT_FORM_SENSITIVITY = 0.12  # poids de l'écart de forme sur 10 matchs -- volontairement plus fort que le H2H

BACK_TO_BACK_PENALTY = 0.015  # petit désavantage si l'équipe joue sans repos
BULLPEN_FATIGUE_WINDOW_DAYS = 3
BULLPEN_FATIGUE_SENSITIVITY = 0.00035  # % de proba par lancer d'écart entre les deux bullpens

# Vague 1 : splits lanceur supplémentaires. Tous via le même mécanisme
# statSplits/sitCodes de l'API MLB -- expérimental, non testé en direct
# (voir pitch_arsenal.py pour le même genre d'avertissement). Si un code
# ne correspond pas à ce que l'API attend vraiment, le split concerné
# retombe simplement sur "aucune donnée" (0.0), sans casser le reste.
MIN_SPLIT_IP = 15.0       # sous ce seuil de manches, un split est jugé trop peu fiable pour être utilisé
MIN_VS_TEAM_IP = 6.0      # seuil plus bas pour "vs cette équipe" -- l'échantillon y est structurellement petit
HOME_ROAD_SENSITIVITY = 0.05
DAYNIGHT_SENSITIVITY = 0.03    # poids volontairement limité, comme demandé
PLATOON_SENSITIVITY = 0.05
VS_TEAM_SENSITIVITY = 0.03     # poids réduit, échantillon historique souvent minuscule

SIT_CODE_HOME = "h"
SIT_CODE_ROAD = "r"
SIT_CODE_DAY = "d"
SIT_CODE_NIGHT = "n"
SIT_CODE_VS_LHP = "vl"
SIT_CODE_VS_RHP = "vr"

# Vague 2 : style de jeu (approche + pression sur les bases) et batteurs
# déjà rencontrés par ce titulaire.
LEAGUE_AVG_K_PCT = 0.225   # approximation courante, à ajuster si besoin
LEAGUE_AVG_BB_PCT = 0.085  # approximation courante, à ajuster si besoin
PLAYSTYLE_SENSITIVITY = 0.15
STOLEN_BASE_SENSITIVITY = 0.00008  # % de proba par vol de but d'écart (petit, sur toute la saison)

MIN_VS_PITCHER_PA = 3       # sous ce nombre de passages, l'historique batteur/lanceur est ignoré (pur bruit)
VS_PITCHER_SENSITIVITY = 0.02   # poids symbolique, volontairement faible -- petit échantillon structurel

H2H_MIN_GAMES = 2             # sous ce seuil, on ignore le H2H (trop peu de matchs pour compter)
H2H_MAX_WEIGHT_GAMES = 12     # au-delà, le poids ne grossit plus (rivalité de division bien établie)
H2H_SENSITIVITY = 0.10        # poids maximal, atteint seulement à partir de H2H_MAX_WEIGHT_GAMES matchs

VALUE_TIER_FORTE = 0.08    # edge >= 8 points de % -> "value forte"
VALUE_TIER_MODEREE = 0.03  # edge >= 3 points de % -> "value modérée"
VALUE_TIER_SUSPECT = 0.15  # au-delà, un edge aussi large vs Pinnacle est plus probablement une erreur qu'une pépite

BOOKS_AGREE_TOLERANCE = 0.05  # Betclic et Pinnacle jugés "d'accord" si leurs probas dévigorées sont à <5 pts

# Système d'unités pour le suivi : 1u = 1% d'une bankroll de suivi fictive
# de 100 (départ). Convertis toi-même en euros comme tu veux (ex: 1u = 10€
# si tu joues réellement avec 1000€ de bankroll, ou une valeur arbitraire
# si c'est juste pour suivre la cohérence du modèle). 3u = mise maximale.
MAX_UNITS_PER_PICK = 2.0
MAX_SUMMARY_PICKS = 5  # le résumé du soir se limite aux picks les plus fiables -- les messages par match, eux, gardent tout

EUR_PER_UNIT = 2.5  # 1u = 2.5€, 2u = 5€ -- ta grille personnelle de mise réelle


def units_to_eur_display(units: float) -> str:
    """Traduit une mise en euros pour ta bankroll réelle. Le palier 0.5u
    (méfiance) n'est volontairement pas calculé au prorata -- c'est à toi
    de juger si tu joues le minimum ou si tu passes ce pick.
    """
    if units <= 0.5:
        return "2.5€ à ta discrétion (ou 0€ si tu préfères passer)"
    return f"{units * EUR_PER_UNIT:.2f}€"
PARIS_TZ = ZoneInfo("Europe/Paris")


def format_game_time_paris(game_time_utc: str) -> str:
    """Convertit l'heure UTC du match (format API MLB) en heure de Paris lisible."""
    try:
        dt_utc = datetime.datetime.fromisoformat(game_time_utc.replace("Z", "+00:00"))
        dt_paris = dt_utc.astimezone(PARIS_TZ)
        return dt_paris.strftime("%d/%m %Hh%M")
    except Exception:
        return "heure inconnue"


def _safe_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 1. Programme du jour, avec line-up réel si déjà publié
# ---------------------------------------------------------------------------

def get_games_for_value_bets(date_str: str | None = None) -> list[dict]:
    """Comme get_schedule.get_games_for_date, avec en plus le hydrate
    "lineups" : le line-up réel n'est généralement publié que 1 à 3h avant
    le match, donc ce script est plus précis lancé en fin d'après-midi/soir
    qu'au réveil.
    """
    if date_str is None:
        date_str = datetime.date.today().isoformat()

    params = {
        "sportId": 1,
        "date": date_str,
        "hydrate": "team,probablePitcher,venue,lineups",
    }
    data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)

    games = []
    for day in data.get("dates", []):
        games.extend(day.get("games", []))

    games.sort(key=lambda g: g.get("gameDate", ""))  # ordre chronologique -- pas garanti tel quel par l'API
    return games


# ---------------------------------------------------------------------------
# 2. Bilans domicile/extérieur/forme récente (pour le Log5 + forme)
# ---------------------------------------------------------------------------

def get_team_split_records(season: int) -> dict:
    """Bilan domicile/extérieur/global/10 derniers matchs, via les standings MLB."""
    params = {"leagueId": "103,104", "season": season, "hydrate": "team"}
    data = get_json_with_retries(f"{MLB_API_BASE}/standings", params)

    records = {}
    for division in data.get("records", []):
        for team_record in division.get("teamRecords", []):
            team_id = team_record.get("team", {}).get("id")
            if team_id is None:
                continue

            overall_pct = _safe_float(team_record.get("winningPercentage")) or 0.5
            home_pct = None
            road_pct = None
            last10_pct = None

            for split in team_record.get("splitRecords", []):
                wins = split.get("wins")
                losses = split.get("losses")
                total = (wins or 0) + (losses or 0)
                if total == 0:
                    continue
                pct = wins / total
                split_type = split.get("type")
                if split_type == "home":
                    home_pct = pct
                elif split_type in ("away", "road"):
                    road_pct = pct
                elif split_type == "lastTen":
                    last10_pct = pct

            records[team_id] = {
                "overall_pct": overall_pct,
                "home_pct": home_pct if home_pct is not None else overall_pct,
                "road_pct": road_pct if road_pct is not None else overall_pct,
                "last10_pct": last10_pct if last10_pct is not None else overall_pct,
            }

    return records


def log5(pct_a: float, pct_b: float) -> float:
    """Probabilité que A batte B, méthode Log5 (Bill James)."""
    denom = pct_a + pct_b - 2 * pct_a * pct_b
    if denom <= 0:
        return 0.5
    return (pct_a - pct_a * pct_b) / denom


def recent_form_edge(home_last10_pct: float, away_last10_pct: float) -> float:
    """Écart de forme sur les 10 derniers matchs -- en PLUS du bilan de
    saison utilisé dans le Log5, pas à sa place.
    """
    return (home_last10_pct - away_last10_pct) * RECENT_FORM_SENSITIVITY


# ---------------------------------------------------------------------------
# 3. Titulaire + adversité (line-up réel si publié, sinon OPS de saison)
# ---------------------------------------------------------------------------

def starter_edge(pitcher_fip: float | None) -> float:
    """Avantage en proba apporté par un titulaire, vs la moyenne de la ligue."""
    if pitcher_fip is None:
        return 0.0
    return (LEAGUE_AVG_FIP - pitcher_fip) * STARTER_SENSITIVITY


def opposing_offense_penalty(opposing_ops) -> float:
    """Module l'avantage d'un titulaire selon la force de la ligne de
    batteurs qu'il affronte CE SOIR, par rapport à la moyenne de la ligue.
    """
    ops = _safe_float(opposing_ops)
    if ops is None:
        return 0.0
    return (LEAGUE_AVG_OPS - ops) * OFFENSE_SENSITIVITY


def _extract_lineup_player_ids(game: dict, side: str) -> list[int]:
    """Essaie plusieurs formes possibles du champ "lineups" de l'API MLB.

    Champ expérimental, non testé en direct -- si ça ne remonte jamais
    rien de grave : le code appelant retombe sur l'OPS de saison.
    """
    lineups = game.get("lineups", {})
    for key in (f"{side}Players", side):
        entries = lineups.get(key)
        if not entries:
            continue
        ids = []
        for entry in entries:
            if isinstance(entry, dict):
                player_id = entry.get("id") or entry.get("person", {}).get("id")
            else:
                player_id = entry
            if player_id:
                ids.append(player_id)
        if ids:
            return ids
    return []


def get_batter_season_ops(player_id: int, season: int) -> float | None:
    params = {"stats": "season", "group": "hitting", "season": season}
    data = get_json_with_retries(f"{MLB_API_BASE}/people/{player_id}/stats", params)
    stats_groups = data.get("stats", [])
    if not stats_groups or not stats_groups[0].get("splits"):
        return None
    return _safe_float(stats_groups[0]["splits"][0]["stat"].get("ops"))


def get_effective_offense_ops(game: dict, side: str, team_id: int, season: int) -> tuple[float | None, bool]:
    """OPS à utiliser pour l'adversité : le line-up réel s'il est publié,
    sinon l'OPS de saison de l'équipe. Renvoie (ops, line_up_reel_utilise).
    """
    player_ids = _extract_lineup_player_ids(game, side)
    if player_ids:
        ops_values = [v for v in (get_batter_season_ops(pid, season) for pid in player_ids) if v is not None]
        if ops_values:
            return sum(ops_values) / len(ops_values), True

    offense = get_team_season_offense(team_id, season)
    return (_safe_float(offense["ops"]) if offense else None), False


# ---------------------------------------------------------------------------
# 3bis. Splits lanceur supplémentaires (domicile/extérieur, jour/nuit,
# platoon, vs cette équipe) -- Vague 1
# ---------------------------------------------------------------------------

def get_pitcher_split_stat(pitcher_id: int, season: int, sit_code: str) -> tuple[float, float] | None:
    """ERA et manches lancées pour un split donné (sitCodes). None si
    indisponible ou si l'échantillon est trop petit pour être fiable.
    """
    params = {"stats": "statSplits", "group": "pitching", "season": season, "sitCodes": sit_code}
    try:
        data = get_json_with_retries(f"{MLB_API_BASE}/people/{pitcher_id}/stats", params)
    except Exception:
        return None

    stats_groups = data.get("stats", [])
    if not stats_groups or not stats_groups[0].get("splits"):
        return None

    stat = stats_groups[0]["splits"][0]["stat"]
    era = _safe_float(stat.get("era"))
    ip = innings_to_float(stat.get("inningsPitched"))
    if era is None or ip < MIN_SPLIT_IP:
        return None
    return era, ip


def get_pitcher_vs_team_stat(pitcher_id: int, season: int, opposing_team_id: int) -> tuple[float, float] | None:
    """ERA et manches lancées d'un lanceur face à une équipe précise cette
    saison. Seuil de fiabilité plus bas que les autres splits -- un
    lanceur n'affronte une équipe donnée que 2-3 fois par saison au mieux.
    """
    params = {"stats": "vsTeam", "group": "pitching", "season": season, "opposingTeamId": opposing_team_id}
    try:
        data = get_json_with_retries(f"{MLB_API_BASE}/people/{pitcher_id}/stats", params)
    except Exception:
        return None

    stats_groups = data.get("stats", [])
    if not stats_groups or not stats_groups[0].get("splits"):
        return None

    stat = stats_groups[0]["splits"][0]["stat"]
    era = _safe_float(stat.get("era"))
    ip = innings_to_float(stat.get("inningsPitched"))
    if era is None or ip < MIN_VS_TEAM_IP:
        return None
    return era, ip


def get_pitcher_hand(pitcher_id: int, probable_pitcher_obj: dict) -> str | None:
    """Main du lanceur ('L' ou 'R'). Essaie d'abord l'objet déjà récupéré
    via le programme du jour, sinon fait un appel dédié.
    """
    hand = probable_pitcher_obj.get("pitchHand", {}).get("code")
    if hand:
        return hand
    try:
        data = get_json_with_retries(f"{MLB_API_BASE}/people/{pitcher_id}", {})
        people = data.get("people", [])
        if people:
            return people[0].get("pitchHand", {}).get("code")
    except Exception:
        pass
    return None


def get_team_offense_vs_hand(team_id: int, season: int, hand_code: str) -> float | None:
    """OPS d'une équipe face aux lanceurs d'une main donnée ('L' ou 'R')."""
    sit_code = SIT_CODE_VS_LHP if hand_code == "L" else SIT_CODE_VS_RHP
    params = {"stats": "statSplits", "group": "hitting", "season": season, "sitCodes": sit_code}
    try:
        data = get_json_with_retries(f"{MLB_API_BASE}/teams/{team_id}/stats", params)
    except Exception:
        return None

    stats_groups = data.get("stats", [])
    if not stats_groups or not stats_groups[0].get("splits"):
        return None
    return _safe_float(stats_groups[0]["splits"][0]["stat"].get("ops"))


def is_day_game(game_time_utc: str) -> bool:
    """Heuristique simple (avant/après 21h UTC) -- approximatif car ne
    tient pas compte du fuseau exact du stade, mais suffisant vu le poids
    volontairement limité de cet ajustement.
    """
    try:
        return int(game_time_utc[11:13]) < 21
    except Exception:
        return True


def split_era_edge(split_stat: tuple[float, float] | None, season_era, sensitivity: float) -> float:
    """Ajustement générique à partir d'un split ERA vs l'ERA de saison --
    réutilisé pour domicile/extérieur, jour/nuit, et vs cette équipe.
    """
    season_era = _safe_float(season_era)  # l'API renvoie l'ERA en chaîne ("3.45"), pas en nombre
    if split_stat is None or season_era is None:
        return 0.0
    split_era, _ip = split_stat
    return (season_era - split_era) * sensitivity


def platoon_edge(team_ops_vs_hand: float | None, team_ops_season: float | None) -> float:
    """Edge pour LE LANCEUR selon que la ligne adverse frappe mieux ou
    moins bien que sa moyenne de saison contre sa main précise. Positif =
    avantage pour le lanceur (ligne plus faible que d'habitude contre
    cette main).
    """
    if team_ops_vs_hand is None or team_ops_season is None:
        return 0.0
    return (team_ops_season - team_ops_vs_hand) * PLATOON_SENSITIVITY


# ---------------------------------------------------------------------------
# Vague 2 : style de jeu (approche au bâton + pression sur les bases) et
# batteurs déjà rencontrés par ce titulaire
# ---------------------------------------------------------------------------

def playstyle_edge(opposing_offense: dict | None) -> float:
    """Avantage pour LE LANCEUR selon le style de jeu de la ligne adverse :
    une ligne qui prend beaucoup de strikeouts et peu de buts sur balles
    (style agressif, contact tôt dans le compte) est un peu plus facile à
    négocier ; une ligne patiente (BB% haut, K% bas) est plus coriace. Le
    vol de bases (pression) joue légèrement en défaveur du lanceur.
    """
    if opposing_offense is None:
        return 0.0

    k_pct = opposing_offense.get("k_pct")
    bb_pct = opposing_offense.get("bb_pct")
    stolen_bases = opposing_offense.get("stolen_bases")

    edge = 0.0
    if k_pct is not None:
        edge += (k_pct - LEAGUE_AVG_K_PCT) * PLAYSTYLE_SENSITIVITY
    if bb_pct is not None:
        edge -= (bb_pct - LEAGUE_AVG_BB_PCT) * PLAYSTYLE_SENSITIVITY
    if stolen_bases is not None:
        edge -= stolen_bases * STOLEN_BASE_SENSITIVITY  # plus l'équipe vole de buts sur la saison, plus la pression est réelle

    return edge


def get_batter_vs_pitcher_stat(batter_id: int, pitcher_id: int, season: int) -> dict | None:
    """Historique d'un batteur précis face à CE lanceur précis, cette
    saison. None si l'échantillon est trop petit (quelques passages au
    bâton seulement -- ce qui est presque toujours le cas).
    """
    params = {"stats": "vsPlayer", "group": "hitting", "season": season, "opposingPlayerId": pitcher_id}
    try:
        data = get_json_with_retries(f"{MLB_API_BASE}/people/{batter_id}/stats", params)
    except Exception:
        return None

    stats_groups = data.get("stats", [])
    if not stats_groups or not stats_groups[0].get("splits"):
        return None

    stat = stats_groups[0]["splits"][0]["stat"]
    pa = stat.get("plateAppearances") or 0
    if pa < MIN_VS_PITCHER_PA:
        return None
    return {"ops": _safe_float(stat.get("ops")), "pa": pa}


def batters_vs_pitcher_edge(batter_ids: list[int], pitcher_id: int | None, season: int, league_avg_ops: float) -> float:
    """Moyenne (fortement atténuée) de l'historique des batteurs du
    line-up réel face à ce titulaire précis. Volontairement pondéré très
    bas : l'échantillon par duel individuel est presque toujours minuscule
    (quelques face-à-face dans la saison), donc peu fiable en soi -- mais
    ça reste un signal que le staff et les joueurs eux-mêmes regardent.
    """
    if pitcher_id is None or not batter_ids:
        return 0.0

    ops_values = []
    for batter_id in batter_ids:
        stat = get_batter_vs_pitcher_stat(batter_id, pitcher_id, season)
        if stat and stat["ops"] is not None:
            ops_values.append(stat["ops"])

    if not ops_values:
        return 0.0

    avg_ops = sum(ops_values) / len(ops_values)
    return (avg_ops - league_avg_ops) * VS_PITCHER_SENSITIVITY




# ---------------------------------------------------------------------------
# 4. Repos + fatigue du bullpen
# ---------------------------------------------------------------------------

def rest_edge(days_off: int | None) -> float:
    if days_off == 0:
        return -BACK_TO_BACK_PENALTY
    return 0.0


def _get_recent_final_games(team_id: int, before_date_str: str, window_days: int) -> list[dict]:
    game_date = datetime.date.fromisoformat(before_date_str)
    window_start = game_date - datetime.timedelta(days=window_days)

    params = {
        "sportId": 1,
        "teamId": team_id,
        "startDate": window_start.isoformat(),
        "endDate": (game_date - datetime.timedelta(days=1)).isoformat(),
        "gameType": "R",
        "hydrate": "probablePitcher",
    }
    data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)

    games_info = []
    for day in data.get("dates", []):
        for game in day.get("games", []):
            if game.get("status", {}).get("abstractGameState") != "Final":
                continue
            side = "home" if game["teams"]["home"]["team"]["id"] == team_id else "away"
            starter_id = game["teams"][side].get("probablePitcher", {}).get("id")
            games_info.append({"gamePk": game.get("gamePk"), "side": side, "starter_id": starter_id})
    return games_info


def _get_bullpen_pitches_for_game(game_pk: int, side: str, starter_id) -> int:
    """Lancers de tous les pitchers d'un match SAUF le titulaire (= bullpen)."""
    data = get_json_with_retries(f"{MLB_API_BASE}/game/{game_pk}/boxscore", {})
    players = data.get("teams", {}).get(side, {}).get("players", {})

    total = 0
    for player_data in players.values():
        pitching = player_data.get("stats", {}).get("pitching")
        if not pitching:
            continue
        player_id = player_data.get("person", {}).get("id")
        if player_id == starter_id:
            continue
        total += pitching.get("numberOfPitches", 0) or 0
    return total


def get_bullpen_fatigue(team_id: int, before_date_str: str) -> int | None:
    """Total de lancers du bullpen sur les derniers jours. Plus c'est haut,
    plus le bullpen est sollicité récemment (donc potentiellement fatigué).
    """
    try:
        games_info = _get_recent_final_games(team_id, before_date_str, BULLPEN_FATIGUE_WINDOW_DAYS)
    except Exception:
        return None

    total_pitches = 0
    any_success = False
    for g in games_info:
        if not g["gamePk"]:
            continue
        try:
            total_pitches += _get_bullpen_pitches_for_game(g["gamePk"], g["side"], g["starter_id"])
            any_success = True
        except Exception:
            continue  # un boxscore indisponible ne doit pas faire échouer tout le calcul

    return total_pitches if any_success else None


def bullpen_fatigue_edge(home_pitches, away_pitches) -> float:
    if home_pitches is None or away_pitches is None:
        return 0.0
    return (away_pitches - home_pitches) * BULLPEN_FATIGUE_SENSITIVITY


# ---------------------------------------------------------------------------
# 5. H2H pondéré par l'échantillon
# ---------------------------------------------------------------------------

def h2h_edge(h2h: dict) -> float:
    games = h2h.get("games_played", 0)
    if games < H2H_MIN_GAMES:
        return 0.0
    home_rate = h2h["wins_a"] / games
    confidence = min(games / H2H_MAX_WEIGHT_GAMES, 1.0)
    return (home_rate - 0.5) * H2H_SENSITIVITY * confidence


# ---------------------------------------------------------------------------
# 6. Cotes (The Odds API) : Pinnacle pour la value, Betclic pour le prix réel
# ---------------------------------------------------------------------------

def get_odds_for_mlb() -> list[dict]:
    if not ODDS_API_KEY:
        raise RuntimeError(
            "ODDS_API_KEY doit être défini dans ton .env. Crée un compte gratuit sur "
            "https://the-odds-api.com (pas theoddsapi.com, un service différent)."
        )
    params = {
        "apiKey": ODDS_API_KEY,
        # On cible ces deux bookmakers précis directement, plutôt que de passer
        # par "regions=eu" -- le regroupement par région peut ne pas inclure
        # tous les books d'une région pour un sport donné (ex: MLB, un sport
        # secondaire pour un book comme Betclic). "bookmakers" prend le pas
        # sur "regions" si les deux sont fournis, et coûte le même nombre de
        # credits pour 2 books (le tarif est par tranche de 10 bookmakers).
        "bookmakers": "betclic,unibet,pinnacle",
        "markets": "h2h",
        "oddsFormat": "decimal",
    }
    return get_json_with_retries(f"{ODDS_API_BASE}/sports/baseball_mlb/odds", params)


def _nickname_candidates(team_name: str) -> list[str]:
    words = team_name.split()
    candidates = []
    if len(words) >= 2:
        candidates.append(" ".join(words[-2:]))
    candidates.append(words[-1])
    return candidates


def find_odds_event(odds_events: list[dict], home_team: str, away_team: str) -> dict | None:
    for event in odds_events:
        if event.get("home_team") == home_team and event.get("away_team") == away_team:
            return event

    home_candidates = _nickname_candidates(home_team)
    away_candidates = _nickname_candidates(away_team)
    for event in odds_events:
        ev_home = event.get("home_team", "")
        ev_away = event.get("away_team", "")
        if any(c in ev_home for c in home_candidates) and any(c in ev_away for c in away_candidates):
            return event
    return None


def extract_book_prices(odds_event: dict) -> dict:
    """Renvoie {'pinnacle': {'TeamName': cote, ...}, 'betclic': {...}, 'unibet': {...}} si trouvés."""
    prices = {}
    for bookmaker in odds_event.get("bookmakers", []):
        key = bookmaker.get("key")
        if key not in ("pinnacle", "betclic", "unibet"):
            continue
        for market in bookmaker.get("markets", []):
            if market.get("key") != "h2h":
                continue
            prices[key] = {o["name"]: o["price"] for o in market.get("outcomes", [])}
    return prices


def devig_two_way(price_a: float, price_b: float) -> tuple[float, float]:
    """Retire la marge du bookmaker pour obtenir la vraie proba de marché implicite."""
    imp_a = 1 / price_a
    imp_b = 1 / price_b
    total = imp_a + imp_b
    return imp_a / total, imp_b / total


# ---------------------------------------------------------------------------
# 7. Assemblage : modèle, value, message
# ---------------------------------------------------------------------------

DETAIL_THRESHOLD = 0.02  # 2 points -- en dessous, un composant est jugé négligeable dans l'explication en texte


def format_detail_explanation(model: dict) -> str:
    """Traduit le détail numérique du modèle en une explication en langage
    courant : d'où vient la proba, quels facteurs du soir pèsent vraiment,
    lesquels sont négligeables. Remplace l'ancienne ligne de chiffres bruts.
    """
    home_team = model["home_name"]
    away_team = model["away_name"]
    baseline_pct = model["baseline"] * 100

    if baseline_pct >= 55:
        base_sentence = f"le bilan de base des deux équipes penche pour {home_team} ({baseline_pct:.0f}% à domicile)"
    elif baseline_pct <= 45:
        base_sentence = f"le bilan de base des deux équipes penche contre {home_team} ({baseline_pct:.0f}% à domicile)"
    else:
        base_sentence = f"le bilan de base des deux équipes est quasiment à égalité ({baseline_pct:.0f}% pour {home_team})"

    components = [
        ("la forme récente", model["form_adj"]),
        ("le duel des titulaires", model["pitcher_quality_adj"]),
        ("le bilan domicile/extérieur du titulaire", model["home_road_adj"]),
        ("le bilan jour/nuit du titulaire", model["daynight_adj"]),
        ("le platoon (main du titulaire vs ligne adverse)", model["platoon_adj"]),
        ("l'historique du titulaire face à cette équipe", model["vs_team_adj"]),
        ("le style de jeu de la ligne adverse", model["playstyle_adj"]),
        ("l'historique des batteurs face à ce titulaire", model["batters_faced_adj"]),
        ("le repos", model["rest_adj"]),
        ("la fatigue du bullpen", model["bullpen_adj"]),
        ("l'historique (H2H)", model["h2h_adj"]),
    ]
    significant = [(label, val) for label, val in components if abs(val) >= DETAIL_THRESHOLD]
    base_sentence = base_sentence[0].upper() + base_sentence[1:]  # 1ère lettre seulement -- .capitalize() écraserait la casse des noms d'équipes

    if not significant:
        return f"{base_sentence}. Aucun facteur du soir ne change vraiment la donne — tous les ajustements sont mineurs."

    significant.sort(key=lambda x: abs(x[1]), reverse=True)
    parts = []
    for label, val in significant[:2]:  # les 2 plus significatifs seulement, pour rester court
        direction = home_team if val > 0 else away_team
        parts.append(f"{label} favorise {direction} ({val*100:+.1f} pts)")

    return f"{base_sentence}. Ce soir, {' et '.join(parts)}."


def format_verdict_block(model: dict, value: dict, units: float) -> str:
    """Bloc final : avis en langage clair + suggestion, avec un marqueur de
    confiance (🟢 fiable / 🟠 méfiance / ⚪ aucune value) piloté directement
    par les unités de stake_units -- pas de logique de couleur séparée à
    maintenir en plus.
    """
    if value["best_edge"] is None or value["best_edge"] < VALUE_TIER_MODEREE:
        return "⚪ Aucune value nette détectée ce soir sur ce match.\n"

    home_team = value["home_team"]
    away_team = value["away_team"]
    value_team = home_team if value["best_side"] == "home" else away_team
    favori_side = "home" if model["p_home"] >= 0.5 else "away"
    favori_team = home_team if favori_side == "home" else away_team
    agree = value["best_side"] == favori_side

    if units <= 0.5:
        marker = "🟠"
        avis = (
            f"Un écart aussi large contre Pinnacle seul ({abs(value['best_edge'])*100:.0f} pts) est plus "
            f"souvent le signe d'une donnée manquante ou erronée de notre côté qu'une vraie occasion."
        )
        suggestion = f"Méfiance — à vérifier avant de suivre. Mise réduite ({units}u)."
    elif agree:
        marker = "🟢"
        avis = (
            f"Le marché sous-estime {value_team} : il lui donne moins de chances qu'on ne lui en "
            f"donne, un écart de {abs(value['best_edge'])*100:.1f} points."
        )
        suggestion = f"{value_team} — favori et value pointent dans la même direction, signal cohérent ({units}u)."
    else:
        marker = "🟢"
        avis = (
            f"{favori_team} reste notre léger favori, mais le marché le surcote nettement plus que "
            f"nous ne le faisons — l'opportunité se loge côté {value_team}."
        )
        suggestion = f"{value_team} — c'est là que se situe l'écart avec le marché, pas côté favori ({units}u)."

    return f"{marker} <b>Avis</b> : {avis}\n{marker} <b>Suggestion</b> : {suggestion}\n"


PLAYABLE_BOOKS_PRIORITY = ["betclic", "unibet"]  # ordre de préférence pour le prix "réellement jouable"


def evaluate_value(model: dict, odds_events: list[dict]) -> dict:
    """Rassemble tout ce qui concerne les cotes et la value pour un match :
    prix jouable (Betclic en priorité, Unibet en secours puisque Betclic ne
    couvre pas le MLB sur cette API), proba de marché Pinnacle dévigorée,
    edge, et si les deux books sont d'accord entre eux. Utilisé à la fois
    par le message par match et par le résumé de fin de soirée.
    """
    home_team = model["home_name"]
    away_team = model["away_name"]
    p_home = model["p_home"]
    p_away = 1 - p_home

    result = {
        "home_team": home_team,
        "away_team": away_team,
        "p_home": p_home,
        "p_away": p_away,
        "odds_found": False,
        "pinnacle_found": False,
        "playable_book": None,
        "playable_home_price": None,
        "playable_away_price": None,
        "market_home": None,
        "market_away": None,
        "edge_home": None,
        "edge_away": None,
        "best_side": None,
        "best_edge": None,
        "books_agree": None,
    }

    odds_event = find_odds_event(odds_events, home_team, away_team)
    if odds_event is None:
        return result
    result["odds_found"] = True

    book_prices = extract_book_prices(odds_event)
    pinnacle = book_prices.get("pinnacle")
    ev_home_name = odds_event.get("home_team", home_team)
    ev_away_name = odds_event.get("away_team", away_team)

    for book_key in PLAYABLE_BOOKS_PRIORITY:
        prices = book_prices.get(book_key)
        if prices and prices.get(ev_home_name) and prices.get(ev_away_name):
            result["playable_book"] = book_key
            result["playable_home_price"] = prices.get(ev_home_name)
            result["playable_away_price"] = prices.get(ev_away_name)
            break

    if not pinnacle:
        return result
    result["pinnacle_found"] = True

    market_home, market_away = devig_two_way(pinnacle[ev_home_name], pinnacle[ev_away_name])
    result["market_home"] = market_home
    result["market_away"] = market_away

    edge_home = p_home - market_home
    edge_away = p_away - market_away
    result["edge_home"] = edge_home
    result["edge_away"] = edge_away

    if edge_home >= edge_away:
        result["best_side"] = "home"
        result["best_edge"] = edge_home
    else:
        result["best_side"] = "away"
        result["best_edge"] = edge_away

    if result["playable_home_price"] and result["playable_away_price"]:
        playable_market_home, _ = devig_two_way(result["playable_home_price"], result["playable_away_price"])
        result["books_agree"] = abs(playable_market_home - market_home) < BOOKS_AGREE_TOLERANCE

    return result


def stake_units(value: dict, model: dict) -> tuple[float, list[str]]:
    """Détermine la mise en UNITÉS (u) -- PAS l'edge le plus gros qui donne
    le plus d'unités, mais la fiabilité perçue du signal (voir la
    discussion Blue Jays/Rays : un edge extrême vs Pinnacle seul est plus
    souvent un signe d'erreur que d'opportunité, donc plafonné bas ici).
    1u = 1% de la bankroll de suivi. Renvoie les unités et les raisons,
    pour le débrief.
    """
    edge = value["best_edge"]
    if edge is None or edge < VALUE_TIER_MODEREE:
        return 0.0, ["pas de value nette"]

    reasons = []

    if edge > VALUE_TIER_SUSPECT:
        reasons.append("edge très large vs Pinnacle seul -- suspect, mise plafonnée par précaution")
        return 0.5, reasons

    units = 2.0 if edge >= VALUE_TIER_FORTE else 1.0
    reasons.append(f"edge {'fort' if edge >= VALUE_TIER_FORTE else 'modéré'} et cohérent (pas un outlier)")

    if value.get("books_agree"):
        units += 1.0
        book_name = (value.get("playable_book") or "le book jouable").capitalize()
        reasons.append(f"{book_name} et Pinnacle d'accord entre eux")
    elif value.get("playable_home_price") is None:
        reasons.append("aucun book jouable listé (Betclic/Unibet) -- confirmation impossible")

    side = value["best_side"]
    lineup_used = model["home_lineup_used"] if side == "home" else model["away_lineup_used"]
    if lineup_used:
        units += 0.5
        reasons.append("line-up réel confirmé côté favori de la value")

    return min(units, MAX_UNITS_PER_PICK), reasons


def compute_model_probability(game: dict, split_records: dict) -> dict:
    """Calcule la proba modèle + le détail de chaque brique, pour le débrief."""
    home_id = game["teams"]["home"]["team"]["id"]
    away_id = game["teams"]["away"]["team"]["id"]
    home_name = game["teams"]["home"]["team"]["name"]
    away_name = game["teams"]["away"]["team"]["name"]
    game_time_utc = game.get("gameDate", "")
    game_date = game_time_utc[:10]

    home_rec = split_records.get(home_id, {"home_pct": 0.5, "road_pct": 0.5, "last10_pct": 0.5})
    away_rec = split_records.get(away_id, {"home_pct": 0.5, "road_pct": 0.5, "last10_pct": 0.5})
    baseline = log5(home_rec["home_pct"], away_rec["road_pct"])
    form_adj = recent_form_edge(home_rec["last10_pct"], away_rec["last10_pct"])

    home_pitcher_id = game["teams"]["home"].get("probablePitcher", {}).get("id")
    away_pitcher_id = game["teams"]["away"].get("probablePitcher", {}).get("id")
    home_pitcher_name = game["teams"]["home"].get("probablePitcher", {}).get("fullName", "Titulaire non encore annoncé")
    away_pitcher_name = game["teams"]["away"].get("probablePitcher", {}).get("fullName", "Titulaire non encore annoncé")
    home_pitcher_stats = get_pitcher_season_stats(home_pitcher_id, CURRENT_SEASON) if home_pitcher_id else None
    away_pitcher_stats = get_pitcher_season_stats(away_pitcher_id, CURRENT_SEASON) if away_pitcher_id else None
    home_fip = home_pitcher_stats["fip"] if home_pitcher_stats else None
    away_fip = away_pitcher_stats["fip"] if away_pitcher_stats else None
    home_era = home_pitcher_stats["era"] if home_pitcher_stats else None
    away_era = away_pitcher_stats["era"] if away_pitcher_stats else None

    home_ops, home_lineup_used = get_effective_offense_ops(game, "home", home_id, CURRENT_SEASON)
    away_ops, away_lineup_used = get_effective_offense_ops(game, "away", away_id, CURRENT_SEASON)

    pitcher_quality_adj = starter_edge(home_fip) - starter_edge(away_fip)
    adversity_adj = opposing_offense_penalty(away_ops) - opposing_offense_penalty(home_ops)

    # Ajustement fin par arsenal de lancers -- seulement possible si le
    # line-up réel est publié (on a besoin des vrais batteurs, pas d'une
    # moyenne d'équipe). Enveloppé en try/except par précaution : c'est la
    # brique la plus expérimentale du modèle, une erreur ici ne doit jamais
    # faire échouer le calcul du reste.
    arsenal_adj = 0.0
    try:
        home_batter_ids = _extract_lineup_player_ids(game, "home")
        away_batter_ids = _extract_lineup_player_ids(game, "away")
        pitcher_arsenal_df, batter_arsenal_df = pitch_arsenal.load_arsenal_data(CURRENT_SEASON)
        home_arsenal_edge = pitch_arsenal.arsenal_adjustment(
            pitcher_arsenal_df, batter_arsenal_df, home_pitcher_id, away_batter_ids
        )
        away_arsenal_edge = pitch_arsenal.arsenal_adjustment(
            pitcher_arsenal_df, batter_arsenal_df, away_pitcher_id, home_batter_ids
        )
        arsenal_adj = home_arsenal_edge - away_arsenal_edge
    except Exception as exc:
        print(f"  (ajustement arsenal indisponible pour ce match : {exc})")

    # Vague 1 : splits lanceur supplémentaires -- chacun enveloppé séparément
    # en try/except, pour qu'un souci sur l'un n'affecte jamais les autres.
    home_road_adj = 0.0
    try:
        home_split = get_pitcher_split_stat(home_pitcher_id, CURRENT_SEASON, SIT_CODE_HOME) if home_pitcher_id else None
        away_split = get_pitcher_split_stat(away_pitcher_id, CURRENT_SEASON, SIT_CODE_ROAD) if away_pitcher_id else None
        home_road_adj = split_era_edge(home_split, home_era, HOME_ROAD_SENSITIVITY) - split_era_edge(
            away_split, away_era, HOME_ROAD_SENSITIVITY
        )
    except Exception as exc:
        print(f"  (split domicile/extérieur indisponible : {exc})")

    daynight_adj = 0.0
    try:
        sit_code = SIT_CODE_DAY if is_day_game(game_time_utc) else SIT_CODE_NIGHT
        home_dn_split = get_pitcher_split_stat(home_pitcher_id, CURRENT_SEASON, sit_code) if home_pitcher_id else None
        away_dn_split = get_pitcher_split_stat(away_pitcher_id, CURRENT_SEASON, sit_code) if away_pitcher_id else None
        daynight_adj = split_era_edge(home_dn_split, home_era, DAYNIGHT_SENSITIVITY) - split_era_edge(
            away_dn_split, away_era, DAYNIGHT_SENSITIVITY
        )
    except Exception as exc:
        print(f"  (split jour/nuit indisponible : {exc})")

    platoon_adj = 0.0
    playstyle_adj = 0.0
    try:
        home_hand = get_pitcher_hand(home_pitcher_id, game["teams"]["home"].get("probablePitcher", {})) if home_pitcher_id else None
        away_hand = get_pitcher_hand(away_pitcher_id, game["teams"]["away"].get("probablePitcher", {})) if away_pitcher_id else None
        away_ops_vs_home_hand = get_team_offense_vs_hand(away_id, CURRENT_SEASON, home_hand) if home_hand else None
        home_ops_vs_away_hand = get_team_offense_vs_hand(home_id, CURRENT_SEASON, away_hand) if away_hand else None
        away_offense = get_team_season_offense(away_id, CURRENT_SEASON)
        home_offense = get_team_season_offense(home_id, CURRENT_SEASON)
        away_ops_season = _safe_float(away_offense["ops"]) if away_offense else None
        home_ops_season = _safe_float(home_offense["ops"]) if home_offense else None
        platoon_adj = platoon_edge(away_ops_vs_home_hand, away_ops_season) - platoon_edge(
            home_ops_vs_away_hand, home_ops_season
        )
        # Style de jeu : le titulaire home affronte le style de la ligne away, et vice versa
        playstyle_adj = playstyle_edge(away_offense) - playstyle_edge(home_offense)
    except Exception as exc:
        print(f"  (platoon split / style de jeu indisponible : {exc})")

    batters_faced_adj = 0.0
    try:
        home_batter_ids_bvp = _extract_lineup_player_ids(game, "home")
        away_batter_ids_bvp = _extract_lineup_player_ids(game, "away")
        home_bvp_edge = batters_vs_pitcher_edge(away_batter_ids_bvp, home_pitcher_id, CURRENT_SEASON, LEAGUE_AVG_OPS)
        away_bvp_edge = batters_vs_pitcher_edge(home_batter_ids_bvp, away_pitcher_id, CURRENT_SEASON, LEAGUE_AVG_OPS)
        # Edge positif pour le batteur = mauvais pour le lanceur -> on inverse le signe
        batters_faced_adj = -home_bvp_edge + away_bvp_edge
    except Exception as exc:
        print(f"  (historique batteurs vs titulaire indisponible : {exc})")

    vs_team_adj = 0.0
    try:
        home_vs_team = get_pitcher_vs_team_stat(home_pitcher_id, CURRENT_SEASON, away_id) if home_pitcher_id else None
        away_vs_team = get_pitcher_vs_team_stat(away_pitcher_id, CURRENT_SEASON, home_id) if away_pitcher_id else None
        vs_team_adj = split_era_edge(home_vs_team, home_era, VS_TEAM_SENSITIVITY) - split_era_edge(
            away_vs_team, away_era, VS_TEAM_SENSITIVITY
        )
    except Exception as exc:
        print(f"  (historique vs cette équipe indisponible : {exc})")

    # Météo : récupérée et loggée pour un futur marché Total, mais PAS
    # pondérée dans le moneyline -- son effet réel joue sur le nombre de
    # runs marqués, pas directement sur qui gagne, et l'estimer sans
    # profil de puissance des deux équipes serait trop approximatif.
    weather = None
    try:
        stadium = get_stadium_info(home_name)
        if stadium and not stadium["roofed"]:
            weather = get_weather_for_game(stadium["lat"], stadium["lon"], game_time_utc)
    except Exception as exc:
        print(f"  (météo indisponible : {exc})")

    home_rest = get_days_rest(home_id, game_date)
    away_rest = get_days_rest(away_id, game_date)
    rest_adj = rest_edge(home_rest) - rest_edge(away_rest)

    home_bullpen_pitches = get_bullpen_fatigue(home_id, game_date)
    away_bullpen_pitches = get_bullpen_fatigue(away_id, game_date)
    bullpen_adj = bullpen_fatigue_edge(home_bullpen_pitches, away_bullpen_pitches)

    h2h = get_h2h_record(home_id, away_id, game_date)
    h2h_adj = h2h_edge(h2h)

    p_home = (
        baseline + form_adj + pitcher_quality_adj + adversity_adj + arsenal_adj
        + home_road_adj + daynight_adj + platoon_adj + vs_team_adj
        + playstyle_adj + batters_faced_adj
        + rest_adj + bullpen_adj + h2h_adj
    )
    p_home = min(max(p_home, 0.03), 0.97)  # on évite les probas absurdes à 0% ou 100%

    return {
        "p_home": p_home,
        "baseline": baseline,
        "form_adj": form_adj,
        "pitcher_quality_adj": pitcher_quality_adj,
        "adversity_adj": adversity_adj,
        "arsenal_adj": arsenal_adj,
        "home_road_adj": home_road_adj,
        "daynight_adj": daynight_adj,
        "platoon_adj": platoon_adj,
        "vs_team_adj": vs_team_adj,
        "playstyle_adj": playstyle_adj,
        "batters_faced_adj": batters_faced_adj,
        "weather": weather,
        "rest_adj": rest_adj,
        "bullpen_adj": bullpen_adj,
        "h2h_adj": h2h_adj,
        "h2h_games": h2h.get("games_played", 0),
        "home_lineup_used": home_lineup_used,
        "away_lineup_used": away_lineup_used,
        "home_name": home_name,
        "away_name": away_name,
        "game_time_utc": game_time_utc,
        "home_pitcher_name": home_pitcher_name,
        "away_pitcher_name": away_pitcher_name,
        "home_era": home_era,
        "away_era": away_era,
        "home_fip": home_fip,
        "away_fip": away_fip,
    }


def pitcher_confirmation_status(model: dict) -> str:
    """Indique si les DEUX titulaires sont confirmés (nom annoncé + stats
    trouvées) ou si l'un des deux repose sur une donnée incomplète -- utile
    pour un lancement proche des matchs, quand on veut être sûr que le
    modèle tourne sur du solide plutôt que sur un repli.
    """
    home_ok = model["home_pitcher_name"] != "Titulaire non encore annoncé" and model["home_fip"] is not None
    away_ok = model["away_pitcher_name"] != "Titulaire non encore annoncé" and model["away_fip"] is not None

    if home_ok and away_ok:
        return "✅ Titulaires confirmés, stats disponibles"

    missing = []
    if not home_ok:
        missing.append(model["home_name"])
    if not away_ok:
        missing.append(model["away_name"])
    return f"⚠️ Titulaire non confirmé ou stats manquantes : {', '.join(missing)}"


OFFENSE_CONTEXT_THRESHOLD = 0.02


def format_offense_context(model: dict) -> str:
    """Ligne dédiée à l'attaque adverse (adversité OPS + arsenal), séparée
    du duel titulaires -- pour que le contexte batteurs ne soit jamais
    éclipsé par un gros ajustement titulaire par ailleurs.
    """
    combined = model["adversity_adj"] + model["arsenal_adj"]
    if abs(combined) < OFFENSE_CONTEXT_THRESHOLD:
        return "⚔️ Attaques : rien de spécial à signaler ce soir."

    favored = model["home_name"] if combined > 0 else model["away_name"]
    return f"⚔️ Attaques : l'avantage penche pour {favored} face à l'arsenal adverse ({combined*100:+.1f} pts)."


def format_weather_line(model: dict) -> str:
    """Affiche la météo si disponible (stade sans toit). Pas encore
    pondérée dans le calcul du moneyline -- collectée en vue d'un futur
    marché Total, où son effet est bien plus direct.
    """
    weather = model.get("weather")
    if weather is None:
        return "🌤 Météo : non disponible ou stade avec toit"
    return (
        f"🌤 Météo : {weather['temp_c']}°C | vent {weather['wind_kmh']} km/h | "
        f"humidité {weather['humidity_pct']}% <i>(pas encore utilisée dans le calcul, collectée pour le Total à venir)</i>"
    )


def format_value_block(model: dict, value: dict) -> str:
    home_team = model["home_name"]
    away_team = model["away_name"]
    p_home = model["p_home"]
    p_away = 1 - p_home

    lineup_note = ""
    if model["home_lineup_used"] or model["away_lineup_used"]:
        who = []
        if model["home_lineup_used"]:
            who.append(home_team)
        if model["away_lineup_used"]:
            who.append(away_team)
        lineup_note = f" [line-up réel : {', '.join(who)}]"

    lines = [
        f"⚾ <b>{away_team} @ {home_team}</b>{lineup_note}\n",
        f"🕐 {format_game_time_paris(model['game_time_utc'])} (heure de Paris)\n",
        f"📊 Modèle : {home_team} {p_home*100:.1f}% | {away_team} {p_away*100:.1f}%\n",
        f"🎯 Titulaires : {model['away_pitcher_name']} (ERA {model['away_era'] or '?'}, FIP {model['away_fip'] or '?'}) "
        f"@ {model['home_pitcher_name']} (ERA {model['home_era'] or '?'}, FIP {model['home_fip'] or '?'})\n",
        f"{pitcher_confirmation_status(model)}\n",
        f"🔧 <b>Détail</b> : {format_detail_explanation(model)}\n",
        f"{format_offense_context(model)}\n",
        f"{format_weather_line(model)}\n",
    ]

    favori = home_team if p_home >= 0.5 else away_team
    lines.append(f"⭐ Favori (modèle) : {favori}\n")

    if not value["odds_found"]:
        lines.append("🎲 Cotes : indisponibles pour ce match (pas encore ouvertes, ou hors couverture)\n")
        return "\n".join(lines)

    if value["playable_book"]:
        book_label = value["playable_book"].capitalize()
        lines.append(
            f"🎲 Cote {book_label} : {home_team} @{value['playable_home_price']} | "
            f"{away_team} @{value['playable_away_price']}\n"
        )
    else:
        lines.append("🎲 Cote : non listée pour ce match (Betclic/Unibet)\n")

    if not value["pinnacle_found"]:
        lines.append("💰 Value : impossible à calculer (pas de prix Pinnacle pour ce match)\n")
        return "\n".join(lines)

    lines.append(
        f"📈 Marché (Pinnacle, dévigoré) : {home_team} {value['market_home']*100:.1f}% | "
        f"{away_team} {value['market_away']*100:.1f}%\n"
    )

    units, _reasons = stake_units(value, model)

    best_edge = value["best_edge"]
    if best_edge is not None and best_edge >= VALUE_TIER_MODEREE:
        value_team = home_team if value["best_side"] == "home" else away_team
        lines.append(
            f"💰 Value : {value_team} {best_edge*100:+.1f} pts\n"
            f"<i>écart entre notre estimation et celle du marché</i>\n"
        )

    lines.append(format_verdict_block(model, value, units))

    return "\n".join(lines)


def format_summary(picks: list[dict]) -> str:
    """Résumé de fin de soirée -- les MAX_SUMMARY_PICKS values les plus
    fiables (les messages par match, eux, gardent l'analyse de tous les
    matchs), dimensionnées en unités et en euros (ta grille personnelle).
    """
    if not picks:
        return (
            "📋 <b>Résumé du soir</b>\n\n"
            "Aucune value jugée assez fiable ce soir. C'est un résultat normal -- "
            "pas besoin de forcer un pick chaque soir.\n"
        )

    lines = [f"📋 <b>Résumé du soir -- top {len(picks)} pick(s) sur la fiabilité</b>\n"]
    total_units = 0.0
    for i, pick in enumerate(picks, start=1):
        total_units += pick["units"]
        marker = "🟠" if pick["units"] <= 0.5 else "🟢"
        lines.append(
            f"{marker} {i}. <b>{pick['team']}</b> ({pick['matchup']}) — edge {pick['edge']*100:+.1f} pts — "
            f"<b>{pick['units']}u</b> ({units_to_eur_display(pick['units'])})\n"
            f"   {'; '.join(pick['reasons'])}\n"
        )
    lines.append(f"\nTotal engagé ce soir : <b>{total_units}u</b> (1u = 1% de la bankroll de suivi, départ 100)\n")
    lines.append(
        "⚠️ Dimensionné par fiabilité perçue du signal, pas par edge brut. "
        "Le modèle reste non validé -- à toi de juger.\n"
    )
    return "\n".join(lines)


def chunk_message(text: str, limit: int = 3800) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)]


def save_predictions_log(date_str: str, log_entries: list[dict], collection_errors: list[dict]) -> None:
    """Sauvegarde les prédictions du soir dans data/YYYY-MM-DD.json, pour
    que debrief.py puisse les comparer aux résultats réels le lendemain,
    sans aucun copier-coller manuel. Garde aussi la trace des matchs qui
    ont échoué à la collecte (et pourquoi), pour ne plus jamais se
    demander pourquoi le compte de matchs analysés est plus bas que prévu.
    """
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{date_str}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"date": date_str, "games": log_entries, "collection_errors": collection_errors},
            f, indent=2, ensure_ascii=False,
        )
    print(f"Prédictions sauvegardées dans {path}")


def main() -> None:
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    date_str = date_arg or datetime.date.today().isoformat()
    games = get_games_for_value_bets(date_arg)

    if not games:
        send_message("Aucun match MLB programmé pour cette date.")
        print("Aucun match trouvé.")
        return

    print("Récupération des bilans domicile/extérieur/forme récente...")
    split_records = get_team_split_records(CURRENT_SEASON)

    print("Récupération des cotes (The Odds API)...")
    odds_events = get_odds_for_mlb()

    matchs_envoyes = 0
    matchs_echoues = 0
    candidates = []
    log_entries = []
    collection_errors = []

    for game in games:
        home = game["teams"]["home"]["team"]["name"]
        away = game["teams"]["away"]["team"]["name"]
        try:
            model = compute_model_probability(game, split_records)
            value = evaluate_value(model, odds_events)

            value_text = format_value_block(model, value)
            for chunk in chunk_message(value_text):
                send_message(chunk)
            matchs_envoyes += 1

            log_entry = {
                "game_pk": game.get("gamePk"),
                "home_team": model["home_name"],
                "away_team": model["away_name"],
                "p_home": model["p_home"],
                "best_side": value["best_side"],
                "best_edge": value["best_edge"],
                "stake_units": 0.0,
                "stake_price": None,
                "stake_reasons": [],
                "books_agree": value.get("books_agree"),
                "home_lineup_used": model["home_lineup_used"],
                "away_lineup_used": model["away_lineup_used"],
                "components": {
                    "baseline": model["baseline"],
                    "form_adj": model["form_adj"],
                    "pitcher_quality_adj": model["pitcher_quality_adj"],
                    "adversity_adj": model["adversity_adj"],
                    "arsenal_adj": model["arsenal_adj"],
                    "home_road_adj": model["home_road_adj"],
                    "daynight_adj": model["daynight_adj"],
                    "platoon_adj": model["platoon_adj"],
                    "vs_team_adj": model["vs_team_adj"],
                    "playstyle_adj": model["playstyle_adj"],
                    "batters_faced_adj": model["batters_faced_adj"],
                    "rest_adj": model["rest_adj"],
                    "bullpen_adj": model["bullpen_adj"],
                    "h2h_adj": model["h2h_adj"],
                },
            }

            units, reasons = stake_units(value, model)
            if units > 0:
                side = value["best_side"]
                # Prix réellement jouable en priorité (Betclic, puis Unibet en
                # secours) ; à défaut, le prix "juste" implicite de Pinnacle --
                # utile pour le suivi du modèle, mais pas un prix réellement
                # disponible si aucun des deux books ne liste ce match.
                if side == "home":
                    price = value.get("playable_home_price") or (
                        1 / value["market_home"] if value.get("market_home") else None
                    )
                else:
                    price = value.get("playable_away_price") or (
                        1 / value["market_away"] if value.get("market_away") else None
                    )
                log_entry["stake_units"] = units
                log_entry["stake_price"] = price
                log_entry["stake_reasons"] = reasons

                team = value["home_team"] if side == "home" else value["away_team"]
                candidates.append(
                    {
                        "team": team,
                        "matchup": f"{value['away_team']} @ {value['home_team']}",
                        "edge": value["best_edge"],
                        "units": units,
                        "reasons": reasons,
                    }
                )

            log_entries.append(log_entry)
        except Exception as exc:
            print(f"  ⚠️ Match ignoré ({away} @ {home}) après erreur : {exc}")
            collection_errors.append({"matchup": f"{away} @ {home}", "reason": str(exc)})
            matchs_echoues += 1
        time.sleep(0.3)

    candidates.sort(key=lambda c: c["units"], reverse=True)
    picks = candidates[:MAX_SUMMARY_PICKS]  # le résumé se limite aux plus fiables ; les messages par match gardent tout

    summary_text = format_summary(picks)
    for chunk in chunk_message(summary_text):
        send_message(chunk)

    save_predictions_log(date_str, log_entries, collection_errors)

    print(f"{matchs_envoyes} verdict(s) envoyé(s) sur Telegram, {matchs_echoues} ignoré(s), {len(picks)} pick(s) dimensionné(s).")


if __name__ == "__main__":
    main()
