"""Fetch WNBA player-game box scores into wnba.sqlite."""
import argparse
import sqlite3
import time
from pathlib import Path

from nba_api.stats.endpoints import leaguegamelog
from nba_api.stats.library.parameters import PlayerOrTeamAbbreviation, SeasonTypePlayoffs

DB_PATH = Path(__file__).resolve().parent / "wnba.sqlite"
LEAGUE_ID_WNBA = "10"
REQUEST_PAUSE_SEC = 0.7
RETRY_MAX = 4
SEASON_TYPES = [SeasonTypePlayoffs.regular, SeasonTypePlayoffs.playoffs]
FIRST_SEASON = 1997


def season_str(year: int) -> str:
    return str(year)


STAT_COLUMNS = [
    "pts", "reb", "ast",
    "stl", "blk", "tov", "pf",
    "fgm", "fga", "fg3m", "fg3a", "ftm", "fta",
]


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS player_games (
            game_id        TEXT NOT NULL,
            game_date      TEXT,
            season         TEXT,
            season_type    TEXT,
            player_id      INTEGER NOT NULL,
            player_name    TEXT,
            team_id        INTEGER,
            team_abbr      TEXT,
            matchup        TEXT,
            min            REAL,
            pts            INTEGER,
            reb            INTEGER,
            ast            INTEGER,
            stl            INTEGER,
            blk            INTEGER,
            tov            INTEGER,
            pf             INTEGER,
            fgm            INTEGER,
            fga            INTEGER,
            fg3m           INTEGER,
            fg3a           INTEGER,
            ftm            INTEGER,
            fta            INTEGER,
            PRIMARY KEY (game_id, player_id)
        );
        CREATE INDEX IF NOT EXISTS idx_pra ON player_games (pts, reb, ast);
        CREATE INDEX IF NOT EXISTS idx_season ON player_games (season);
        CREATE INDEX IF NOT EXISTS idx_player ON player_games (player_id);

        CREATE TABLE IF NOT EXISTS seasons_done (
            season       TEXT NOT NULL,
            season_type  TEXT NOT NULL,
            row_count    INTEGER,
            fetched_at   TEXT,
            PRIMARY KEY (season, season_type)
        );
        """
    )
    existing = {row[1] for row in conn.execute("PRAGMA table_info(player_games)")}
    for col in STAT_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE player_games ADD COLUMN {col} INTEGER")
    conn.commit()


def fetch_season(season: str, season_type: str):
    last_err = None
    for attempt in range(RETRY_MAX):
        try:
            ep = leaguegamelog.LeagueGameLog(
                league_id=LEAGUE_ID_WNBA,
                season=season,
                season_type_all_star=season_type,
                player_or_team_abbreviation=PlayerOrTeamAbbreviation.player,
                timeout=60,
            )
            ds = ep.league_game_log.get_dict()
            headers = ds["headers"]
            rows = ds["data"]
            return [dict(zip(headers, r)) for r in rows]
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            print(f"  ! {season} {season_type} attempt {attempt + 1} failed: {e}; sleeping {wait}s")
            time.sleep(wait)
    raise RuntimeError(f"Giving up on {season} {season_type}: {last_err}")


def upsert_rows(conn: sqlite3.Connection, season: str, season_type: str, rows: list[dict]) -> int:
    payload = []
    for r in rows:
        pts = r.get("PTS")
        reb = r.get("REB")
        ast = r.get("AST")
        if pts is None and reb is None and ast is None:
            continue
        minutes = r.get("MIN")
        if minutes is not None and float(minutes) == 0:
            continue
        payload.append(
            (
                r.get("GAME_ID"),
                r.get("GAME_DATE"),
                season,
                season_type,
                r.get("PLAYER_ID"),
                r.get("PLAYER_NAME"),
                r.get("TEAM_ID"),
                r.get("TEAM_ABBREVIATION"),
                r.get("MATCHUP"),
                r.get("MIN"),
                pts,
                reb,
                ast,
                r.get("STL"),
                r.get("BLK"),
                r.get("TOV"),
                r.get("PF"),
                r.get("FGM"),
                r.get("FGA"),
                r.get("FG3M"),
                r.get("FG3A"),
                r.get("FTM"),
                r.get("FTA"),
            )
        )
    conn.executemany(
        """INSERT OR REPLACE INTO player_games
           (game_id, game_date, season, season_type, player_id, player_name,
            team_id, team_abbr, matchup, min, pts, reb, ast,
            stl, blk, tov, pf, fgm, fga, fg3m, fg3a, ftm, fta)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        payload,
    )
    conn.execute(
        """INSERT OR REPLACE INTO seasons_done (season, season_type, row_count, fetched_at)
           VALUES (?, ?, ?, datetime('now'))""",
        (season, season_type, len(payload)),
    )
    conn.commit()
    return len(payload)


def already_done(conn: sqlite3.Connection, season: str, season_type: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM seasons_done WHERE season=? AND season_type=?",
        (season, season_type),
    )
    return cur.fetchone() is not None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, default=FIRST_SEASON, help="first season year (default 1997)")
    ap.add_argument("--end", type=int, default=2026, help="last season year inclusive (default 2026)")
    ap.add_argument("--season", type=str, help="single season e.g. 2024 (overrides --start/--end)")
    ap.add_argument("--season-type", type=str, choices=SEASON_TYPES + ["both"], default="both")
    ap.add_argument("--force", action="store_true", help="re-fetch even if already in seasons_done")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.season:
        seasons = [args.season]
    else:
        seasons = [season_str(y) for y in range(args.start, args.end + 1)]

    types = SEASON_TYPES if args.season_type == "both" else [args.season_type]

    total_rows = 0
    for season in seasons:
        for st in types:
            if not args.force and already_done(conn, season, st):
                print(f"= {season} {st}: already done, skipping")
                continue
            print(f"-> {season} {st} ... ", end="", flush=True)
            t0 = time.time()
            rows = fetch_season(season, st)
            n = upsert_rows(conn, season, st, rows)
            total_rows += n
            print(f"{n} rows  ({time.time() - t0:.1f}s)")
            time.sleep(REQUEST_PAUSE_SEC)

    print(f"\nDone. Inserted/updated {total_rows} rows this run.")
    print(f"DB: {DB_PATH}")


if __name__ == "__main__":
    main()
