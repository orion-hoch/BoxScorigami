# BoxScorigami

Interactive 3D visualization of player-game stat lines across the NBA, NFL,
MLB, and WNBA. Pick any 3 stats for the X/Y/Z axes, rotate, peel layers, and click a
voxel to see the most recent game (or player-season) that produced that exact
line, plus a leaderboard of who owns the most unique combos.

The deployed site is a fully static bundle in `public/` — no backend at
runtime. Every axis combo is precomputed to JSON per sport; the viewer just
fetches `public/<sport>/tally/*.json`.

## Repo layout

```
NBA_Cube/
├── public/                 # deploy target — unified static site (Vercel serves this)
│   ├── index.html          #   the tabbed 3D viewer (NBA / NFL / MLB)
│   ├── boxscorigami.svg
│   ├── nba/                #   stats.json, tally/ (per-game), tally-season/
│   ├── nfl/                #   "
│   ├── mlb/                #   "
│   └── wnba/               #   "
├── nba/                    # NBA pipeline (nba_api)
│   ├── collect.py          #   fetch player_games -> nba.sqlite (--league wnba for WNBA)
│   ├── server.py           #   local dev JSON API (optional)
│   └── nba.sqlite          #   gitignored
├── nfl/                    # NFL pipeline -> nfl_full.sqlite, public/nfl/
│   ├── collect.py          #   scrape Pro-Football-Reference -> nfl_full.sqlite
│   └── server.py           #   ATTACHes nfl_full.sqlite for the full history
├── mlb/                    # MLB pipeline (MLB-StatsAPI) -> mlb.sqlite, public/mlb/
├── export_static.py        # generate public/<sport>/ from the db (--sport nba|nfl|mlb)
├── stats.py                # shared query layer + dev server used by each sport
└── .gitignore
```

Each sport follows the same shape: `collect.py` scrapes into a SQLite db,
`export_static.py` precomputes every combo into `public/<sport>/`. SQLite files
are gitignored (too big — regenerate from the collect scripts).

## Local dev (per sport, e.g. nba)

```
# 1. (one-time) Scrape into SQLite
python3 nba/collect.py

# 2. (whenever the db changes) Generate the static combos
python3 export_static.py --sport nba

# 3. Serve the unified site from the repo root
python3 -m http.server --directory public 8000
# -> http://127.0.0.1:8000/
```

The per-sport `server.py` scripts query the db live and are handy for spot
checks, but the deployed viewer reads only the precomputed JSON.
