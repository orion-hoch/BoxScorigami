"""Tiny stdlib HTTP server: static files + live tally endpoint for MLB batting.

GET /                          -> static files (the viewer lives in /public)
GET /stats                     -> JSON list of valid stat axes
GET /tally?x=ab&y=h&z=hr        -> tally cells + leaderboard

Run:
    python3 mlb/server.py [--port 8768]

Mirror of nfl/server.py with MLB stat columns.
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


def open_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _validate_axes(x, y, z):
    for v in (x, y, z):
        if v not in STATS:
            raise ValueError(f"unknown stat: {v}")
    if len({x, y, z}) != 3:
        raise ValueError("x, y, z must be distinct")


@lru_cache(maxsize=64)
def compute_payload(x, y, z) -> str:
    _validate_axes(x, y, z)
    cx, cy, cz = STATS[x]["col"], STATS[y]["col"], STATS[z]["col"]
    conn = open_db()

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
    def _gid(v):
        return str(v) if v is not None else None
    cells = []
    cur = None
    for r in rows:
        if cur is None or (r["x"], r["y"], r["z"]) != (cur["p"], cur["r"], cur["a"]):
            cur = {"p": r["x"], "r": r["y"], "a": r["z"], "n": r["n"],
                   "d": r["last_date"], "pl": r["player_name"], "t": r["team_abbr"],
                   "m": r["matchup"], "g": _gid(r["game_id"]),
                   "pid": r["player_id"]}
            cells.append(cur)
        if r["n"] >= 2:
            cur.setdefault("recent", []).append({
                "pl": r["player_name"], "d": r["game_date"], "t": r["team_abbr"],
                "m": r["matchup"], "g": _gid(r["game_id"]), "pid": r["player_id"]})
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
        "cells": cells,
        "leaderboard": leaderboard,
    }
    return json.dumps(payload, separators=(",", ":"))


class Handler(SimpleHTTPRequestHandler):
    def translate_path(self, path):
        original = super().translate_path(path)
        rel = Path(original).resolve().relative_to(Path.cwd().resolve())
        return str(HERE / rel)

    def _send_json(self, s, status=200):
        body = s.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/stats":
            self._send_json(json.dumps({
                "stats": [{"key": k, "label": v["label"], "color": v["color"]}
                          for k, v in STATS.items()]
            }))
            return
        if url.path == "/tally":
            qs = parse_qs(url.query)
            x = (qs.get("x") or ["ab"])[0]
            y = (qs.get("y") or ["h"])[0]
            z = (qs.get("z") or ["hr"])[0]
            try:
                self._send_json(compute_payload(x, y, z))
            except ValueError as e:
                self._send_json(json.dumps({"error": str(e)}), status=400)
            return
        super().do_GET()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8768)
    ap.add_argument("--host", type=str, default="127.0.0.1")
    args = ap.parse_args()
    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Run collect.py first.", file=sys.stderr)
        sys.exit(1)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"mlb scorigami on http://{args.host}:{args.port}/")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
