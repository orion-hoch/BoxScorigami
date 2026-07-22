"""Collect MLB player-game stats into mlb.sqlite and build the cube dumps."""
import argparse
import datetime as dt
import json
import sqlite3
import sys
import time
from pathlib import Path

import brotli
import requests

DB_PATH = Path(__file__).resolve().parent / "mlb.sqlite"
OUT_ROOT = Path(__file__).resolve().parent.parent / "public" / "mlb"
DEFAULT_DELAY = 0.5
BULK_BATCH = 100


def _to_int(v):
    if v in (None, "", "-", ".---", "-.--"):
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None

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

STAT_COLUMNS = [c for c, _ in SB_HIT_FIELDS]
PITCH_COLUMNS = ["k", "bb", "h", "er", "r", "hr", "outs", "p", "s",
                 "wp", "bk", "won", "sv", "bs", "sho", "cg"]


def ip_to_outs(ip):
    """Convert innings-pitched notation ("6.2") to outs."""
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


def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    stat_cols_sql = ",\n            ".join(f"{c} INTEGER" for c in STAT_COLUMNS)
    pitch_cols_sql = ",\n            ".join(f"{c} INTEGER" for c in PITCH_COLUMNS)
    gb_cols_sql = ",\n        ".join(f"{c} INTEGER" for c, _ in GB_FIELDS)
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

    CREATE TABLE IF NOT EXISTS season_position (
        player_id  INTEGER NOT NULL,
        season     INTEGER NOT NULL,
        position   TEXT,
        PRIMARY KEY (player_id, season)
    );

    CREATE TABLE IF NOT EXISTS players_positioned (
        player_id  INTEGER PRIMARY KEY,
        status     TEXT,
        error_msg  TEXT,
        fetched_at TEXT
    );

    CREATE TABLE IF NOT EXISTS game_position (
        game_id INTEGER NOT NULL, player_id INTEGER NOT NULL,
        season INTEGER, game_date TEXT, position TEXT,
        PRIMARY KEY (game_id, player_id)
    );

    CREATE TABLE IF NOT EXISTS game_batting (
        game_id INTEGER NOT NULL, player_id INTEGER NOT NULL,
        season INTEGER, game_date TEXT,
        {gb_cols_sql},
        PRIMARY KEY (game_id, player_id)
    );

    CREATE TABLE IF NOT EXISTS game_positions_done (
        player_id INTEGER NOT NULL, season INTEGER NOT NULL,
        status TEXT, error_msg TEXT, fetched_at TEXT,
        PRIMARY KEY (player_id, season)
    );

    CREATE TABLE IF NOT EXISTS game_batting_done (
        player_id INTEGER NOT NULL, season INTEGER NOT NULL,
        status TEXT, error_msg TEXT, fetched_at TEXT,
        PRIMARY KEY (player_id, season)
    );

    CREATE TABLE IF NOT EXISTS pitcher_extras_done (
        player_id INTEGER NOT NULL, season INTEGER NOT NULL,
        status TEXT, error_msg TEXT, fetched_at TEXT,
        PRIMARY KEY (player_id, season)
    );
    """)


def backfill_names(conn, delay):
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


def _season_positions_from_fielding(payload):
    """Return {season: most-played position abbr} from a fielding payload."""
    best = {}
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

    BATCH = 100
    total = len(players)
    nbatches = (total + BATCH - 1) // BATCH
    print(f"Backfilling season positions for {total:,} players in {nbatches:,} batches of {BATCH}...")
    t0 = time.time()
    ok = fail = 0
    now = dt.datetime.utcnow().isoformat()
    for bi, off in enumerate(range(0, total, BATCH)):
        batch = players[off:off + BATCH]
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
    seas_range = conn.execute("SELECT MIN(season), MAX(season) FROM seasons_enumerated").fetchone()
    print(f"seasons enumerated: {seas}  (range {seas_range[0]}–{seas_range[1]})")
    print(f"games:              {total:,}  done={done:,}  error={err:,}  pending={pend:,}")
    print(f"player-game rows:   {pg:,}")
    print(f"pitcher-game rows:  {pit:,}  (across {pit_games:,} games)")
    print(f"season positions:   {sp_rows:,} player-seasons  ({sp_players:,} players resolved)")
    if sp_need:
        print(f"  -> {sp_need:,} players still need positions: run `collect.py backfill-positions`")
    if need_pitch:
        print(f"  -> {need_pitch:,} done games still need pitching: run `collect.py season-batch`")


# ---------------- season-batch ----------------
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


# ---------------- bulk gameLog backfills ----------------
GB_FIELDS = [
    ("pa", "plateAppearances"), ("hbp", "hitByPitch"), ("ibb", "intentionalWalks"),
    ("sf", "sacFlies"), ("sh", "sacBunts"), ("gidp", "groundIntoDoublePlay"),
    ("cs", "caughtStealing"), ("lob", "leftOnBase"),
]


def gp_rows_from_gamelog(person, season, valid_games):
    """(game_id, player_id, season, date, position) keeping max-innings position."""
    pid = person.get("id")
    try:
        splits = person["stats"][0]["splits"]
    except (KeyError, IndexError, TypeError):
        return []
    best = {}
    for s in splits:
        gid = (s.get("game") or {}).get("gamePk")
        if gid is None or (valid_games is not None and gid not in valid_games):
            continue
        st = s.get("stat") or {}
        abbr = (st.get("position") or {}).get("abbreviation")
        if not abbr:
            continue
        outs = ip_to_outs(st.get("innings")) or 0
        started = _to_int(st.get("gamesStarted")) or 0
        if gid not in best or (outs, started) > best[gid][:2]:
            best[gid] = (outs, started, s.get("date"), abbr)
    return [(gid, pid, season, d, a) for gid, (_, _, d, a) in best.items()]


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


def pe_rows_from_gamelog(person, season, valid_games):
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


GB_COLS = "game_id, player_id, season, game_date, " + ", ".join(c for c, _ in GB_FIELDS)

BULK_JOBS = {
    "game-positions": ("fielding", "player_games", "game_positions_done", gp_rows_from_gamelog,
                       "INSERT OR REPLACE INTO game_position "
                       "(game_id, player_id, season, game_date, position) VALUES (?,?,?,?,?)",
                       "game-position"),
    "game-batting": ("hitting", "player_games", "game_batting_done", gb_rows_from_gamelog,
                     f"INSERT OR REPLACE INTO game_batting ({GB_COLS}) "
                     f"VALUES ({','.join('?' * (4 + len(GB_FIELDS)))})",
                     "game-batting"),
    "pitcher-extras": ("pitching", "pitcher_games", "pitcher_extras_done", pe_rows_from_gamelog,
                       "UPDATE pitcher_games SET wp=?, bk=?, won=?, sv=?, bs=?, sho=?, cg=? "
                       "WHERE game_id=? AND player_id=?",
                       "pitcher-extras"),
}


def bulk_gamelog(conn, job, start, end, delay, retry_failed=False):
    group, src, done_table, extract, write_sql, label = BULK_JOBS[job]
    seasons = [r[0] for r in conn.execute(
        f"SELECT DISTINCT season FROM {src} WHERE season IS NOT NULL "
        "AND season BETWEEN ? AND ? ORDER BY season", (start, end))]
    if not seasons:
        print(f"No seasons in range {start}-{end}.")
        return
    valid_games = {r[0] for r in conn.execute(
        "SELECT game_id FROM games_to_scrape WHERE status='done'")}
    print(f"Loaded {len(valid_games):,} scraped game ids; {len(seasons)} seasons "
          f"({seasons[0]}-{seasons[-1]})...")
    done_filter = "(d.player_id IS NULL OR d.status='error')" if retry_failed else "d.player_id IS NULL"
    mark_done = (f"INSERT OR REPLACE INTO {done_table} "
                 "(player_id, season, status, error_msg, fetched_at) VALUES (?,?,?,?,?)")
    now = dt.datetime.utcnow().isoformat()
    t0 = time.time()
    grand_ok = grand_rows = 0
    for season in seasons:
        players = [r[0] for r in conn.execute(
            f"""SELECT DISTINCT pg.player_id FROM {src} pg
                LEFT JOIN {done_table} d ON d.player_id=pg.player_id AND d.season=pg.season
                WHERE pg.season=? AND pg.player_id IS NOT NULL AND {done_filter}
                ORDER BY pg.player_id""", (season,))]
        if not players:
            continue
        nb = (len(players) + BULK_BATCH - 1) // BULK_BATCH
        s_rows = 0
        for bi, off in enumerate(range(0, len(players), BULK_BATCH)):
            batch = players[off:off + BULK_BATCH]
            try:
                resp = requests.get(
                    "https://statsapi.mlb.com/api/v1/people",
                    params={"personIds": ",".join(map(str, batch)),
                            "hydrate": f"stats(group=[{group}],type=[gameLog],"
                                       f"season={season},gameType=[{SB_GAME_TYPES}])"},
                    timeout=60)
                resp.raise_for_status()
                rows = []
                for person in resp.json().get("people", []):
                    if person.get("id") is None:
                        continue
                    rows.extend(extract(person, season, valid_games))
                conn.executemany(write_sql, rows)
                conn.executemany(mark_done, [(p, season, 'done', None, now) for p in batch])
                conn.commit()
                s_rows += len(rows); grand_ok += len(batch); grand_rows += len(rows)
            except Exception as e:
                conn.executemany(mark_done, [(p, season, 'error', str(e)[:500], now) for p in batch])
                conn.commit()
                print(f"  [{season} batch {bi+1}/{nb}] ERROR: {e}", file=sys.stderr)
            time.sleep(delay)
        print(f"[{season}] +{s_rows:,} {label} rows  (total {grand_ok:,} players, {(time.time()-t0)/60:.1f}m)")
    print(f"\nDONE. {grand_ok:,} player-seasons, {grand_rows:,} {label} rows in {(time.time()-t0)/3600:.2f}h")


# ---------------- build ----------------
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
            SELECT player_id, MAX(player_name) AS player_name, season,
                   CASE WHEN game_type='R' THEN 0 ELSE 1 END AS po, COUNT(*) AS g,
                   SUM(ab) ab, SUM(r) r, SUM(h) h, SUM(doubles) doubles, SUM(triples) triples,
                   SUM(hr) hr, SUM(rbi) rbi, SUM(bb) bb, SUM(k) so, SUM(sb) sb
            FROM player_games WHERE game_type IN ('R','F','D','L','W','C')
            GROUP BY player_id, season, po),
        bevt AS (
            SELECT gb.player_id, gb.season,
                   CASE WHEN gs.game_type='R' THEN 0 ELSE 1 END AS po,
                   SUM(pa) pa, SUM(ibb) ibb, SUM(cs) cs, SUM(hbp) hbp, SUM(sf) sf
            FROM game_batting gb
            JOIN games_to_scrape gs ON gs.game_id = gb.game_id
            WHERE gb.season IS NOT NULL AND gs.game_type IN ('R','F','D','L','W','C')
            GROUP BY gb.player_id, gb.season, po),
        pit AS (
            SELECT player_id, MAX(player_name) AS player_name, season,
                   CASE WHEN game_type='R' THEN 0 ELSE 1 END AS po,
                   SUM(outs) p_outs, SUM(k) p_k, SUM(bb) p_bb, SUM(h) p_h, SUM(er) p_er,
                   SUM(r) p_r, SUM(hr) p_hr, SUM(p) p_np, SUM(s) p_s, SUM(wp) p_wp, SUM(bk) p_bk,
                   SUM(CASE WHEN won=1 THEN 1 ELSE 0 END) w, SUM(CASE WHEN won=0 THEN 1 ELSE 0 END) l,
                   SUM(sv) sv, SUM(bs) bs, SUM(sho) sho, SUM(cg) cg
            FROM pitcher_games WHERE game_type IN ('R','F','D','L','W','C')
            GROUP BY player_id, season, po),
        ids AS (SELECT player_id, season, po FROM bat
                UNION SELECT player_id, season, po FROM pit)
        SELECT i.player_id, i.season, i.po,
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
            CAST(CASE WHEN pit.p_np>0 THEN round(1000.0*pit.p_s/pit.p_np) END AS INTEGER) AS strike_pct,
            -- Playing-time qualifiers. Rate stats computed off a handful of PA or
            -- outs (a 1-for-1 season, a 1-out 7-ER outing) are what stretch the
            -- rate axes: unfiltered ERA runs to 189.00 and AVG to 1.000.
            -- Playoff runs top out around 20 games, so they qualify at 30 PA /
            -- 30 outs (10 IP) instead of the 150 a full season demands.
            CASE WHEN COALESCE(bevt.pa, 0) >=
                 (CASE WHEN i.po=1 THEN 30 ELSE 150 END) THEN 1 ELSE 0 END AS qual_bat,
            CASE WHEN COALESCE(pit.p_outs, 0) >=
                 (CASE WHEN i.po=1 THEN 30 ELSE 150 END) THEN 1 ELSE 0 END AS qual_pit
        FROM ids i
        LEFT JOIN bat  ON bat.player_id=i.player_id  AND bat.season=i.season  AND bat.po=i.po
        LEFT JOIN bevt ON bevt.player_id=i.player_id AND bevt.season=i.season AND bevt.po=i.po
        LEFT JOIN pit  ON pit.player_id=i.player_id  AND pit.season=i.season  AND pit.po=i.po
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

# Which playing-time qualifier each rate stat depends on. Counting stats have
# none, so the Qualified toggle only bites on cubes that use a rate axis.
RATE_DOMAIN = ({k: "bat" for k, _ in BAT_RATE}
               | {k: "pit" for k, _ in PIT_RATE})
POSITIONS = {
    "all": ("All", None, "full"), "p": ("Pitcher", "P", "full"),
    "pos": ("Position Players", None, "bat"),
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
        if key in RATE_DOMAIN:
            s["qual"] = RATE_DOMAIN[key]
        out.append(s)
    return {"stats": out}


def emit_dump(conn, poskey, mode):
    label, pos_abbr, domain = POSITIONS[poskey]
    defs = axes_for(domain, mode)
    keys = [k for k, _ in defs]
    table = "game_unified" if mode == "game" else "season_unified"
    if poskey == "pos":
        where = "WHERE COALESCE(position,'')!='P'"
    elif pos_abbr is None:
        where = ""
    else:
        where = f"WHERE position='{pos_abbr}'"
    wl = mode == "game" and domain == "full"
    # Season lines carry the qualifier flags the way game lines carry `won`:
    # folded into the partition key so the client can filter without a refetch.
    qual = mode == "season"
    # Playoff flag for the client's Reg/Playoffs toggle. season_unified already
    # carries po (one row per player-season per type); game rows compute it
    # here. All-Star, spring and exhibition games stay NULL: shown with the
    # filter off, hidden by either exclusive state (same semantics as a
    # no-decision under W/L).
    po = True
    if mode == "game":
        table = ("(SELECT *, CASE WHEN game_type='R' THEN 0 "
                 "WHEN game_type IN ('F','D','L','W','C') THEN 1 END AS po "
                 f"FROM {table})")
    part_cols = keys + (["won"] if wl else []) + (["po"] if po else []) \
        + (["qual_bat", "qual_pit"] if qual else [])
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
            if po:
                cur["po"] = r["po"]
            if qual:
                cur["qb"] = r["qual_bat"]
                cur["qp"] = r["qual_pit"]
            lines.append(cur)
        if mode == "game":
            cur["r"].append([r["player_id"], r["player_name"], r["team_abbr"], r["matchup"],
                             str(r["game_id"]) if r["game_id"] is not None else None,
                             r["game_date"], r["won"]])
        else:
            cur["r"].append([r["player_id"], r["player_name"], str(r["season"]), r["g"]])
    base = OUT_ROOT / poskey
    base.mkdir(parents=True, exist_ok=True)
    (base / ("stats.json" if mode == "game" else "stats-season.json")).write_text(
        json.dumps(stats_json(defs), separators=(",", ":")))
    payload = json.dumps({"mode": mode, "axes": keys, "lines": lines}, separators=(",", ":"))
    stem = "game" if mode == "game" else "season"
    for legacy in (f"{stem}.json", f"{stem}.json.gz"):
        (base / legacy).unlink(missing_ok=True)
    # Brotli like every other sport, so dump_to_binary.js can transcode it:
    #   node dump_to_binary.js public/mlb/*/{game,season}.json.br
    (base / f"{stem}.json.br").write_bytes(
        brotli.compress(payload.encode("utf-8"), quality=9))
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
            print(f"  [{pk}/{m}] {n:,} distinct lines -> {OUT_ROOT/pk}/{'game' if m=='game' else 'season'}.json.br")
    print(f"\nDONE in {(time.time()-t0)/60:.1f}m -> {OUT_ROOT}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    bn = sub.add_parser("backfill-names",
                        help="Replace boxscore short names with full names")
    bn.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    bpos = sub.add_parser("backfill-positions",
                        help="Record each player's per-season most-played position")
    bpos.add_argument("--retry-failed", action="store_true",
                      help="also re-attempt players whose last fetch errored")
    bpos.add_argument("--delay", type=float, default=DEFAULT_DELAY)

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
    bc.add_argument("--positions", default="all,p,pos,c,1b,2b,3b,ss,lf,cf,rf,dh")
    bc.add_argument("--mode", choices=["game", "season", "both"], default="both")
    bc.add_argument("--no-rebuild", action="store_true",
                    help="reuse existing unified tables (just re-emit dumps)")

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
    if args.cmd == "backfill-names":
        backfill_names(conn, args.delay)
    elif args.cmd == "backfill-positions":
        backfill_positions(conn, args.delay, args.retry_failed)
    elif args.cmd == "season-batch":
        season_batch(conn, args.season, args.delay)
    elif args.cmd in BULK_JOBS:
        bulk_gamelog(conn, args.cmd, args.start, args.end, args.delay, args.retry_failed)
    elif args.cmd == "build":
        positions = [p.strip() for p in args.positions.split(",") if p.strip()]
        build_cubes(conn, positions, args.mode, args.no_rebuild)
        conn.close()
        return

    show_stats(conn)


if __name__ == "__main__":
    main()
