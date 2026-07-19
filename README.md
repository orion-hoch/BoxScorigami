# BoxScorigami

Interactive 3D visualization of player-game stat lines across the NBA, NFL,
MLB, and WNBA. Pick any 3 stats for the X/Y/Z axes, rotate, peel layers, and click a
voxel to see the most recent game (or player-season) that produced that exact
line, plus a leaderboard of who owns the most unique combos.

The deployed site is a fully static bundle in `public/` — no backend at
runtime. Each sport ships one gzipped dump per mode holding every distinct stat
line once; the viewer fetches `public/<sport>/{game,season}.json.gz` and rolls
the three chosen axes up in the browser.

Earlier versions precomputed a JSON file per axis combination. That wrote each
line into all C(n-1,2) files it belonged to — 91 copies per line for NBA's 15
stats — turning a 255 MB database into ~1 GB of JSON. The dump format stores
each line once and is ~20x smaller in total.

## Repo layout

```
NBA_Cube/
├── public/                 # deploy target — unified static site (Vercel serves this)
│   ├── index.html          #   the tabbed 3D viewer (NBA / NFL / MLB)
│   ├── boxscorigami.svg
│   ├── nba/                #   stats.json, game.json.gz, season.json.gz
│   ├── nfl/                #   off/ def/ st/, each with the same three files
│   ├── mlb/                #   one dir per position (all, p, pos, c, 1b, ...)
│   └── wnba/               #   "
├── nba/                    # NBA pipeline (nba_api)
│   ├── collect.py          #   fetch player_games -> nba.sqlite (--league wnba for WNBA)
│   ├── server.py           #   local dev JSON API (optional)
│   └── nba.sqlite          #   gitignored
├── nfl/                    # NFL pipeline -> nfl_full.sqlite, public/nfl/
│   ├── collect.py          #   scrape Pro-Football-Reference -> nfl_full.sqlite
│   └── server.py           #   ATTACHes nfl_full.sqlite for the full history
├── mlb/                    # MLB pipeline (MLB-StatsAPI) -> mlb.sqlite, public/mlb/
├── export_dumps.py         # generate public/<sport>/*.json.gz (--sport nba|nfl|wnba)
├── stats.py                # shared query layer + dev server used by each sport
└── .gitignore
```

Each sport follows the same shape: `collect.py` scrapes into a SQLite db, then
an exporter emits the dumps into `public/<sport>/`. NBA/NFL/WNBA use
`export_dumps.py`; MLB emits its own via `python3 mlb/collect.py build`, split by
position. SQLite files are gitignored (too big — regenerate from collect.py).

## Local dev (per sport, e.g. nba)

```
# 1. (one-time) Scrape into SQLite
python3 nba/collect.py

# 2. (whenever the db changes) Generate the dumps
python3 export_dumps.py --sport nba
# MLB instead: python3 mlb/collect.py build

# 3. Serve the unified site from the repo root
python3 -m http.server --directory public 8000
# -> http://127.0.0.1:8000/
```

The per-sport `server.py` scripts query the db live and are handy for spot
checks, but the deployed viewer reads only the dumps.
