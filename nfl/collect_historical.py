"""Scrape every NFL game box score from Pro-Football-Reference (1933+).

Uses patchright (a Playwright fork that defeats Cloudflare's bot wall) with
a non-headless persistent context. Two phases:

  Phase A (enumerate) — hit /years/YYYY/games.htm for each season and pull
  every boxscore URL into the games_to_scrape table. ~93 requests.

  Phase B (scrape) — visit each boxscore page and extract the player_offense
  table, inserting one row per (game, player) into player_games. ~17K
  requests. SQLite checkpoint per game; resumable.

PFR serves one boxscore per page, so Phase B cannot be batched — it's one
request per game by design. PFR rate limit guidance: <=20 req/min. Default
delay 5s is well under.

Usage:
    python collect_historical.py enumerate                      # Phase A
    python collect_historical.py scrape                         # Phase B (all pending)
    python collect_historical.py scrape --start 1980 --end 1990 # subset
    python collect_historical.py scrape --retry-failed          # second pass on errored games
    python collect_historical.py backfill-opponent              # repair opponent/matchup (no network)
    python collect_historical.py stats                          # show progress
"""
import argparse
import datetime as dt
import re
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

from bs4 import BeautifulSoup, Comment
from patchright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nfl_full.sqlite"
USER_DATA_DIR = HERE / ".browser_profile"

BASE = "https://www.pro-football-reference.com"
DEFAULT_DELAY = 5.0
CF_CHALLENGE_TIMEOUT = 30  # seconds to wait for "Just a moment..." to clear


# ---------------- DB ----------------
def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS games_to_scrape (
        game_id      TEXT PRIMARY KEY,
        game_date    TEXT,
        season       INTEGER,
        week         TEXT,           -- '1'..'18' or 'Wild Card', etc.
        game_type    TEXT,           -- 'REG' / 'POST'
        home_team    TEXT,
        away_team    TEXT,
        status       TEXT DEFAULT 'pending',  -- 'pending','done','error'
        error_msg    TEXT,
        scraped_at   TEXT
    );

    CREATE TABLE IF NOT EXISTS player_games (
        game_id        TEXT NOT NULL,
        game_date      TEXT,
        season         INTEGER,
        game_type      TEXT,
        player_pfr_id  TEXT NOT NULL,
        player_name    TEXT,
        team_abbr      TEXT,
        opponent       TEXT,
        matchup        TEXT,
        pass_cmp       INTEGER,
        pass_att       INTEGER,
        pass_yds       INTEGER,
        pass_td        INTEGER,
        pass_int       INTEGER,
        sacks          INTEGER,
        sack_yds       INTEGER,
        rush_att       INTEGER,
        rush_yds       INTEGER,
        rush_td        INTEGER,
        tgt            INTEGER,
        rec            INTEGER,
        rec_yds        INTEGER,
        rec_td         INTEGER,
        fumbles        INTEGER,
        fumbles_lost   INTEGER,
        PRIMARY KEY (game_id, player_pfr_id)
    );

    CREATE INDEX IF NOT EXISTS idx_pg_season ON player_games(season);
    CREATE INDEX IF NOT EXISTS idx_pg_player ON player_games(player_pfr_id);

    CREATE TABLE IF NOT EXISTS seasons_enumerated (
        season         INTEGER PRIMARY KEY,
        n_games        INTEGER,
        enumerated_at  TEXT
    );
    """)


# ---------------- Browser ----------------
# Asset types we never need from PFR. Blocking them saves ~70% of bytes per
# page (CSS, fonts, banner ads, analytics scripts) without affecting the
# HTML table content we parse.
BLOCKED_RESOURCE_TYPES = {"image", "stylesheet", "font", "media"}
BLOCKED_URL_PATTERNS = (
    "google-analytics", "googletagmanager", "doubleclick",
    "adservice", "facebook", "twitter", "amazon-adsystem",
)


class Browser:
    """Wraps patchright's persistent context. Bypasses Cloudflare via
    non-headless chromium with a stable user data directory.

    Periodically rotates the page (and optionally the whole context) to
    avoid Chromium memory bloat over long scrapes."""

    def __init__(self, restart_every=500):
        self._pw = None
        self._ctx = None
        self._page = None
        self._restart_every = restart_every
        self._since_restart = 0

    def __enter__(self):
        self._pw = sync_playwright().start()
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._clear_stale_locks()
        self._open_context()
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()
            # Best-effort cleanup of singleton lock files so the next run
            # doesn't trip "Opening in existing browser session".
            self._clear_stale_locks()

    @staticmethod
    def _clear_stale_locks():
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (USER_DATA_DIR / name).unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def _open_context(self):
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            no_viewport=True,
        )
        self._page = self._ctx.new_page()
        # Block heavyweight resources — saves bandwidth + DOM weight.
        def _route(route):
            req = route.request
            if req.resource_type in BLOCKED_RESOURCE_TYPES:
                return route.abort()
            if any(p in req.url for p in BLOCKED_URL_PATTERNS):
                return route.abort()
            return route.continue_()
        self._page.route("**/*", _route)
        self._since_restart = 0

    def _restart(self):
        print(f"   [browser restart after {self._since_restart} pages]", file=sys.stderr)
        try:
            self._ctx.close()
        except Exception:
            pass
        self._open_context()

    def fetch(self, url, retries=2):
        """Navigate to URL, wait for Cloudflare to clear, return HTML."""
        if self._since_restart >= self._restart_every:
            self._restart()
        for attempt in range(retries + 1):
            try:
                # 'commit' returns as soon as the response headers arrive — we
                # don't need to wait for images/CSS/JS to finish loading.
                self._page.goto(url, wait_until="commit", timeout=60_000)
                # Wait briefly for the page body to be present.
                self._page.wait_for_selector("body", timeout=10_000)
                # Cloudflare check — most pages will not have the challenge
                # title once cookies are warm.
                title = self._page.title()
                if "Just a moment" in title:
                    for _ in range(CF_CHALLENGE_TIMEOUT):
                        time.sleep(1)
                        title = self._page.title()
                        if title and "Just a moment" not in title:
                            break
                    else:
                        raise RuntimeError("Cloudflare challenge did not clear")
                self._since_restart += 1
                return self._page.content()
            except Exception as e:
                if attempt == retries:
                    raise
                print(f"   retry {attempt+1}/{retries}: {e}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))


# ---------------- Phase A: enumerate ----------------
GAME_LINK_RE = re.compile(r'/boxscores/(\d{8}0\w{3})\.htm')


def parse_schedule_page(html, season):
    """Extract (game_id, game_date, week, game_type, home_team, away_team) for each row."""
    soup = BeautifulSoup(html, "html.parser")
    out = []
    table = soup.find("table", id="games")
    if not table:
        # Some pre-1970 seasons split into multiple tables; fall back to all boxscore links.
        for m in GAME_LINK_RE.finditer(html):
            gid = m.group(1)
            date_iso = f"{gid[:4]}-{gid[4:6]}-{gid[6:8]}"
            out.append({
                "game_id": gid, "game_date": date_iso, "season": season,
                "week": None, "game_type": None,
                "home_team": gid[-3:], "away_team": None,
            })
        return out

    for tr in table.find("tbody").find_all("tr"):
        # Skip section headers
        if "thead" in (tr.get("class") or []):
            continue
        # Boxscore link is in a td with data-stat='boxscore_word' (modern) or in any cell
        link = tr.find("a", href=GAME_LINK_RE)
        if not link:
            continue
        gid = GAME_LINK_RE.search(link["href"]).group(1)
        # Cells by data-stat
        cells = {td.get("data-stat"): td for td in tr.find_all(["td", "th"])}
        week = (cells.get("week_num") or {}).get_text(strip=True) if cells.get("week_num") else None
        date_iso = (cells.get("game_date") or {}).get_text(strip=True) if cells.get("game_date") else None
        if not date_iso:
            # Derive from game_id (always YYYYMMDD prefix)
            date_iso = f"{gid[:4]}-{gid[4:6]}-{gid[6:8]}"
        # Game type: post-season weeks have names; regular season is numeric or empty
        gtype = "POST" if (week and not week.isdigit()) else "REG"
        # Teams
        winner = (cells.get("winner") or {}).get_text(strip=True) if cells.get("winner") else None
        loser = (cells.get("loser") or {}).get_text(strip=True) if cells.get("loser") else None
        # "game_location" cell holds '@' when winner was away
        at_cell = cells.get("game_location")
        winner_was_away = at_cell and at_cell.get_text(strip=True) == "@"
        if winner_was_away:
            home_team, away_team = loser, winner
        else:
            home_team, away_team = winner, loser
        out.append({
            "game_id": gid, "game_date": date_iso, "season": season,
            "week": week, "game_type": gtype,
            "home_team": home_team, "away_team": away_team,
        })
    return out


def enumerate_seasons(conn, browser, start, end, delay):
    cur = conn.cursor()
    for season in range(start, end + 1):
        already = cur.execute(
            "SELECT n_games FROM seasons_enumerated WHERE season=?", (season,)
        ).fetchone()
        if already:
            print(f"[skip] {season} already enumerated ({already[0]} games)")
            continue
        url = f"{BASE}/years/{season}/games.htm"
        print(f"[fetch] {url}")
        try:
            html = browser.fetch(url)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            time.sleep(delay)
            continue
        games = parse_schedule_page(html, season)
        cur.executemany(
            """INSERT OR IGNORE INTO games_to_scrape
               (game_id, game_date, season, week, game_type, home_team, away_team)
               VALUES (:game_id, :game_date, :season, :week, :game_type, :home_team, :away_team)""",
            games,
        )
        cur.execute(
            "INSERT OR REPLACE INTO seasons_enumerated (season, n_games, enumerated_at) "
            "VALUES (?, ?, ?)",
            (season, len(games), dt.datetime.utcnow().isoformat()),
        )
        conn.commit()
        print(f"  -> {len(games)} games")
        time.sleep(delay)


# ---------------- Phase B: scrape boxscores ----------------
def extract_player_offense(html):
    """Returns list of dicts (one per player) from the player_offense table.

    PFR hides several tables inside HTML comments to evade scrapers. We
    expand any comment that contains <table id='player_offense'>.
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="player_offense")
    if not table:
        # Walk comments
        for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
            if "player_offense" in c:
                inner = BeautifulSoup(c, "html.parser")
                table = inner.find("table", id="player_offense")
                if table:
                    break
    if not table:
        return []

    rows = []
    for tr in table.find("tbody").find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        link = tr.find("th", {"data-stat": "player"})
        if not link:
            continue
        a = link.find("a")
        if not a:
            continue
        href = a.get("href", "")
        # /players/B/BradTo00.htm -> BradTo00
        m = re.search(r"/players/[A-Z]/([A-Za-z0-9.\-]+)\.htm", href)
        if not m:
            continue
        pfr_id = m.group(1)
        name = a.get_text(strip=True)

        cells = {td.get("data-stat"): td.get_text(strip=True) for td in tr.find_all("td")}

        def i(key):
            v = cells.get(key, "")
            if v in ("", None):
                return None
            try:
                return int(v)
            except ValueError:
                try:
                    return int(float(v))
                except ValueError:
                    return None

        rows.append({
            "player_pfr_id": pfr_id,
            "player_name": name,
            "team_abbr": cells.get("team", "") or None,
            "pass_cmp": i("pass_cmp"),
            "pass_att": i("pass_att"),
            "pass_yds": i("pass_yds"),
            "pass_td":  i("pass_td"),
            "pass_int": i("pass_int"),
            "sacks":    i("pass_sacked"),
            "sack_yds": i("pass_sacked_yds"),
            "rush_att": i("rush_att"),
            "rush_yds": i("rush_yds"),
            "rush_td":  i("rush_td"),
            "tgt":      i("targets"),
            "rec":      i("rec"),
            "rec_yds":  i("rec_yds"),
            "rec_td":   i("rec_td"),
            "fumbles":      i("fumbles"),
            "fumbles_lost": i("fumbles_lost"),
        })
    return rows


def insert_player_rows(conn, game, players):
    if not players:
        return 0
    # Derive opponent/matchup from the two 3-letter team codes present in this
    # game's player rows. games_to_scrape.home_team/away_team hold full names
    # from the schedule page ("Portsmouth Spartans"), which never match the
    # boxscore's team_abbr ("CRD") — so we resolve purely within the boxscore.
    teams = sorted({p["team_abbr"] for p in players if p["team_abbr"]})
    opp_of = {teams[0]: teams[1], teams[1]: teams[0]} if len(teams) == 2 else {}
    sql = """
    INSERT OR REPLACE INTO player_games (
        game_id, game_date, season, game_type,
        player_pfr_id, player_name, team_abbr, opponent, matchup,
        pass_cmp, pass_att, pass_yds, pass_td, pass_int,
        sacks, sack_yds,
        rush_att, rush_yds, rush_td,
        tgt, rec, rec_yds, rec_td,
        fumbles, fumbles_lost
    ) VALUES (
        :game_id, :game_date, :season, :game_type,
        :player_pfr_id, :player_name, :team_abbr, :opponent, :matchup,
        :pass_cmp, :pass_att, :pass_yds, :pass_td, :pass_int,
        :sacks, :sack_yds,
        :rush_att, :rush_yds, :rush_td,
        :tgt, :rec, :rec_yds, :rec_td,
        :fumbles, :fumbles_lost
    )"""
    batch = []
    for p in players:
        team = p["team_abbr"]
        opp = opp_of.get(team)
        matchup = f"{team} vs {opp}" if team and opp else None
        batch.append({
            **p,
            "game_id": game["game_id"],
            "game_date": game["game_date"],
            "season": game["season"],
            "game_type": game["game_type"],
            "opponent": opp,
            "matchup": matchup,
        })
    conn.executemany(sql, batch)
    return len(batch)


def scrape_games(conn, browser, start, end, retry_failed, delay):
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

    total = len(games)
    print(f"Scraping {total:,} games...")
    t0 = time.time()
    success = 0
    fail = 0
    recent = deque(maxlen=30)  # per-game seconds for last 30 games
    for i, g in enumerate(games, 1):
        url = f"{BASE}/boxscores/{g['game_id']}.htm"
        t_start = time.time()
        try:
            html = browser.fetch(url)
            players = extract_player_offense(html)
            n = insert_player_rows(conn, dict(g), players)
            conn.execute(
                "UPDATE games_to_scrape SET status='done', error_msg=NULL, scraped_at=? WHERE game_id=?",
                (dt.datetime.utcnow().isoformat(), g["game_id"]),
            )
            conn.commit()
            success += 1
            recent.append(time.time() - t_start + delay)
            avg = sum(recent) / len(recent)
            eta_h = (total - i) * avg / 3600
            print(f"[{i:>5}/{total}] {g['season']} {g['game_id']}  +{n} players  "
                  f"(last30 avg {avg:.1f}s/game, eta {eta_h:.1f}h)")
        except Exception as e:
            conn.execute(
                "UPDATE games_to_scrape SET status='error', error_msg=? WHERE game_id=?",
                (str(e)[:500], g["game_id"]),
            )
            conn.commit()
            fail += 1
            print(f"[{i:>5}/{total}] {g['season']} {g['game_id']}  ERROR: {e}", file=sys.stderr)
        time.sleep(delay)

    print(f"\nDONE. {success:,} succeeded, {fail:,} failed in {(time.time()-t0)/3600:.1f}h")


def recheck_empty_games(conn, browser, delay, start=None, end=None):
    """Re-scrape games marked 'done' that produced zero player rows.

    These are usually silent failures — the page loaded but the
    player_offense table didn't render or was malformed. After a successful
    re-scrape that still produces 0 rows, we mark status='empty' so future
    rechecks skip it (likely a truly missing/cancelled game)."""
    where = "g.status = 'done'"
    if start is not None:
        where += f" AND g.season >= {start}"
    if end is not None:
        where += f" AND g.season <= {end}"

    games = conn.execute(f"""
        SELECT g.* FROM games_to_scrape g
        LEFT JOIN (SELECT game_id, COUNT(*) AS n FROM player_games GROUP BY game_id) p
          ON p.game_id = g.game_id
        WHERE {where} AND COALESCE(p.n, 0) = 0
        ORDER BY g.season, g.game_date, g.game_id
    """).fetchall()
    if not games:
        print("\nRecheck: no zero-player 'done' games to recheck.")
        return

    print(f"\nRecheck: {len(games):,} games marked done but have 0 player rows. Re-scraping...")
    fixed = 0
    still_empty = 0
    for i, g in enumerate(games, 1):
        url = f"{BASE}/boxscores/{g['game_id']}.htm"
        try:
            html = browser.fetch(url)
            players = extract_player_offense(html)
            n = insert_player_rows(conn, dict(g), players)
            if n > 0:
                conn.execute(
                    "UPDATE games_to_scrape SET status='done', error_msg=NULL, scraped_at=? WHERE game_id=?",
                    (dt.datetime.utcnow().isoformat(), g["game_id"]),
                )
                fixed += 1
                print(f"[{i:>4}/{len(games)}] {g['season']} {g['game_id']}  +{n} players (FIXED)")
            else:
                # Two consecutive empty results — mark as empty to skip future rechecks.
                conn.execute(
                    "UPDATE games_to_scrape SET status='empty', error_msg='no player_offense after recheck', scraped_at=? WHERE game_id=?",
                    (dt.datetime.utcnow().isoformat(), g["game_id"]),
                )
                still_empty += 1
                print(f"[{i:>4}/{len(games)}] {g['season']} {g['game_id']}  still 0 players -> 'empty'")
            conn.commit()
        except Exception as e:
            print(f"[{i:>4}/{len(games)}] {g['season']} {g['game_id']}  RECHECK ERROR: {e}", file=sys.stderr)
        time.sleep(delay)

    print(f"\nRecheck done. fixed={fixed:,}  still_empty={still_empty:,}")


def backfill_opponent(conn):
    """Repair opponent/matchup on already-scraped rows (no network).

    Newly scraped games get these from insert_player_rows directly; this fixes
    rows written before that derivation existed. Both come from the two distinct
    team_abbr values present per game; games without exactly two are left as-is.
    """
    games = conn.execute("""
        SELECT game_id, GROUP_CONCAT(DISTINCT team_abbr) AS teams
        FROM player_games
        WHERE team_abbr IS NOT NULL
        GROUP BY game_id
    """).fetchall()
    print(f"scanning {len(games):,} games...")
    fixed = weird = 0
    for g in games:
        teams = [t for t in (g["teams"] or "").split(",") if t]
        if len(teams) != 2:
            weird += 1
            continue
        a, b = teams
        conn.execute(
            "UPDATE player_games SET opponent=?, matchup=? WHERE game_id=? AND team_abbr=?",
            (b, f"{a} vs {b}", g["game_id"], a),
        )
        conn.execute(
            "UPDATE player_games SET opponent=?, matchup=? WHERE game_id=? AND team_abbr=?",
            (a, f"{b} vs {a}", g["game_id"], b),
        )
        fixed += 1
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM player_games WHERE matchup IS NULL"
    ).fetchone()[0]
    print(f"fixed {fixed:,} games  ({weird:,} skipped: not exactly 2 teams; "
          f"{remaining:,} rows still NULL)")


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
    print(f"player rows:        {pg:,}")


# ---------------- Main ----------------
def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    # Defaults stop at 1998 because nflverse already covers 1999+ cleanly via
    # the parallel nfl/ pipeline; no point re-scraping from PFR.
    e = sub.add_parser("enumerate", help="Phase A: pull game URLs from /years/YYYY/games.htm")
    e.add_argument("--start", type=int, default=1933)
    e.add_argument("--end", type=int, default=1998)
    e.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    s = sub.add_parser("scrape", help="Phase B: scrape each pending boxscore")
    s.add_argument("--start", type=int)
    s.add_argument("--end", type=int, default=1998)
    s.add_argument("--retry-failed", action="store_true")
    s.add_argument("--no-recheck", action="store_true",
                   help="skip the post-run recheck of zero-player 'done' games")
    s.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    r = sub.add_parser("recheck", help="Re-scrape games marked done that produced 0 player rows")
    r.add_argument("--start", type=int)
    r.add_argument("--end", type=int)
    r.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    sub.add_parser("backfill-opponent", help="Repair opponent/matchup from team_abbr (no network)")
    sub.add_parser("stats", help="show progress")

    args = ap.parse_args()
    conn = open_db()
    init_db(conn)

    if args.cmd == "stats":
        show_stats(conn)
        return
    if args.cmd == "backfill-opponent":
        backfill_opponent(conn)
        return

    with Browser() as browser:
        if args.cmd == "enumerate":
            enumerate_seasons(conn, browser, args.start, args.end, args.delay)
        elif args.cmd == "scrape":
            scrape_games(conn, browser, args.start, args.end, args.retry_failed, args.delay)
            if not args.no_recheck:
                recheck_empty_games(conn, browser, args.delay, args.start, args.end)
        elif args.cmd == "recheck":
            recheck_empty_games(conn, browser, args.delay, args.start, args.end)

    show_stats(conn)


if __name__ == "__main__":
    main()
