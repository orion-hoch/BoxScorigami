import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent))
import stats as shared  # noqa: E402

DB_PATH = HERE / "nba.sqlite"

STATS = {
    "pts":  {"col": "pts",  "label": "Points",          "color": "#ff6b6b"},
    "reb":  {"col": "reb",  "label": "Rebounds",        "color": "#6bd06b"},
    "oreb": {"col": "oreb", "label": "Off. Rebounds",   "color": "#3fae5a"},
    "dreb": {"col": "dreb", "label": "Def. Rebounds",   "color": "#9ade8c"},
    "ast":  {"col": "ast",  "label": "Assists",         "color": "#6b9eff"},
    "stl":  {"col": "stl",  "label": "Steals",          "color": "#f7c948"},
    "blk":  {"col": "blk",  "label": "Blocks",          "color": "#a07bff"},
    "tov":  {"col": "tov",  "label": "Turnovers",       "color": "#ff9f43"},
    "pf":   {"col": "pf",   "label": "Personal Fouls",  "color": "#bdbdbd"},
    "fgm":  {"col": "fgm",  "label": "FG Made",         "color": "#ff5fa0"},
    "fga":  {"col": "fga",  "label": "FG Attempts",     "color": "#ff8fbf"},
    "fg3m": {"col": "fg3m", "label": "3PT Made",        "color": "#5fd5ff"},
    "fg3a": {"col": "fg3a", "label": "3PT Attempts",    "color": "#9fe6ff"},
    "ftm":  {"col": "ftm",  "label": "FT Made",         "color": "#8ee27a"},
    "fta":  {"col": "fta",  "label": "FT Attempts",     "color": "#b9ec9f"},
}

SANITY_BAD = [
    "fgm > fga",
    "fg3m > fg3a",
    "ftm > fta",
    "fg3m > fgm",
    "fg3a > fga",
    "pts <> 2*fgm + fg3m + ftm",
    "pf > 7",
] + [f"{s['col']} < 0" for s in STATS.values()]
SANITY_FILTER = shared.sanity_filter(SANITY_BAD)

DEFAULTS = ("pts", "reb", "ast")


def open_db():
    return shared.open_db(DB_PATH)


@lru_cache(maxsize=64)
def compute_payload(x, y, z) -> str:
    return shared.compute_payload(DB_PATH, STATS, SANITY_FILTER, x, y, z,
                                  legacy_max=True)


if __name__ == "__main__":
    shared.serve("nba", HERE, DB_PATH, STATS, DEFAULTS, 8765, compute_payload)
