"""Build the (pts, reb, ast) tally table from player_games.

Writes a pra_tally table with one row per unique combo: count, first/last
dates, info on the most recent occurrence, and a sample of earliest
game/player IDs.

Usage:
    python tally.py                 # rebuild from all player_games
    python tally.py --sample 10     # keep up to 10 sample game_ids per cell
    python tally.py --export-csv    # dump pra_tally.csv
    python tally.py --export-json   # dump pra_cube.json for the 3D viz
"""
import argparse
import csv
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "scorigami.sqlite"
CSV_PATH = Path(__file__).resolve().parent / "pra_tally.csv"
JSON_PATH = Path(__file__).resolve().parent / "pra_cube.json"


def build_tally(conn: sqlite3.Connection, sample_size: int) -> int:
    conn.executescript(
        """
        DROP TABLE IF EXISTS pra_tally;
        CREATE TABLE pra_tally (
            pts               INTEGER NOT NULL,
            reb               INTEGER NOT NULL,
            ast               INTEGER NOT NULL,
            n                 INTEGER NOT NULL,
            first_date        TEXT,
            last_date         TEXT,
            last_game_id      TEXT,
            last_player_id    INTEGER,
            last_player_name  TEXT,
            last_team_abbr    TEXT,
            last_matchup      TEXT,
            sample_games      TEXT,   -- "GAME_ID:PLAYER_ID,..." (earliest)
            PRIMARY KEY (pts, reb, ast)
        );
        """
    )
    cur = conn.execute(
        """
        SELECT pts, reb, ast,
               COUNT(*)      AS n,
               MIN(game_date) AS first_date,
               MAX(game_date) AS last_date
        FROM player_games
        WHERE pts IS NOT NULL AND reb IS NOT NULL AND ast IS NOT NULL
        GROUP BY pts, reb, ast
        """
    )
    cells = cur.fetchall()

    rows_out = []
    for pts, reb, ast, n, first_date, last_date in cells:
        # Most recent occurrence (tiebreak by game_id for determinism)
        latest = conn.execute(
            """SELECT game_id, player_id, player_name, team_abbr, matchup
               FROM player_games
               WHERE pts=? AND reb=? AND ast=?
               ORDER BY game_date DESC, game_id DESC LIMIT 1""",
            (pts, reb, ast),
        ).fetchone()
        last_game_id, last_pid, last_pname, last_team, last_matchup = (
            latest if latest else (None, None, None, None, None)
        )
        sample = conn.execute(
            """SELECT game_id, player_id FROM player_games
               WHERE pts=? AND reb=? AND ast=?
               ORDER BY game_date LIMIT ?""",
            (pts, reb, ast, sample_size),
        ).fetchall()
        sample_str = ",".join(f"{g}:{p}" for g, p in sample)
        rows_out.append(
            (pts, reb, ast, n, first_date, last_date,
             last_game_id, last_pid, last_pname, last_team, last_matchup, sample_str)
        )

    conn.executemany(
        """INSERT INTO pra_tally
           (pts, reb, ast, n, first_date, last_date,
            last_game_id, last_player_id, last_player_name, last_team_abbr, last_matchup,
            sample_games)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows_out,
    )
    conn.commit()
    return len(rows_out)


def build_leaderboard(conn: sqlite3.Connection, top_n: int = 50) -> list[dict]:
    """Players ranked by number of n=1 (unique-scorigami) cells they own.

    When pra_tally.n == 1, exactly one player_games row matches that cell,
    so the join produces one row per scorigami cell and grouping by player
    counts how many of those unique cells each player owns.
    """
    cur = conn.execute(
        """
        SELECT p.player_id, p.player_name, COUNT(*) AS unique_cells
        FROM player_games p
        JOIN pra_tally t
          ON p.pts = t.pts AND p.reb = t.reb AND p.ast = t.ast
        WHERE t.n = 1
        GROUP BY p.player_id, p.player_name
        ORDER BY unique_cells DESC, p.player_name
        LIMIT ?
        """,
        (top_n,),
    )
    return [
        {"player_id": pid, "name": name, "n": count}
        for pid, name, count in cur.fetchall()
    ]


def export_json(conn: sqlite3.Connection) -> None:
    """Write a compact JSON for the 3D viz: cells + bounding box + leaderboard."""
    rows = conn.execute(
        """SELECT pts, reb, ast, n, last_date, last_player_name,
                  last_team_abbr, last_matchup, last_game_id
           FROM pra_tally"""
    ).fetchall()
    cells = [
        {
            "p": r[0], "r": r[1], "a": r[2], "n": r[3],
            "d": r[4], "pl": r[5], "t": r[6], "m": r[7], "g": r[8],
        }
        for r in rows
    ]
    bbox = conn.execute(
        "SELECT MAX(pts), MAX(reb), MAX(ast) FROM pra_tally"
    ).fetchone()
    leaderboard = build_leaderboard(conn, top_n=50)
    payload = {
        "max_pts": bbox[0], "max_reb": bbox[1], "max_ast": bbox[2],
        "cells": cells,
        "leaderboard": leaderboard,
    }
    with JSON_PATH.open("w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"JSON: {JSON_PATH}  ({len(cells)} cells, bbox {bbox}, {len(leaderboard)} leaders)")


def export_csv(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """SELECT pts, reb, ast, n, first_date, last_date,
                  last_game_id, last_player_name, last_team_abbr, last_matchup, sample_games
           FROM pra_tally ORDER BY pts, reb, ast"""
    ).fetchall()
    with CSV_PATH.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "pts", "reb", "ast", "n", "first_date", "last_date",
            "last_game_id", "last_player", "last_team", "last_matchup", "sample_games",
        ])
        w.writerows(rows)
    print(f"CSV: {CSV_PATH}  ({len(rows)} rows)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=5, help="sample game_id:player_id pairs to keep per cell")
    ap.add_argument("--export-csv", action="store_true")
    ap.add_argument("--export-json", action="store_true", help="write pra_cube.json for the 3D viz")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    n_cells = build_tally(conn, args.sample)
    print(f"pra_tally rebuilt: {n_cells} unique (pts, reb, ast) cells")

    # Quick summary
    summary = conn.execute(
        """SELECT COUNT(*) AS cells,
                  SUM(n) AS total_games,
                  MAX(n) AS max_count,
                  MAX(pts) AS max_pts, MAX(reb) AS max_reb, MAX(ast) AS max_ast
           FROM pra_tally"""
    ).fetchone()
    print(f"  cells={summary[0]}  total_player_games={summary[1]}")
    print(f"  max_count_in_a_cell={summary[2]}")
    print(f"  max_pts={summary[3]}  max_reb={summary[4]}  max_ast={summary[5]}")

    if args.export_csv:
        export_csv(conn)
    if args.export_json:
        export_json(conn)


if __name__ == "__main__":
    main()
