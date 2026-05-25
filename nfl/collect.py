"""Collect every NFL player-game stat line (1999 -> current) into SQLite.

Uses nflreadpy.load_player_stats joined with load_schedules to attach
game_id, game_date, and matchup. One request per season (cached locally by
nflreadpy). Resumable: skips (season, season_type) pairs already done.

Usage:
    python collect.py                # full run, 1999 -> current
    python collect.py --start 2020   # start at 2020 season
    python collect.py --season 2024  # only 2024
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


def already_done(conn: sqlite3.Connection, season: str, stype: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM seasons_done WHERE season=? AND season_type=?",
        (season, stype),
    ).fetchone() is not None


def fetch_season(season: int) -> pl.DataFrame:
    """Load player stats + schedule join for a single season."""
    stats = nfl.load_player_stats(seasons=[season])
    sched = nfl.load_schedules(seasons=[season]).select([
        "game_id", "season", "week", "gameday", "home_team", "away_team",
    ])
    # A player_stats row identifies the game by (season, week, team, opponent_team).
    # Join twice — once where the player's team is home, once where away —
    # then coalesce. Simpler: build a (season, week, team_a, team_b) -> game_id map.
    cols = ["game_id", "season", "week", "gameday", "team", "opponent_team"]
    home = sched.rename({"home_team": "team", "away_team": "opponent_team"}).select(cols)
    away = sched.rename({"away_team": "team", "home_team": "opponent_team"}).select(cols)
    keyed = pl.concat([home, away])  # one row per (game, team)

    df = stats.join(
        keyed.select(["season", "week", "team", "opponent_team", "game_id", "gameday"]),
        on=["season", "week", "team", "opponent_team"],
        how="left",
    )
    return df


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

    seasons = [args.season] if args.season else range(args.start, args.end + 1)

    for season in seasons:
        season_s = str(season)
        # We pull both REG + POST in one shot, but the season_type comes from
        # the player_stats column. We treat the season as the "done" key —
        # split by season_type after fetch.
        if not args.force and already_done(conn, season_s, "ALL"):
            print(f"[skip] {season_s} (already done)")
            continue
        print(f"[fetch] {season_s} ...")
        try:
            df = fetch_season(season)
        except Exception as e:
            print(f"  ERROR fetching {season_s}: {e}", file=sys.stderr)
            continue
        n = insert_season(conn, df)
        conn.execute(
            "INSERT OR REPLACE INTO seasons_done (season, season_type, n_rows) "
            "VALUES (?, 'ALL', ?)",
            (season_s, n),
        )
        conn.commit()
        print(f"  -> {n:,} player-game rows")

    # Summary
    total = conn.execute("SELECT COUNT(*) FROM player_games").fetchone()[0]
    seasons_n = conn.execute("SELECT COUNT(*) FROM seasons_done").fetchone()[0]
    print(f"\nDONE. {total:,} total player-game rows across {seasons_n} seasons.")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
