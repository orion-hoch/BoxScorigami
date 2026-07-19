"""Shared data + HTTP layer for the per-sport scorigami servers."""
import argparse
import json
import sqlite3
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def sanity_filter(bad):
    return "NOT (" + " OR ".join(f"IFNULL(({c}), 0)" for c in bad) + ")"


def open_db(path) -> sqlite3.Connection:
    if not Path(path).exists():
        raise SystemExit(f"no database at {path} — run collect.py first")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def validate_axes(stats, x, y, z):
    for v in (x, y, z):
        if v not in stats:
            raise ValueError(f"unknown stat: {v}")
    if len({x, y, z}) != 3:
        raise ValueError("x, y, z must be distinct")


def compute_payload(db, stats, sanity, x, y, z, clamp=False,
                    pid_col="player_id", legacy_max=False,
                    table="player_games", matchup="matchup") -> str:
    validate_axes(stats, x, y, z)
    raw_cx, raw_cy, raw_cz = (stats[k]["col"] for k in (x, y, z))

    def q(col):
        return f"MAX({col}, 0)" if clamp else col

    cx, cy, cz = q(raw_cx), q(raw_cy), q(raw_cz)
    notnull = (f"{raw_cx} IS NOT NULL AND {raw_cy} IS NOT NULL "
               f"AND {raw_cz} IS NOT NULL")
    # The box-score tables (player_defense, returns, ...) carry no matchup column.
    matchup_sel = f"{matchup} AS matchup" if matchup else "NULL AS matchup"
    conn = open_db(db)

    rows = conn.execute(
        f"""
        WITH counts AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z,
                   COUNT(*) AS n,
                   MIN(game_date) AS first_date,
                   MAX(game_date) AS last_date
            FROM {table}
            WHERE {notnull} AND {sanity}
            GROUP BY {cx}, {cy}, {cz}
        ),
        latest AS (
            SELECT {cx} AS x, {cy} AS y, {cz} AS z,
                   game_id, {pid_col} AS pid, player_name, team_abbr, {matchup_sel},
                   game_date,
                   ROW_NUMBER() OVER (
                       PARTITION BY {cx}, {cy}, {cz}
                       ORDER BY game_date DESC, game_id DESC
                   ) AS rn
            FROM {table}
            WHERE {notnull} AND {sanity}
        )
        SELECT c.x, c.y, c.z, c.n, c.last_date,
               l.pid, l.player_name, l.team_abbr, l.matchup, l.game_id,
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
        gid = None if r["game_id"] is None else str(r["game_id"])
        if cur is None or (r["x"], r["y"], r["z"]) != (cur["p"], cur["r"], cur["a"]):
            cur = {
                "p": r["x"], "r": r["y"], "a": r["z"], "n": r["n"],
                "d": r["last_date"], "pl": r["player_name"],
                "t": r["team_abbr"], "m": r["matchup"], "g": gid,
                "pid": r["pid"],
            }
            cells.append(cur)
        if r["n"] >= 2:
            cur.setdefault("recent", []).append({
                "pl": r["player_name"], "d": r["game_date"], "t": r["team_abbr"],
                "m": r["matchup"], "g": gid, "pid": r["pid"],
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
            FROM {table}
            WHERE {notnull} AND {sanity}
            GROUP BY {cx}, {cy}, {cz}
        )
        SELECT p.{pid_col} AS player_id, p.player_name, COUNT(*) AS unique_cells
        FROM {table} p
        JOIN counts c
          ON {cx} = c.x AND {cy} = c.y AND {cz} = c.z
        WHERE c.n = 1 AND {sanity}
        GROUP BY p.{pid_col}, p.player_name
        ORDER BY unique_cells DESC, p.player_name
        LIMIT 50
        """
    ).fetchall()

    payload = {
        "axes": {
            "x": {"key": x, "label": stats[x]["label"], "color": stats[x]["color"], "max": max_x},
            "y": {"key": y, "label": stats[y]["label"], "color": stats[y]["color"], "max": max_y},
            "z": {"key": z, "label": stats[z]["label"], "color": stats[z]["color"], "max": max_z},
        },
    }
    if legacy_max:
        payload.update(max_pts=max_x, max_reb=max_y, max_ast=max_z)
    payload["cells"] = cells
    payload["leaderboard"] = [
        {"player_id": r["player_id"], "name": r["player_name"], "n": r["unique_cells"]}
        for r in leaders
    ]
    return json.dumps(payload, separators=(",", ":"))


def serve(sport, here, db, stats, defaults, port, payload_fn):
    class Handler(SimpleHTTPRequestHandler):
        def translate_path(self, path):
            original = super().translate_path(path)
            rel = Path(original).resolve().relative_to(Path.cwd().resolve())
            return str(here / rel)

        def _send_json(self, payload_str, status=200):
            body = payload_str.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status, message):
            self._send_json(json.dumps({"error": message}), status=status)

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/stats":
                return self._send_json(json.dumps({
                    "stats": [
                        {"key": k, "label": v["label"], "color": v["color"]}
                        for k, v in stats.items()
                    ]
                }))
            if url.path == "/tally":
                qs = parse_qs(url.query)
                axes = [(qs.get(k) or [d])[0]
                        for k, d in zip("xyz", defaults)]
                try:
                    payload = payload_fn(*axes)
                except ValueError as e:
                    return self._send_error_json(400, str(e))
                except sqlite3.OperationalError as e:
                    return self._send_error_json(500, f"db error: {e}")
                return self._send_json(payload)
            if url.path in ("/", ""):
                return self._send_json(json.dumps({
                    "service": f"{sport} scorigami dev API",
                    "endpoints": ["/stats", "/tally?x=%s&y=%s&z=%s" % defaults],
                    "viewer": "serve the repo's /public/ folder and open index.html",
                }))
            return super().do_GET()

        def log_message(self, fmt, *args):
            sys.stderr.write(f"[server] {self.address_string()} - {fmt % args}\n")

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=port)
    ap.add_argument("--host", type=str, default="127.0.0.1")
    args = ap.parse_args()

    if not db.exists():
        print(f"ERROR: {db} not found. Run collect.py first.", file=sys.stderr)
        sys.exit(1)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"{sport} scorigami server on http://{args.host}:{args.port}/")
    print(f"  serving from: {here}")
    print(f"  db:           {db}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
