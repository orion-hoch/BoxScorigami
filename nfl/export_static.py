"""Pre-generate static JSON for every C(13,3)=286 NFL axis-stat combo.

Mirror of nba/export_static.py. Writes into public/nfl/.

Two passes are written:
    public/nfl/tally/         per-game box-score scorigami
    public/nfl/tally-season/  per-game AVERAGES over a player-season
                              (season total / games played, rounded)

Run:
    python3 nfl/export_static.py
"""
import json
import sys
import time
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server import STATS, compute_payload, open_db, _validate_axes  # noqa: E402

# Regular season only — excludes POST (playoffs) from the per-game averages.
SEASON_TYPE = "REG"


# --- Season per-game-average tally (static export only) ----------------------
# Each entry is a player-SEASON, valued by its per-game average for each stat:
# round(SUM(stat) / games played). Averages are derived once into a small
# season_avg table so each combo query hits ~25k rows instead of the full
# player_games scan.
def build_season_avg(conn):
    avgs = ", ".join(
        f"CAST(ROUND(1.0 * SUM({s['col']}) / COUNT(*)) AS INT) AS {s['col']}"
        for s in STATS.values()
    )
    conn.execute("DROP TABLE IF EXISTS season_avg")
    conn.execute(
        f"""
        CREATE TABLE season_avg AS
        SELECT player_id, player_name, season, COUNT(*) AS gp, {avgs}
        FROM player_games
        WHERE season_type = '{SEASON_TYPE}'
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
            FROM season_avg GROUP BY {cx}, {cy}, {cz}
        ),
        rep AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, player_id, player_name, season, gp,
                   ROW_NUMBER() OVER (
                       PARTITION BY {cx}, {cy}, {cz}
                       ORDER BY gp DESC, season DESC, player_name
                   ) AS rn
            FROM season_avg
        )
        SELECT c.x, c.y, c.z, c.n, r.player_id, r.player_name, r.season, r.gp
        FROM counts c
        JOIN rep r
          ON r.x = c.x AND r.y = c.y AND r.z = c.z AND r.rn = 1
        """
    ).fetchall()

    cells = [
        {"p": r["x"], "r": r["y"], "a": r["z"], "n": r["n"],
         "d": str(r["season"]), "pl": r["player_name"], "t": None,
         "m": f"{r['season']} (per game)", "g": None, "gp": r["gp"],
         "pid": r["player_id"]}
        for r in rows
    ]
    if cells:
        max_x = max(c["p"] for c in cells)
        max_y = max(c["r"] for c in cells)
        max_z = max(c["a"] for c in cells)
    else:
        max_x = max_y = max_z = 0

    leaders = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, COUNT(*) AS n
            FROM season_avg GROUP BY {cx}, {cy}, {cz}
        )
        SELECT s.player_id, s.player_name, COUNT(*) AS unique_cells
        FROM season_avg s
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


OUT_DIR    = HERE.parent / "public" / "nfl"
TALLY_DIR  = OUT_DIR / "tally"
SEASON_DIR = OUT_DIR / "tally-season"
TALLY_DIR.mkdir(parents=True, exist_ok=True)
SEASON_DIR.mkdir(parents=True, exist_ok=True)

stats_keys = list(STATS.keys())

stats_payload = {
    "stats": [
        {"key": k, "label": STATS[k]["label"], "color": STATS[k]["color"]}
        for k in stats_keys
    ]
}
(OUT_DIR / "stats.json").write_text(json.dumps(stats_payload, separators=(",", ":")))
print(f"-> public/nfl/stats.json  ({len(stats_keys)} stats)")

print("building season_avg table ...")
build_season_avg(open_db())

combos = list(combinations(sorted(stats_keys), 3))


def export(label, fn, out_dir):
    t0 = time.time()
    total_bytes = 0
    print(f"\ngenerating {len(combos)} {label} combo files ...")
    for i, (a, b, c) in enumerate(combos, 1):
        payload = fn(a, b, c)
        (out_dir / f"{a}_{b}_{c}.json").write_text(payload)
        total_bytes += len(payload)
        if i % 25 == 0 or i == len(combos):
            print(f"  [{i:>3}/{len(combos)}] {a}_{b}_{c}.json  ({len(payload):>9,} bytes)")
    print(f"wrote {len(combos)} {label} files / {total_bytes/1e6:.1f} MB in {time.time()-t0:.1f}s")


export("per-game", compute_payload, TALLY_DIR)
export("season", compute_payload_season, SEASON_DIR)
print(f"\ndeploy folder: {OUT_DIR}")
