"""MLB BoxScorigami — one script for the whole pipeline (collect → build → serve).

Uses MLB-StatsAPI. Everything is resumable/idempotent and writes to mlb.sqlite.

Adding a current season (fast, batched — recommended):
    python collect.py season-batch --season 2026     # player_games + pitcher_games (full)
    python collect.py game-positions --start 2026 --end 2026
    python collect.py game-batting   --start 2026 --end 2026
    python collect.py build                          # rebuild cube dumps for the site

Commands:
  collect (boxscore, legacy/full-history):
    enumerate / scrape / all      schedule() + per-game boxscores
    backfill-pitching             pitcher_games for already-scraped games
    backfill-names                short names -> full names
    backfill-positions            per-season most-played position
  collect (batched, bulk gameLog — 100 players/call):
    season-batch --season Y       player_games + pitcher_games (incl. wp/bk/won/sv/...)
    game-positions [--start --end]  per-game fielding position
    game-batting   [--start --end]  pa/ibb/cs/hbp/sf/sh/gidp/lob (for OBP etc.)
    pitcher-extras [--start --end]  wp/bk/won/sv/bs/sho/cg (only needed for old
                                    boxscore-scraped seasons; season-batch fills these)
  build/serve:
    build [--positions … --mode … --no-rebuild]   unified tables + per-position dumps
    serve [--port 8777]           no-cache static dev server on public/
    stats                         show progress
"""
import argparse
import datetime as dt
import gzip
import json
import sqlite3
import sys
import time
from pathlib import Path

import statsapi
import requests

DB_PATH = Path(__file__).resolve().parent / "mlb.sqlite"
OUT_ROOT = Path(__file__).resolve().parent.parent / "public" / "mlb"
DEFAULT_DELAY = 0.5
BULK_BATCH = 100   # players per bulk /people gameLog call


def _to_int(v):
    """Parse an API stat value to int, or None when blank/missing."""
    if v in (None, "", "-", ".---", "-.--"):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None

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
        scraped_at   TEXT,
        pitch_status     TEXT,
        pitch_error      TEXT,
        pitch_scraped_at TEXT
    );

    CREATE TABLE IF NOT EXISTS seasons_enumerated (
        season         INTEGER PRIMARY KEY,
        season_type    TEXT,
        n_games        INTEGER,
        enumerated_at  TEXT
    );

    -- Each player's most-played fielding position per season (from the people
    -- yearByYear fielding endpoint). Lets the cube tag a player-season — and,
    -- by extension, every game in that season — with one clean position.
    CREATE TABLE IF NOT EXISTS season_position (
        player_id  INTEGER NOT NULL,
        season     INTEGER NOT NULL,
        position   TEXT,
        PRIMARY KEY (player_id, season)
    );

    -- Marks players already processed by backfill-positions (even those with no
    -- fielding splits) so they aren't re-fetched every run.
    CREATE TABLE IF NOT EXISTS players_positioned (
        player_id  INTEGER PRIMARY KEY,
        status     TEXT,
        error_msg  TEXT,
        fetched_at TEXT
    );
    """)


def migrate_db(conn):
    """Add the pitching-backfill tracking columns to a pre-existing DB and seed
    them once. Without a marker, games that produce zero pitcher rows (old
    seasons, empty boxscores) never leave the backfill queue and get re-fetched
    on every run. Idempotent: only seeds on the run that first adds the column."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(games_to_scrape)")}
    added = False
    if "pitch_status" not in cols:
        conn.execute("ALTER TABLE games_to_scrape ADD COLUMN pitch_status TEXT")
        added = True
    if "pitch_error" not in cols:
        conn.execute("ALTER TABLE games_to_scrape ADD COLUMN pitch_error TEXT")
    if "pitch_scraped_at" not in cols:
        conn.execute("ALTER TABLE games_to_scrape ADD COLUMN pitch_scraped_at TEXT")
    if added:
        # Anything that already has pitcher rows was clearly backfilled — mark it
        # done so the new marker matches the old "NOT IN pitcher_games" behavior.
        n = conn.execute(
            "UPDATE games_to_scrape SET pitch_status='done' "
            "WHERE pitch_status IS NULL "
            "AND game_id IN (SELECT DISTINCT game_id FROM pitcher_games)"
        ).rowcount
        print(f"[migrate] added pitching-backfill tracking; "
              f"marked {n:,} already-backfilled games as done")
    conn.commit()


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


def backfill_pitching(conn, start, end, delay, retry_failed=False):
    """Fill pitcher_games for already-'done' games (their pitching wasn't stored
    when first scraped). Re-fetches each boxscore — batting is re-written
    idempotently (INSERT OR REPLACE) and pitching is added. Resumable via the
    pitch_status marker: a game drops out once it's been processed, EVEN IF it
    had no pitchers (so empty/old games aren't re-fetched every run). Pass
    retry_failed to also re-attempt games whose last backfill errored."""
    where = "status='done' AND pitch_status IS NULL"
    if retry_failed:
        where = "status='done' AND (pitch_status IS NULL OR pitch_status='error')"
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
            # Mark processed regardless of pitcher count so a 0-pitcher game
            # isn't re-fetched on the next run.
            conn.execute(
                "UPDATE games_to_scrape SET pitch_status='done', pitch_error=NULL, "
                "pitch_scraped_at=? WHERE game_id=?",
                (dt.datetime.utcnow().isoformat(), g["game_id"]),
            )
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
            conn.execute(
                "UPDATE games_to_scrape SET pitch_status='error', pitch_error=? WHERE game_id=?",
                (str(e)[:500], g["game_id"]),
            )
            conn.commit()
            fail += 1
            print(f"[{i:>6}/{total}] {g['season']} {g['game_id']}  ERROR: {e}", file=sys.stderr)
        time.sleep(delay)

    print(f"\nDONE pitching backfill. {ok:,} ok ({empty:,} had no pitchers), "
          f"{fail:,} failed in {(time.time()-t0)/3600:.2f}h")


def _season_positions_from_fielding(payload):
    """From a yearByYear fielding stats payload, return {season(int): abbr},
    picking each season's most-played (max games) fielding position."""
    best = {}  # season -> (games, abbr)
    try:
        splits = payload["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return {}
    for s in splits:
        st = s.get("stat") or {}
        abbr = (st.get("position") or {}).get("abbreviation")
        if not abbr:
            continue
        try:
            season = int(s.get("season"))
        except (TypeError, ValueError):
            continue
        try:
            games = int(st.get("games") or 0)
        except (TypeError, ValueError):
            games = 0
        if season not in best or games > best[season][0]:
            best[season] = (games, abbr)
    return {season: abbr for season, (_, abbr) in best.items()}


def backfill_positions(conn, delay, retry_failed=False):
    """Record each player's per-season most-played fielding position via the
    people yearByYear fielding endpoint — ONE call per player, no game re-scrape.
    Resumable: players_positioned is marked for every processed player (even
    those with no fielding splits) so they aren't re-fetched on the next run."""
    where = "pp.player_id IS NULL"
    if retry_failed:
        where = "(pp.player_id IS NULL OR pp.status='error')"
    players = [r[0] for r in conn.execute(
        f"""SELECT DISTINCT pg.player_id
            FROM player_games pg
            LEFT JOIN players_positioned pp ON pp.player_id = pg.player_id
            WHERE pg.player_id IS NOT NULL AND {where}
            ORDER BY pg.player_id"""
    )]
    if not players:
        print("No players need position backfill.")
        return

    BATCH = 100  # players per /people call; ~100 IDs keeps the URL well under limits
    total = len(players)
    nbatches = (total + BATCH - 1) // BATCH
    print(f"Backfilling season positions for {total:,} players in {nbatches:,} batches of {BATCH}...")
    t0 = time.time()
    ok = fail = 0
    now = dt.datetime.utcnow().isoformat()
    for bi in range(nbatches):
        batch = players[bi * BATCH:(bi + 1) * BATCH]
        t_start = time.time()
        try:
            resp = requests.get(
                "https://statsapi.mlb.com/api/v1/people",
                params={"personIds": ",".join(str(p) for p in batch),
                        "hydrate": "stats(group=[fielding],type=[yearByYear])"},
                timeout=60,
            )
            resp.raise_for_status()
            people = resp.json().get("people", [])
            sp_rows = []
            for person in people:
                pid = person.get("id")
                if pid is None:
                    continue
                seas_pos = _season_positions_from_fielding({"stats": person.get("stats") or []})
                sp_rows.extend((pid, s, p) for s, p in seas_pos.items())
            conn.executemany(
                "INSERT OR REPLACE INTO season_position (player_id, season, position) VALUES (?,?,?)",
                sp_rows,
            )
            # Mark EVERY requested id done — including any the API omitted (no
            # fielding record) — so they don't get re-queued on the next run.
            conn.executemany(
                "INSERT OR REPLACE INTO players_positioned (player_id, status, error_msg, fetched_at) "
                "VALUES (?, 'done', NULL, ?)",
                [(p, now) for p in batch],
            )
            conn.commit()
            ok += len(batch)
            avg = (time.time() - t0) / (bi + 1)
            eta_h = (nbatches - bi - 1) * avg / 3600
            print(f"[batch {bi+1:>4}/{nbatches}] {ok:,} players  "
                  f"+{len(sp_rows)} season-rows  ({len(people)}/{len(batch)} returned, "
                  f"{time.time()-t_start:.2f}s, eta {eta_h:.2f}h)")
        except Exception as e:
            conn.executemany(
                "INSERT OR REPLACE INTO players_positioned (player_id, status, error_msg, fetched_at) "
                "VALUES (?, 'error', ?, ?)",
                [(p, str(e)[:500], now) for p in batch],
            )
            conn.commit()
            fail += len(batch)
            print(f"[batch {bi+1:>4}/{nbatches}] ERROR: {e}", file=sys.stderr)
        time.sleep(delay)

    print(f"\nDONE positions. {ok:,} ok, {fail:,} failed in {(time.time()-t0)/3600:.2f}h")


def show_stats(conn):
    seas = conn.execute("SELECT COUNT(*) FROM seasons_enumerated").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM games_to_scrape").fetchone()[0]
    done = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='done'").fetchone()[0]
    err = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='error'").fetchone()[0]
    pend = conn.execute("SELECT COUNT(*) FROM games_to_scrape WHERE status='pending'").fetchone()[0]
    pg = conn.execute("SELECT COUNT(*) FROM player_games").fetchone()[0]
    pit = conn.execute("SELECT COUNT(*) FROM pitcher_games").fetchone()[0]
    pit_games = conn.execute("SELECT COUNT(DISTINCT game_id) FROM pitcher_games").fetchone()[0]
    sp_rows = conn.execute("SELECT COUNT(*) FROM season_position").fetchone()[0]
    sp_players = conn.execute("SELECT COUNT(*) FROM players_positioned WHERE status='done'").fetchone()[0]
    sp_need = conn.execute(
        "SELECT COUNT(DISTINCT pg.player_id) FROM player_games pg "
        "LEFT JOIN players_positioned pp ON pp.player_id = pg.player_id "
        "WHERE pg.player_id IS NOT NULL AND pp.player_id IS NULL"
    ).fetchone()[0]
    need_pitch = conn.execute(
        "SELECT COUNT(*) FROM games_to_scrape WHERE status='done' AND pitch_status IS NULL"
    ).fetchone()[0]
    pitch_err = conn.execute(
        "SELECT COUNT(*) FROM games_to_scrape WHERE pitch_status='error'"
    ).fetchone()[0]
    seas_range = conn.execute("SELECT MIN(season), MAX(season) FROM seasons_enumerated").fetchone()
    print(f"seasons enumerated: {seas}  (range {seas_range[0]}–{seas_range[1]})")
    print(f"games:              {total:,}  done={done:,}  error={err:,}  pending={pend:,}")
    print(f"player-game rows:   {pg:,}")
    print(f"pitcher-game rows:  {pit:,}  (across {pit_games:,} games)")
    print(f"season positions:   {sp_rows:,} player-seasons  ({sp_players:,} players resolved)")
    if sp_need:
        print(f"  -> {sp_need:,} players still need positions: run `collect.py backfill-positions`")
    if need_pitch:
        print(f"  -> {need_pitch:,} done games still need pitching: run `collect.py backfill-pitching`")
    if pitch_err:
        print(f"  -> {pitch_err:,} games errored on pitching backfill: retry with `backfill-pitching --retry-failed`")


# ============================================================================
# season-batch — fill player_games + pitcher_games for a season via bulk
# gameLogs (one /people call per 100 players) instead of per-game boxscores.
# The pitching gameLog carries the full line, so pitcher_games is populated
# COMPLETE (incl. wp/bk/won/sv/bs/sho/cg) — no separate backfill needed.
# ============================================================================
SB_GAME_TYPES = "R,D,L,W,F"
SB_HIT_FIELDS = [
    ("ab", "atBats"), ("r", "runs"), ("h", "hits"), ("doubles", "doubles"),
    ("triples", "triples"), ("hr", "homeRuns"), ("rbi", "rbi"),
    ("bb", "baseOnBalls"), ("k", "strikeOuts"), ("sb", "stolenBases"),
]
SB_PIT_FIELDS = [
    ("k", "strikeOuts"), ("bb", "baseOnBalls"), ("h", "hits"), ("er", "earnedRuns"),
    ("r", "runs"), ("hr", "homeRuns"), ("p", "numberOfPitches"), ("s", "strikes"),
    ("wp", "wildPitches"), ("bk", "balks"), ("sv", "saves"), ("bs", "blownSaves"),
    ("sho", "shutouts"), ("cg", "completeGames"),
]


def sb_team_map():
    r = requests.get("https://statsapi.mlb.com/api/v1/teams",
                     params={"sportId": 1}, timeout=30)
    r.raise_for_status()
    return {t["id"]: t.get("abbreviation") for t in r.json()["teams"]}


def sb_player_ids(season):
    r = requests.get("https://statsapi.mlb.com/api/v1/sports/1/players",
                     params={"season": season}, timeout=60)
    r.raise_for_status()
    return [p["id"] for p in r.json().get("people", [])]


def sb_meta(split, teams):
    gid = (split.get("game") or {}).get("gamePk")
    date = split.get("date")
    gtype = split.get("gameType")
    team = teams.get((split.get("team") or {}).get("id"))
    opp = teams.get((split.get("opponent") or {}).get("id"))
    matchup = f"{team} vs {opp}" if team and opp else None
    home, away = (team, opp) if split.get("isHome") else (opp, team)
    return gid, date, gtype, team, opp, matchup, home, away


def season_batch(conn, season, delay):
    pe_ensure_schema(conn)
    teams = sb_team_map()
    pids = sb_player_ids(season)
    print(f"{len(teams)} teams, {len(pids):,} {season} players; pulling gameLogs "
          f"in {(len(pids)+BULK_BATCH-1)//BULK_BATCH} batches of {BULK_BATCH}...")
    pg_rows, pit_rows, games = [], [], {}
    t0 = time.time()
    for bi in range(0, len(pids), BULK_BATCH):
        batch = pids[bi:bi + BULK_BATCH]
        try:
            resp = requests.get(
                "https://statsapi.mlb.com/api/v1/people",
                params={"personIds": ",".join(map(str, batch)),
                        "hydrate": f"stats(group=[hitting,pitching],type=[gameLog],"
                                   f"season={season},gameType=[{SB_GAME_TYPES}])"},
                timeout=90,
            )
            resp.raise_for_status()
        except Exception as e:
            print(f"  [batch {bi//BULK_BATCH+1}] ERROR: {e}", file=sys.stderr)
            time.sleep(delay)
            continue
        for person in resp.json().get("people", []):
            pid, name = person.get("id"), person.get("fullName")
            for blk in person.get("stats", []):
                grp = (blk.get("group") or {}).get("displayName")
                for s in blk.get("splits", []):
                    gid, date, gtype, team, opp, matchup, home, away = sb_meta(s, teams)
                    if gid is None:
                        continue
                    st = s.get("stat") or {}
                    games[gid] = (date, season, gtype, home, away)
                    base = {"game_id": gid, "game_date": date, "season": season,
                            "game_type": gtype, "player_id": pid, "player_name": name,
                            "team_abbr": team, "opponent": opp, "matchup": matchup}
                    if grp == "hitting":
                        row = dict(base)
                        for col, api in SB_HIT_FIELDS:
                            row[col] = _to_int(st.get(api))
                        pg_rows.append(row)
                    elif grp == "pitching":
                        wins = _to_int(st.get("wins")) or 0
                        losses = _to_int(st.get("losses")) or 0
                        row = dict(base, outs=ip_to_outs(st.get("inningsPitched")),
                                   won=1 if wins else (0 if losses else None))
                        for col, api in SB_PIT_FIELDS:
                            row[col] = _to_int(st.get(api))
                        pit_rows.append(row)
        done = min(bi + BULK_BATCH, len(pids))
        print(f"  [{done:>5}/{len(pids)}] players  pg={len(pg_rows):,} "
              f"pit={len(pit_rows):,} games={len(games):,}  ({time.time()-t0:.0f}s)")
        time.sleep(delay)

    now = dt.datetime.utcnow().isoformat()
    conn.executemany(
        """INSERT OR REPLACE INTO games_to_scrape
           (game_id, game_date, season, game_type, home_abbr, away_abbr,
            status, scraped_at, pitch_status, pitch_scraped_at)
           VALUES (?,?,?,?,?,?, 'done', ?, 'done', ?)""",
        [(gid, d, s, gt, h, a, now, now) for gid, (d, s, gt, h, a) in games.items()],
    )
    pg_cols = ["game_id", "game_date", "season", "game_type", "player_id",
               "player_name", "team_abbr", "opponent", "matchup"] + [c for c, _ in SB_HIT_FIELDS]
    conn.executemany(
        f"INSERT OR REPLACE INTO player_games ({','.join(pg_cols)}) "
        f"VALUES ({','.join(':'+c for c in pg_cols)})", pg_rows)
    pit_cols = ["game_id", "game_date", "season", "game_type", "player_id",
                "player_name", "team_abbr", "opponent", "matchup", "outs", "won"] \
        + [c for c, _ in SB_PIT_FIELDS]
    conn.executemany(
        f"INSERT OR REPLACE INTO pitcher_games ({','.join(pit_cols)}) "
        f"VALUES ({','.join(':'+c for c in pit_cols)})", pit_rows)
    conn.commit()
    print(f"\nDONE {season}: {len(pg_rows):,} batting rows, {len(pit_rows):,} pitching "
          f"rows, {len(games):,} games in {(time.time()-t0)/60:.1f}m")


# ============================================================================
# game-positions — per-game fielding position from the bulk fielding gameLog.
# ============================================================================
def _innings_to_outs(ip):
    if ip in (None, "", "-"):
        return 0
    s = str(ip)
    try:
        if "." in s:
            whole, frac = s.split(".", 1)
            return int(whole or 0) * 3 + int(frac[0])
        return int(s) * 3
    except (ValueError, IndexError):
        return 0


def gp_ensure_tables(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS game_position (
               game_id INTEGER NOT NULL, player_id INTEGER NOT NULL,
               season INTEGER, game_date TEXT, position TEXT,
               PRIMARY KEY (game_id, player_id))""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS game_positions_done (
               player_id INTEGER NOT NULL, season INTEGER NOT NULL,
               status TEXT, error_msg TEXT, fetched_at TEXT,
               PRIMARY KEY (player_id, season))""")
    conn.commit()


def gp_from_gamelog(person, valid_games):
    """{game_id: (date, position)} keeping the max-innings position per game."""
    try:
        splits = person["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return {}
    best = {}
    for s in splits:
        gid = (s.get("game") or {}).get("gamePk")
        if gid is None or (valid_games is not None and gid not in valid_games):
            continue
        st = s.get("stat") or {}
        abbr = (st.get("position") or {}).get("abbreviation")
        if not abbr:
            continue
        outs = _innings_to_outs(st.get("innings"))
        started = _to_int(st.get("gamesStarted")) or 0
        if gid not in best or (outs, started) > best[gid][:2]:
            best[gid] = (outs, started, s.get("date"), abbr)
    return {gid: (d, a) for gid, (_, _, d, a) in best.items()}


def game_positions(conn, start, end, delay, retry_failed=False):
    gp_ensure_tables(conn)
    seasons = [r[0] for r in conn.execute(
        "SELECT DISTINCT season FROM player_games WHERE season IS NOT NULL "
        "AND season BETWEEN ? AND ? ORDER BY season", (start, end))]
    if not seasons:
        print(f"No seasons in range {start}-{end}.")
        return
    valid_games = {r[0] for r in conn.execute(
        "SELECT game_id FROM games_to_scrape WHERE status='done'")}
    print(f"Loaded {len(valid_games):,} scraped game ids; {len(seasons)} seasons "
          f"({seasons[0]}-{seasons[-1]})...")
    done_filter = "(gpd.player_id IS NULL OR gpd.status='error')" if retry_failed else "gpd.player_id IS NULL"
    now = dt.datetime.utcnow().isoformat()
    t0 = time.time()
    grand_ok = grand_rows = 0
    for season in seasons:
        players = [r[0] for r in conn.execute(
            f"""SELECT DISTINCT pg.player_id FROM player_games pg
                LEFT JOIN game_positions_done gpd ON gpd.player_id=pg.player_id AND gpd.season=pg.season
                WHERE pg.season=? AND pg.player_id IS NOT NULL AND {done_filter}
                ORDER BY pg.player_id""", (season,))]
        if not players:
            continue
        nb = (len(players) + BULK_BATCH - 1) // BULK_BATCH
        s_rows = 0
        for bi in range(nb):
            batch = players[bi*BULK_BATCH:(bi+1)*BULK_BATCH]
            try:
                resp = requests.get(
                    "https://statsapi.mlb.com/api/v1/people",
                    params={"personIds": ",".join(map(str, batch)),
                            "hydrate": f"stats(group=[fielding],type=[gameLog],season={season},gameType=[R,D,L,W,F])"},
                    timeout=60)
                resp.raise_for_status()
                rows = []
                for person in resp.json().get("people", []):
                    pid = person.get("id")
                    if pid is None:
                        continue
                    for gid, (gdate, abbr) in gp_from_gamelog(person, valid_games).items():
                        rows.append((gid, pid, season, gdate, abbr))
                conn.executemany(
                    "INSERT OR REPLACE INTO game_position (game_id, player_id, season, game_date, position) VALUES (?,?,?,?,?)", rows)
                conn.executemany(
                    "INSERT OR REPLACE INTO game_positions_done (player_id, season, status, error_msg, fetched_at) VALUES (?,?,'done',NULL,?)",
                    [(p, season, now) for p in batch])
                conn.commit()
                s_rows += len(rows); grand_ok += len(batch); grand_rows += len(rows)
            except Exception as e:
                conn.executemany(
                    "INSERT OR REPLACE INTO game_positions_done (player_id, season, status, error_msg, fetched_at) VALUES (?,?,'error',?,?)",
                    [(p, season, str(e)[:500], now) for p in batch])
                conn.commit()
                print(f"  [{season} batch {bi+1}/{nb}] ERROR: {e}", file=sys.stderr)
            time.sleep(delay)
        print(f"[{season}] +{s_rows:,} game-position rows  (total {grand_ok:,} players, {(time.time()-t0)/60:.1f}m)")
    print(f"\nDONE positions. {grand_ok:,} player-seasons, {grand_rows:,} rows in {(time.time()-t0)/3600:.2f}h")


# ============================================================================
# game-batting — extended per-game batting events (pa/ibb/cs/hbp/sf/sh/gidp/lob)
# that player_games lacks, needed for OBP etc. From the bulk hitting gameLog.
# ============================================================================
GB_FIELDS = [
    ("pa", "plateAppearances"), ("hbp", "hitByPitch"), ("ibb", "intentionalWalks"),
    ("sf", "sacFlies"), ("sh", "sacBunts"), ("gidp", "groundIntoDoublePlay"),
    ("cs", "caughtStealing"), ("lob", "leftOnBase"),
]


def gb_ensure_tables(conn):
    cols = ",\n               ".join(f"{c} INTEGER" for c, _ in GB_FIELDS)
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS game_batting (
               game_id INTEGER NOT NULL, player_id INTEGER NOT NULL,
               season INTEGER, game_date TEXT,
               {cols},
               PRIMARY KEY (game_id, player_id))""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS game_batting_done (
               player_id INTEGER NOT NULL, season INTEGER NOT NULL,
               status TEXT, error_msg TEXT, fetched_at TEXT,
               PRIMARY KEY (player_id, season))""")
    conn.commit()


def gb_rows_from_gamelog(person, season, valid_games):
    pid = person.get("id")
    try:
        splits = person["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return []
    by_game = {}
    for s in splits:
        gid = (s.get("game") or {}).get("gamePk")
        if gid is None or (valid_games is not None and gid not in valid_games):
            continue
        st = s.get("stat") or {}
        by_game[gid] = (gid, pid, season, s.get("date"),
                        *[_to_int(st.get(api)) for _, api in GB_FIELDS])
    return list(by_game.values())


def game_batting(conn, start, end, delay, retry_failed=False):
    gb_ensure_tables(conn)
    seasons = [r[0] for r in conn.execute(
        "SELECT DISTINCT season FROM player_games WHERE season IS NOT NULL "
        "AND season BETWEEN ? AND ? ORDER BY season", (start, end))]
    if not seasons:
        print(f"No seasons in range {start}-{end}.")
        return
    valid_games = {r[0] for r in conn.execute(
        "SELECT game_id FROM games_to_scrape WHERE status='done'")}
    print(f"Loaded {len(valid_games):,} scraped game ids; {len(seasons)} seasons "
          f"({seasons[0]}-{seasons[-1]})...")
    done_filter = "(gbd.player_id IS NULL OR gbd.status='error')" if retry_failed else "gbd.player_id IS NULL"
    placeholders = "?,?,?,?," + ",".join("?" for _ in GB_FIELDS)
    colnames = "game_id, player_id, season, game_date, " + ", ".join(c for c, _ in GB_FIELDS)
    now = dt.datetime.utcnow().isoformat()
    t0 = time.time()
    grand_ok = grand_rows = 0
    for season in seasons:
        players = [r[0] for r in conn.execute(
            f"""SELECT DISTINCT pg.player_id FROM player_games pg
                LEFT JOIN game_batting_done gbd ON gbd.player_id=pg.player_id AND gbd.season=pg.season
                WHERE pg.season=? AND pg.player_id IS NOT NULL AND {done_filter}
                ORDER BY pg.player_id""", (season,))]
        if not players:
            continue
        nb = (len(players) + BULK_BATCH - 1) // BULK_BATCH
        s_rows = 0
        for bi in range(nb):
            batch = players[bi*BULK_BATCH:(bi+1)*BULK_BATCH]
            try:
                resp = requests.get(
                    "https://statsapi.mlb.com/api/v1/people",
                    params={"personIds": ",".join(map(str, batch)),
                            "hydrate": f"stats(group=[hitting],type=[gameLog],season={season},gameType=[R,D,L,W,F])"},
                    timeout=60)
                resp.raise_for_status()
                rows = []
                for person in resp.json().get("people", []):
                    if person.get("id") is None:
                        continue
                    rows.extend(gb_rows_from_gamelog(person, season, valid_games))
                conn.executemany(
                    f"INSERT OR REPLACE INTO game_batting ({colnames}) VALUES ({placeholders})", rows)
                conn.executemany(
                    "INSERT OR REPLACE INTO game_batting_done (player_id, season, status, error_msg, fetched_at) VALUES (?,?,'done',NULL,?)",
                    [(p, season, now) for p in batch])
                conn.commit()
                s_rows += len(rows); grand_ok += len(batch); grand_rows += len(rows)
            except Exception as e:
                conn.executemany(
                    "INSERT OR REPLACE INTO game_batting_done (player_id, season, status, error_msg, fetched_at) VALUES (?,?,'error',?,?)",
                    [(p, season, str(e)[:500], now) for p in batch])
                conn.commit()
                print(f"  [{season} batch {bi+1}/{nb}] ERROR: {e}", file=sys.stderr)
            time.sleep(delay)
        print(f"[{season}] +{s_rows:,} rows  (total {grand_ok:,} players, {(time.time()-t0)/60:.1f}m)")
    print(f"\nDONE. {grand_ok:,} player-seasons, {grand_rows:,} game-batting rows in {(time.time()-t0)/3600:.2f}h")


# ============================================================================
# pitcher-extras — wp/bk/won/sv/bs/sho/cg into pitcher_games (for DBs built by
# the legacy boxscore scraper, which didn't capture them). season-batch already
# fills these, so this is only needed for older per-game-scraped seasons.
# ============================================================================
def pe_ensure_schema(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(pitcher_games)")}
    for c in ("wp", "bk", "won", "sv", "bs", "sho", "cg"):
        if c not in cols:
            conn.execute(f"ALTER TABLE pitcher_games ADD COLUMN {c} INTEGER")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pitcher_extras_done (
               player_id INTEGER NOT NULL, season INTEGER NOT NULL,
               status TEXT, error_msg TEXT, fetched_at TEXT,
               PRIMARY KEY (player_id, season))""")
    conn.commit()


def pe_rows_from_gamelog(person, valid_games):
    pid = person.get("id")
    try:
        splits = person["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return []
    by_game = {}
    for s in splits:
        gid = (s.get("game") or {}).get("gamePk")
        if gid is None or (valid_games is not None and gid not in valid_games):
            continue
        st = s.get("stat") or {}
        wins = _to_int(st.get("wins")) or 0
        losses = _to_int(st.get("losses")) or 0
        by_game[gid] = (_to_int(st.get("wildPitches")), _to_int(st.get("balks")),
                        1 if wins else (0 if losses else None),
                        _to_int(st.get("saves")), _to_int(st.get("blownSaves")),
                        _to_int(st.get("shutouts")), _to_int(st.get("completeGames")),
                        gid, pid)
    return list(by_game.values())


def pitcher_extras(conn, start, end, delay, retry_failed=False):
    pe_ensure_schema(conn)
    seasons = [r[0] for r in conn.execute(
        "SELECT DISTINCT season FROM pitcher_games WHERE season IS NOT NULL "
        "AND season BETWEEN ? AND ? ORDER BY season", (start, end))]
    if not seasons:
        print(f"No pitcher seasons in range {start}-{end}.")
        return
    valid_games = {r[0] for r in conn.execute(
        "SELECT game_id FROM games_to_scrape WHERE status='done'")}
    print(f"Loaded {len(valid_games):,} scraped game ids; {len(seasons)} seasons "
          f"({seasons[0]}-{seasons[-1]})...")
    done_filter = "(ped.player_id IS NULL OR ped.status='error')" if retry_failed else "ped.player_id IS NULL"
    now = dt.datetime.utcnow().isoformat()
    t0 = time.time()
    grand_ok = grand_rows = 0
    for season in seasons:
        players = [r[0] for r in conn.execute(
            f"""SELECT DISTINCT pg.player_id FROM pitcher_games pg
                LEFT JOIN pitcher_extras_done ped ON ped.player_id=pg.player_id AND ped.season=pg.season
                WHERE pg.season=? AND pg.player_id IS NOT NULL AND {done_filter}
                ORDER BY pg.player_id""", (season,))]
        if not players:
            continue
        nb = (len(players) + BULK_BATCH - 1) // BULK_BATCH
        s_rows = 0
        for bi in range(nb):
            batch = players[bi*BULK_BATCH:(bi+1)*BULK_BATCH]
            try:
                resp = requests.get(
                    "https://statsapi.mlb.com/api/v1/people",
                    params={"personIds": ",".join(map(str, batch)),
                            "hydrate": f"stats(group=[pitching],type=[gameLog],season={season},gameType=[R,D,L,W,F])"},
                    timeout=60)
                resp.raise_for_status()
                rows = []
                for person in resp.json().get("people", []):
                    if person.get("id") is None:
                        continue
                    rows.extend(pe_rows_from_gamelog(person, valid_games))
                conn.executemany(
                    "UPDATE pitcher_games SET wp=?, bk=?, won=?, sv=?, bs=?, sho=?, cg=? WHERE game_id=? AND player_id=?", rows)
                conn.executemany(
                    "INSERT OR REPLACE INTO pitcher_extras_done (player_id, season, status, error_msg, fetched_at) VALUES (?,?,'done',NULL,?)",
                    [(p, season, now) for p in batch])
                conn.commit()
                s_rows += len(rows); grand_ok += len(batch); grand_rows += len(rows)
            except Exception as e:
                conn.executemany(
                    "INSERT OR REPLACE INTO pitcher_extras_done (player_id, season, status, error_msg, fetched_at) VALUES (?,?,'error',?,?)",
                    [(p, season, str(e)[:500], now) for p in batch])
                conn.commit()
                print(f"  [{season} batch {bi+1}/{nb}] ERROR: {e}", file=sys.stderr)
            time.sleep(delay)
        print(f"[{season}] {s_rows:,} rows updated  (total {grand_ok:,} pitchers, {(time.time()-t0)/60:.1f}m)")
    print(f"\nDONE. {grand_ok:,} pitcher-seasons, {grand_rows:,} rows in {(time.time()-t0)/3600:.2f}h")


# ============================================================================
# build — materialize game_unified / season_unified, then emit one distinct-
# line dump per position+mode. The browser (index.html) rolls a dump up onto
# any 3 axes; cube cells, recents, leaderboard, and W/L filter reconstruct
# exactly. Rate stats are stored as scaled-integer bins (RATE_SCALE).
# ============================================================================
RATE_SCALE = {
    "avg": (100, 2), "obp": (100, 2), "slg": (100, 2), "ops": (100, 2), "babip": (100, 2),
    "win_pct": (1000, 3), "strike_pct": (1000, 3), "whip": (100, 2),
    "era": (10, 1), "k9": (10, 1), "kbb": (10, 1),
}


def build_game_unified(conn):
    conn.execute("DROP TABLE IF EXISTS game_unified")
    conn.execute("""
        CREATE TABLE game_unified AS
        WITH keys AS (
            SELECT game_id, player_id FROM player_games
            UNION SELECT game_id, player_id FROM pitcher_games)
        SELECT k.game_id, k.player_id,
            COALESCE(b.player_name, p.player_name) AS player_name,
            COALESCE(b.season, p.season) AS season,
            COALESCE(b.game_type, p.game_type) AS game_type,
            COALESCE(b.team_abbr, p.team_abbr) AS team_abbr,
            COALESCE(b.matchup, p.matchup) AS matchup,
            COALESCE(b.game_date, p.game_date) AS game_date,
            COALESCE(gp.position, CASE WHEN p.outs IS NOT NULL THEN 'P' END) AS position,
            gb.pa, b.ab, b.r, b.h, b.doubles, b.triples, b.hr, b.rbi, b.bb,
            gb.ibb, b.k AS so, b.sb, gb.cs, gb.hbp, gb.sf,
            CASE WHEN b.h IS NOT NULL THEN b.h + b.doubles + 2*b.triples + 3*b.hr END AS tb,
            p.outs AS p_outs, p.k AS p_k, p.bb AS p_bb, p.h AS p_h,
            p.er AS p_er, p.r AS p_r, p.hr AS p_hr, p.p AS p_np, p.s AS p_s,
            p.wp AS p_wp, p.bk AS p_bk, p.won AS won, p.sv, p.bs, p.sho, p.cg
        FROM keys k
        LEFT JOIN player_games  b  ON b.game_id=k.game_id AND b.player_id=k.player_id
        LEFT JOIN pitcher_games p  ON p.game_id=k.game_id AND p.player_id=k.player_id
        LEFT JOIN game_batting  gb ON gb.game_id=k.game_id AND gb.player_id=k.player_id
        LEFT JOIN game_position gp ON gp.game_id=k.game_id AND gp.player_id=k.player_id
    """)
    conn.execute("CREATE INDEX ix_gu_pos ON game_unified(position)")
    conn.execute("CREATE INDEX ix_gu_seas ON game_unified(season)")
    conn.commit()


def build_season_unified(conn):
    conn.execute("DROP TABLE IF EXISTS season_unified")
    conn.execute("""
        CREATE TABLE season_unified AS
        WITH bat AS (
            SELECT player_id, MAX(player_name) AS player_name, season, COUNT(*) AS g,
                   SUM(ab) ab, SUM(r) r, SUM(h) h, SUM(doubles) doubles, SUM(triples) triples,
                   SUM(hr) hr, SUM(rbi) rbi, SUM(bb) bb, SUM(k) so, SUM(sb) sb
            FROM player_games WHERE game_type='R' GROUP BY player_id, season),
        bevt AS (
            SELECT player_id, season, SUM(pa) pa, SUM(ibb) ibb, SUM(cs) cs, SUM(hbp) hbp, SUM(sf) sf
            FROM game_batting WHERE season IS NOT NULL GROUP BY player_id, season),
        pit AS (
            SELECT player_id, MAX(player_name) AS player_name, season,
                   SUM(outs) p_outs, SUM(k) p_k, SUM(bb) p_bb, SUM(h) p_h, SUM(er) p_er,
                   SUM(r) p_r, SUM(hr) p_hr, SUM(p) p_np, SUM(s) p_s, SUM(wp) p_wp, SUM(bk) p_bk,
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) w, SUM(CASE WHEN won=0 THEN 1 ELSE 0 END) l,
                   SUM(sv) sv, SUM(bs) bs, SUM(sho) sho, SUM(cg) cg
            FROM pitcher_games WHERE game_type='R' GROUP BY player_id, season),
        ids AS (SELECT player_id, season FROM bat UNION SELECT player_id, season FROM pit)
        SELECT i.player_id, i.season,
            COALESCE(bat.player_name, pit.player_name) AS player_name,
            COALESCE(sp.position, CASE WHEN pit.p_outs IS NOT NULL THEN 'P' END) AS position,
            COALESCE(bat.g, 0) AS g,
            bat.ab, bat.r, bat.h, bat.doubles, bat.triples, bat.hr, bat.rbi, bat.bb, bat.so, bat.sb,
            bevt.pa, bevt.ibb, bevt.cs, bevt.hbp,
            (bat.h + bat.doubles + 2*bat.triples + 3*bat.hr) AS tb,
            CAST(CASE WHEN bat.ab>0 THEN round(100.0*bat.h/bat.ab) END AS INTEGER) AS avg,
            CAST(CASE WHEN (bat.ab+bat.bb+COALESCE(bevt.hbp,0)+COALESCE(bevt.sf,0))>0
                 THEN round(100.0*(bat.h+bat.bb+COALESCE(bevt.hbp,0))/(bat.ab+bat.bb+COALESCE(bevt.hbp,0)+COALESCE(bevt.sf,0))) END AS INTEGER) AS obp,
            CAST(CASE WHEN bat.ab>0 THEN round(100.0*(bat.h+bat.doubles+2*bat.triples+3*bat.hr)/bat.ab) END AS INTEGER) AS slg,
            CAST(CASE WHEN bat.ab>0 AND (bat.ab+bat.bb+COALESCE(bevt.hbp,0)+COALESCE(bevt.sf,0))>0
                 THEN round(100.0*((1.0*(bat.h+bat.bb+COALESCE(bevt.hbp,0))/(bat.ab+bat.bb+COALESCE(bevt.hbp,0)+COALESCE(bevt.sf,0)))
                      + (1.0*(bat.h+bat.doubles+2*bat.triples+3*bat.hr)/bat.ab))) END AS INTEGER) AS ops_x,
            CAST(CASE WHEN (bat.ab-bat.so-bat.hr+COALESCE(bevt.sf,0))>0
                 THEN round(100.0*(bat.h-bat.hr)/(bat.ab-bat.so-bat.hr+COALESCE(bevt.sf,0))) END AS INTEGER) AS babip,
            pit.p_outs, pit.p_k, pit.p_bb, pit.p_h, pit.p_er, pit.p_r, pit.p_hr,
            pit.p_np, pit.p_wp, pit.p_bk, pit.w, pit.l, pit.sv, pit.bs, pit.sho, pit.cg,
            CAST(CASE WHEN (pit.w+pit.l)>0 THEN round(1000.0*pit.w/(pit.w+pit.l)) END AS INTEGER) AS win_pct,
            CAST(CASE WHEN pit.p_outs>0 THEN round(10.0*27.0*pit.p_er/pit.p_outs) END AS INTEGER) AS era,
            CAST(CASE WHEN pit.p_outs>0 THEN round(100.0*3.0*(pit.p_bb+pit.p_h)/pit.p_outs) END AS INTEGER) AS whip,
            CAST(CASE WHEN pit.p_outs>0 THEN round(10.0*27.0*pit.p_k/pit.p_outs) END AS INTEGER) AS k9,
            CAST(CASE WHEN pit.p_bb>0 THEN round(10.0*pit.p_k/pit.p_bb) END AS INTEGER) AS kbb,
            CAST(CASE WHEN pit.p_np>0 THEN round(1000.0*pit.p_s/pit.p_np) END AS INTEGER) AS strike_pct
        FROM ids i
        LEFT JOIN bat  ON bat.player_id=i.player_id  AND bat.season=i.season
        LEFT JOIN bevt ON bevt.player_id=i.player_id AND bevt.season=i.season
        LEFT JOIN pit  ON pit.player_id=i.player_id  AND pit.season=i.season
        LEFT JOIN season_position sp ON sp.player_id=i.player_id AND sp.season=i.season
    """)
    conn.execute("ALTER TABLE season_unified RENAME COLUMN ops_x TO ops")
    conn.execute("CREATE INDEX ix_su_pos ON season_unified(position)")
    conn.execute("CREATE INDEX ix_su_seas ON season_unified(season)")
    conn.commit()


PALETTE = ["#ff6b6b", "#f7c948", "#6bd06b", "#9fc2ff", "#a07bff", "#ff5fa0",
           "#ff9f43", "#8ee27a", "#bdbdbd", "#5fd5ff", "#e879f9", "#fbbf24",
           "#34d399", "#60a5fa", "#f87171", "#c084fc"]
BAT_COUNT = [
    ("pa", "Plate Appearances"), ("ab", "At Bats"), ("r", "Runs"), ("h", "Hits"),
    ("doubles", "Doubles"), ("triples", "Triples"), ("hr", "Home Runs"), ("rbi", "RBI"),
    ("bb", "Walks"), ("ibb", "Intentional Walks"), ("so", "Struck Out"),
    ("sb", "Stolen Bases"), ("cs", "Caught Stealing"), ("hbp", "Hit By Pitch"), ("tb", "Total Bases"),
]
BAT_RATE = [("avg", "AVG"), ("obp", "OBP"), ("slg", "SLG"), ("ops", "OPS"), ("babip", "BABIP")]
PIT_COUNT_GAME = [
    ("p_outs", "Outs"), ("p_k", "Strikeouts"), ("p_bb", "Walks Issued"), ("p_h", "Hits Allowed"),
    ("p_r", "Runs Allowed"), ("p_er", "Earned Runs"), ("p_hr", "HR Allowed"),
    ("p_np", "Pitches"), ("p_wp", "Wild Pitches"), ("p_bk", "Balks"),
]
PIT_COUNT_SEASON = PIT_COUNT_GAME + [
    ("w", "Wins"), ("l", "Losses"), ("sv", "Saves"), ("bs", "Blown Saves"),
    ("sho", "Shutouts"), ("cg", "Complete Games"),
]
PIT_RATE = [("win_pct", "Win%"), ("era", "ERA"), ("whip", "WHIP"),
            ("k9", "K/9"), ("kbb", "K:BB"), ("strike_pct", "Strike%")]
# pos key -> (label, position abbr or None, domain)
POSITIONS = {
    "all": ("All", None, "full"), "p": ("Pitcher", "P", "full"),
    "c": ("Catcher", "C", "bat"), "1b": ("First Base", "1B", "bat"),
    "2b": ("Second Base", "2B", "bat"), "3b": ("Third Base", "3B", "bat"),
    "ss": ("Shortstop", "SS", "bat"), "lf": ("Left Field", "LF", "bat"),
    "cf": ("Center Field", "CF", "bat"), "rf": ("Right Field", "RF", "bat"),
    "dh": ("Designated Hitter", "DH", "bat"),
}


def axes_for(domain, mode):
    if mode == "game":
        defs = list(BAT_COUNT) + (PIT_COUNT_GAME if domain == "full" else [])
    else:
        defs = BAT_COUNT + BAT_RATE + (PIT_COUNT_SEASON + PIT_RATE if domain == "full" else [])
    return defs


def stats_json(defs):
    out = []
    for i, (key, label) in enumerate(defs):
        s = {"key": key, "label": label, "color": PALETTE[i % len(PALETTE)]}
        if key in RATE_SCALE:
            s["scale"], s["decimals"] = RATE_SCALE[key]
        out.append(s)
    return {"stats": out}


def emit_dump(conn, poskey, mode):
    label, pos_abbr, domain = POSITIONS[poskey]
    defs = axes_for(domain, mode)
    keys = [k for k, _ in defs]
    table = "game_unified" if mode == "game" else "season_unified"
    where = "" if pos_abbr is None else f"WHERE position='{pos_abbr}'"
    wl = mode == "game" and domain == "full"
    part_cols = keys + (["won"] if wl else [])
    part = ",".join(part_cols)
    if mode == "game":
        extra = "player_id, player_name, team_abbr, matchup, game_id, game_date, won"
        order = "game_date DESC, game_id DESC, player_id DESC"
    else:
        extra = "player_id, player_name, season, g"
        order = "season DESC, player_name ASC, player_id ASC"
    rows = conn.execute(
        f"""WITH ranked AS (
                SELECT {part}, {extra},
                       ROW_NUMBER() OVER (PARTITION BY {part} ORDER BY {order}) rn,
                       COUNT(*) OVER (PARTITION BY {part}) n
                FROM {table} {where})
            SELECT * FROM ranked WHERE rn<=5 ORDER BY {part}, rn""").fetchall()
    lines, cur, curkey = [], None, object()
    for r in rows:
        key = tuple(r[c] for c in part_cols)
        if key != curkey:
            curkey = key
            cur = {"v": [r[c] for c in keys], "n": r["n"], "r": []}
            if wl:
                cur["w"] = r["won"]
            lines.append(cur)
        if mode == "game":
            cur["r"].append([r["player_id"], r["player_name"], r["team_abbr"], r["matchup"],
                             str(r["game_id"]) if r["game_id"] is not None else None,
                             r["game_date"], r["won"]])
        else:
            cur["r"].append([r["player_id"], r["player_name"], str(r["season"]), r["g"]])
    base = OUT_ROOT / poskey
    base.mkdir(parents=True, exist_ok=True)
    # stats.json stays plain (tiny, loaded directly for the dropdowns). The dump
    # is gzipped — the 'all'/'p' game dumps are 100-150 MB raw (pitching pitch
    # counts barely collapse) and exceed GitHub's 100 MB file limit; gzip cuts
    # them ~5x. The browser decompresses via DecompressionStream.
    (base / ("stats.json" if mode == "game" else "stats-season.json")).write_text(
        json.dumps(stats_json(defs), separators=(",", ":")))
    payload = json.dumps({"mode": mode, "axes": keys, "lines": lines}, separators=(",", ":"))
    stem = "game" if mode == "game" else "season"
    (base / f"{stem}.json").unlink(missing_ok=True)   # remove any stale uncompressed dump
    with gzip.open(base / f"{stem}.json.gz", "wt", encoding="utf-8") as fh:
        fh.write(payload)
    return len(lines)


def build_cubes(conn, positions, mode, no_rebuild):
    if not no_rebuild:
        print("building game_unified ...")
        build_game_unified(conn)
        print(f"  -> {conn.execute('SELECT COUNT(*) FROM game_unified').fetchone()[0]:,} rows")
        print("building season_unified ...")
        build_season_unified(conn)
        print(f"  -> {conn.execute('SELECT COUNT(*) FROM season_unified').fetchone()[0]:,} rows")
    bad = [p for p in positions if p not in POSITIONS]
    if bad:
        raise SystemExit(f"unknown positions: {bad}")
    modes = [m for m in ("game", "season") if mode in (m, "both")]
    t0 = time.time()
    for pk in positions:
        print(f"== position '{pk}' ({POSITIONS[pk][0]}) ==")
        for m in modes:
            n = emit_dump(conn, pk, m)
            print(f"  [{pk}/{m}] {n:,} distinct lines -> {OUT_ROOT/pk}/{'game' if m=='game' else 'season'}.json")
    print(f"\nDONE in {(time.time()-t0)/60:.1f}m -> {OUT_ROOT}")


def serve(port):
    """No-cache static dev server rooted at the deploy folder (public/)."""
    import http.server, socketserver, os
    os.chdir(OUT_ROOT.parent)

    class H(http.server.SimpleHTTPRequestHandler):
        def end_headers(self):
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            super().end_headers()
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), H) as httpd:
        print(f"no-cache server on http://127.0.0.1:{port}/")
        httpd.serve_forever()


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
    bp.add_argument("--retry-failed", action="store_true",
                    help="also re-attempt games whose last backfill errored")
    bp.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    bn = sub.add_parser("backfill-names",
                        help="Replace boxscore short names with full names")
    bn.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    bpos = sub.add_parser("backfill-positions",
                        help="Record each player's per-season most-played position")
    bpos.add_argument("--retry-failed", action="store_true",
                      help="also re-attempt players whose last fetch errored")
    bpos.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    # --- batched collectors (bulk gameLog) ---
    sb = sub.add_parser("season-batch",
                        help="Fast season ingest: player_games + pitcher_games (full) via bulk gameLogs")
    sb.add_argument("--season", type=int, required=True)
    sb.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    for name, helptext in [
        ("game-positions", "Per-game fielding position (bulk fielding gameLog)"),
        ("game-batting", "Extended per-game batting events: pa/ibb/cs/hbp/sf/sh/gidp/lob"),
        ("pitcher-extras", "Backfill wp/bk/won/sv/bs/sho/cg into pitcher_games"),
    ]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--start", type=int, default=1901)
        sp.add_argument("--end", type=int, default=2100)
        sp.add_argument("--retry-failed", action="store_true")
        sp.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    bc = sub.add_parser("build", help="Rebuild unified tables + emit position cube dumps")
    bc.add_argument("--positions", default="all,p,c,1b,2b,3b,ss,lf,cf,rf,dh")
    bc.add_argument("--mode", choices=["game", "season", "both"], default="both")
    bc.add_argument("--no-rebuild", action="store_true",
                    help="reuse existing unified tables (just re-emit dumps)")

    sv = sub.add_parser("serve", help="No-cache static dev server on public/")
    sv.add_argument("--port", type=int, default=8777)

    sub.add_parser("stats", help="show progress")

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        return

    if args.cmd == "serve":
        serve(args.port)
        return

    conn = open_db()
    init_db(conn)
    migrate_db(conn)

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
        backfill_pitching(conn, args.start, args.end, args.delay, args.retry_failed)
    elif args.cmd == "backfill-names":
        backfill_names(conn, args.delay)
    elif args.cmd == "backfill-positions":
        backfill_positions(conn, args.delay, args.retry_failed)
    elif args.cmd == "season-batch":
        season_batch(conn, args.season, args.delay)
    elif args.cmd == "game-positions":
        game_positions(conn, args.start, args.end, args.delay, args.retry_failed)
    elif args.cmd == "game-batting":
        game_batting(conn, args.start, args.end, args.delay, args.retry_failed)
    elif args.cmd == "pitcher-extras":
        pitcher_extras(conn, args.start, args.end, args.delay, args.retry_failed)
    elif args.cmd == "build":
        positions = [p.strip() for p in args.positions.split(",") if p.strip()]
        build_cubes(conn, positions, args.mode, args.no_rebuild)
        conn.close()
        return

    show_stats(conn)


if __name__ == "__main__":
    main()
