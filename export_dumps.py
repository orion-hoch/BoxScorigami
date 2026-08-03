"""Emit one brotli dump per sport/mode; the viewer rolls up the chosen axes."""
import argparse
import brotli
import importlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent

SPORTS = {
    "nba":  {"db": "nba/nba.sqlite",       "pid": "player_id",
             "where": "season_type = 'Regular Season'", "min_gp": 20,
             "po": "season_type = 'Playoffs'", "po_min_gp": 10},
    "wnba": {"db": "wnba/wnba.sqlite",     "pid": "player_id",
             "where": "season_type = 'Regular Season'", "min_gp": 15,
             "po": "season_type = 'Playoffs'", "po_min_gp": 5},
    "nfl":  {"db": "nfl/nfl_full.sqlite",  "pid": "player_pfr_id",
             "where": "game_type = 'REG'",              "min_gp": 6,
             "po": "game_type = 'POST'", "po_min_gp": 2,
             "season_modes": [("season", "SUM({col})"), ("season-avg", None)]},
    "nhl":  {"db": "nhl/nhl.sqlite",       "pid": "player_id",
             "where": "game_type = 2",                  "min_gp": 20,
             "po": "game_type = 3", "po_min_gp": 10,
             "season_modes": [("season", "SUM({col})"), ("season-avg", None)]},
}

SEASON_AGG = "CAST(ROUND(1.0 * SUM({col}) / COUNT(*)) AS INT)"


def build_season_avg(conn, stats, sanity, pid, where, table, dest,
                     agg=SEASON_AGG, po="0"):
    """Season rollup by player-season, one column per stat key."""
    aggs = ", ".join((s.get("season_agg") or agg).format(col=s["col"]) + f" AS {k}"
                     for k, s in stats.items())
    conn.execute(f"DROP TABLE IF EXISTS {dest}")
    conn.execute(f"""
        CREATE TABLE {dest} AS
        SELECT {pid} AS player_id, player_name, season,
               CASE WHEN {po} THEN 1 ELSE 0 END AS po, COUNT(*) AS gp, {aggs}
        FROM {table}
        WHERE (({where}) OR ({po})) AND {sanity}
        GROUP BY {pid}, player_name, season, po
    """)
    conn.commit()


def emit(conn, dest_dir, mode, stats, *, table, sanity, pid, where, min_gp,
         matchup="matchup", po=None, po_min_gp=None):
    keys = list(stats)
    if mode == "game":
        sel = ", ".join(f"{s['col']} AS {k}" for k, s in stats.items())
        mu = f"{matchup} AS matchup" if matchup else "NULL AS matchup"
        extra = (f"{pid} AS pid, player_name, team_abbr, {mu}, "
                 f"game_id, game_date")
        extra_names = ["pid", "player_name", "team_abbr", "matchup",
                       "game_id", "game_date"]
        order = "game_date DESC, game_id DESC, pid DESC"
        flags = ["po"]
        sel += f", CASE WHEN {po} THEN 1 ELSE 0 END AS po"
        src_where = sanity
    else:
        sel = ", ".join(keys) + ", po"
        extra = "player_id AS pid, player_name, season, gp"
        extra_names = ["pid", "player_name", "season", "gp"]
        order = "season DESC, player_name ASC, pid ASC"
        flags = ["q", "po"]
        sel += (f", CASE WHEN gp >= (CASE WHEN po = 1 THEN {po_min_gp} "
                f"ELSE {min_gp} END) THEN 1 ELSE 0 END AS q")
        src_where = "1=1"

    part_cols = keys + flags
    part = ", ".join(part_cols)
    rows = conn.execute(f"""
        WITH src AS (
            SELECT {sel}, {extra} FROM {table} t WHERE {src_where}
        ), ranked AS (
            SELECT {part}, {', '.join(extra_names)},
                   ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order}) rn,
                   COUNT(*) OVER (PARTITION BY {part}) n
            FROM src
        )
        SELECT * FROM ranked WHERE rn <= 5 ORDER BY {part}, rn
    """).fetchall()

    lines, cur, curkey = [], None, object()
    for r in rows:
        key = tuple(r[c] for c in part_cols)
        if key != curkey:
            curkey = key
            cur = {"v": [r[k] for k in keys], "n": r["n"], "r": []}
            for f in flags:
                cur[f] = r[f]
            lines.append(cur)
        if mode == "game":
            cur["r"].append([
                r["pid"], r["player_name"], r["team_abbr"], r["matchup"],
                str(r["game_id"]) if r["game_id"] is not None else None,
                r["game_date"], None])
        else:
            cur["r"].append([r["pid"], r["player_name"], str(r["season"]),
                             r["gp"]])

    dest_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"mode": mode, "axes": keys, "lines": lines},
                         separators=(",", ":"))
    out = dest_dir / f"{mode}.json.br"
    data = brotli.compress(payload.encode("utf-8"), quality=9)
    out.write_bytes(data)
    return len(lines), len(payload), out.stat().st_size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", required=True, choices=sorted(SPORTS))
    ap.add_argument("--db", help="read from this sqlite file instead of the default")
    ap.add_argument("--group", action="append", help="limit to these stat groups")
    args = ap.parse_args()
    cfg = SPORTS[args.sport]

    sys.path.insert(0, str(ROOT / args.sport))
    mod = importlib.import_module("server")
    if args.db:
        mod.DB_PATH = Path(args.db).resolve()
    conn = mod.open_db()

    groups = getattr(mod, "GROUPS", None)
    out_root = ROOT / "public" / args.sport
    if groups:
        wanted = args.group or list(groups)
        jobs = [(out_root / k, groups[k]["stats"], groups[k]["sanity"],
                 groups[k]["table"], k, groups[k].get("matchup", "matchup"))
                for k in wanted]
    else:
        jobs = [(out_root, mod.STATS, mod.SANITY_FILTER, "player_games", "",
                 "matchup")]

    t0 = time.time()
    for dest_dir, stats, sanity, table, tag, matchup in jobs:
        label = f"{args.sport}{'/' + tag if tag else ''}"
        print(f"\n== {label} ({len(stats)} stats) ==")
        (dest_dir / "stats.json").parent.mkdir(parents=True, exist_ok=True)
        (dest_dir / "stats.json").write_text(json.dumps(
            {"stats": [{"key": k, **{f: v[f] for f in
                        ("label", "color", "scale", "decimals") if f in v}}
                       for k, v in stats.items()]}, separators=(",", ":")))

        jobs_by_mode = [("game", table)]
        for mode, agg in cfg.get("season_modes", [("season", None)]):
            tbl = f"season_dump_{mode.replace('-', '_')}{'_' + tag if tag else ''}"
            build_season_avg(conn, stats, sanity, cfg["pid"], cfg["where"],
                             table, tbl, agg or SEASON_AGG, cfg["po"])
            jobs_by_mode.append((mode, tbl))

        for mode, tbl in jobs_by_mode:
            n, raw, gz = emit(conn, dest_dir, mode, stats, table=tbl,
                              sanity=sanity, pid=cfg["pid"],
                              where=cfg["where"], min_gp=cfg["min_gp"],
                              matchup=matchup if mode == "game" else None,
                              po=cfg["po"], po_min_gp=cfg["po_min_gp"])
            print(f"  {mode:6} {n:>9,} lines  {raw/1e6:7.1f} MB raw "
                  f"-> {gz/1e6:5.1f} MB gz")
    print(f"\nDONE in {(time.time()-t0)/60:.1f}m")


if __name__ == "__main__":
    main()
