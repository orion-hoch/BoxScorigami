import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent))
import stats as shared  # noqa: E402

DB_PATH = HERE / "nfl_full.sqlite"

OFFENSE = {
    "pass_cmp": {"col": "pass_cmp", "label": "Pass Cmp",      "color": "#ff6b6b"},
    "pass_att": {"col": "pass_att", "label": "Pass Att",      "color": "#ff8e8e"},
    "pass_yds": {"col": "pass_yds", "label": "Pass Yds",      "color": "#ff5fa0"},
    "pass_td":  {"col": "pass_td",  "label": "Pass TD",       "color": "#f7c948"},
    "pass_int": {"col": "pass_int", "label": "INT Thrown",    "color": "#bdbdbd"},
    "sacks":    {"col": "sacks",    "label": "Sacks Taken",   "color": "#a07bff"},
    "rush_att": {"col": "rush_att", "label": "Rush Att",      "color": "#6bd06b"},
    "rush_yds": {"col": "rush_yds", "label": "Rush Yds",      "color": "#43a047"},
    "rush_td":  {"col": "rush_td",  "label": "Rush TD",       "color": "#8ee27a"},
    "rec":      {"col": "rec",      "label": "Receptions",    "color": "#6b9eff"},
    "tgt":      {"col": "tgt",      "label": "Targets",       "color": "#9fc2ff"},
    "rec_yds":  {"col": "rec_yds",  "label": "Rec Yds",       "color": "#5fd5ff"},
    "rec_td":   {"col": "rec_td",   "label": "Rec TD",        "color": "#4dd0e1"},
}

DEFENSE = {
    "tackles_combined": {"col": "tackles_combined", "label": "Tackles",  "color": "#ff6b6b"},
    "tackles_solo":     {"col": "tackles_solo",     "label": "Solo Tk",  "color": "#ff8e8e"},
    "tackles_assists":  {"col": "tackles_assists",  "label": "Ast Tk",   "color": "#ffb3b3"},
    "sacks":            {"col": "CAST(sacks * 2 AS INT)",
                         "label": "Sacks", "color": "#a07bff",
                         "scale": 2, "decimals": 1},
    "qb_hits":          {"col": "qb_hits",          "label": "QB Hits",  "color": "#c39bff"},
    "tackles_loss":     {"col": "tackles_loss",     "label": "TFL",      "color": "#8ee27a"},
    "def_int":          {"col": "def_int",          "label": "INT",      "color": "#f7c948"},
    "pass_defended":    {"col": "pass_defended",    "label": "Pass Def", "color": "#5fd5ff"},
    "fumbles_forced":   {"col": "fumbles_forced",   "label": "FF",       "color": "#6bd06b"},
    "def_int_td":       {"col": "def_int_td",       "label": "INT TD",   "color": "#ff5fa0"},
    "fumbles_rec_td":   {"col": "fumbles_rec_td",   "label": "FR TD",    "color": "#ff9f43"},
}

SPECIAL = {
    "kick_ret":      {"col": "kick_ret",      "label": "KR",       "color": "#ff6b6b"},
    "kick_ret_yds":  {"col": "kick_ret_yds",  "label": "KR Yds",   "color": "#ff8e8e"},
    "kick_ret_td":   {"col": "kick_ret_td",   "label": "KR TD",    "color": "#ff5fa0"},
    "punt_ret":      {"col": "punt_ret",      "label": "PR",       "color": "#6b9eff"},
    "punt_ret_yds":  {"col": "punt_ret_yds",  "label": "PR Yds",   "color": "#5fd5ff"},
    "punt_ret_td":   {"col": "punt_ret_td",   "label": "PR TD",    "color": "#4dd0e1"},
    "fgm":           {"col": "fgm",           "label": "FG Made",  "color": "#f7c948"},
    "fga":           {"col": "fga",           "label": "FG Att",   "color": "#ffd97a"},
    "xpm":           {"col": "xpm",           "label": "XP Made",  "color": "#8ee27a"},
    "punt":          {"col": "punt",          "label": "Punts",    "color": "#a07bff"},
    "punt_yds":      {"col": "punt_yds",      "label": "Punt Yds", "color": "#c39bff"},
}

SPECIAL_TABLE = """(
    SELECT ids.game_id, ids.player_pfr_id,
           COALESCE(r.game_date, k.game_date)     AS game_date,
           COALESCE(r.season, k.season)           AS season,
           COALESCE(r.game_type, k.game_type)     AS game_type,
           COALESCE(r.player_name, k.player_name) AS player_name,
           COALESCE(r.team_abbr, k.team_abbr)     AS team_abbr,
           IFNULL(r.kick_ret, 0)     AS kick_ret,
           IFNULL(r.kick_ret_yds, 0) AS kick_ret_yds,
           IFNULL(r.kick_ret_td, 0)  AS kick_ret_td,
           IFNULL(r.punt_ret, 0)     AS punt_ret,
           IFNULL(r.punt_ret_yds, 0) AS punt_ret_yds,
           IFNULL(r.punt_ret_td, 0)  AS punt_ret_td,
           IFNULL(k.fgm, 0)          AS fgm,
           IFNULL(k.fga, 0)          AS fga,
           IFNULL(k.xpm, 0)          AS xpm,
           IFNULL(k.punt, 0)         AS punt,
           IFNULL(k.punt_yds, 0)     AS punt_yds
    FROM (SELECT game_id, player_pfr_id FROM returns
          UNION
          SELECT game_id, player_pfr_id FROM kicking) ids
    LEFT JOIN returns r
           ON r.game_id = ids.game_id AND r.player_pfr_id = ids.player_pfr_id
    LEFT JOIN kicking k
           ON k.game_id = ids.game_id AND k.player_pfr_id = ids.player_pfr_id
)"""

SANITY_BAD = [
    "pass_cmp > pass_att",
    "pass_cmp + pass_int > pass_att",
    "pass_td > pass_cmp",
    "pass_cmp = 0 AND pass_yds <> 0",
    "rush_td > rush_att",
    "rush_att = 0 AND rush_yds <> 0",
    "rec > tgt",
    "rec_td > rec",
    "rec = 0 AND rec_yds <> 0",
] + [f"{c} < 0" for c in ("pass_cmp", "pass_att", "pass_td", "pass_int",
                          "sacks", "rush_att", "rush_td", "rec", "tgt", "rec_td")]
SANITY_FILTER = shared.sanity_filter(SANITY_BAD)

DEF_SANITY = shared.sanity_filter([
    "tackles_solo > tackles_combined",
    "tackles_assists > tackles_combined",
    "def_int_td > def_int",
] + [f"{c} < 0" for c in DEFENSE if c != "sacks"] + ["sacks < 0"])

ST_SANITY = shared.sanity_filter([
    "kick_ret_td > kick_ret",
    "punt_ret_td > punt_ret",
    "fgm > fga",
    "kick_ret = 0 AND kick_ret_yds <> 0",
    "punt_ret = 0 AND punt_ret_yds <> 0",
    "punt = 0 AND punt_yds <> 0",
] + [f"{c} < 0" for c in ("kick_ret", "kick_ret_td", "punt_ret", "punt_ret_td",
                          "fgm", "fga", "xpm", "punt")])

MATCHUP_LOOKUP = """(SELECT p.matchup FROM player_games p
     WHERE p.game_id = t.game_id AND p.team_abbr = t.team_abbr LIMIT 1)"""

GROUPS = {
    "off": {"label": "Offense", "stats": OFFENSE, "sanity": SANITY_FILTER,
            "table": "player_games", "matchup": "matchup",
            "defaults": ("rec_yds", "rec", "rec_td")},
    "def": {"label": "Defense", "stats": DEFENSE, "sanity": DEF_SANITY,
            "table": "player_defense", "matchup": MATCHUP_LOOKUP,
            "defaults": ("tackles_combined", "sacks", "def_int")},
    "st":  {"label": "Special Teams", "stats": SPECIAL, "sanity": ST_SANITY,
            "table": SPECIAL_TABLE, "matchup": MATCHUP_LOOKUP,
            "defaults": ("kick_ret_yds", "punt_ret_yds", "fgm")},
}

STATS = OFFENSE
DEFAULTS = ("pass_yds", "pass_td", "rush_yds")


def open_db():
    return shared.open_db(DB_PATH)


@lru_cache(maxsize=64)
def compute_payload(x, y, z, group="off") -> str:
    g = GROUPS[group]
    return shared.compute_payload(DB_PATH, g["stats"], g["sanity"], x, y, z,
                                  pid_col="player_pfr_id",
                                  legacy_max=True,
                                  table=g["table"], matchup=g["matchup"])


if __name__ == "__main__":
    shared.serve("nfl", HERE, DB_PATH, STATS, DEFAULTS, 8766, compute_payload)
