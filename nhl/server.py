import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent))
import stats as shared  # noqa: E402

DB_PATH = HERE / "nhl.sqlite"

# Era cutoffs: the API (and the boxscore fallback especially) reports 0 for
# stats that simply weren't tracked yet, which would flood the cube with fake
# statlines. First seasons with real (nonzero) data, measured from the db:
#   sog / plus_minus 1959-60 · shifts / EN goals / goalie splits 1997-98
#   hits / blocked / giveaways / takeaways / missed shots 2005-06
# CASE them to NULL below the cutoff. Inside these subqueries `season` on the
# right-hand side is the raw source integer (20232024); the aliased output
# `season` is the display string ("2023-24") used by the season dumps.
SEASON_STR = "(season/10000) || '-' || printf('%02d', season % 100)"

SK_TABLE = f"""(SELECT game_id, game_date, {SEASON_STR} AS season, game_type,
    player_id, player_name, position, team_abbr, matchup,
    goals, assists, points, pim, ppg, pp_points, sh_goals, gw_goals, ot_goals,
    CASE WHEN season >= 19591960 THEN plus_minus END AS plus_minus,
    CASE WHEN season >= 19591960 THEN sog END AS sog,
    CASE WHEN season >= 20052006 THEN missed_shots END AS missed_shots,
    CASE WHEN season >= 20052006 THEN shot_attempts END AS shot_attempts,
    CASE WHEN season >= 20052006 THEN hits END AS hits,
    CASE WHEN season >= 20052006 THEN blocked END AS blocked,
    CASE WHEN season >= 20052006 THEN takeaways END AS takeaways,
    CASE WHEN season >= 20052006 THEN giveaways END AS giveaways,
    CASE WHEN season >= 19971998 THEN en_goals END AS en_goals,
    CASE WHEN season >= 19971998 THEN shifts END AS shifts,
    CAST(ROUND(toi_sec / 60.0) AS INT) AS toi_min
    FROM skater_games)"""

GL_TABLE = f"""(SELECT game_id, game_date, {SEASON_STR} AS season, game_type,
    player_id, player_name, position, team_abbr, matchup,
    saves, shots_against, goals_against, shutouts, goals, assists, points, pim,
    CASE WHEN season >= 19971998 THEN es_saves END AS es_saves,
    CASE WHEN season >= 19971998 THEN pp_saves END AS pp_saves,
    CASE WHEN season >= 19971998 THEN sh_saves END AS sh_saves,
    CAST(ROUND(save_pct * 100) AS INT) AS save_pct,
    CAST(ROUND(toi_sec / 60.0) AS INT) AS toi_min
    FROM goalie_games)"""

SKATERS = {
    "goals":         {"col": "goals",         "label": "Goals",         "color": "#ff6b6b"},
    "assists":       {"col": "assists",       "label": "Assists",       "color": "#6b9eff"},
    "points":        {"col": "points",        "label": "Points",        "color": "#f7c948"},
    "plus_minus":    {"col": "plus_minus",    "label": "+/-",           "color": "#bdbdbd"},
    "pim":           {"col": "pim",           "label": "PIM",           "color": "#ff9f43"},
    "sog":           {"col": "sog",           "label": "Shots",         "color": "#ff5fa0"},
    "missed_shots":  {"col": "missed_shots",  "label": "Missed Shots",  "color": "#ff8fbf"},
    "shot_attempts": {"col": "shot_attempts", "label": "Shot Attempts", "color": "#60a5fa"},
    "hits":          {"col": "hits",          "label": "Hits",          "color": "#a07bff"},
    "blocked":       {"col": "blocked",       "label": "Blocked Shots", "color": "#8ee27a"},
    "ppg":           {"col": "ppg",           "label": "PP Goals",      "color": "#5fd5ff"},
    "pp_points":     {"col": "pp_points",     "label": "PP Points",     "color": "#9fe6ff"},
    "sh_goals":      {"col": "sh_goals",      "label": "SH Goals",      "color": "#4dd0e1"},
    "gw_goals":      {"col": "gw_goals",      "label": "GW Goals",      "color": "#ffd97a"},
    "ot_goals":      {"col": "ot_goals",      "label": "OT Goals",      "color": "#e879f9"},
    "en_goals":      {"col": "en_goals",      "label": "EN Goals",      "color": "#34d399"},
    "takeaways":     {"col": "takeaways",     "label": "Takeaways",     "color": "#6bd06b"},
    "giveaways":     {"col": "giveaways",     "label": "Giveaways",     "color": "#ff8e8e"},
    "shifts":        {"col": "shifts",        "label": "Shifts",        "color": "#c39bff"},
    "toi_min":       {"col": "toi_min",       "label": "TOI (min)",     "color": "#9ade8c"},
}

GOALIES = {
    "saves":         {"col": "saves",         "label": "Saves",         "color": "#6bd06b"},
    "shots_against": {"col": "shots_against", "label": "Shots Against", "color": "#6b9eff"},
    "goals_against": {"col": "goals_against", "label": "Goals Against", "color": "#ff6b6b"},
    "es_saves":      {"col": "es_saves",      "label": "EV Saves",      "color": "#8ee27a"},
    "pp_saves":      {"col": "pp_saves",      "label": "PP Saves",      "color": "#5fd5ff"},
    "sh_saves":      {"col": "sh_saves",      "label": "SH Saves",      "color": "#4dd0e1"},
    # Season SV% is recomputed from summed saves/shots — summing the per-game
    # scaled ints is meaningless and averaging them over-weights short outings.
    "save_pct":      {"col": "save_pct",      "label": "SV%",           "color": "#f7c948",
                      "scale": 100, "decimals": 2,
                      "season_agg": "CAST(ROUND(100.0 * SUM(saves) / "
                                    "NULLIF(SUM(shots_against), 0)) AS INT)"},
    "shutouts":      {"col": "shutouts",      "label": "Shutouts",      "color": "#bdbdbd"},
    "goals":         {"col": "goals",         "label": "Goals",         "color": "#ff5fa0"},
    "assists":       {"col": "assists",       "label": "Assists",       "color": "#9fc2ff"},
    "points":        {"col": "points",        "label": "Points",        "color": "#ffd97a"},
    "pim":           {"col": "pim",           "label": "PIM",           "color": "#ff9f43"},
    "toi_min":       {"col": "toi_min",       "label": "TOI (min)",     "color": "#a07bff"},
}

SK_SANITY = shared.sanity_filter([
    "points <> goals + assists",
    "sog < goals",
    "ppg > goals",
    "sh_goals > goals",
    "gw_goals > goals",
    "ot_goals > goals",
    "en_goals > goals",
    "pp_points > points",
    "sog + missed_shots > shot_attempts",
] + [f"{c} < 0" for c in SKATERS if c != "plus_minus"])

GL_SANITY = shared.sanity_filter([
    "saves > shots_against",
    "shots_against - saves <> goals_against",
    "es_saves > saves",
    "shutouts > 0 AND goals_against > 0",
] + [f"{c} < 0" for c in GOALIES])

GROUPS = {
    "sk": {"label": "Skaters", "stats": SKATERS, "sanity": SK_SANITY,
           "table": SK_TABLE, "matchup": "matchup",
           "defaults": ("goals", "assists", "sog")},
    "g":  {"label": "Goalies", "stats": GOALIES, "sanity": GL_SANITY,
           "table": GL_TABLE, "matchup": "matchup",
           "defaults": ("saves", "goals_against", "toi_min")},
}

STATS = SKATERS
DEFAULTS = ("goals", "assists", "sog")


def open_db():
    return shared.open_db(DB_PATH)


@lru_cache(maxsize=64)
def compute_payload(x, y, z, group="sk") -> str:
    g = GROUPS[group]
    return shared.compute_payload(DB_PATH, g["stats"], g["sanity"], x, y, z,
                                  table=g["table"], matchup=g["matchup"])


if __name__ == "__main__":
    shared.serve("nhl", HERE, DB_PATH, STATS, DEFAULTS, 8767, compute_payload)
