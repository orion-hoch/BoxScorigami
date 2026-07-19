import argparse
import json
import os
import sqlite3
import sys
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
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
SANITY_FILTER = "NOT (" + " OR ".join(f"IFNULL(({c}), 0)" for c in SANITY_BAD) + ")"


def open_db() -> sqlite3.Connection:
    if not os.path.exists(DB_PATH):
        print(
            f"ERROR: {DB_PATH} not found. Run collect.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='player_games'"
    ).fetchone()
    if not has_table:
        print(
            f"ERROR: {DB_PATH} has no player_games table. Run "
            f"collect.py first.",
            file=sys.stderr,
        )
        sys.exit(1)
    conn.execute("""
        CREATE TEMP VIEW player_games_unified AS
        SELECT
            game_id, game_date, season,
            player_pfr_id AS player_uid,
            player_name, team_abbr, opponent, matchup,
            pass_cmp, pass_att, pass_yds, pass_td, pass_int, sacks,
            rush_att, rush_yds, rush_td,
            tgt, rec, rec_yds, rec_td
        FROM player_games
    """)
    return conn


def _validate_axes(x: str, y: str, z: str):
    for v in (x, y, z):
        if v not in STATS:
            raise ValueError(f"unknown stat: {v}")
    if len({x, y, z}) != 3:
        raise ValueError("x, y, z must be distinct")


@lru_cache(maxsize=64)
def compute_payload(x: str, y: str, z: str) -> str:
    _validate_axes(x, y, z)
    cx = f"MAX({STATS[x]['col']}, 0)"
    cy = f"MAX({STATS[y]['col']}, 0)"
    cz = f"MAX({STATS[z]['col']}, 0)"
    raw_cx, raw_cy, raw_cz = STATS[x]["col"], STATS[y]["col"], STATS[z]["col"]
    conn = open_db()

    rows = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z,
                   COUNT(*) AS n,
                   MIN(game_date) AS first_date,
                   MAX(game_date) AS last_date
            FROM player_games_unified
            WHERE {raw_cx} IS NOT NULL AND {raw_cy} IS NOT NULL AND {raw_cz} IS NOT NULL
              AND {SANITY_FILTER}
            GROUP BY {cx}, {cy}, {cz}
        ),
        latest AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z,
                   game_id, player_uid, player_name, team_abbr, matchup, game_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY {cx}, {cy}, {cz}
                       ORDER BY game_date DESC, game_id DESC
                   ) AS rn
            FROM player_games_unified
            WHERE {raw_cx} IS NOT NULL AND {raw_cy} IS NOT NULL AND {raw_cz} IS NOT NULL
              AND {SANITY_FILTER}
        )
        SELECT c.x, c.y, c.z, c.n, c.last_date,
               l.player_uid, l.player_name, l.team_abbr, l.matchup, l.game_id,
               l.game_date, l.rn
        FROM counts c
        JOIN latest l
          ON l.x = c.x AND l.y = c.y AND l.z = c.z AND l.rn <= 5
        ORDER BY c.x, c.y, c.z, l.rn
        """
    ).fetchall()

    cells = []
    cur = None
    for r in rows:
        if cur is None or (r["x"], r["y"], r["z"]) != (cur["p"], cur["r"], cur["a"]):
            cur = {
                "p": r["x"], "r": r["y"], "a": r["z"], "n": r["n"],
                "d": r["last_date"], "pl": r["player_name"],
                "t": r["team_abbr"], "m": r["matchup"], "g": r["game_id"],
                "pid": r["player_uid"],
            }
            cells.append(cur)
        if r["n"] >= 2:
            cur.setdefault("recent", []).append({
                "pl": r["player_name"], "d": r["game_date"], "t": r["team_abbr"],
                "m": r["matchup"], "g": r["game_id"], "pid": r["player_uid"],
            })

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
            FROM player_games_unified
            WHERE {raw_cx} IS NOT NULL AND {raw_cy} IS NOT NULL AND {raw_cz} IS NOT NULL
              AND {SANITY_FILTER}
            GROUP BY {cx}, {cy}, {cz}
        )
        SELECT p.player_uid AS player_id, p.player_name, COUNT(*) AS unique_cells
        FROM player_games_unified p
        JOIN counts c
          ON MAX(p.{raw_cx}, 0) = c.x AND MAX(p.{raw_cy}, 0) = c.y AND MAX(p.{raw_cz}, 0) = c.z
        WHERE c.n = 1 AND {SANITY_FILTER}
        GROUP BY p.player_uid, p.player_name
        ORDER BY unique_cells DESC, p.player_name
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
        "max_pts": max_x, "max_reb": max_y, "max_ast": max_z,
        "cells": cells,
        "leaderboard": leaderboard,
    }
    return json.dumps(payload, separators=(",", ":"))


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        original = super().translate_path(path)
        rel = Path(original).resolve().relative_to(Path.cwd().resolve())
        return str(HERE / rel)

    def _send_json(self, payload_str: str, status: int = 200) -> None:
        body = payload_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, status: int, message: str) -> None:
        self._send_json(json.dumps({"error": message}), status=status)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/stats":
            self._send_json(json.dumps({
                "stats": [
                    {"key": k, "label": v["label"], "color": v["color"]}
                    for k, v in STATS.items()
                ]
            }))
            return
        if url.path == "/tally":
            qs = parse_qs(url.query)
            x = (qs.get("x") or ["pass_yds"])[0]
            y = (qs.get("y") or ["pass_td"])[0]
            z = (qs.get("z") or ["rush_yds"])[0]
            try:
                payload = compute_payload(x, y, z)
            except ValueError as e:
                return self._send_error_json(400, str(e))
            except sqlite3.OperationalError as e:
                return self._send_error_json(500, f"db error: {e}")
            self._send_json(payload)
            return
        if url.path in ("/", ""):
            self._send_json(json.dumps({
                "service": "nfl scorigami dev API",
                "endpoints": ["/stats", "/tally?x=rec_yds&y=rec&z=rec_td"],
                "viewer": "serve the repo's /public/ folder and open index.html",
            }))
            return
        return super().do_GET()

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[server] {self.address_string()} - {fmt % args}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--host", type=str, default="127.0.0.1")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(
            f"ERROR: {DB_PATH} not found. Run collect.py first.",
            file=sys.stderr,
        )
        sys.exit(1)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"nfl scorigami server on http://{args.host}:{args.port}/")
    print(f"  serving from: {HERE}")
    print(f"  db:           {DB_PATH}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
