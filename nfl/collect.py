"""Scrape NFL player-game box scores from Pro-Football-Reference into nfl_full.sqlite."""
import argparse
import datetime as dt
import gzip
import re
import sqlite3
import sys
import time
from collections import deque
from pathlib import Path

from bs4 import BeautifulSoup, Comment
from curl_cffi import requests as cffi
from patchright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "nfl_full.sqlite"
USER_DATA_DIR = HERE / ".browser_profile"

BASE = "https://www.pro-football-reference.com"
# Sports Reference blocks above 20 requests/minute; 3.5s leaves headroom.
DEFAULT_DELAY = 3.5
CF_CHALLENGE_TIMEOUT = 60
IMPERSONATE = "chrome"


def current_season() -> int:
    today = dt.date.today()
    return today.year if today.month >= 9 else today.year - 1


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
        week         TEXT,
        game_type    TEXT,
        home_team    TEXT,
        away_team    TEXT,
        status       TEXT DEFAULT 'pending',
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
    -- defense/special-teams dumps borrow matchup from here by (game, team)
    CREATE INDEX IF NOT EXISTS idx_pg_game_team ON player_games(game_id, team_abbr);

    CREATE TABLE IF NOT EXISTS seasons_enumerated (
        season         INTEGER PRIMARY KEY,
        n_games        INTEGER,
        enumerated_at  TEXT
    );

    CREATE TABLE IF NOT EXISTS game_html (
        game_id     TEXT PRIMARY KEY,
        html        BLOB,
        fetched_at  TEXT
    );
    """)
    conn.executescript(box_tables_sql())
    conn.commit()


class Browser:
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
            self._clear_stale_locks()

    @staticmethod
    def _clear_stale_locks():
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (USER_DATA_DIR / name).unlink(missing_ok=True)

    def _open_context(self):
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            no_viewport=True,
        )
        self._page = self._ctx.new_page()
        self._since_restart = 0

    def _restart(self):
        print(f"   [browser restart after {self._since_restart} pages]", file=sys.stderr)
        try:
            self._ctx.close()
        except Exception:
            pass
        self._open_context()

    def credentials(self):
        """User-Agent and cookies of the current context."""
        return self._page.evaluate("navigator.userAgent"), self._ctx.cookies()

    def fetch(self, url, retries=2):
        """Navigate, wait for Cloudflare to clear, return HTML."""
        # ponytail: kept — Chromium leaks memory over a multi-hour scrape; the
        # periodic restart is the only thing that keeps long runs from dying.
        if self._since_restart >= self._restart_every:
            self._restart()
        for attempt in range(retries + 1):
            try:
                self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                deadline = time.time() + CF_CHALLENGE_TIMEOUT
                while time.time() < deadline:
                    title = self._page.title()
                    if title and "Just a moment" not in title:
                        break
                    time.sleep(1)
                else:
                    raise RuntimeError("Cloudflare challenge did not clear")
                self._since_restart += 1
                return self._page.content()
            except Exception as e:
                if attempt == retries:
                    raise
                print(f"   retry {attempt+1}/{retries}: {e}", file=sys.stderr)
                time.sleep(5 * (attempt + 1))


class Fetcher:
    """Mints a Cloudflare clearance cookie in the browser, then fetches over plain HTTP."""

    def __init__(self):
        self._sess = None

    def __enter__(self):
        self._mint()
        return self

    def __exit__(self, *exc):
        if self._sess:
            self._sess.close()

    def _mint(self):
        with Browser() as browser:
            browser.fetch(BASE + "/")
            ua, cookies = browser.credentials()
        sess = cffi.Session(impersonate=IMPERSONATE)
        sess.headers["User-Agent"] = ua
        for c in cookies:
            sess.cookies.set(c["name"], c["value"], domain=c["domain"])
        if self._sess:
            self._sess.close()
        self._sess = sess
        print("   [clearance minted]", file=sys.stderr)

    def fetch(self, url, retries=2):
        """Fetch over HTTP, re-minting clearance if Cloudflare challenges us."""
        for attempt in range(retries + 1):
            try:
                r = self._sess.get(url, timeout=40)
                if r.status_code == 404:
                    raise RuntimeError("404 Not Found")
                if r.status_code == 200 and "Just a moment" not in r.text[:2000]:
                    return r.text
                err = RuntimeError(f"challenged (status {r.status_code})")
            except RuntimeError:
                raise
            except Exception as e:
                err = e
            if attempt == retries:
                raise err
            print(f"   retry {attempt + 1}/{retries}: {err}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
            self._mint()


GAME_LINK_RE = re.compile(r'/boxscores/(\d{8}0\w{3})\.htm')


def parse_schedule_page(html, season):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    table = soup.find("table", id="games")
    if not table:
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
        if "thead" in (tr.get("class") or []):
            continue
        link = tr.find("a", href=GAME_LINK_RE)
        if not link:
            continue
        gid = GAME_LINK_RE.search(link["href"]).group(1)
        cells = {td.get("data-stat"): td for td in tr.find_all(["td", "th"])}
        week = (cells.get("week_num") or {}).get_text(strip=True) if cells.get("week_num") else None
        date_iso = (cells.get("game_date") or {}).get_text(strip=True) if cells.get("game_date") else None
        if not date_iso:
            date_iso = f"{gid[:4]}-{gid[4:6]}-{gid[6:8]}"
        gtype = "POST" if (week and not week.isdigit()) else "REG"
        winner = (cells.get("winner") or {}).get_text(strip=True) if cells.get("winner") else None
        loser = (cells.get("loser") or {}).get_text(strip=True) if cells.get("loser") else None
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


def enumerate_seasons(conn, fetcher, start, end, delay, refresh=False):
    cur = conn.cursor()
    for season in range(start, end + 1):
        already = cur.execute(
            "SELECT n_games FROM seasons_enumerated WHERE season=?", (season,)
        ).fetchone()
        if already and not refresh:
            print(f"[skip] {season} already enumerated ({already[0]} games)")
            continue
        url = f"{BASE}/years/{season}/games.htm"
        print(f"[fetch] {url}")
        try:
            html = fetcher.fetch(url)
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


def extract_player_offense(html):
    """One dict per player from the player_offense table."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="player_offense")
    if not table:
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


BOX_TABLES = {
    "player_offense": [
        "pass_cmp", "pass_att", "pass_yds", "pass_td", "pass_int", "pass_sacked",
        "pass_sacked_yds", "pass_long", "pass_rating", "rush_att", "rush_yds",
        "rush_td", "rush_long", "rec", "rec_yds", "rec_td", "rec_long", "targets",
        "fumbles", "fumbles_lost"],
    "player_defense": [
        "def_int", "def_int_yds", "def_int_td", "def_int_long", "sacks",
        "tackles_combined", "tackles_solo", "tackles_assists", "fumbles_rec",
        "fumbles_rec_yds", "fumbles_rec_td", "fumbles_forced", "pass_defended",
        "tackles_loss", "qb_hits"],
    "returns": [
        "kick_ret", "kick_ret_yds", "kick_ret_yds_per_ret", "kick_ret_td",
        "kick_ret_long", "punt_ret", "punt_ret_yds", "punt_ret_yds_per_ret",
        "punt_ret_td", "punt_ret_long"],
    "kicking": [
        "xpm", "xpa", "fgm", "fga", "punt", "punt_yds", "punt_yds_per_punt",
        "punt_long"],
    "passing_advanced": [
        "pass_cmp", "pass_att", "pass_yds", "pass_first_down", "pass_first_down_pct",
        "pass_target_yds", "pass_tgt_yds_per_att", "pass_air_yds",
        "pass_air_yds_per_cmp", "pass_air_yds_per_att", "pass_yac",
        "pass_yac_per_cmp", "pass_drops", "pass_drop_pct", "pass_poor_throws",
        "pass_poor_throw_pct", "pass_sacked", "pass_blitzed", "pass_hurried",
        "pass_hits", "pass_pressured", "pass_pressured_pct", "rush_scrambles",
        "rush_scrambles_yds_per_att"],
    "rushing_advanced": [
        "rush_att", "rush_yds", "rush_td", "rush_first_down",
        "rush_yds_before_contact", "rush_yds_bc_per_rush", "rush_yac",
        "rush_yac_per_rush", "rush_broken_tackles", "rush_broken_tackles_per_rush"],
    "receiving_advanced": [
        "targets", "rec", "rec_yds", "rec_td", "rec_first_down", "rec_air_yds",
        "rec_air_yds_per_rec", "rec_yac", "rec_yac_per_rec", "rec_adot",
        "rec_broken_tackles", "rec_broken_tackles_per_rec", "rec_drops",
        "rec_drop_pct", "rec_target_int", "rec_pass_rating"],
    "defense_advanced": [
        "def_int", "def_targets", "def_cmp", "def_cmp_perc", "def_cmp_yds",
        "def_yds_per_cmp", "def_yds_per_target", "def_cmp_td", "def_pass_rating",
        "def_tgt_yds_per_att", "def_air_yds", "def_yac", "blitzes", "qb_hurry",
        "qb_knockdown", "sacks", "pressures", "tackles_combined", "tackles_missed",
        "tackles_missed_pct"],
}

# Split home/visitor tables; these carry pos instead of team.
SIDE_TABLES = {
    "snap_counts": ["offense", "off_pct", "defense", "def_pct",
                    "special_teams", "st_pct"],
    "starters": [],
}

PLAYER_ID_RE = re.compile(r"/players/[A-Z]/([A-Za-z0-9.\-]+)\.htm")
META_COLS = ["game_id", "game_date", "season", "game_type",
             "player_pfr_id", "player_name"]


def box_tables_sql():
    out = []
    for name, stats in BOX_TABLES.items():
        cols = ",\n        ".join(f"{c} NUMERIC" for c in stats)
        out.append(f"""
    CREATE TABLE IF NOT EXISTS {name} (
        game_id        TEXT NOT NULL,
        game_date      TEXT,
        season         INTEGER,
        game_type      TEXT,
        player_pfr_id  TEXT NOT NULL,
        player_name    TEXT,
        team_abbr      TEXT,
        {cols},
        PRIMARY KEY (game_id, player_pfr_id)
    );
    CREATE INDEX IF NOT EXISTS idx_{name}_season ON {name}(season);
    CREATE INDEX IF NOT EXISTS idx_{name}_player ON {name}(player_pfr_id);""")
    for name, stats in SIDE_TABLES.items():
        cols = "".join(f"\n        {c} NUMERIC," for c in stats)
        out.append(f"""
    CREATE TABLE IF NOT EXISTS {name} (
        game_id        TEXT NOT NULL,
        game_date      TEXT,
        season         INTEGER,
        game_type      TEXT,
        player_pfr_id  TEXT NOT NULL,
        player_name    TEXT,
        side           TEXT,
        pos            TEXT,{cols}
        PRIMARY KEY (game_id, player_pfr_id, pos)
    );
    CREATE INDEX IF NOT EXISTS idx_{name}_season ON {name}(season);""")
    return "\n".join(out)


def _num(v):
    if v is None:
        return None
    v = v.strip().replace("%", "").replace(",", "")
    if v in ("", "-", "--"):
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return int(f) if f.is_integer() else f


def soup_tables(html):
    """Every table on the page by id, including the comment-hidden ones."""
    found = {}

    def scan(soup):
        for t in soup.find_all("table"):
            tid = t.get("id")
            if tid and tid not in found:
                found[tid] = t

    soup = BeautifulSoup(html, "html.parser")
    scan(soup)
    for c in soup.find_all(string=lambda t: isinstance(t, Comment)):
        if "<table" in c:
            scan(BeautifulSoup(c, "html.parser"))
    return found


def parse_rows(table):
    """(player_pfr_id, player_name, {data-stat: text}) per body row."""
    out = []
    body = table.find("tbody")
    if not body:
        return out
    for tr in body.find_all("tr"):
        if "thead" in (tr.get("class") or []):
            continue
        pid = name = None
        cells = {}
        for c in tr.find_all(["th", "td"]):
            ds = c.get("data-stat")
            if not ds:
                continue
            if ds == "player":
                a = c.find("a")
                name = (a or c).get_text(strip=True)
                m = PLAYER_ID_RE.search(a.get("href", "")) if a else None
                pid = m.group(1) if m else None
            else:
                cells[ds] = c.get_text(strip=True)
        if pid:
            out.append((pid, name, cells))
    return out


def insert_box_tables(conn, game, html):
    """Populate every per-player table present on the page. Returns {table: n}."""
    tabs = soup_tables(html)
    meta = (game["game_id"], game["game_date"], game["season"], game["game_type"])
    counts = {}
    for name, stats in BOX_TABLES.items():
        t = tabs.get(name)
        if t is None:
            continue
        batch = [meta + (pid, pname, cells.get("team") or None)
                 + tuple(_num(cells.get(s)) for s in stats)
                 for pid, pname, cells in parse_rows(t)]
        if not batch:
            continue
        cols = META_COLS + ["team_abbr"] + stats
        conn.executemany(
            f"INSERT OR REPLACE INTO {name} ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", batch)
        counts[name] = len(batch)
    for name, stats in SIDE_TABLES.items():
        batch = []
        for side in ("vis", "home"):
            t = tabs.get(f"{side}_{name}")
            if t is None:
                continue
            batch += [meta + (pid, pname, side, cells.get("pos") or None)
                      + tuple(_num(cells.get(s)) for s in stats)
                      for pid, pname, cells in parse_rows(t)]
        if not batch:
            continue
        cols = META_COLS + ["side", "pos"] + stats
        conn.executemany(
            f"INSERT OR REPLACE INTO {name} ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})", batch)
        counts[name] = len(batch)
    return counts


def store_html(conn, game_id, html):
    conn.execute(
        "INSERT OR REPLACE INTO game_html (game_id, html, fetched_at) VALUES (?,?,?)",
        (game_id, gzip.compress(html.encode("utf-8")), dt.datetime.utcnow().isoformat()))


def load_html(conn, game_id):
    r = conn.execute("SELECT html FROM game_html WHERE game_id=?", (game_id,)).fetchone()
    return gzip.decompress(r["html"]).decode("utf-8") if r else None


def reparse(conn, start=None, end=None):
    """Rebuild every stat table from cached HTML. No network."""
    where = "1=1"
    if start is not None:
        where += f" AND g.season >= {start}"
    if end is not None:
        where += f" AND g.season <= {end}"
    games = conn.execute(f"""
        SELECT g.* FROM games_to_scrape g
        JOIN game_html h ON h.game_id = g.game_id
        WHERE {where} ORDER BY g.season, g.game_date, g.game_id""").fetchall()
    if not games:
        print("No cached HTML to reparse. Run scrape first.")
        return
    total = len(games)
    print(f"Reparsing {total:,} cached games...")
    t0 = time.time()
    totals = {}
    for i, g in enumerate(games, 1):
        html = load_html(conn, g["game_id"])
        if not html:
            continue
        game = dict(g)
        insert_player_rows(conn, game, extract_player_offense(html))
        for k, v in insert_box_tables(conn, game, html).items():
            totals[k] = totals.get(k, 0) + v
        if i % 200 == 0 or i == total:
            conn.commit()
            print(f"[{i:>6}/{total}] {i / max(1e-9, time.time() - t0):.0f} games/s")
    conn.commit()
    print(f"\nDONE in {(time.time() - t0) / 60:.1f}m")
    for k in sorted(totals):
        print(f"  {k:22} {totals[k]:>9,} rows")


def insert_player_rows(conn, game, players):
    if not players:
        return 0
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


def scrape_games(conn, fetcher, start, end, retry_failed, delay):
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
    recent = deque(maxlen=30)
    for i, g in enumerate(games, 1):
        url = f"{BASE}/boxscores/{g['game_id']}.htm"
        t_start = time.time()
        try:
            html = fetcher.fetch(url)
            store_html(conn, g["game_id"], html)
            players = extract_player_offense(html)
            n = insert_player_rows(conn, dict(g), players)
            counts = insert_box_tables(conn, dict(g), html)
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
                  f"[{len(counts)} tables, {sum(counts.values())} rows]  "
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


def backfill_opponent(conn):
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
    cached = conn.execute("SELECT COUNT(*) FROM game_html").fetchone()[0]
    mb = (conn.execute("SELECT COALESCE(SUM(LENGTH(html)),0) FROM game_html").fetchone()[0]) / 1e6
    print(f"cached pages:       {cached:,}  ({mb:,.0f} MB gzipped)")
    for name in list(BOX_TABLES) + list(SIDE_TABLES):
        n = conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
        rng = conn.execute(f"SELECT MIN(season), MAX(season) FROM {name}").fetchone()
        span = f"  {rng[0]}-{rng[1]}" if n else ""
        print(f"  {name:22} {n:>9,}{span}")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enumerate", help="Phase A: pull game URLs from /years/YYYY/games.htm")
    e.add_argument("--start", type=int, default=1933)
    e.add_argument("--end", type=int, default=current_season())
    e.add_argument("--refresh", action="store_true",
                   help="re-enumerate seasons already done (picks up newly played games)")
    e.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    s = sub.add_parser("scrape", help="Phase B: scrape each pending boxscore")
    s.add_argument("--start", type=int)
    s.add_argument("--end", type=int, default=current_season())
    s.add_argument("--retry-failed", action="store_true")
    s.add_argument("--delay", type=float, default=DEFAULT_DELAY)

    r = sub.add_parser("reparse", help="Rebuild all stat tables from cached HTML (no network)")
    r.add_argument("--start", type=int)
    r.add_argument("--end", type=int)

    sub.add_parser("stats", help="show progress")
    sub.add_parser("backfill-opponent", help="Repair opponent/matchup from team_abbr (no network)")

    args = ap.parse_args()
    conn = open_db()
    init_db(conn)

    if args.cmd == "stats":
        show_stats(conn)
        return
    if args.cmd == "backfill-opponent":
        backfill_opponent(conn)
        return
    if args.cmd == "reparse":
        reparse(conn, args.start, args.end)
        return

    with Fetcher() as fetcher:
        if args.cmd == "enumerate":
            enumerate_seasons(conn, fetcher, args.start, args.end, args.delay,
                              refresh=args.refresh)
        elif args.cmd == "scrape":
            scrape_games(conn, fetcher, args.start, args.end, args.retry_failed, args.delay)

    show_stats(conn)


if __name__ == "__main__":
    main()
