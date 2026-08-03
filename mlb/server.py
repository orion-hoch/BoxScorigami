import sys
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent))
import stats as shared

DB_PATH = HERE / "mlb.sqlite"

STATS = {
    "ab":      {"col": "ab",      "label": "At Bats",    "color": "#ff6b6b"},
    "r":       {"col": "r",       "label": "Runs",       "color": "#f7c948"},
    "h":       {"col": "h",       "label": "Hits",       "color": "#6bd06b"},
    "doubles": {"col": "doubles", "label": "Doubles",    "color": "#9fc2ff"},
    "triples": {"col": "triples", "label": "Triples",    "color": "#a07bff"},
    "hr":      {"col": "hr",      "label": "Home Runs",  "color": "#ff5fa0"},
    "rbi":     {"col": "rbi",     "label": "RBI",        "color": "#ff9f43"},
    "bb":      {"col": "bb",      "label": "Walks",      "color": "#8ee27a"},
    "k":       {"col": "k",       "label": "Strikeouts", "color": "#bdbdbd"},
    "sb":      {"col": "sb",      "label": "Stolen Bases","color": "#5fd5ff"},
}

SANITY_BAD = [
    "h > ab",
    "doubles + triples + hr > h",
    "h + k > ab",
] + [f"{s['col']} < 0" for s in STATS.values()]
SANITY_FILTER = shared.sanity_filter(SANITY_BAD)

DEFAULTS = ("ab", "h", "hr")


def open_db():
    return shared.open_db(DB_PATH)


@lru_cache(maxsize=64)
def compute_payload(x, y, z) -> str:
    return shared.compute_payload(DB_PATH, STATS, SANITY_FILTER, x, y, z)


if __name__ == "__main__":
    shared.serve("mlb", HERE, DB_PATH, STATS, DEFAULTS, 8768, compute_payload)
