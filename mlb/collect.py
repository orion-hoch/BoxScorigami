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
import requests

DB_PATH = Path(__file__).resolve().parent / "mlb.sqlite"
DEFAULT_DELAY = 0.5

# Map our cube/DB stat keys to the field names in MLB's raw boxscore JSON.
# Used by the raw-API fallback that bypasses statsapi.boxscore_data() for
# old games (pre-1920ish) where players lack a 'position' field and the
# convenience wrapper KeyError's out.
RAW_BATTING_FIELDS = [
    ("ab",      "atBats"),
    ("r",       "runs"),
    ("h",       "hits"),
    ("doubles", "doubles"),
    ("triples", "triples"),
    ("hr",      "homeRuns"),
    ("rbi",     "rbi"),
    ("bb",      "baseOnBalls"),
    ("k",       "strikeOuts"),
    ("sb",      "stolenBases"),
]

# Pitching fields in the raw boxscore JSON, mirrored into the convenience
# (boxscore_data) key names parse_pitchers reads. innings pitched is handled
# separately because it needs the "6.2" -> 20-outs conversion.
RAW_PITCHING_FIELDS = [
    ("h",  "hits"),
    ("r",  "runs"),
    ("er", "earnedRuns"),
    ("bb", "baseOnBalls"),
    ("k",  "strikeOuts"),
    ("hr", "homeRuns"),
    ("p",  "numberOfPitches"),   # ~1988+ only; absent -> empty
    ("s",  "strikes"),           # ~1988+ only; absent -> empty
]


def ip_to_outs(ip):
    """Convert MLB innings-pitched notation to a clean integer of outs.
    "6.2" -> 6*3 + 2 = 20 outs; "6.0"/"6" -> 18; "" / None -> None."""
    if ip in (None, "", "-"):
        return None
    s = str(ip)
    try:
        if "." in s:
            whole, frac = s.split(".", 1)
            return int(whole or 0) * 3 + int(frac[0])
        return int(s) * 3
    except (ValueError, IndexError):
        return None


def fetch_boxscore(game_id):
    """Pull a boxscore. Tries statsapi.boxscore_data() first (rich format),
    falls back to the raw /v1/game/{id}/boxscore endpoint when the wrapper
    crashes — common for pre-1930 games where:
      - 'position' or 'boxscoreName' fields are missing from the response
        (the convenience wrapper KeyError's out)
      - the wrapper's /v1.1/.../feed/live endpoint returns 500 (the raw
        /v1/.../boxscore endpoint is a different URL that usually works)
    If the fallback also fails, the original error is re-raised so the game
    gets logged with a useful message."""
    try:
        return statsapi.boxscore_data(game_id)
    except Exception as primary:
        try:
            return _boxscore_from_raw(game_id)
        except Exception:
            raise primary


def _boxscore_from_raw(game_id):
    """Construct a boxscore_data-compatible dict from the raw API endpoint,
    ignoring position info entirely. We only populate the keys parse_batters
    + insert_game actually read."""
    url = f"https://statsapi.mlb.com/api/v1/game/{game_id}/boxscore"
    resp = requests.get(url, timeout=20)
    # MLB's API returns 500 with a JSON error body for some games. If we
    # parsed that body and proceeded, the empty result would look like a
    # valid "no data" game; instead, fail loud so the scraper marks it
    # 'error' (retryable) rather than 'done' (empty).
    resp.raise_for_status()
    raw = resp.json()
    out = {
        "teamInfo": {
            side: {"abbreviation": (raw.get("teams", {}).get(side, {})
                                       .get("team", {}).get("abbreviation"))}
            for side in ("away", "home")
        },
        "awayBatters": [],
        "homeBatters": [],
        "awayPitchers": [],
        "homePitchers": [],
    }
    for side in ("away", "home"):
        team = raw.get("teams", {}).get(side, {})
        players = team.get("players", {})
        # team.batters is the batting order (player IDs). Older games may omit
        # it; fall back to any player with batting stats present.
        ids = team.get("batters") or [
            int(k[2:]) for k, p in players.items()
            if (p.get("stats") or {}).get("batting")
        ]
        for pid in ids:
            p = players.get(f"ID{pid}") or {}
            stats = (p.get("stats") or {}).get("batting") or {}
            row = {
                "personId": pid,
                "name": (p.get("person") or {}).get("fullName"),
            }
            for db_key, api_key in RAW_BATTING_FIELDS:
                v = stats.get(api_key)
                row[db_key] = "" if v is None else str(v)
            out[f"{side}Batters"].append(row)

        # Pitchers — mirror the convenience-format keys parse_pitchers reads.
        pit_ids = team.get("pitchers") or [
            int(k[2:]) for k, p in players.items()
            if (p.get("stats") or {}).get("pitching")
        ]
        for pid in pit_ids:
            p = players.get(f"ID{pid}") or {}
            stats = (p.get("stats") or {}).get("pitching") or {}
            row = {
                "personId": pid,
                "name": (p.get("person") or {}).get("fullName"),
                "ip": "" if stats.get("inningsPitched") is None else str(stats.get("inningsPitched")),
            }
            for db_key, api_key in RAW_PITCHING_FIELDS:
                v = stats.get(api_key)
                row[db_key] = "" if v is None else str(v)
            out[f"{side}Pitchers"].append(row)
    return out

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

# Pitching stats, stored in their own pitcher_games table (different domain —
# pitcher lines don't share an axis space with hitter lines, so this becomes
# its own cube). `outs` is innings-pitched as a clean integer; `p`/`s` (pitches
# and strikes) only exist for ~1988+ games and are NULL before that.
PITCH_COLUMNS = [
    ("k",    "k"),
    ("bb",   "bb"),
    ("h",    "h"),
    ("er",   "er"),
    ("r",    "r"),
    ("hr",   "hr"),
    ("outs", "outs"),
    ("p",    "p"),
    ("s",    "s"),
]


def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    stat_cols_sql = ",\n            ".join(f"{c} INTEGER" for c, _ in STAT_COLUMNS)
    pitch_cols_sql = ",\n            ".join(f"{c} INTEGER" for c, _ in PITCH_COLUMNS)
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

    CREATE TABLE IF NOT EXISTS pitcher_games (
        game_id        INTEGER NOT NULL,
        game_date      TEXT,
        season         INTEGER,
        game_type      TEXT,
        player_id      INTEGER NOT NULL,
        player_name    TEXT,
        team_abbr      TEXT,
        opponent       TEXT,
        matchup        TEXT,
        {pitch_cols_sql},
        PRIMARY KEY (game_id, player_id)
    );

    CREATE INDEX IF NOT EXISTS idx_pit_season ON pitcher_games(season);
    CREATE INDEX IF NOT EXISTS idx_pit_player ON pitcher_games(player_id);

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


def parse_pitchers(bd, side, team_abbr, opp_abbr):
    """Yield row dicts for one team's pitchers."""
    for p in bd.get(f"{side}Pitchers", []):
        pid = p.get("personId")
        if not pid:        # skips None and the API's personId=0 totals/header row
            continue

        def i(key):
            v = p.get(key, "")
            if v in ("", None, "-"):
                return None
            try:
                return int(v)
            except ValueError:
                return None

        yield {
            "player_id":   pid,
            "player_name": p.get("name") or p.get("namefield"),
            "team_abbr":   team_abbr,
            "opponent":    opp_abbr,
            "matchup":     f"{team_abbr} vs {opp_abbr}" if team_abbr and opp_abbr else None,
            "k":    i("k"),
            "bb":   i("bb"),
            "h":    i("h"),
            "er":   i("er"),
            "r":    i("r"),
            "hr":   i("hr"),
            "outs": ip_to_outs(p.get("ip")),
            "p":    i("p"),
            "s":    i("s"),
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

    pitch_sql = (
        "INSERT OR REPLACE INTO pitcher_games "
        "(game_id, game_date, season, game_type, player_id, player_name, "
        " team_abbr, opponent, matchup, "
        + ", ".join(c for c, _ in PITCH_COLUMNS) +
        ") VALUES ("
        ":game_id, :game_date, :season, :game_type, :player_id, :player_name, "
        ":team_abbr, :opponent, :matchup, "
        + ", ".join(f":{c}" for c, _ in PITCH_COLUMNS) +
        ")"
    )

    gmeta = {"game_id": game["game_id"], "game_date": game["game_date"],
             "season": game["season"], "game_type": game["game_type"]}

    rows = []
    for r in parse_batters(bd, "away", away_abbr, home_abbr):
        rows.append({**r, **gmeta})
    for r in parse_batters(bd, "home", home_abbr, away_abbr):
        rows.append({**r, **gmeta})
    if rows:
        conn.executemany(sql, rows)

    pitch_rows = []
    for r in parse_pitchers(bd, "away", away_abbr, home_abbr):
        pitch_rows.append({**r, **gmeta})
    for r in parse_pitchers(bd, "home", home_abbr, away_abbr):
        pitch_rows.append({**r, **gmeta})
    if pitch_rows:
        conn.executemany(pitch_sql, pitch_rows)

    # Persist the resolved abbreviations back so they're queryable without the boxscore.
    conn.execute(
        "UPDATE games_to_scrape SET home_abbr=?, away_abbr=? WHERE game_id=?",
        (home_abbr, away_abbr, game["game_id"]),
    )
    return len(rows), len(pitch_rows)


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
    skipped = 0
    recent = deque(maxlen=30)
    # Per-season empty-streak tracking. If a season produces this many
    # zero-batter games in a row, we mark the rest of that season as 'empty'
    # without burning API calls. MLB simply doesn't have batter-level data
    # digitized for some old seasons (e.g. 1931).
    EMPTY_STREAK_LIMIT = 8
    empty_streak = 0
    current_season = None
    dead_seasons = set()

    for i, g in enumerate(games, 1):
        season = g["season"]
        if season != current_season:
            empty_streak = 0
            current_season = season

        # Fast-skip games in known-dead seasons (no API call).
        if season in dead_seasons:
            conn.execute(
                "UPDATE games_to_scrape SET status='empty', error_msg='season has no batter data', scraped_at=? WHERE game_id=?",
                (dt.datetime.utcnow().isoformat(), g["game_id"]),
            )
            conn.commit()
            skipped += 1
            if skipped % 200 == 0:
                print(f"[{i:>6}/{total}] {season} {g['game_id']}  (skipped {skipped} from dead seasons)")
            continue

        t_start = time.time()
        try:
            bd = fetch_boxscore(g["game_id"])
            nb, npit = insert_game(conn, dict(g), bd)
            conn.execute(
                "UPDATE games_to_scrape SET status='done', error_msg=NULL, scraped_at=? WHERE game_id=?",
                (dt.datetime.utcnow().isoformat(), g["game_id"]),
            )
            conn.commit()
            success += 1
            if nb + npit == 0:
                empty_streak += 1
                if empty_streak >= EMPTY_STREAK_LIMIT:
                    dead_seasons.add(season)
                    print(f"  >> {season}: {empty_streak} empty games in a row — marking rest of season as 'empty'")
            else:
                empty_streak = 0
            recent.append(time.time() - t_start + delay)
            avg = sum(recent) / len(recent)
            eta_h = (total - i) * avg / 3600
            print(f"[{i:>6}/{total}] {season} {g['game_id']}  +{nb} batters +{npit} pitchers  "
                  f"(last30 avg {avg:.2f}s, eta {eta_h:.1f}h)")
        except Exception as e:
            conn.execute(
                "UPDATE games_to_scrape SET status='error', error_msg=? WHERE game_id=?",
                (str(e)[:500], g["game_id"]),
            )
            conn.commit()
            fail += 1
            print(f"[{i:>6}/{total}] {season} {g['game_id']}  ERROR: {e}", file=sys.stderr)
        time.sleep(delay)

    print(f"\nDONE. {success:,} succeeded, {fail:,} failed, {skipped:,} skipped (dead seasons: {sorted(dead_seasons)}) "
          f"in {(time.time()-t0)/3600:.2f}h")


def backfill_names(conn, delay):
    """Replace boxscore short names ("Bregman") with full names ("Alex Bregman")
    via MLB's batch people endpoint. Cheap: ~hundreds of ids per call, so all of
    history resolves in a few hundred calls. Updates both batting + pitching."""
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT player_id FROM ("
        "  SELECT player_id FROM player_games "
        "  UNION SELECT player_id FROM pitcher_games"
        ") WHERE player_id IS NOT NULL"
    )]
    total = len(ids)
    print(f"resolving full names for {total:,} players ...")
    BATCH = 300
    updated = 0
    for i in range(0, total, BATCH):
        chunk = ids[i:i + BATCH]
        try:
            resp = requests.get(
                "https://statsapi.mlb.com/api/v1/people",
                params={"personIds": ",".join(map(str, chunk))}, timeout=30,
            )
            resp.raise_for_status()
            people = resp.json().get("people", [])
        except Exception as e:
            print(f"  batch at {i} ERROR: {e}", file=sys.stderr)
            continue
        rows = [(p.get("fullName"), p.get("id")) for p in people if p.get("fullName")]
        conn.executemany("UPDATE player_games SET player_name=? WHERE player_id=?", rows)
        conn.executemany("UPDATE pitcher_games SET player_name=? WHERE player_id=?", rows)
        conn.commit()
        updated += len(rows)
        print(f"  [{min(i + BATCH, total):>6}/{total}] resolved {updated:,}")
        time.sleep(delay)
    print(f"done. updated names for {updated:,} players.")


def backfill_pitching(conn, start, end, delay):
    """Fill pitcher_games for already-'done' games (their pitching wasn't stored
    when first scraped). Re-fetches each boxscore — batting is re-written
    idempotently (INSERT OR REPLACE) and pitching is added. Resumable: a game
    drops out once its pitcher rows exist."""
    where = ("status='done' AND game_id NOT IN "
             "(SELECT DISTINCT game_id FROM pitcher_games)")
    if start is not None:
        where += f" AND season >= {start}"
    if end is not None:
        where += f" AND season <= {end}"

    games = conn.execute(
        f"SELECT * FROM games_to_scrape WHERE {where} ORDER BY season, game_date, game_id"
    ).fetchall()
    if not games:
        print("No games need pitching backfill.")
        return

    from collections import deque
    total = len(games)
    print(f"Backfilling pitching for {total:,} games...")
    t0 = time.time()
    ok = fail = empty = 0
    recent = deque(maxlen=30)
    for i, g in enumerate(games, 1):
        t_start = time.time()
        try:
            bd = fetch_boxscore(g["game_id"])
            _, npit = insert_game(conn, dict(g), bd)
            conn.commit()
            ok += 1
            if npit == 0:
                empty += 1
            recent.append(time.time() - t_start + delay)
            avg = sum(recent) / len(recent)
            eta_h = (total - i) * avg / 3600
            print(f"[{i:>6}/{total}] {g['season']} {g['game_id']}  +{npit} pitchers  "
                  f"(last30 avg {avg:.2f}s, eta {eta_h:.1f}h)")
        except Exception as e:
            fail += 1
            print(f"[{i:>6}/{total}] {g['season']} {g['game_id']}  ERROR: {e}", file=sys.stderr)
        time.sleep(delay)

    print(f"\nDONE pitching backfill. {ok:,} ok ({empty:,} had no pitchers), "
          f"{fail:,} failed in {(time.time()-t0)/3600:.2f}h")


def show_stats(conn):
    seas = conn.execute("SELECT COUNT(*) FROM seasons_enumerated").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM games_to_scrape").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='done'").fetchone()[0]
    err = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='error'").fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='pending'").fetchone()[0]
    pg = conn.execute("SELECT COUNT(*) FROM player_games").fetchone()[0]
    pit = conn.execute("SELECT COUNT(*) FROM pitcher_games").fetchone()[0]
    pit_games = conn.execute("SELECT COUNT(DISTINCT game_id) FROM pitcher_games").fetchone()[0]
    need_pitch = conn.execute(
        "SELECT COUNT(*) FROM games_to_scrape WHERE status='done' "
        "AND game_id NOT IN (SELECT DISTINCT game_id FROM pitcher_games)"
    ).fetchone()[0]
    seas_range = conn.execute("SELECT MIN(season), MAX(season) FROM seasons_enumerated").fetchone()
    print(f"seasons enumerated: {seas}  (range {seas_range[0]}–{seas_range[1]})")
    print(f"games:              {total:,}  done={done:,}  error={err:,}  pending={pend:,}")
    print(f"player-game rows:   {pg:,}")
    print(f"pitcher-game rows:  {pit:,}  (across {pit_games:,} games)")
    if need_pitch:
        print(f"  -> {need_pitch:,} done games still need pitching: run `collect.py backfill-pitching`")


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

    bp = sub.add_parser("backfill-pitching",
                        help="Fill pitcher_games for already-scraped games")
    bp.add_argument("--start", type=int)
    bp.add_argument("--end", type=int)
    bp.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    bn = sub.add_parser("backfill-names",
                        help="Replace boxscore short names with full names")
    bn.add_argument("--delay", type=float, default=DEFAULT_DELAY)

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
    elif args.cmd == "backfill-pitching":
        backfill_pitching(conn, args.start, args.end, args.delay)
    elif args.cmd == "backfill-names":
        backfill_names(conn, args.delay)

    show_stats(conn)


if __name__ == "__main__":
    main()
