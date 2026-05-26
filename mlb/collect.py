"""Collect every MLB player-game batting line into SQLite.

Uses MLB-StatsAPI: schedule() to enumerate game IDs per season, then
boxscore_data() per game to pull per-player batting stats. Resumable per
(season, season_type) and per game.

Data goes back to 1901. ~2K games per modern season, ~600/season pre-1962.
At 0.5s polite delay per game: a single recent season is ~30 min; the full
1901-2025 range is ~30 hours.

Usage:
    python collect.py                       # 2024 only (default test scope)
    python collect.py --start 2010 --end 2024
    python collect.py --start 1901          # full history (long)
    python collect.py --season 2024 --season-type R
"""
import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

import statsapi

DB_PATH = Path(__file__).resolve().parent / "mlb.sqlite"
DEFAULT_DELAY = 0.5

# Batting stats exposed as cube axes. Keys are the cube/URL identifiers,
# matched against the boxscore_data field names. Pitching is intentionally
# separate (different domain — pitcher stat lines don't share a space with
# hitter stat lines), to be added as its own cube later.
STAT_COLUMNS = [
    ("ab",      "ab"),
    ("r",       "r"),
    ("h",       "h"),
    ("doubles", "doubles"),
    ("triples", "triples"),
    ("hr",      "hr"),
    ("rbi",     "rbi"),
    ("bb",      "bb"),
    ("k",       "k"),
    ("sb",      "sb"),
]


def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    stat_cols_sql = ",\n            ".join(f"{c} INTEGER" for c, _ in STAT_COLUMNS)
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS player_games (
        game_id        INTEGER NOT NULL,
        game_date      TEXT,
        season         INTEGER,
        game_type      TEXT,
        player_id      INTEGER NOT NULL,
        player_name    TEXT,
        team_abbr      TEXT,
        opponent       TEXT,
        matchup        TEXT,
        {stat_cols_sql},
        PRIMARY KEY (game_id, player_id)
    );

    CREATE INDEX IF NOT EXISTS idx_pg_season ON player_games(season);
    CREATE INDEX IF NOT EXISTS idx_pg_player ON player_games(player_id);

    CREATE TABLE IF NOT EXISTS games_to_scrape (
        game_id      INTEGER PRIMARY KEY,
        game_date    TEXT,
        season       INTEGER,
        game_type    TEXT,
        home_abbr    TEXT,
        away_abbr    TEXT,
        status       TEXT DEFAULT 'pending',
        error_msg    TEXT,
        scraped_at   TEXT
    );

    CREATE TABLE IF NOT EXISTS seasons_enumerated (
        season         INTEGER PRIMARY KEY,
        season_type    TEXT,
        n_games        INTEGER,
        enumerated_at  TEXT
    );
    """)


def enumerate_season(conn, season, delay):
    """Pull game IDs for a season's regular + postseason games."""
    already = conn.execute(
        "SELECT n_games FROM seasons_enumerated WHERE season=?", (season,)
    ).fetchone()
    if already:
        print(f"[skip] {season} already enumerated ({already[0]} games)")
        return

    print(f"[fetch] schedule {season} ...")
    try:
        sched = statsapi.schedule(
            start_date=f"{season}-01-01",
            end_date=f"{season}-12-31",
        )
    except Exception as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        return

    rows = []
    for g in sched:
        # game_type: R = regular, F = wildcard, D = division, L = league, W = world series
        if g.get('status') != 'Final':
            continue
        rows.append({
            "game_id": g["game_id"],
            "game_date": g["game_date"],
            "season": season,
            "game_type": g.get("game_type"),
            # Resolve abbreviations later from boxscore (schedule has full names only).
            "home_abbr": None,
            "away_abbr": None,
        })

    conn.executemany(
        """INSERT OR IGNORE INTO games_to_scrape
           (game_id, game_date, season, game_type, home_abbr, away_abbr)
           VALUES (:game_id, :game_date, :season, :game_type, :home_abbr, :away_abbr)""",
        rows,
    )
    conn.execute(
        "INSERT OR REPLACE INTO seasons_enumerated (season, season_type, n_games, enumerated_at) "
        "VALUES (?, 'ALL', ?, ?)",
        (season, len(rows), dt.datetime.utcnow().isoformat()),
    )
    conn.commit()
    print(f"  -> {len(rows)} final games")
    time.sleep(delay)


def parse_batters(bd, side, team_abbr, opp_abbr):
    """Yield row dicts for one team's batters."""
    batters = bd.get(f"{side}Batters", [])
    for b in batters:
        pid = b.get("personId")
        if not pid or "substitution" in b and b.get("substitution") and not b.get("ab"):
            # Skip empty rows (header / total rows surface as substitution=True with no AB).
            pass
        # Walk our axis stats and pull the int value (or 0 if missing).
        def i(key):
            v = b.get(key, "")
            if v in ("", None, "-"):
                return None
            try:
                return int(v)
            except ValueError:
                return None

        if pid is None:
            continue
        yield {
            "player_id":   pid,
            "player_name": b.get("name") or b.get("namefield"),
            "team_abbr":   team_abbr,
            "opponent":    opp_abbr,
            "matchup":     f"{team_abbr} vs {opp_abbr}" if team_abbr and opp_abbr else None,
            "ab":      i("ab"),
            "r":       i("r"),
            "h":       i("h"),
            "doubles": i("doubles"),
            "triples": i("triples"),
            "hr":      i("hr"),
            "rbi":     i("rbi"),
            "bb":      i("bb"),
            "k":       i("k"),
            "sb":      i("sb"),
        }


def insert_game(conn, game, bd):
    info = bd.get("teamInfo") or {}
    home_abbr = info.get("home", {}).get("abbreviation")
    away_abbr = info.get("away", {}).get("abbreviation")

    sql = (
        "INSERT OR REPLACE INTO player_games "
        "(game_id, game_date, season, game_type, player_id, player_name, "
        " team_abbr, opponent, matchup, "
        + ", ".join(c for c, _ in STAT_COLUMNS) +
        ") VALUES ("
        ":game_id, :game_date, :season, :game_type, :player_id, :player_name, "
        ":team_abbr, :opponent, :matchup, "
        + ", ".join(f":{c}" for c, _ in STAT_COLUMNS) +
        ")"
    )

    rows = []
    for r in parse_batters(bd, "away", away_abbr, home_abbr):
        rows.append({**r, "game_id": game["game_id"], "game_date": game["game_date"],
                     "season": game["season"], "game_type": game["game_type"]})
    for r in parse_batters(bd, "home", home_abbr, away_abbr):
        rows.append({**r, "game_id": game["game_id"], "game_date": game["game_date"],
                     "season": game["season"], "game_type": game["game_type"]})
    if rows:
        conn.executemany(sql, rows)

    # Persist the resolved abbreviations back so they're queryable without the boxscore.
    conn.execute(
        "UPDATE games_to_scrape SET home_abbr=?, away_abbr=? WHERE game_id=?",
        (home_abbr, away_abbr, game["game_id"]),
    )
    return len(rows)


def scrape_games(conn, start, end, retry_failed, delay):
    where = "status = 'pending'"
    if retry_failed:
        where = "status IN ('pending','error')"
    if start is not None:
        where += f" AND season >= {start}"
    if end is not None:
        where += f" AND season <= {end}"

    games = conn.execute(
        f"SELECT * FROM games_to_scrape WHERE {where} ORDER BY season, game_date, game_id"
    ).fetchall()
    if not games:
        print("Nothing to scrape.")
        return

    from collections import deque
    total = len(games)
    print(f"Scraping {total:,} boxscores...")
    t0 = time.time()
    success = 0
    fail = 0
    recent = deque(maxlen=30)
    for i, g in enumerate(games, 1):
        t_start = time.time()
        try:
            bd = statsapi.boxscore_data(g["game_id"])
            n = insert_game(conn, dict(g), bd)
            conn.execute(
                "UPDATE games_to_scrape SET status='done', error_msg=NULL, scraped_at=? WHERE game_id=?",
                (dt.datetime.utcnow().isoformat(), g["game_id"]),
            )
            conn.commit()
            success += 1
            recent.append(time.time() - t_start + delay)
            avg = sum(recent) / len(recent)
            eta_h = (total - i) * avg / 3600
            print(f"[{i:>5}/{total}] {g['season']} {g['game_id']}  +{n} batters  "
                  f"(last30 avg {avg:.2f}s, eta {eta_h:.1f}h)")
        except Exception as e:
            conn.execute(
                "UPDATE games_to_scrape SET status='error', error_msg=? WHERE game_id=?",
                (str(e)[:500], g["game_id"]),
            )
            conn.commit()
            fail += 1
            print(f"[{i:>5}/{total}] {g['season']} {g['game_id']}  ERROR: {e}", file=sys.stderr)
        time.sleep(delay)

    print(f"\nDONE. {success:,} succeeded, {fail:,} failed in {(time.time()-t0)/3600:.2f}h")


def show_stats(conn):
    seas = conn.execute("SELECT COUNT(*) FROM seasons_enumerated").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM games_to_scrape").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='done'").fetchone()[0]
    err = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='error'").fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='pending'").fetchone()[0]
    pg = conn.execute("SELECT COUNT(*) FROM player_games").fetchone()[0]
    seas_range = conn.execute("SELECT MIN(season), MAX(season) FROM seasons_enumerated").fetchone()
    print(f"seasons enumerated: {seas}  (range {seas_range[0]}–{seas_range[1]})")
    print(f"games:              {total:,}  done={done:,}  error={err:,}  pending={pend:,}")
    print(f"player-game rows:   {pg:,}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    e = sub.add_parser("enumerate", help="Pull game IDs from schedule()")
    e.add_argument("--start", type=int, default=2024)
    e.add_argument("--end", type=int, default=2024)
    e.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    s = sub.add_parser("scrape", help="Pull each pending boxscore")
    s.add_argument("--start", type=int)
    s.add_argument("--end", type=int)
    s.add_argument("--retry-failed", action="store_true")
    s.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    a = sub.add_parser("all", help="enumerate + scrape in one go")
    a.add_argument("--start", type=int, default=2024)
    a.add_argument("--end", type=int, default=2024)
    a.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    sub.add_parser("stats", help="show progress")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return

    conn = open_db()
    init_db(conn)

    if args.cmd == "stats":
        show_stats(conn)
        return
    if args.cmd == "enumerate":
        for y in range(args.start, args.end + 1):
            enumerate_season(conn, y, args.delay)
    elif args.cmd == "scrape":
        scrape_games(conn, args.start, args.end, args.retry_failed, args.delay)
    elif args.cmd == "all":
        for y in range(args.start, args.end + 1):
            enumerate_season(conn, y, args.delay)
        scrape_games(conn, args.start, args.end, False, args.delay)

    show_stats(conn)


if __name__ == "__main__":
    main()
