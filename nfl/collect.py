"""Collect every NFL player-game stat line (1999 -> current) into SQLite.

Batched: one load_player_stats + one load_schedules call covers every requested
season at once (nflreadpy caches the per-season parquets locally), joined to
attach game_id/date/matchup, then written one season at a time. Resumable —
seasons already in seasons_done are skipped; re-run with --force to refresh.

Usage:
    python collect.py                # all seasons 1999 -> current
    python collect.py --start 2020   # 2020 -> current
    python collect.py --season 2024  # only 2024
    python collect.py --force        # re-pull seasons already marked done
"""
import argparse
import sqlite3
import sys
from pathlib import Path

import nflreadpy as nfl
import polars as pl

DB_PATH = Path(__file__).resolve().parent / "nfl.sqlite"

# (db_col, source_col) — every stat we expose as a possible cube axis.
STAT_COLUMNS = [
    ("pass_cmp",  "completions"),
    ("pass_att",  "attempts"),
    ("pass_yds",  "passing_yards"),
    ("pass_td",   "passing_tds"),
    ("pass_int",  "passing_interceptions"),
    ("sacks",     "sacks_suffered"),
    ("rush_att",  "carries"),
    ("rush_yds",  "rushing_yards"),
    ("rush_td",   "rushing_tds"),
    ("rec",       "receptions"),
    ("tgt",       "targets"),
    ("rec_yds",   "receiving_yards"),
    ("rec_td",    "receiving_tds"),
]


def init_db(conn: sqlite3.Connection) -> None:
    stat_cols_sql = ",\n            ".join(f"{c} INTEGER" for c, _ in STAT_COLUMNS)
    conn.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS player_games (
            game_id        TEXT NOT NULL,
            game_date      TEXT,
            season         TEXT,
            season_type    TEXT,
            week           INTEGER,
            player_id      TEXT NOT NULL,
            player_name    TEXT,
            position       TEXT,
            team_abbr      TEXT,
            opponent       TEXT,
            matchup        TEXT,
            {stat_cols_sql},
            PRIMARY KEY (game_id, player_id)
        );

        CREATE INDEX IF NOT EXISTS idx_pg_player ON player_games(player_id);
        CREATE INDEX IF NOT EXISTS idx_pg_season ON player_games(season);

        CREATE TABLE IF NOT EXISTS seasons_done (
            season       TEXT NOT NULL,
            season_type  TEXT NOT NULL,
            n_rows       INTEGER,
            done_at      TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (season, season_type)
        );
        """
    )


def already_done(conn: sqlite3.Connection, season: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seasons_done WHERE season=? AND season_type='ALL'",
        (season,),
    ).fetchone() is not None


def fetch_all(seasons: list[int]) -> pl.DataFrame:
    """Load player stats + schedule join for every requested season in one pull.

    A player_stats row identifies its game by (season, week, team,
    opponent_team). We build a (season, week, team, opponent_team) -> game_id
    map from the schedule (one row per team per game) and left-join it on.
    """
    stats = nfl.load_player_stats(seasons=seasons)
    sched = nfl.load_schedules(seasons=seasons)
    cols = ["game_id", "season", "week", "gameday", "team", "opponent_team"]
    home = sched.rename({"home_team": "team", "away_team": "opponent_team"}).select(cols)
    away = sched.rename({"away_team": "team", "home_team": "opponent_team"}).select(cols)
    keyed = pl.concat([home, away])  # one row per (game, team)

    return stats.join(
        keyed,
        on=["season", "week", "team", "opponent_team"],
        how="left",
    )


def rows_for_insert(df: pl.DataFrame):
    """Yield tuples shaped for executemany INSERT."""
    src_cols = [s for _, s in STAT_COLUMNS]
    needed = ["game_id", "gameday", "season", "season_type", "week",
              "player_id", "player_display_name", "position", "team",
              "opponent_team"] + src_cols
    # Some columns may be missing for very old seasons; fill with None.
    for col in needed:
        if col not in df.columns:
            df = df.with_columns(pl.lit(None).alias(col))

    for r in df.iter_rows(named=True):
        if r["game_id"] is None or r["player_id"] is None:
            continue  # skip rows that didn't join (rare; usually pre-week roster moves)
        matchup = f"{r['team']} vs {r['opponent_team']}" if r["team"] else None
        stats = tuple(
            int(r[s]) if r[s] is not None else 0
            for s in src_cols
        )
        yield (
            r["game_id"],
            r["gameday"],
            str(r["season"]) if r["season"] is not None else None,
            r["season_type"],
            r["week"],
            r["player_id"],
            r["player_display_name"] or r.get("player_name"),
            r["position"],
            r["team"],
            r["opponent_team"],
            matchup,
            *stats,
        )


def insert_season(conn: sqlite3.Connection, df: pl.DataFrame) -> int:
    cols_sql = ", ".join(c for c, _ in STAT_COLUMNS)
    placeholders = ", ".join("?" for _ in STAT_COLUMNS)
    sql = (
        f"INSERT OR REPLACE INTO player_games "
        f"(game_id, game_date, season, season_type, week, "
        f" player_id, player_name, position, team_abbr, opponent, matchup, "
        f" {cols_sql}) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {placeholders})"
    )
    n = 0
    batch = []
    for row in rows_for_insert(df):
        batch.append(row)
        if len(batch) >= 1000:
            conn.executemany(sql, batch)
            n += len(batch)
            batch = []
    if batch:
        conn.executemany(sql, batch)
        n += len(batch)
    conn.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=1999)
    ap.add_argument("--end", type=int, default=nfl.get_current_season())
    ap.add_argument("--season", type=int, help="run only this season (overrides start/end)")
    ap.add_argument("--force", action="store_true", help="re-pull seasons already marked done")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    requested = [args.season] if args.season else list(range(args.start, args.end + 1))
    todo = [s for s in requested if args.force or not already_done(conn, str(s))]

    if todo:
        print(f"[fetch] {len(todo)} season(s) ({todo[0]}-{todo[-1]}) in one batched pull ...")
        try:
            df = fetch_all(todo)
        except Exception as e:
            print(f"  ERROR fetching {todo[0]}-{todo[-1]}: {e}", file=sys.stderr)
            sys.exit(1)
        # We pull REG + POST together; season_type comes from the player_stats
        # column. seasons_done is keyed on the season (season_type='ALL') —
        # split by season_type happens at query time, not here.
        for season in todo:
            n = insert_season(conn, df.filter(pl.col("season") == season))
            conn.execute(
                "INSERT OR REPLACE INTO seasons_done (season, season_type, n_rows) "
                "VALUES (?, 'ALL', ?)",
                (str(season), n),
            )
            conn.commit()
            print(f"  -> {season}: {n:,} player-game rows")
    else:
        print("All requested seasons already collected (use --force to refresh).")

    total = conn.execute("SELECT COUNT(*) FROM player_games").fetchone()[0]
    seasons_n = conn.execute("SELECT COUNT(*) FROM seasons_done").fetchone()[0]
    print(f"\nDONE. {total:,} total player-game rows across {seasons_n} seasons.")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
