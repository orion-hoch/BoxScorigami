"""Collect NHL player-game stats into nhl.sqlite via the official NHL API.

Primary path is season-batch: the stats-REST per-game reports (skater
summary/realtime/timeonice, goalie summary/savesByStrength) return a whole
season in a handful of limit=-1 requests, carry MORE stats than the boxscore
(SH/GW/OT/EN goals, missed-shot subtypes, per-strength TOI, goalie splits),
and come with full names + position. The API caps any one response at 10,000
rows, so a capped fetch is re-pulled in month slices. `scrape` (one boxscore
request per game) remains as the fallback for games the reports miss, and is
the only source of sweater numbers.

Stats are stored raw, exactly as reported. The reports NULL most untracked-era
stats themselves (1917 shots -> NULL); where the API instead reports 0 (e.g.
boxscore hits pre-1997), the export layer's era cutoffs (CASE on season) are
the place to NULL them, so cutoffs stay tunable without re-collecting.

    python3 nhl/collect.py enumerate            # one request: every game since 1917
    python3 nhl/collect.py season-batch         # bulk reports, ~5-40 requests/season
    python3 nhl/collect.py scrape               # per-game boxscores for the leftovers
    python3 nhl/collect.py backfill-names       # full names for boxscore-scraped rows
    python3 nhl/collect.py stats
"""
import argparse
import datetime as dt
import sqlite3
import sys
import time
from pathlib import Path

import requests

DB_PATH = Path(__file__).resolve().parent / "nhl.sqlite"
API = "https://api-web.nhle.com/v1"
STATS_API = "https://api.nhle.com/stats/rest/en"
DEFAULT_DELAY = 0.3
RETRY_MAX = 3
GAME_TYPES = (2, 3)
FINAL_STATE = 7

SKATER_INTS = [
    ("goals", "goals"), ("assists", "assists"), ("points", "points"),
    ("plus_minus", "plusMinus"), ("pim", "pim"), ("hits", "hits"),
    ("ppg", "powerPlayGoals"), ("sog", "sog"),
    ("blocked", "blockedShots"), ("shifts", "shifts"),
    ("giveaways", "giveaways"), ("takeaways", "takeaways"),
]
GOALIE_INTS = [
    ("saves", "saves"), ("shots_against", "shotsAgainst"),
    ("goals_against", "goalsAgainst"),
    ("es_ga", "evenStrengthGoalsAgainst"), ("pp_ga", "powerPlayGoalsAgainst"),
    ("sh_ga", "shorthandedGoalsAgainst"), ("pim", "pim"),
]
GOALIE_SPLITS = [
    ("es", "evenStrengthShotsAgainst"), ("pp", "powerPlayShotsAgainst"),
    ("sh", "shorthandedShotsAgainst"),
]

META_COLS = ["game_id", "game_date", "season", "game_type", "player_id",
             "player_name", "position", "sweater", "team_abbr", "opponent",
             "matchup", "home"]

SKATER_EXTRA = [
    "ev_goals", "ev_points", "pp_points", "sh_goals", "sh_points",
    "gw_goals", "ot_goals", "first_goals",
    "en_goals", "en_assists", "en_points",
    "missed_shots", "missed_crossbar", "missed_goalpost", "missed_over",
    "missed_wide", "missed_short", "missed_bank",
    "shot_attempts", "attempts_blocked",
    "ev_toi_sec", "pp_toi_sec", "sh_toi_sec", "ot_toi_sec",
]
GOALIE_EXTRA = ["goals", "assists", "points", "shutouts"]

SKATER_COLS = (META_COLS + [c for c, _ in SKATER_INTS] + SKATER_EXTRA
               + ["faceoff_pct", "toi_sec"])
GOALIE_COLS = (META_COLS + [c for c, _ in GOALIE_INTS] + GOALIE_EXTRA
               + [f"{p}_{s}" for p, _ in GOALIE_SPLITS for s in ("saves", "sa")]
               + ["save_pct", "toi_sec", "starter", "decision"])

SK_REPORTS = [
    ("skater/summary", {
        "player_name": "skaterFullName", "goals": "goals", "assists": "assists",
        "points": "points", "plus_minus": "plusMinus", "pim": "penaltyMinutes",
        "ppg": "ppGoals", "sog": "shots", "faceoff_pct": "faceoffWinPct",
        "ev_goals": "evGoals", "ev_points": "evPoints", "pp_points": "ppPoints",
        "sh_goals": "shGoals", "sh_points": "shPoints",
        "gw_goals": "gameWinningGoals", "ot_goals": "otGoals"}),
    ("skater/realtime", {
        "player_name": "skaterFullName", "hits": "hits",
        "blocked": "blockedShots", "giveaways": "giveaways",
        "takeaways": "takeaways", "first_goals": "firstGoals",
        "en_goals": "emptyNetGoals", "en_assists": "emptyNetAssists",
        "en_points": "emptyNetPoints", "missed_shots": "missedShots",
        "missed_crossbar": "missedShotCrossbar",
        "missed_goalpost": "missedShotGoalpost",
        "missed_over": "missedShotOverNet", "missed_wide": "missedShotWideOfNet",
        "missed_short": "missedShotShort",
        "missed_bank": "missedShotFailedBankAttempt",
        "shot_attempts": "totalShotAttempts",
        "attempts_blocked": "shotAttemptsBlocked"}),
    ("skater/timeonice", {
        "player_name": "skaterFullName", "toi_sec": "timeOnIce",
        "shifts": "shifts", "ev_toi_sec": "evTimeOnIce",
        "pp_toi_sec": "ppTimeOnIce", "sh_toi_sec": "shTimeOnIce",
        "ot_toi_sec": "otTimeOnIce"}),
]
GL_REPORTS = [
    ("goalie/summary", {
        "player_name": "goalieFullName", "saves": "saves",
        "shots_against": "shotsAgainst", "goals_against": "goalsAgainst",
        "save_pct": "savePct", "toi_sec": "timeOnIce", "starter": "gamesStarted",
        "pim": "penaltyMinutes", "goals": "goals", "assists": "assists",
        "points": "points", "shutouts": "shutouts"}),
    ("goalie/savesByStrength", {
        "player_name": "goalieFullName",
        "es_saves": "evSaves", "es_sa": "evShotsAgainst", "es_ga": "evGoalsAgainst",
        "pp_saves": "ppSaves", "pp_sa": "ppShotsAgainst", "pp_ga": "ppGoalsAgainst",
        "sh_saves": "shSaves", "sh_sa": "shShotsAgainst", "sh_ga": "shGoalsAgainst"}),
]
REPORT_FROM = {"skater/realtime": 19971998, "skater/timeonice": 19971998,
               "goalie/savesByStrength": 19971998}
REPORT_CAP = 10_000


def toi_to_sec(s):
    """"22:31" -> 1351. None/'' -> None."""
    if not s:
        return None
    try:
        m, sec = str(s).split(":")
        return int(m) * 60 + int(sec)
    except ValueError:
        return None


def split_pair(s):
    """"17/19" -> (17, 19). Missing/malformed -> (None, None)."""
    if not s:
        return None, None
    try:
        a, b = str(s).split("/")
        return int(a), int(b)
    except ValueError:
        return None, None


def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    sk = ",\n        ".join(f"{c} INTEGER" for c, _ in SKATER_INTS)
    sk += ",\n        " + ",\n        ".join(f"{c} INTEGER" for c in SKATER_EXTRA)
    gl = ",\n        ".join(f"{c} INTEGER" for c, _ in GOALIE_INTS)
    gl += ",\n        " + ",\n        ".join(f"{c} INTEGER" for c in GOALIE_EXTRA)
    splits = ",\n        ".join(f"{p}_{s} INTEGER"
                                for p, _ in GOALIE_SPLITS for s in ("saves", "sa"))
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS games_to_scrape (
        game_id     INTEGER PRIMARY KEY,
        game_date   TEXT,
        season      INTEGER,
        game_type   INTEGER,
        status      TEXT DEFAULT 'pending',
        error_msg   TEXT,
        scraped_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS skater_games (
        game_id     INTEGER NOT NULL,
        game_date   TEXT,
        season      INTEGER,
        game_type   INTEGER,
        player_id   INTEGER NOT NULL,
        player_name TEXT,
        position    TEXT,
        sweater     INTEGER,
        team_abbr   TEXT,
        opponent    TEXT,
        matchup     TEXT,
        home        INTEGER,
        {sk},
        faceoff_pct REAL,
        toi_sec     INTEGER,
        PRIMARY KEY (game_id, player_id)
    );
    CREATE INDEX IF NOT EXISTS idx_sk_season ON skater_games(season);
    CREATE INDEX IF NOT EXISTS idx_sk_player ON skater_games(player_id);

    CREATE TABLE IF NOT EXISTS goalie_games (
        game_id     INTEGER NOT NULL,
        game_date   TEXT,
        season      INTEGER,
        game_type   INTEGER,
        player_id   INTEGER NOT NULL,
        player_name TEXT,
        position    TEXT,
        sweater     INTEGER,
        team_abbr   TEXT,
        opponent    TEXT,
        matchup     TEXT,
        home        INTEGER,
        {gl},
        {splits},
        save_pct    REAL,
        toi_sec     INTEGER,
        starter     INTEGER,
        decision    TEXT,
        PRIMARY KEY (game_id, player_id)
    );
    CREATE INDEX IF NOT EXISTS idx_gl_season ON goalie_games(season);
    CREATE INDEX IF NOT EXISTS idx_gl_player ON goalie_games(player_id);

    CREATE TABLE IF NOT EXISTS seasons_batched (
        season      INTEGER NOT NULL,
        game_type   INTEGER NOT NULL,
        n_skaters   INTEGER,
        n_goalies   INTEGER,
        fetched_at  TEXT,
        PRIMARY KEY (season, game_type)
    );
    """)
    for table, cols in (("skater_games", SKATER_COLS), ("goalie_games", GOALIE_COLS)):
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for c in cols:
            if c not in have:
                kind = "REAL" if c.endswith("_pct") else \
                       "TEXT" if c == "decision" else "INTEGER"
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {c} {kind}")
    conn.commit()


def upsert_sql(table, cols):
    """INSERT that updates only its own columns on conflict, so the batch and
    boxscore paths can run in either order without wiping each other's fields
    (e.g. sweater is boxscore-only, missed-shot subtypes are batch-only)."""
    setters = ", ".join(f"{c}=excluded.{c}" for c in cols
                        if c not in ("game_id", "player_id"))
    return (f"INSERT INTO {table} ({','.join(cols)}) "
            f"VALUES ({','.join(':' + c for c in cols)}) "
            f"ON CONFLICT(game_id, player_id) DO UPDATE SET {setters}")


def get_json(url, params=None, timeout=60):
    last = None
    for attempt in range(RETRY_MAX):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 404:
                raise LookupError("404 Not Found")
            r.raise_for_status()
            return r.json()
        except LookupError:
            raise
        except Exception as e:
            last = e
            time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up on {url}: {last}")


def enumerate_games(conn):
    print(f"fetching {STATS_API}/game (all games since 1917, one call) ...")
    games = get_json(f"{STATS_API}/game", timeout=180)["data"]
    rows = [(g["id"], g.get("gameDate"), g.get("season"), g.get("gameType"))
            for g in games
            if g.get("gameType") in GAME_TYPES
            and g.get("gameStateId") == FINAL_STATE]
    conn.executemany(
        """INSERT OR IGNORE INTO games_to_scrape (game_id, game_date, season, game_type)
           VALUES (?,?,?,?)""", rows)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM games_to_scrape").fetchone()[0]
    print(f"{len(games):,} games in index -> {len(rows):,} final reg/playoff games "
          f"-> {n:,} total in queue")


def parse_boxscore(d):
    """One boxscore payload -> (skater rows, goalie rows) as dicts."""
    home = (d.get("homeTeam") or {}).get("abbrev")
    away = (d.get("awayTeam") or {}).get("abbrev")
    stats = d.get("playerByGameStats")
    if not stats:
        raise ValueError("no playerByGameStats")
    meta = {"game_id": d["id"], "game_date": d.get("gameDate"),
            "season": d.get("season"), "game_type": d.get("gameType")}
    skaters, goalies = [], []
    for side, team, opp, is_home in (("homeTeam", home, away, 1),
                                     ("awayTeam", away, home, 0)):
        p = stats.get(side) or {}
        base = dict(meta, team_abbr=team, opponent=opp, home=is_home,
                    matchup=f"{team} vs {opp}" if is_home else f"{team} @ {opp}")
        for f in (p.get("forwards") or []) + (p.get("defense") or []):
            row = dict(base, player_id=f["playerId"],
                       player_name=(f.get("name") or {}).get("default"),
                       position=f.get("position"), sweater=f.get("sweaterNumber"),
                       faceoff_pct=f.get("faceoffWinningPctg"),
                       toi_sec=toi_to_sec(f.get("toi")))
            for col, key in SKATER_INTS:
                row[col] = f.get(key)
            skaters.append(row)
        for g in p.get("goalies") or []:
            row = dict(base, player_id=g["playerId"],
                       player_name=(g.get("name") or {}).get("default"),
                       position=g.get("position"), sweater=g.get("sweaterNumber"),
                       save_pct=g.get("savePctg"), toi_sec=toi_to_sec(g.get("toi")),
                       starter=None if g.get("starter") is None else int(g["starter"]),
                       decision=g.get("decision"))
            for col, key in GOALIE_INTS:
                row[col] = g.get(key)
            for prefix, key in GOALIE_SPLITS:
                row[f"{prefix}_saves"], row[f"{prefix}_sa"] = split_pair(g.get(key))
            goalies.append(row)
    return skaters, goalies


BOX_SK_COLS = META_COLS + [c for c, _ in SKATER_INTS] + ["faceoff_pct", "toi_sec"]
BOX_GL_COLS = (META_COLS + [c for c, _ in GOALIE_INTS]
               + [f"{p}_{s}" for p, _ in GOALIE_SPLITS for s in ("saves", "sa")]
               + ["save_pct", "toi_sec", "starter", "decision"])
BATCH_SK_COLS = [c for c in SKATER_COLS if c != "sweater"]
BATCH_GL_COLS = [c for c in GOALIE_COLS if c != "sweater"]

SK_SQL = upsert_sql("skater_games", BOX_SK_COLS)
GL_SQL = upsert_sql("goalie_games", BOX_GL_COLS)


def scrape(conn, start, end, retry_failed, delay, limit=None):
    where = "status IN ('pending','error')" if retry_failed else "status = 'pending'"
    if start is not None:
        where += f" AND season/10000 >= {int(start)}"
    if end is not None:
        where += f" AND season/10000 <= {int(end)}"
    games = conn.execute(
        f"SELECT * FROM games_to_scrape WHERE {where} ORDER BY game_id"
    ).fetchall()
    if limit:
        games = games[:limit]
    if not games:
        print("Nothing to scrape.")
        return
    total = len(games)
    print(f"Scraping {total:,} games (~{total * (delay + 0.15) / 3600:.1f}h) ...")
    t0 = time.time()
    ok = fail = 0
    for i, g in enumerate(games, 1):
        try:
            d = get_json(f"{API}/gamecenter/{g['game_id']}/boxscore")
            skaters, goalies = parse_boxscore(d)
            conn.executemany(SK_SQL, skaters)
            conn.executemany(GL_SQL, goalies)
            conn.execute(
                "UPDATE games_to_scrape SET status='done', error_msg=NULL, scraped_at=? "
                "WHERE game_id=?", (dt.datetime.now(dt.UTC).isoformat(), g["game_id"]))
            conn.commit()
            ok += 1
        except Exception as e:
            conn.execute(
                "UPDATE games_to_scrape SET status='error', error_msg=? WHERE game_id=?",
                (str(e)[:500], g["game_id"]))
            conn.commit()
            fail += 1
            print(f"[{i:>6}/{total}] {g['game_id']}  ERROR: {e}", file=sys.stderr)
        if i % 50 == 0 or i == total:
            rate = i / (time.time() - t0)
            print(f"[{i:>6}/{total}] season {g['season']}  ok={ok:,} err={fail:,}  "
                  f"({rate:.1f} games/s, eta {(total - i) / rate / 3600:.1f}h)")
        time.sleep(delay)
    print(f"\nDONE. {ok:,} scraped, {fail:,} failed in {(time.time() - t0) / 3600:.1f}h")


def month_windows(start_year):
    """[('YYYY-MM-01', next), ...] covering Aug of the season's start year
    through Jul of the next — every date an NHL season can touch."""
    months = [(start_year + (m - 1) // 12, (m - 1) % 12 + 1) for m in range(8, 21)]
    days = [f"{y}-{m:02d}-01" for y, m in months]
    return list(zip(days, days[1:]))


def fetch_report(report, season, gtype, delay):
    def fetch(extra=""):
        cay = f"seasonId={season} and gameTypeId={gtype}" + extra
        rows = get_json(f"{STATS_API}/{report}",
                        params={"isGame": "true", "isAggregate": "false",
                                "limit": -1, "cayenneExp": cay})["data"]
        time.sleep(delay)
        return rows

    rows = fetch()
    if len(rows) < REPORT_CAP:
        return rows
    out = []
    for d0, d1 in month_windows(season // 10000):
        sub = fetch(f' and gameDate>="{d0}" and gameDate<"{d1}"')
        if len(sub) >= REPORT_CAP:
            raise RuntimeError(f"{report} {season} slice {d0} hit the row cap")
        out.extend(sub)
    return out


def merge_report(dst, rows, colmap, season, gtype, cols, pos_default=None):
    for r in rows:
        key = (r["gameId"], r["playerId"])
        row = dst.get(key)
        if row is None:
            team, opp = r.get("teamAbbrev"), r.get("opponentTeamAbbrev")
            home = 1 if r.get("homeRoad") == "H" else 0
            row = dst[key] = dict.fromkeys(cols)
            row.update(game_id=r["gameId"], game_date=r.get("gameDate"),
                       season=season, game_type=gtype, player_id=r["playerId"],
                       team_abbr=team, opponent=opp, home=home,
                       matchup=f"{team} vs {opp}" if home else f"{team} @ {opp}")
        for col, field in colmap.items():
            v = r.get(field)
            if v is not None:
                row[col] = v
        if row["position"] is None:
            row["position"] = r.get("positionCode") or pos_default


def season_batch(conn, start, end, force, delay):
    where = "1=1"
    if start is not None:
        where += f" AND season/10000 >= {int(start)}"
    if end is not None:
        where += f" AND season/10000 <= {int(end)}"
    seasons = [r[0] for r in conn.execute(
        f"SELECT DISTINCT season FROM games_to_scrape WHERE {where} ORDER BY season")]
    if not seasons:
        print("No seasons enumerated — run `collect.py enumerate` first.")
        return
    done = set() if force else {(r[0], r[1]) for r in
                                conn.execute("SELECT season, game_type FROM seasons_batched")}
    t0 = time.time()
    for season in seasons:
        for gtype in GAME_TYPES:
            if (season, gtype) in done:
                continue
            skaters, goalies = {}, {}
            try:
                for report, colmap in SK_REPORTS:
                    if season >= REPORT_FROM.get(report, 0):
                        merge_report(skaters, fetch_report(report, season, gtype, delay),
                                     colmap, season, gtype, BATCH_SK_COLS)
                for report, colmap in GL_REPORTS:
                    if season >= REPORT_FROM.get(report, 0):
                        rows = fetch_report(report, season, gtype, delay)
                        merge_report(goalies, rows, colmap, season, gtype,
                                     BATCH_GL_COLS, pos_default="G")
                        if report == "goalie/summary":
                            for r in rows:
                                d = ("W" if r.get("wins") else
                                     "L" if r.get("losses") else
                                     "O" if r.get("otLosses") else
                                     "T" if r.get("ties") else None)
                                if d:
                                    goalies[(r["gameId"], r["playerId"])]["decision"] = d
            except Exception as e:
                print(f"[{season} type {gtype}] ERROR: {e}", file=sys.stderr)
                continue
            conn.executemany(upsert_sql("skater_games", BATCH_SK_COLS),
                             list(skaters.values()))
            conn.executemany(upsert_sql("goalie_games", BATCH_GL_COLS),
                             list(goalies.values()))
            seen = {k[0] for k in skaters} | {k[0] for k in goalies}
            now = dt.datetime.now(dt.UTC).isoformat()
            conn.executemany(
                "UPDATE games_to_scrape SET status='batch', scraped_at=? "
                "WHERE game_id=? AND status IN ('pending','error')",
                [(now, gid) for gid in seen])
            conn.execute(
                "INSERT OR REPLACE INTO seasons_batched VALUES (?,?,?,?,?)",
                (season, gtype, len(skaters), len(goalies), now))
            conn.commit()
            print(f"[{season} type {gtype}] {len(skaters):,} skater rows, "
                  f"{len(goalies):,} goalie rows, {len(seen):,} games "
                  f"({(time.time() - t0) / 60:.1f}m elapsed)")
    print(f"\nDONE in {(time.time() - t0) / 60:.1f}m")


def backfill_names(conn, delay):
    """Boxscores abbreviate names ("J. Greenway"); the bios report has full ones."""
    seasons = [r[0] for r in conn.execute(
        "SELECT DISTINCT season FROM ("
        "  SELECT season FROM skater_games UNION SELECT season FROM goalie_games"
        ") WHERE season IS NOT NULL ORDER BY season")]
    print(f"resolving full names across {len(seasons)} seasons ...")
    names = {}
    for s in seasons:
        for report, field in (("skater", "skaterFullName"), ("goalie", "goalieFullName")):
            try:
                data = get_json(f"{STATS_API}/{report}/bios",
                                params={"limit": -1,
                                        "cayenneExp": f"seasonId={s}"})["data"]
            except Exception as e:
                print(f"  {s} {report}: ERROR {e}", file=sys.stderr)
                continue
            names.update((p["playerId"], p[field]) for p in data if p.get(field))
            time.sleep(delay)
        print(f"  [{s}] {len(names):,} players known")
    rows = [(v, k) for k, v in names.items()]
    conn.executemany("UPDATE skater_games SET player_name=? WHERE player_id=?", rows)
    conn.executemany("UPDATE goalie_games SET player_name=? WHERE player_id=?", rows)
    conn.commit()
    print(f"done. updated names for {len(rows):,} players.")


def show_stats(conn):
    q = lambda sql: conn.execute(sql).fetchone()[0]
    total = q("SELECT COUNT(*) FROM games_to_scrape")
    by = dict(conn.execute(
        "SELECT status, COUNT(*) FROM games_to_scrape GROUP BY status"))
    rng = conn.execute(
        "SELECT MIN(season)/10000, MAX(season)/10000 FROM games_to_scrape").fetchone()
    print(f"games:        {total:,}  batch={by.get('batch', 0):,}  "
          f"boxscore={by.get('done', 0):,}  error={by.get('error', 0):,}  "
          f"pending={by.get('pending', 0):,}  (seasons {rng[0]}–{rng[1]})")
    print(f"seasons batched: {q('SELECT COUNT(*) FROM seasons_batched'):,} "
          f"season-types")
    print(f"skater rows:  {q('SELECT COUNT(*) FROM skater_games'):,}")
    print(f"goalie rows:  {q('SELECT COUNT(*) FROM goalie_games'):,}")
    abbreviated = q("SELECT COUNT(DISTINCT player_id) FROM skater_games "
                    "WHERE player_name LIKE '_. %'")
    if abbreviated:
        print(f"  -> {abbreviated:,} skaters still have abbreviated names: "
              "run `collect.py backfill-names`")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("enumerate", help="Pull the full game index (one request)")

    sb = sub.add_parser("season-batch",
                        help="Bulk-ingest whole seasons via the stats-REST game reports")
    sb.add_argument("--start", type=int, help="first season start-year (e.g. 1997)")
    sb.add_argument("--end", type=int, help="last season start-year inclusive")
    sb.add_argument("--force", action="store_true",
                    help="re-fetch seasons already in seasons_batched")
    sb.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    s = sub.add_parser("scrape",
                       help="Scrape leftover games' boxscores (also adds sweater numbers)")
    s.add_argument("--start", type=int, help="first season start-year (e.g. 1997)")
    s.add_argument("--end", type=int, help="last season start-year inclusive")
    s.add_argument("--retry-failed", action="store_true")
    s.add_argument("--limit", type=int, help="stop after N games (smoke test)")
    s.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    b = sub.add_parser("backfill-names", help="Replace abbreviated names with full ones")
    b.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    sub.add_parser("stats", help="show progress")

    args = ap.parse_args()
    conn = open_db()
    init_db(conn)

    if args.cmd == "enumerate":
        enumerate_games(conn)
    elif args.cmd == "season-batch":
        season_batch(conn, args.start, args.end, args.force, args.delay)
    elif args.cmd == "scrape":
        scrape(conn, args.start, args.end, args.retry_failed, args.delay, args.limit)
    elif args.cmd == "backfill-names":
        backfill_names(conn, args.delay)
    show_stats(conn)


if __name__ == "__main__":
    main()
