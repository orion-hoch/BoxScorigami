import argparse
import importlib
import json
import sys
import time
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SPORTS = {
    "nba": {
        "table": "season_avg",
        "agg": "CAST(ROUND(1.0 * SUM({col}) / COUNT(*)) AS INT)",
        "pid": "player_id",
        "where": "season_type = 'Regular Season'",
        "gp": True,
        "min_gp": 20,
        "mode": "(per game)",
        "step": 25,
    },
    "nfl": {
        "table": "season_avg",
        "agg": "CAST(ROUND(1.0 * SUM({col}) / COUNT(*)) AS INT)",
        "pid": "player_pfr_id",
        "where": "game_type = 'REG'",
        "gp": True,
        "min_gp": 6,
        "mode": "(per game)",
        "step": 25,
    },
    "mlb": {
        "table": "season_totals",
        "agg": "SUM({col})",
        "pid": "player_id",
        "where": "game_type = 'R'",
        "gp": False,
        "min_gp": None,
        "mode": "season",
        "step": 10,
    },
}


def build_season_table(conn, cfg, stats, sanity):
    aggs = ", ".join(cfg["agg"].format(col=s["col"]) + f" AS {s['col']}"
                     for s in stats.values())
    gp = "COUNT(*) AS gp, " if cfg["gp"] else ""
    conn.execute(f"DROP TABLE IF EXISTS {cfg['table']}")
    conn.execute(
        f"""
        CREATE TABLE {cfg['table']} AS
        SELECT {cfg['pid']} AS player_id, player_name, season, {gp}{aggs}
        FROM player_games
        WHERE {cfg['where']} AND {sanity}
        GROUP BY {cfg['pid']}, player_name, season
        """
    )
    conn.commit()


def season_payload(mod, cfg, x, y, z, min_gp=None) -> str:
    stats = mod.STATS
    mod._validate_axes(x, y, z)
    cx, cy, cz = stats[x]["col"], stats[y]["col"], stats[z]["col"]
    tbl = cfg["table"]
    gp_col = ", gp" if cfg["gp"] else ""
    gp_sel = ", r.gp" if cfg["gp"] else ""
    flt = f"WHERE {cx} IS NOT NULL AND {cy} IS NOT NULL AND {cz} IS NOT NULL"
    if min_gp:
        flt += f" AND gp >= {min_gp}"
    lead_flt = f"AND s.gp >= {min_gp}" if min_gp else ""
    conn = mod.open_db()

    rows = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, COUNT(*) AS n
            FROM {tbl} {flt} GROUP BY {cx}, {cy}, {cz}
        ),
        rep AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, player_id, player_name, season{gp_col},
                   ROW_NUMBER() OVER (
                       PARTITION BY {cx}, {cy}, {cz}
                       ORDER BY season DESC, player_name
                   ) AS rn
            FROM {tbl} {flt}
        )
        SELECT c.x, c.y, c.z, c.n, r.player_id, r.player_name, r.season{gp_sel}
        FROM counts c
        JOIN rep r
          ON r.x = c.x AND r.y = c.y AND r.z = c.z AND r.rn = 1
        """
    ).fetchall()

    cells = []
    for r in rows:
        cell = {"p": r["x"], "r": r["y"], "a": r["z"], "n": r["n"],
                "d": str(r["season"]), "pl": r["player_name"], "t": None,
                "m": f"{r['season']} {cfg['mode']}", "g": None}
        if cfg["gp"]:
            cell["gp"] = r["gp"]
        cell["pid"] = r["player_id"]
        cells.append(cell)
    max_x = max((c["p"] for c in cells), default=0)
    max_y = max((c["r"] for c in cells), default=0)
    max_z = max((c["a"] for c in cells), default=0)

    recent_rows = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, COUNT(*) AS n
            FROM {tbl} {flt} GROUP BY {cx}, {cy}, {cz}
        ),
        recent AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, player_id, player_name, season{gp_col},
                   ROW_NUMBER() OVER (
                       PARTITION BY {cx}, {cy}, {cz}
                       ORDER BY season DESC, player_name
                   ) AS rn
            FROM {tbl} {flt}
        )
        SELECT r.x, r.y, r.z, r.player_id, r.player_name, r.season{gp_sel}
        FROM recent r
        JOIN counts c ON c.x = r.x AND c.y = r.y AND c.z = r.z
        WHERE c.n >= 2 AND r.rn <= 5
        ORDER BY r.x, r.y, r.z, r.rn
        """
    ).fetchall()
    by_key = {}
    for r in recent_rows:
        item = {"pl": r["player_name"], "d": str(r["season"])}
        if cfg["gp"]:
            item["gp"] = r["gp"]
        item["pid"] = r["player_id"]
        by_key.setdefault((r["x"], r["y"], r["z"]), []).append(item)
    for cell in cells:
        rec = by_key.get((cell["p"], cell["r"], cell["a"]))
        if rec:
            cell["recent"] = rec

    leaders = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, COUNT(*) AS n
            FROM {tbl} {flt} GROUP BY {cx}, {cy}, {cz}
        )
        SELECT s.player_id, s.player_name, COUNT(*) AS unique_cells
        FROM {tbl} s
        JOIN counts c ON s.{cx} = c.x AND s.{cy} = c.y AND s.{cz} = c.z
        WHERE c.n = 1 {lead_flt}
        GROUP BY s.player_id, s.player_name
        ORDER BY unique_cells DESC, s.player_name
        LIMIT 50
        """
    ).fetchall()

    payload = {
        "axes": {
            "x": {"key": x, "label": stats[x]["label"], "color": stats[x]["color"], "max": max_x},
            "y": {"key": y, "label": stats[y]["label"], "color": stats[y]["color"], "max": max_y},
            "z": {"key": z, "label": stats[z]["label"], "color": stats[z]["color"], "max": max_z},
        },
        "cells": cells,
        "leaderboard": [
            {"player_id": r["player_id"], "name": r["player_name"], "n": r["unique_cells"]}
            for r in leaders
        ],
    }
    return json.dumps(payload, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", required=True, choices=sorted(SPORTS))
    sport = ap.parse_args().sport
    cfg = SPORTS[sport]

    sys.path.insert(0, str(ROOT / sport))
    mod = importlib.import_module("server")
    stats = mod.STATS

    out_dir = ROOT / "public" / sport
    tally_dir = out_dir / "tally"
    season_dir = out_dir / "tally-season"
    tally_dir.mkdir(parents=True, exist_ok=True)
    season_dir.mkdir(parents=True, exist_ok=True)

    (out_dir / "stats.json").write_text(json.dumps(
        {"stats": [{"key": k, "label": v["label"], "color": v["color"]}
                   for k, v in stats.items()]},
        separators=(",", ":")))
    print(f"-> public/{sport}/stats.json  ({len(stats)} stats)")

    print(f"building {cfg['table']} table ...")
    build_season_table(mod.open_db(), cfg, stats, mod.SANITY_FILTER)

    combos = list(combinations(sorted(stats), 3))

    def export(label, fn, dest):
        t0 = time.time()
        total_bytes = 0
        print(f"\ngenerating {len(combos)} {label} combo files ...")
        for i, (a, b, c) in enumerate(combos, 1):
            payload = fn(a, b, c)
            (dest / f"{a}_{b}_{c}.json").write_text(payload)
            total_bytes += len(payload)
            if i % cfg["step"] == 0 or i == len(combos):
                print(f"  [{i:>3}/{len(combos)}] {a}_{b}_{c}.json  ({len(payload):>9,} bytes)")
        print(f"wrote {len(combos)} {label} files / {total_bytes/1e6:.1f} MB in {time.time()-t0:.1f}s")

    export("per-game", mod.compute_payload, tally_dir)
    export("season", lambda a, b, c: season_payload(mod, cfg, a, b, c), season_dir)
    if cfg["min_gp"]:
        min_dir = out_dir / "tally-season-min"
        min_dir.mkdir(parents=True, exist_ok=True)
        export("season (min games)",
               lambda a, b, c: season_payload(mod, cfg, a, b, c, cfg["min_gp"]),
               min_dir)
    print(f"\ndeploy folder: {out_dir}")


if __name__ == "__main__":
    main()
