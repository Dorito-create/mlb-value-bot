"""
Ajustement fin de l'adversité : au lieu de comparer le titulaire à l'OPS
global de la ligne adverse, on croise SON arsenal précis de lancers
(fastball, slider, curve, etc. et leur fréquence d'usage) avec la
performance de CHAQUE batteur du line-up réel contre chacun de ces types
de lancers. Beaucoup plus fin qu'une moyenne OPS de saison.

Source : Baseball Savant (baseballsavant.mlb.com), via pybaseball -- un
domaine DIFFÉRENT de FanGraphs (qui bloque les accès automatisés depuis
mi-2025, voir get_matchup_stats.py). Ces fonctions n'ont pas pu être
testées en conditions réelles pendant l'écriture : si le nom d'une colonne
ne correspond pas à ce que Baseball Savant renvoie vraiment, la fonction
concernée retombe sur "aucune donnée" (0.0, pas de crash) -- dis-le si tu
vois l'ajustement "arsenal" rester à 0.0 sur tous les matchs, on corrigera
les noms de colonnes ensemble à partir des diagnostics affichés au premier
chargement.

Ne s'applique QUE si le line-up réel est publié (on a besoin des 9 vrais
batteurs, pas d'une moyenne d'équipe) -- sinon l'ajustement reste à 0.0 et
le modèle retombe sur l'adversité OPS classique (get_value_bets.py).
"""

import pandas as pd

try:
    from pybaseball import statcast_pitcher_pitch_arsenal, statcast_batter_pitch_arsenal
    PYBASEBALL_AVAILABLE = True
except ImportError:
    PYBASEBALL_AVAILABLE = False

LEAGUE_AVG_WOBA = 0.310  # approximation courante, à ajuster si besoin
ARSENAL_SENSITIVITY = 0.15  # heuristique de départ, à ajuster via le débrief

# Types de lancers les plus courants dans la nomenclature Statcast.
PITCH_TYPES = ["ff", "si", "fc", "sl", "cu", "ch", "fs", "st", "sv"]

_cache: dict = {"pitcher_arsenal": None, "batter_arsenal": None, "season": None}


def _find_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    """Cherche la première colonne existante parmi plusieurs noms possibles
    (les noms exacts renvoyés par Baseball Savant n'ont pas pu être vérifiés
    à l'avance).
    """
    for candidate in candidates:
        if candidate in df.columns:
            return candidate
    return None


def load_arsenal_data(season: int) -> tuple[pd.DataFrame | None, pd.DataFrame | None]:
    """Charge une fois par exécution l'arsenal de tous les lanceurs et les
    stats de tous les batteurs par type de lancer, pour la saison donnée.
    """
    if not PYBASEBALL_AVAILABLE:
        print("  (pybaseball non installé -- ajustement arsenal désactivé)")
        return None, None

    if _cache["season"] == season and _cache["pitcher_arsenal"] is not None:
        return _cache["pitcher_arsenal"], _cache["batter_arsenal"]

    pitcher_arsenal = None
    batter_arsenal = None

    try:
        print("  Chargement de l'arsenal des lanceurs (Baseball Savant)...")
        pitcher_arsenal = statcast_pitcher_pitch_arsenal(season, minP=50, arsenal_type="n_")
        print(f"    -> {len(pitcher_arsenal)} lanceur(s), colonnes : {list(pitcher_arsenal.columns)[:12]}...")
    except Exception as exc:
        print(f"  ⚠️ Arsenal lanceurs indisponible ({exc}) -- ajustement désactivé pour cette exécution")

    try:
        print("  Chargement des stats batteurs par type de lancer (Baseball Savant)...")
        batter_arsenal = statcast_batter_pitch_arsenal(season, minPA=10)
        print(f"    -> {len(batter_arsenal)} ligne(s), colonnes : {list(batter_arsenal.columns)[:12]}...")
    except Exception as exc:
        print(f"  ⚠️ Stats batteurs/lancer indisponibles ({exc}) -- ajustement désactivé pour cette exécution")

    _cache.update({"pitcher_arsenal": pitcher_arsenal, "batter_arsenal": batter_arsenal, "season": season})
    return pitcher_arsenal, batter_arsenal


def get_pitcher_arsenal_mix(pitcher_arsenal_df: pd.DataFrame, pitcher_id: int) -> dict:
    """{type_de_lancer: % d'usage} pour un lanceur donné."""
    if pitcher_arsenal_df is None:
        return {}

    id_col = _find_column(pitcher_arsenal_df, ["player_id", "pitcher_id", "pitcher", "mlbam_id"])
    if id_col is None:
        return {}

    row = pitcher_arsenal_df[pitcher_arsenal_df[id_col] == pitcher_id]
    if row.empty:
        return {}

    mix = {}
    for pt in PITCH_TYPES:
        col = _find_column(row, [f"n_{pt}", f"n_{pt}%", pt])
        if col is None:
            continue
        value = row.iloc[0][col]
        if pd.notna(value) and value > 0:
            mix[pt] = value / 100 if value > 1 else value  # gère % (ex: 35) vs fraction (0.35)
    return mix


def get_batter_woba_vs_pitch(batter_arsenal_df: pd.DataFrame, batter_id: int, pitch_type: str) -> float | None:
    """wOBA d'un batteur contre un type de lancer précis."""
    if batter_arsenal_df is None:
        return None

    id_col = _find_column(batter_arsenal_df, ["player_id", "batter_id", "batter", "mlbam_id"])
    pt_col = _find_column(batter_arsenal_df, ["pitch_type", "pitch_name"])
    woba_col = _find_column(batter_arsenal_df, ["woba", "est_woba", "wOBA"])
    if id_col is None or pt_col is None or woba_col is None:
        return None

    match = batter_arsenal_df[
        (batter_arsenal_df[id_col] == batter_id)
        & (batter_arsenal_df[pt_col].astype(str).str.upper() == pitch_type.upper())
    ]
    if match.empty:
        return None
    value = match.iloc[0][woba_col]
    return float(value) if pd.notna(value) else None


def expected_woba_for_lineup(
    pitcher_arsenal_df: pd.DataFrame, batter_arsenal_df: pd.DataFrame, pitcher_id: int, batter_ids: list[int]
) -> float | None:
    """wOBA attendu de la ligne adverse contre l'arsenal précis de ce
    titulaire, en pondérant chaque type de lancer par sa fréquence d'usage.
    """
    mix = get_pitcher_arsenal_mix(pitcher_arsenal_df, pitcher_id)
    if not mix:
        return None

    batter_expected = []
    for batter_id in batter_ids:
        total_weight = 0.0
        weighted_woba = 0.0
        for pitch_type, usage in mix.items():
            woba = get_batter_woba_vs_pitch(batter_arsenal_df, batter_id, pitch_type)
            if woba is not None:
                weighted_woba += usage * woba
                total_weight += usage
        if total_weight > 0:
            batter_expected.append(weighted_woba / total_weight)

    if not batter_expected:
        return None
    return sum(batter_expected) / len(batter_expected)


def arsenal_adjustment(
    pitcher_arsenal_df: pd.DataFrame,
    batter_arsenal_df: pd.DataFrame,
    pitcher_id: int | None,
    batter_ids: list[int],
) -> float:
    """Ajustement de proba basé sur l'arsenal -- 0.0 si line-up manquant,
    lanceur non identifié, ou données Savant indisponibles/incompatibles.
    """
    if pitcher_id is None or not batter_ids:
        return 0.0

    expected_woba = expected_woba_for_lineup(pitcher_arsenal_df, batter_arsenal_df, pitcher_id, batter_ids)
    if expected_woba is None:
        return 0.0

    return (LEAGUE_AVG_WOBA - expected_woba) * ARSENAL_SENSITIVITY
