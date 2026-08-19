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

Prérequis : une clé gratuite sur https://the-odds-api.com dans ton .env :
    ODDS_API_KEY=ta_clé

Usage :
    python get_value_bets.py
    python get_value_bets.py 2026-07-21
"""

import os
import sys
import time
import json
import math
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
    SEASON_START,
)
import pitch_arsenal

load_dotenv()

DATA_DIR = Path(__file__).parent / "data"

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
CURRENT_SEASON = datetime.date.today().year

LEAGUE_AVG_FIP = 4.20
STARTER_SENSITIVITY = 0.065
LEAGUE_AVG_OPS = 0.720
OFFENSE_SENSITIVITY = 0.08

RECENT_FORM_SENSITIVITY = 0.12

BACK_TO_BACK_PENALTY = 0.015

MIN_SPLIT_IP = 15.0
HOME_ROAD_SENSITIVITY = 0.05
PLATOON_SENSITIVITY = 0.10

SIT_CODE_HOME = "h"
SIT_CODE_ROAD = "r"
SIT_CODE_VS_LHP = "vl"
SIT_CODE_VS_RHP = "vr"

DAYNIGHT_SENSITIVITY = 0.0
BULLPEN_FATIGUE_SENSITIVITY = 0.0

LEAGUE_AVG_K_PCT = 0.225
LEAGUE_AVG_BB_PCT = 0.085
PLAYSTYLE_SENSITIVITY = 0.15
STOLEN_BASE_SENSITIVITY = 0.00008

MIN_VS_PITCHER_PA = 3
VS_PITCHER_SENSITIVITY = 0.02

H2H_MIN_GAMES = 2
H2H_MAX_WEIGHT_GAMES = 12
H2H_SENSITIVITY = 0.10

VALUE_TIER_FORTE = 0.08
VALUE_TIER_MODEREE = 0.03
VALUE_TIER_SUSPECT = 0.15

BOOKS_AGREE_TOLERANCE = 0.05

CONFIDENCE_WEIGHTS = {
    "pitcher_quality_adj": 1.0,
    "home_road_adj": 1.0,
    "form_adj": 0.4,
    "h2h_adj": 0.35,
    "adversity_adj": 0.3,
    "platoon_adj": 0.25,
    "playstyle_adj": 0.25,
    "arsenal_adj": 0.15,
    "batters_faced_adj": 0.15,
}
CONFIDENCE_SCORE_MIN = 0.05
CONFIDENCE_SCORE_STRONG = 0.12

INDIVIDUAL_CONTRIBUTION_MIN = 0.02
MULTI_FACTOR_MIN_CONTRIBUTORS = 2


def compute_confidence_score(model: dict) -> float:
    return sum(weight * abs(model.get(name, 0.0)) for name, weight in CONFIDENCE_WEIGHTS.items())


def count_meaningful_contributors(model: dict, side: str) -> int:
    count = 0
    for name in CONFIDENCE_WEIGHTS:
        val = model.get(name, 0.0)
        if abs(val) < INDIVIDUAL_CONTRIBUTION_MIN:
            continue
        if (side == "home" and val > 0) or (side == "away" and val < 0):
            count += 1
    return count


CONFIDENCE_STAR_THRESHOLDS = [0.03, 0.05, 0.08, 0.11]
MAX_SINGLE_CONTRIBUTION = 0.08


def compute_confidence_stars_score(model: dict, side: str) -> float:
    contributions = {
        name: min(weight * abs(model.get(name, 0.0)), MAX_SINGLE_CONTRIBUTION)
        for name, weight in CONFIDENCE_WEIGHTS.items()
    }
    raw_score = sum(contributions.values())
    if raw_score <= 0:
        return 0.0
    biggest_share = max(contributions.values()) / raw_score
    return raw_score * (0.5 + 0.5 * (1 - biggest_share))


def confidence_stars(score: float) -> str:
    n = 5
    for i, threshold in enumerate(CONFIDENCE_STAR_THRESHOLDS):
        if score < threshold:
            n = i + 1
            break
    return "⭐" * n


MAX_UNITS_PER_PICK = 2.0
EUR_PER_UNIT = 5.0


def units_to_eur_display(units: float) -> str:
    return f"{units * EUR_PER_UNIT:.2f}€"


PARIS_TZ = ZoneInfo("Europe/Paris")


def format_game_time_paris(game_time_utc: str) -> str:
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


def get_games_for_value_bets(date_str: str | None = None) -> list[dict]:
    if date_str is None:
        date_str = datetime.date.today().isoformat()
    params = {"sportId": 1, "date": date_str, "hydrate": "team,probablePitcher,venue,lineups"}
    data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)
    games = []
    for day in data.get("dates", []):
        games.extend(day.get("games", []))
    games.sort(key=lambda g: g.get("gameDate", ""))
    return games


def get_team_split_records(season: int) -> dict:
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
    denom = pct_a + pct_b - 2 * pct_a * pct_b
    if denom <= 0:
        return 0.5
    return (pct_a - pct_a * pct_b) / denom


def recent_form_edge(home_last10_pct: float, away_last10_pct: float) -> float:
    return (home_last10_pct - away_last10_pct) * RECENT_FORM_SENSITIVITY


def starter_edge(pitcher_fip: float | None) -> float:
    if pitcher_fip is None:
        return 0.0
    return (LEAGUE_AVG_FIP - pitcher_fip) * STARTER_SENSITIVITY


def opposing_offense_penalty(opposing_ops) -> float:
    ops = _safe_float(opposing_ops)
    if ops is None:
        return 0.0
    return (LEAGUE_AVG_OPS - ops) * OFFENSE_SENSITIVITY


def _extract_lineup_player_ids(game: dict, side: str) -> list[int]:
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
    player_ids = _extract_lineup_player_ids(game, side)
    if player_ids:
        ops_values = [v for v in (get_batter_season_ops(pid, season) for pid in player_ids) if v is not None]
        if ops_values:
            return sum(ops_values) / len(ops_values), True
    offense = get_team_season_offense(team_id, season)
    return (_safe_float(offense["ops"]) if offense else None), False


def get_pitcher_split_stat(pitcher_id: int, season: int, sit_code: str) -> tuple[float, float] | None:
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


def get_pitcher_hand(pitcher_id: int, probable_pitcher_obj: dict) -> str | None:
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


def split_era_edge(split_stat: tuple[float, float] | None, season_era, sensitivity: float) -> float:
    season_era = _safe_float(season_era)
    if split_stat is None or season_era is None:
        return 0.0
    split_era, _ip = split_stat
    return (season_era - split_era) * sensitivity


def platoon_edge(team_ops_vs_hand: float | None, team_ops_season: float | None) -> float:
    if team_ops_vs_hand is None or team_ops_season is None:
        return 0.0
    return (team_ops_season - team_ops_vs_hand) * PLATOON_SENSITIVITY


def playstyle_edge(opposing_offense: dict | None) -> float:
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
        edge -= stolen_bases * STOLEN_BASE_SENSITIVITY
    return edge


def get_batter_vs_pitcher_stat(batter_id: int, pitcher_id: int, season: int) -> dict | None:
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


def rest_edge(days_off: int | None) -> float:
    if days_off == 0:
        return -BACK_TO_BACK_PENALTY
    return 0.0


def h2h_edge(h2h: dict) -> float:
    games = h2h.get("games_played", 0)
    if games < H2H_MIN_GAMES:
        return 0.0
    home_rate = h2h["wins_a"] / games
    confidence = min(games / H2H_MAX_WEIGHT_GAMES, 1.0)
    return (home_rate - 0.5) * H2H_SENSITIVITY * confidence


# ---------------------------------------------------------------------------
# Marché Total (over/under) -- ajouté le 18 août, ENTIÈREMENT séparé du
# moneyline. Ne contribue JAMAIS aux étoiles, au choix du vainqueur, ni à
# la mise moneyline -- une section à part dans le message, avec sa propre
# mise indépendante et volontairement simple (1u/0.5u/aucune).
# ---------------------------------------------------------------------------

LEAGUE_AVG_ERA_FOR_TOTALS = 4.20
LEAGUE_AVG_TOTAL_RUNS = 9.0

PARK_FACTOR_TOTAL_MULTIPLIER = {-2: 0.90, -1: 0.95, 0: 1.00, 1: 1.05, 2: 1.10}

LEAGUE_AVG_TEMP_C = 20.0
WEATHER_TEMP_SENSITIVITY = 0.002

H2H_RUNS_MIN_GAMES = 2
H2H_RUNS_MAX_WEIGHT_GAMES = 8
H2H_RUNS_SENSITIVITY = 0.5
SAME_STARTERS_RUNS_SENSITIVITY = 0.3

TOTAL_RUNS_STD_DEV = 3.5

TOTALS_VALUE_TIER_MODEREE = 0.03
TOTALS_VALUE_TIER_SUSPECT = 0.15


def get_h2h_runs_and_same_starters(
    team_a_id: int, team_b_id: int, before_date_str: str, home_pitcher_id, away_pitcher_id
) -> dict:
    params = {
        "sportId": 1,
        "teamId": team_a_id,
        "opponentId": team_b_id,
        "startDate": SEASON_START,
        "endDate": before_date_str,
        "gameType": "R",
        "hydrate": "linescore,probablePitcher",
    }
    try:
        data = get_json_with_retries(f"{MLB_API_BASE}/schedule", params)
    except Exception:
        return {"games_played": 0, "avg_total": None, "same_starters_games": 0, "same_starters_avg_total": None}

    all_totals = []
    same_starters_totals = []
    pitchers_today = {home_pitcher_id, away_pitcher_id}

    for day in data.get("dates", []):
        for game in day.get("games", []):
            linescore_teams = game.get("linescore", {}).get("teams", {})
            home_runs = linescore_teams.get("home", {}).get("runs")
            away_runs = linescore_teams.get("away", {}).get("runs")
            if home_runs is None or away_runs is None:
                continue
            total = home_runs + away_runs
            all_totals.append(total)

            game_home_pitcher = game["teams"]["home"].get("probablePitcher", {}).get("id")
            game_away_pitcher = game["teams"]["away"].get("probablePitcher", {}).get("id")
            if None not in pitchers_today and {game_home_pitcher, game_away_pitcher} == pitchers_today:
                same_starters_totals.append(total)

    return {
        "games_played": len(all_totals),
        "avg_total": (sum(all_totals) / len(all_totals)) if all_totals else None,
        "same_starters_games": len(same_starters_totals),
        "same_starters_avg_total": (sum(same_starters_totals) / len(same_starters_totals)) if same_starters_totals else None,
    }


def h2h_runs_adjustment(h2h_runs: dict) -> float:
    adj = 0.0
    games = h2h_runs.get("games_played", 0)
    if games >= H2H_RUNS_MIN_GAMES and h2h_runs.get("avg_total") is not None:
        confidence = min(games / H2H_RUNS_MAX_WEIGHT_GAMES, 1.0)
        adj += (h2h_runs["avg_total"] - LEAGUE_AVG_TOTAL_RUNS) * H2H_RUNS_SENSITIVITY * confidence / LEAGUE_AVG_TOTAL_RUNS
    same_games = h2h_runs.get("same_starters_games", 0)
    if same_games >= 1 and h2h_runs.get("same_starters_avg_total") is not None:
        adj += (h2h_runs["same_starters_avg_total"] - LEAGUE_AVG_TOTAL_RUNS) * SAME_STARTERS_RUNS_SENSITIVITY / LEAGUE_AVG_TOTAL_RUNS
    return adj


def compute_expected_total(
    home_runs_pg, away_runs_pg, home_era, away_era, park_tier: int, weather: dict | None, h2h_runs: dict
) -> float | None:
    if not home_runs_pg or not away_runs_pg:
        return None
    home_era_f = _safe_float(home_era) or LEAGUE_AVG_ERA_FOR_TOTALS
    away_era_f = _safe_float(away_era) or LEAGUE_AVG_ERA_FOR_TOTALS

    expected_home = home_runs_pg * (away_era_f / LEAGUE_AVG_ERA_FOR_TOTALS)
    expected_away = away_runs_pg * (home_era_f / LEAGUE_AVG_ERA_FOR_TOTALS)
    total = expected_home + expected_away

    park_multiplier = PARK_FACTOR_TOTAL_MULTIPLIER.get(park_tier, 1.0)
    total *= park_multiplier

    if weather and weather.get("temp_c") is not None:
        total *= 1.0 + (weather["temp_c"] - LEAGUE_AVG_TEMP_C) * WEATHER_TEMP_SENSITIVITY

    total += h2h_runs_adjustment(h2h_runs) * LEAGUE_AVG_TOTAL_RUNS

    return max(total, 1.0)


def prob_over_line(expected_total: float, line: float) -> float:
    z = (line - expected_total) / TOTAL_RUNS_STD_DEV
    return 0.5 * (1 - math.erf(z / math.sqrt(2)))


def find_totals_odds(odds_events: list[dict], home_team: str, away_team: str) -> dict | None:
    event = find_odds_event(odds_events, home_team, away_team)
    if event is None:
        return None

    result = {"line": None, "pinnacle_over": None, "pinnacle_under": None, "pmu_over": None, "pmu_under": None}
    for bookmaker in event.get("bookmakers", []):
        key = bookmaker.get("key")
        if key not in ("pinnacle", "pmu_fr"):
            continue
        for market in bookmaker.get("markets", []):
            if market.get("key") != "totals":
                continue
            for outcome in market.get("outcomes", []):
                point = outcome.get("point")
                name = outcome.get("name")
                price = outcome.get("price")
                if result["line"] is None:
                    result["line"] = point
                if point != result["line"]:
                    continue
                if key == "pinnacle" and name == "Over":
                    result["pinnacle_over"] = price
                elif key == "pinnacle" and name == "Under":
                    result["pinnacle_under"] = price
                elif key == "pmu_fr" and name == "Over":
                    result["pmu_over"] = price
                elif key == "pmu_fr" and name == "Under":
                    result["pmu_under"] = price

    if result["line"] is None:
        return None
    return result


def evaluate_totals_value(expected_total: float | None, totals_odds: dict | None) -> dict:
    result = {
        "line": None, "expected_total": expected_total, "best_side": None, "best_edge": None,
        "pinnacle_found": False, "playable_price": None,
    }
    if expected_total is None or totals_odds is None:
        return result

    result["line"] = totals_odds["line"]
    pin_over, pin_under = totals_odds.get("pinnacle_over"), totals_odds.get("pinnacle_under")
    if not pin_over or not pin_under:
        return result
    result["pinnacle_found"] = True

    market_over, market_under = devig_two_way(pin_over, pin_under)
    model_over = prob_over_line(expected_total, totals_odds["line"])
    model_under = 1 - model_over

    edge_over = model_over - market_over
    edge_under = model_under - market_under

    if edge_over >= edge_under:
        result["best_side"] = "over"
        result["best_edge"] = edge_over
        result["playable_price"] = totals_odds.get("pmu_over")
        result["model_prob"] = model_over
    else:
        result["best_side"] = "under"
        result["best_edge"] = edge_under
        result["playable_price"] = totals_odds.get("pmu_under")
        result["model_prob"] = model_under

    return result


def stake_totals_units(totals_value: dict) -> tuple[float, str]:
    edge = totals_value.get("best_edge")
    if edge is None or edge < TOTALS_VALUE_TIER_MODEREE:
        return 0.0, "pas de value nette sur le Total"
    if edge > TOTALS_VALUE_TIER_SUSPECT:
        return 0.5, "edge très large -- suspect, mise réduite par précaution"
    return 1.0, "edge net vs Pinnacle sur le Total"


def format_totals_block(totals_value: dict, units: float, reason: str) -> str:
    if totals_value.get("expected_total") is None:
        return "🎯 Total : données offensives insuffisantes pour estimer ce soir.\n"
    if totals_value.get("line") is None:
        return f"🎯 Total : modèle {totals_value['expected_total']:.1f} runs -- pas de ligne PMU/Pinnacle disponible.\n"
    if not totals_value.get("pinnacle_found"):
        return f"🎯 Total : modèle {totals_value['expected_total']:.1f} runs (ligne {totals_value['line']}) -- pas de prix Pinnacle pour comparer.\n"

    side_label = "Over" if totals_value["best_side"] == "over" else "Under"
    lines = [
        f"🎯 Total : modèle {totals_value['expected_total']:.1f} runs vs ligne {totals_value['line']} "
        f"-- edge {totals_value['best_edge']*100:+.1f} pts sur {side_label}\n"
    ]

    price = totals_value.get("playable_price")
    if units <= 0:
        lines.append(f"⚪ Aucune sélection sur le Total ({reason}).\n")
    else:
        marker = "🟠" if units <= 0.5 else "🟢"
        eur = units * EUR_PER_UNIT
        if price:
            ev_pct = compute_ev_pct(totals_value["model_prob"], price)
            lines.append(
                f"{marker} <b>Total : {side_label} {totals_value['line']}</b> — {units}u ({eur:.2f}€) "
                f"-- cote PMU @{price}, EV {ev_pct:+.1f}%\n"
            )
        else:
            lines.append(
                f"{marker} <b>Total : {side_label} {totals_value['line']}</b> — {units}u ({eur:.2f}€) "
                f"-- cote PMU indisponible, prix non confirmé\n"
            )
        lines.append(f"<i>{reason}</i>\n")

    return "".join(lines)


def get_odds_for_mlb() -> list[dict]:
    if not ODDS_API_KEY:
        raise RuntimeError(
            "ODDS_API_KEY doit être défini dans ton .env. Crée un compte gratuit sur "
            "https://the-odds-api.com (pas theoddsapi.com, un service différent)."
        )
    params = {
        "apiKey": ODDS_API_KEY,
        "bookmakers": "betclic,unibet,pinnacle,pmu_fr",
        "markets": "h2h,totals",
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
    imp_a = 1 / price_a
    imp_b = 1 / price_b
    total = imp_a + imp_b
    return imp_a / total, imp_b / total


DETAIL_THRESHOLD = 0.02


def format_detail_explanation(model: dict) -> str:
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
        ("le platoon (main du titulaire vs ligne adverse)", model["platoon_adj"]),
        ("le style de jeu de la ligne adverse", model["playstyle_adj"]),
        ("l'historique des batteurs face à ce titulaire", model["batters_faced_adj"]),
        ("le repos", model["rest_adj"]),
        ("l'historique (H2H)", model["h2h_adj"]),
    ]
    significant = [(label, val) for label, val in components if abs(val) >= DETAIL_THRESHOLD]
    base_sentence = base_sentence[0].upper() + base_sentence[1:]

    if not significant:
        return f"{base_sentence}. Aucun facteur du soir ne change vraiment la donne — tous les ajustements sont mineurs."

    significant.sort(key=lambda x: abs(x[1]), reverse=True)
    parts = []
    for label, val in significant[:2]:
        direction = home_team if val > 0 else away_team
        parts.append(f"{label} favorise {direction} ({val*100:+.1f} pts)")

    return f"{base_sentence}. Ce soir, {' et '.join(parts)}."


def compute_fair_odds(prob: float | None) -> float | None:
    if not prob or prob <= 0:
        return None
    return 1 / prob


def compute_ev_pct(prob: float | None, price: float | None) -> float | None:
    if not prob or not price:
        return None
    return (prob * price - 1) * 100


def format_selection_block(model: dict, value: dict, units: float, reasons: list[str]) -> str:
    edge = value.get("best_edge")
    stars = confidence_stars(compute_confidence_stars_score(model, value.get("best_side") or "home"))

    if edge is None or edge < VALUE_TIER_MODEREE:
        return f"⚪ Aucune value nette détectée ce soir sur ce match. Confiance : {stars}\n"

    home_team = value["home_team"]
    away_team = value["away_team"]
    side = value["best_side"]
    team = home_team if side == "home" else away_team
    prob = model["p_home"] if side == "home" else 1 - model["p_home"]
    price = value["playable_home_price"] if side == "home" else value["playable_away_price"]
    fair_odds = compute_fair_odds(prob)

    lines = []
    if fair_odds is not None:
        if price:
            ev_pct = compute_ev_pct(prob, price)
            lines.append(
                f"📐 Cote juste (modèle) : @{fair_odds:.2f} -- value dès que la cote dépasse ce seuil "
                f"(cote réelle @{price}, EV {ev_pct:+.1f}%)\n"
            )
            if ev_pct < MIN_REAL_EV_PCT:
                lines.append(
                    f"🔵 <i>Value confirmée, mais écart modeste avec la cote réellement jouable "
                    f"(EV au prix réel {ev_pct:+.1f}%, surtout sensible sur une cote courte) -- la mise "
                    f"ci-dessous reste basée sur la confiance du modèle, pas sur ce prix. Peut valoir le "
                    f"coup en combiné même si l'intérêt est plus limité en simple.</i>\n"
                )
        else:
            lines.append(f"📐 Cote juste (modèle) : @{fair_odds:.2f} (cote réelle indisponible pour ce match)\n")

    marker = "🟠" if units <= 0.5 else "🟢"
    eur = units * EUR_PER_UNIT
    lines.append(f"{marker} <b>Sélection : {team}</b> — {units}u ({eur:.2f}€)\n")
    lines.append(f"<i>{'; '.join(reasons)}</i>\n")
    lines.append(f"Confiance (indépendante de l'edge) : {stars}\n")

    return "".join(lines)


PLAYABLE_BOOKS_PRIORITY = ["betclic", "unibet"]


def evaluate_value(model: dict, odds_events: list[dict]) -> dict:
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


MIN_REAL_EV_PCT = 2.0


def stake_units(value: dict, model: dict) -> tuple[float, list[str]]:
    edge = value["best_edge"]
    if edge is None or edge < VALUE_TIER_MODEREE:
        return 0.0, ["pas de value nette"]

    reasons = []

    if edge > VALUE_TIER_SUSPECT:
        reasons.append("edge très large vs Pinnacle seul -- suspect, mise plafonnée par précaution")
        return 0.5, reasons

    confidence = compute_confidence_score(model)

    if confidence < CONFIDENCE_SCORE_MIN:
        reasons.append(
            "signal combiné (titulaire + reste du modèle) quasi neutre -- l'edge vient surtout d'ailleurs, "
            "zone historiquement la moins fiable"
        )
        return 0.5, reasons

    contributors = count_meaningful_contributors(model, value["best_side"])

    if confidence >= CONFIDENCE_SCORE_STRONG and edge >= VALUE_TIER_MODEREE and contributors >= MULTI_FACTOR_MIN_CONTRIBUTORS:
        units = 2.0
        reasons.append(
            f"signal combiné marqué, confirmé par {contributors} facteurs indépendants -- "
            f"la zone la plus rentable observée"
        )
    elif confidence >= CONFIDENCE_SCORE_STRONG:
        units = 1.0
        reasons.append(
            "score fort mais porté par un seul facteur -- mise prudente en attendant confirmation "
            "d'un second (pas de 2u sur un facteur isolé, même solide)"
        )
    else:
        units = 1.0
        reasons.append("edge confirmé par un signal combiné réel, sans être extrême")

    if value.get("books_agree"):
        book_name = (value.get("playable_book") or "le book jouable").capitalize()
        reasons.append(f"{book_name} et Pinnacle d'accord entre eux (info seulement, ne change plus la mise)")

    return min(units, MAX_UNITS_PER_PICK), reasons


def compute_model_probability(game: dict, split_records: dict) -> dict:
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

    home_road_adj = 0.0
    try:
        home_split = get_pitcher_split_stat(home_pitcher_id, CURRENT_SEASON, SIT_CODE_HOME) if home_pitcher_id else None
        away_split = get_pitcher_split_stat(away_pitcher_id, CURRENT_SEASON, SIT_CODE_ROAD) if away_pitcher_id else None
        home_road_adj = split_era_edge(home_split, home_era, HOME_ROAD_SENSITIVITY) - split_era_edge(
            away_split, away_era, HOME_ROAD_SENSITIVITY
        )
    except Exception as exc:
        print(f"  (split domicile/extérieur indisponible : {exc})")

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
        playstyle_adj = playstyle_edge(away_offense) - playstyle_edge(home_offense)
    except Exception as exc:
        print(f"  (platoon split / style de jeu indisponible : {exc})")

    batters_faced_adj = 0.0
    try:
        home_batter_ids_bvp = _extract_lineup_player_ids(game, "home")
        away_batter_ids_bvp = _extract_lineup_player_ids(game, "away")
        home_bvp_edge = batters_vs_pitcher_edge(away_batter_ids_bvp, home_pitcher_id, CURRENT_SEASON, LEAGUE_AVG_OPS)
        away_bvp_edge = batters_vs_pitcher_edge(home_batter_ids_bvp, away_pitcher_id, CURRENT_SEASON, LEAGUE_AVG_OPS)
        batters_faced_adj = -home_bvp_edge + away_bvp_edge
    except Exception as exc:
        print(f"  (historique batteurs vs titulaire indisponible : {exc})")

    daynight_adj = 0.0
    vs_team_adj = 0.0
    bullpen_adj = 0.0

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

    h2h = get_h2h_record(home_id, away_id, game_date)
    h2h_adj = h2h_edge(h2h)

    p_home = (
        baseline + form_adj + pitcher_quality_adj + adversity_adj + arsenal_adj
        + home_road_adj + daynight_adj + platoon_adj + vs_team_adj
        + playstyle_adj + batters_faced_adj
        + rest_adj + bullpen_adj + h2h_adj
    )
    p_home = min(max(p_home, 0.03), 0.97)

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
    combined = model["adversity_adj"] + model["arsenal_adj"]
    if abs(combined) < OFFENSE_CONTEXT_THRESHOLD:
        return "⚔️ Attaques : rien de spécial à signaler ce soir."
    favored = model["home_name"] if combined > 0 else model["away_name"]
    return f"⚔️ Attaques : l'avantage penche pour {favored} face à l'arsenal adverse ({combined*100:+.1f} pts)."


def format_weather_line(model: dict) -> str:
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
        lines.append("💰 Sélection : impossible à calculer (pas de prix Pinnacle pour ce match)\n")
        return "\n".join(lines)

    lines.append(
        f"📈 Marché (Pinnacle, dévigoré) : {home_team} {value['market_home']*100:.1f}% | "
        f"{away_team} {value['market_away']*100:.1f}%\n"
    )

    units, reasons = stake_units(value, model)
    lines.append(format_selection_block(model, value, units, reasons))

    return "\n".join(lines)


def _short_tier_label(reasons: list[str]) -> str:
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


def build_recap_entry(model: dict, value: dict, units: float, reasons: list[str]) -> dict | None:
    side = value.get("best_side")
    edge = value.get("best_edge")
    if side is None or edge is None:
        return None

    team = value["home_team"] if side == "home" else value["away_team"]
    model_prob = model["p_home"] if side == "home" else 1 - model["p_home"]
    pinnacle_prob = model_prob - edge

    return {
        "team": team,
        "edge": edge,
        "units": units,
        "fair_odds": compute_fair_odds(model_prob),
        "pinnacle_odds": compute_fair_odds(pinnacle_prob),
        "tier_label": _short_tier_label(reasons),
        "stars": confidence_stars(compute_confidence_stars_score(model, side)),
    }


def format_full_recap(entries: list[dict]) -> str:
    if not entries:
        return "📋 <b>Récap complet</b>\n\nAucun match avec cotes Pinnacle disponibles ce soir.\n"

    tiers: dict[float, list[dict]] = {2.0: [], 1.0: [], 0.5: [], 0.0: []}
    for e in entries:
        tiers.setdefault(e["units"], []).append(e)

    counts = (
        f"{len(tiers[2.0])} en 2u, {len(tiers[1.0])} en 1u, "
        f"{len(tiers[0.5])} en 0.5u, {len(tiers[0.0])} sans sélection"
    )
    lines = [f"📋 <b>Récap complet</b>\n", f"<i>{len(entries)} match(s) — {counts}</i>\n"]

    def render_tier(label: str, marker: str, group: list[dict], show_edge_zero_note: bool = False) -> None:
        if not group:
            return
        lines.append(f"\n{marker} <b>{label}</b>\n")
        for e in sorted(group, key=lambda x: -x["edge"]):
            tag = f" ({e['tier_label']})" if e["tier_label"] else ""
            lines.append(
                f"{marker} {e['team']}{tag} — {e['edge']*100:.1f} pts · "
                f"modèle @{e['fair_odds']:.2f} vs Pinnacle @{e['pinnacle_odds']:.2f} · {e['stars']}\n"
            )

    render_tier("CONFIANCE FORTE (2u)", "🟢", tiers[2.0])
    render_tier("CONFIANCE MODÉRÉE (1u)", "🔵", tiers[1.0])
    render_tier("MÉFIANCE (0.5u)", "🟠", tiers[0.5])
    render_tier("AUCUNE SÉLECTION (edge < 3 pts)", "⚪", tiers[0.0])

    total_units = sum(e["units"] for e in entries)
    lines.append(
        f"\nTotal engagé ce soir : <b>{total_units}u</b> ({units_to_eur_display(total_units)}) -- "
        f"1u = 1% de la bankroll de suivi, départ 100\n"
    )
    lines.append("⚠️ Dimensionné par fiabilité perçue du signal, pas par edge brut. Le modèle reste non validé -- à toi de juger.\n")
    return "\n".join(lines)


def chunk_message(text: str, limit: int = 3800) -> list[str]:
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


def save_predictions_log(date_str: str, log_entries: list[dict], collection_errors: list[dict]) -> None:
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
    matchs_deja_commences = 0
    recap_entries = []
    log_entries = []
    collection_errors = []

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    for game in games:
        home = game["teams"]["home"]["team"]["name"]
        away = game["teams"]["away"]["team"]["name"]

        try:
            game_start = datetime.datetime.fromisoformat(game.get("gameDate", "").replace("Z", "+00:00"))
            if game_start <= now_utc:
                matchs_deja_commences += 1
                continue
        except Exception:
            pass

        try:
            model = compute_model_probability(game, split_records)
            value = evaluate_value(model, odds_events)

            value_text = format_value_block(model, value)

            # Section Total -- entièrement séparée, n'affecte jamais les
            # étoiles, la sélection moneyline, ou sa mise. Valeurs par
            # défaut définies AVANT le try (corrigé le 19 août) : sans ça,
            # si le bloc échouait, ces variables n'existaient pas du tout
            # au moment de construire log_entry plus bas.
            totals_value: dict = {}
            totals_units, totals_reason = 0.0, "indisponible (erreur de calcul)"
            try:
                home_id = game["teams"]["home"]["team"]["id"]
                away_id = game["teams"]["away"]["team"]["id"]
                game_date = game.get("gameDate", "")[:10]
                home_pitcher_id = game["teams"]["home"].get("probablePitcher", {}).get("id")
                away_pitcher_id = game["teams"]["away"].get("probablePitcher", {}).get("id")

                home_offense = get_team_season_offense(home_id, CURRENT_SEASON)
                away_offense = get_team_season_offense(away_id, CURRENT_SEASON)
                home_runs_pg = home_offense.get("runs_per_game") if home_offense else None
                away_runs_pg = away_offense.get("runs_per_game") if away_offense else None

                stadium = get_stadium_info(model["home_name"])
                park_tier = stadium["park_factor_tier"] if stadium else 0

                h2h_runs = get_h2h_runs_and_same_starters(
                    home_id, away_id, game_date, home_pitcher_id, away_pitcher_id
                )

                expected_total = compute_expected_total(
                    home_runs_pg, away_runs_pg, model.get("home_era"), model.get("away_era"),
                    park_tier, model.get("weather"), h2h_runs,
                )
                totals_odds = find_totals_odds(odds_events, model["home_name"], model["away_name"])
                totals_value = evaluate_totals_value(expected_total, totals_odds)
                totals_units, totals_reason = stake_totals_units(totals_value)

                value_text += "\n" + format_totals_block(totals_value, totals_units, totals_reason)
            except Exception as exc:
                print(f"  (section Total indisponible pour ce match : {exc})")

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
                # Total (over/under) -- ENTIÈREMENT séparé du moneyline
                # ci-dessus, réglé et suivi indépendamment par debrief.py
                # (bankroll_totals.json, jamais mélangée à bankroll.json).
                "totals": {
                    "line": totals_value.get("line"),
                    "side": totals_value.get("best_side"),
                    "units": totals_units,
                    "price": totals_value.get("playable_price"),
                    "reason": totals_reason,
                    "expected_total": totals_value.get("expected_total"),
                },
            }

            units, reasons = stake_units(value, model)

            recap_entry = build_recap_entry(model, value, units, reasons)
            if recap_entry is not None:
                recap_entries.append(recap_entry)

            if units > 0:
                side = value["best_side"]
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

            log_entries.append(log_entry)
        except Exception as exc:
            print(f"  ⚠️ Match ignoré ({away} @ {home}) après erreur : {exc}")
            collection_errors.append({"matchup": f"{away} @ {home}", "reason": str(exc)})
            matchs_echoues += 1
        time.sleep(0.3)

    save_predictions_log(date_str, log_entries, collection_errors)

    try:
        summary_text = format_full_recap(recap_entries)
        for chunk in chunk_message(summary_text):
            send_message(chunk)
    except Exception as exc:
        print(f"⚠️ Envoi du récap échoué (les données du soir sont déjà sauvegardées, rien n'est perdu) : {exc}")

    print(
        f"{matchs_envoyes} verdict(s) envoyé(s) sur Telegram, {matchs_echoues} échoué(s), "
        f"{matchs_deja_commences} déjà commencé(s) ignoré(s), {len(recap_entries)} match(s) dans le récap."
    )


if __name__ == "__main__":
    main()
