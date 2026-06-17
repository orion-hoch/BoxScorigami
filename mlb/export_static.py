"""Pre-generate static JSON for every C(10,3)=120 MLB axis-stat combo.

Writes into the unified deploy folder at <repo>/public/mlb/.
Run after collect.py.
"""
import json
import sys
import time
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server import STATS, compute_payload, open_db, _validate_axes  # noqa: E402


# --- Season-totals tally (static export only) --------------------------------
# Each entry is a player-SEASON total instead of a single game. Stats are summed
# per (player_id, season), regular season only ('R' — excludes spring training,
# playoffs, All-Star), then the (sumX, sumY, sumZ) line is tallied across all
# player-seasons. A small season_totals table is derived once so each combo
# query hits ~50k rows instead of re-scanning the 5M-row player_games.
def build_season_totals(conn):
    sums = ", ".join(f"SUM({s['col']}) AS {s['col']}" for s in STATS.values())
    conn.execute("DROP TABLE IF EXISTS season_totals")
    conn.execute(
        f"""
        CREATE TABLE season_totals AS
        SELECT player_id, player_name, season, {sums}
        FROM player_games
        WHERE game_type = 'R'
        GROUP BY player_id, player_name, season
        """
    )
    conn.commit()


def compute_payload_season(x, y, z) -> str:
    _validate_axes(x, y, z)
    cx, cy, cz = STATS[x]["col"], STATS[y]["col"], STATS[z]["col"]
    conn = open_db()

    rows = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, COUNT(*) AS n
            FROM season_totals GROUP BY {cx}, {cy}, {cz}
        ),
        latest AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, player_id, player_name, season,
                   ROW_NUMBER() OVER (
                       PARTITION BY {cx}, {cy}, {cz}
                       ORDER BY season DESC, player_name
                   ) AS rn
            FROM season_totals
        )
        SELECT c.x, c.y, c.z, c.n, l.player_id, l.player_name, l.season
        FROM counts c
        JOIN latest l
          ON l.x = c.x AND l.y = c.y AND l.z = c.z AND l.rn = 1
        """
    ).fetchall()

    cells = [
        {"p": r["x"], "r": r["y"], "a": r["z"], "n": r["n"],
         "d": str(r["season"]), "pl": r["player_name"], "t": None,
         "m": f"{r['season']} season", "g": None, "pid": r["player_id"]}
        for r in rows
    ]
    if cells:
        max_x = max(c["p"] for c in cells)
        max_y = max(c["r"] for c in cells)
        max_z = max(c["a"] for c in cells)
    else:
        max_x = max_y = max_z = 0

    # Up to 5 most-recent (season DESC) player-seasons per repeated cell (n>=2),
    # attached as `recent` for the detail panel's "Recent Seasons" list.
    recent_rows = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, COUNT(*) AS n
            FROM season_totals GROUP BY {cx}, {cy}, {cz}
        ),
        recent AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, player_id, player_name, season,
                   ROW_NUMBER() OVER (
                       PARTITION BY {cx}, {cy}, {cz}
                       ORDER BY season DESC, player_name
                   ) AS rn
            FROM season_totals
        )
        SELECT r.x, r.y, r.z, r.player_id, r.player_name, r.season
        FROM recent r
        JOIN counts c ON c.x = r.x AND c.y = r.y AND c.z = r.z
        WHERE c.n >= 2 AND r.rn <= 5
        ORDER BY r.x, r.y, r.z, r.rn
        """
    ).fetchall()
    by_key = {}
    for r in recent_rows:
        by_key.setdefault((r["x"], r["y"], r["z"]), []).append(
            {"pl": r["player_name"], "d": str(r["season"]), "pid": r["player_id"]})
    for cell in cells:
        rec = by_key.get((cell["p"], cell["r"], cell["a"]))
        if rec:
            cell["recent"] = rec

    leaders = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, COUNT(*) AS n
            FROM season_totals GROUP BY {cx}, {cy}, {cz}
        )
        SELECT s.player_id, s.player_name, COUNT(*) AS unique_cells
        FROM season_totals s
        JOIN counts c ON s.{cx} = c.x AND s.{cy} = c.y AND s.{cz} = c.z
        WHERE c.n = 1
        GROUP BY s.player_id, s.player_name
        ORDER BY unique_cells DESC, s.player_name
        LIMIT 50
        """
    ).fetchall()
    leaderboard = [
        {"player_id": r["player_id"], "name": r["player_name"], "n": r["unique_cells"]}
        for r in leaders
    ]

    payload = {
        "axes": {
            "x": {"key": x, "label": STATS[x]["label"], "color": STATS[x]["color"], "max": max_x},
            "y": {"key": y, "label": STATS[y]["label"], "color": STATS[y]["color"], "max": max_y},
            "z": {"key": z, "label": STATS[z]["label"], "color": STATS[z]["color"], "max": max_z},
        },
        "cells": cells,
        "leaderboard": leaderboard,
    }
    return json.dumps(payload, separators=(",", ":"))


# Rebuild the materialized season-totals table so the season export reflects
# the current player_games data.
print("building season_totals table ...")
build_season_totals(open_db())

OUT_DIR    = HERE.parent / "public" / "mlb"
TALLY_DIR  = OUT_DIR / "tally"
SEASON_DIR = OUT_DIR / "tally-season"
TALLY_DIR.mkdir(parents=True, exist_ok=True)
SEASON_DIR.mkdir(parents=True, exist_ok=True)

stats_keys = list(STATS.keys())
stats_payload = {
    "stats": [{"key": k, "label": STATS[k]["label"], "color": STATS[k]["color"]}
              for k in stats_keys]
}
(OUT_DIR / "stats.json").write_text(json.dumps(stats_payload, separators=(",", ":")))
print(f"-> public/mlb/stats.json  ({len(stats_keys)} stats)")

combos = list(combinations(sorted(stats_keys), 3))


def export(label, fn, out_dir):
    t0 = time.time()
    total_bytes = 0
    print(f"\ngenerating {len(combos)} {label} combo files ...")
    for i, (a, b, c) in enumerate(combos, 1):
        payload = fn(a, b, c)
        (out_dir / f"{a}_{b}_{c}.json").write_text(payload)
        total_bytes += len(payload)
        if i % 10 == 0 or i == len(combos):
            print(f"  [{i:>3}/{len(combos)}] {a}_{b}_{c}.json  ({len(payload):>9,} bytes)")
    print(f"wrote {len(combos)} {label} files / {total_bytes/1e6:.1f} MB in {time.time()-t0:.1f}s")


export("per-game", compute_payload, TALLY_DIR)
export("season", compute_payload_season, SEASON_DIR)
print(f"\ndeploy folder: {OUT_DIR}")
