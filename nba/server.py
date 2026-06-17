"""Tiny stdlib HTTP server: static files + live tally endpoint.

GET /                          -> JSON describing this dev API (viewer lives in /public)
GET /<file>                    -> static file from this directory
GET /stats                     -> JSON list of valid stat axes
GET /tally?x=pts&y=reb&z=ast   -> tally cells + leaderboard for the chosen axes

Run:
    python3 nba/server.py [--port 8765]

The server queries nba.sqlite directly with window functions so any
3-stat combo is computed on demand. Typical response < 1s on a laptop.
"""
import argparse
import json
import sqlite3
import sys
from functools import lru_cache
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nba.sqlite"

# Allow-list of stat columns we expose. Keys map UI labels to DB columns;
# the labels are what the dropdown sends back as the axis identifier.
STATS = {
    "pts":  {"col": "pts",  "label": "Points",          "color": "#ff6b6b"},
    "reb":  {"col": "reb",  "label": "Rebounds",        "color": "#6bd06b"},
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


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_axes(x: str, y: str, z: str):
    for v in (x, y, z):
        if v not in STATS:
            raise ValueError(f"unknown stat: {v}")
    if len({x, y, z}) != 3:
        raise ValueError("x, y, z must be distinct")


@lru_cache(maxsize=64)
def compute_payload(x: str, y: str, z: str) -> str:
    """Return JSON string for a given axis trio. Cached in-memory."""
    _validate_axes(x, y, z)
    cx, cy, cz = STATS[x]["col"], STATS[y]["col"], STATS[z]["col"]
    conn = open_db()

    # Tally + most-recent occurrence per cell, in one query via window function.
    # The PARTITION key for ROW_NUMBER must mirror the GROUP BY in `counts`.
    rows = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z,
                   COUNT(*) AS n,
                   MIN(game_date) AS first_date,
                   MAX(game_date) AS last_date
            FROM player_games
            WHERE {cx} IS NOT NULL AND {cy} IS NOT NULL AND {cz} IS NOT NULL
            GROUP BY {cx}, {cy}, {cz}
        ),
        latest AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z,
                   game_id, player_id, player_name, team_abbr, matchup, game_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY {cx}, {cy}, {cz}
                       ORDER BY game_date DESC, game_id DESC
                   ) AS rn
            FROM player_games
            WHERE {cx} IS NOT NULL AND {cy} IS NOT NULL AND {cz} IS NOT NULL
        )
        SELECT c.x, c.y, c.z, c.n, c.last_date,
               l.player_id, l.player_name, l.team_abbr, l.matchup, l.game_id,
               l.game_date, l.rn
        FROM counts c
        JOIN latest l
          ON l.x = c.x AND l.y = c.y AND l.z = c.z AND l.rn <= 5
        ORDER BY c.x, c.y, c.z, l.rn
        """
    ).fetchall()

    # Rows arrive grouped by cell (ORDER BY x,y,z,rn). The rn=1 row is the
    # headline occurrence (keeps existing fields/filters intact); repeated cells
    # (n>=2) also get the full up-to-5 list as `recent` for the detail panel.
    cells = []
    cur = None
    for r in rows:
        if cur is None or (r["x"], r["y"], r["z"]) != (cur["p"], cur["r"], cur["a"]):
            cur = {
                "p": r["x"], "r": r["y"], "a": r["z"], "n": r["n"],
                "d": r["last_date"], "pl": r["player_name"],
                "t": r["team_abbr"], "m": r["matchup"], "g": r["game_id"],
                "pid": r["player_id"],
            }
            cells.append(cur)
        if r["n"] >= 2:
            cur.setdefault("recent", []).append({
                "pl": r["player_name"], "d": r["game_date"], "t": r["team_abbr"],
                "m": r["matchup"], "g": r["game_id"], "pid": r["player_id"],
            })

    # Bounding box from the actual data
    if cells:
        max_x = max(c["p"] for c in cells)
        max_y = max(c["r"] for c in cells)
        max_z = max(c["a"] for c in cells)
    else:
        max_x = max_y = max_z = 0

    # Leaderboard: players with the most n=1 cells for THIS combo.
    leaders = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z, COUNT(*) AS n
            FROM player_games
            WHERE {cx} IS NOT NULL AND {cy} IS NOT NULL AND {cz} IS NOT NULL
            GROUP BY {cx}, {cy}, {cz}
        )
        SELECT p.player_id, p.player_name, COUNT(*) AS unique_cells
        FROM player_games p
        JOIN counts c
          ON p.{cx} = c.x AND p.{cy} = c.y AND p.{cz} = c.z
        WHERE c.n = 1
        GROUP BY p.player_id, p.player_name
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
        "max_pts": max_x, "max_reb": max_y, "max_ast": max_z,  # legacy keys
        "cells": cells,
        "leaderboard": leaderboard,
    }
    return json.dumps(payload, separators=(",", ":"))


class Handler(SimpleHTTPRequestHandler):
    # Serve files from the nba/ directory regardless of cwd
    def translate_path(self, path):
        original = super().translate_path(path)
        # SimpleHTTPRequestHandler serves relative to cwd; force HERE
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
            x = (qs.get("x") or ["pts"])[0]
            y = (qs.get("y") or ["reb"])[0]
            z = (qs.get("z") or ["ast"])[0]
            try:
                payload = compute_payload(x, y, z)
            except ValueError as e:
                return self._send_error_json(400, str(e))
            except sqlite3.OperationalError as e:
                return self._send_error_json(500, f"db error: {e}")
            self._send_json(payload)
            return
        # No HTML viewer here — the unified site lives in /public. This dev
        # server only exposes the live JSON API.
        if url.path in ("/", ""):
            return self._send_json(json.dumps({
                "service": "nba scorigami dev API",
                "endpoints": ["/stats", "/tally?x=pts&y=reb&z=ast"],
                "viewer": "serve the repo's /public/ folder and open index.html",
            }))
        return super().do_GET()

    def log_message(self, fmt, *args):
        # Quieter logging
        sys.stderr.write(f"[server] {self.address_string()} - {fmt % args}\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", type=str, default="127.0.0.1")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Run collect.py first.", file=sys.stderr)
        sys.exit(1)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"nba scorigami server on http://{args.host}:{args.port}/")
    print(f"  serving from: {HERE}")
    print(f"  db:           {DB_PATH}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
