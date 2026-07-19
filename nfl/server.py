import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent))
import stats as shared  # noqa: E402

DB_PATH = HERE / "nfl_full.sqlite"

STATS = {
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

DEFAULTS = ("pass_yds", "pass_td", "rush_yds")


def open_db():
    return shared.open_db(DB_PATH)


def _validate_axes(x, y, z):
    shared.validate_axes(STATS, x, y, z)


@lru_cache(maxsize=64)
def compute_payload(x, y, z) -> str:
    return shared.compute_payload(DB_PATH, STATS, SANITY_FILTER, x, y, z,
                                  clamp=True, pid_col="player_pfr_id",
                                  legacy_max=True)


if __name__ == "__main__":
    shared.serve("nfl", HERE, DB_PATH, STATS, DEFAULTS, 8766, compute_payload)
