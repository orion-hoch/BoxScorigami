# NBA Scorigami Cube

Interactive 3D visualization of every NBA player-game stat-line in history.
Pick any 3 of 13 stats for the X/Y/Z axes, rotate, peel layers, click voxels
for the most recent game that produced that exact stat line, and inspect the
leaderboard of players who own the most unique combos.

Data covers every regular-season and playoff game from 1950-51 through the
current season (~1.35M player-game rows) via [nba_api](https://github.com/swar/nba_api).

The deployed site is a fully static bundle living in `scorigami/public/` —
no backend at runtime. All 286 axis combos are precomputed into JSON.

## Repo layout

```
NBA_Cube/
├── scorigami/
│   ├── public/             # deploy target — what Vercel serves
│   │   ├── index.html
│   │   ├── stats.json
│   │   └── tally/          # 286 combo JSON files
│   ├── collect.py          # fetch player_games via nba_api -> SQLite
│   ├── server.py           # local dev server (optional)
│   ├── tally.py            # legacy single-combo tally builder
│   ├── export_static.py    # generate scorigami/public/ from SQLite
│   └── scorigami.sqlite    # gitignored — 229 MB
├── nba_api/                # gitignored — clone of swar/nba_api
└── .gitignore
```

## Local dev

```
# 1. (one-time) Fetch all seasons into SQLite (~3 min)
python3 scorigami/collect.py

# 2. (one-time, or whenever SQLite changes) Generate static combos
python3 scorigami/export_static.py

# 3. Serve the deploy folder
python3 -m http.server --directory scorigami/public 8765
# -> http://127.0.0.1:8765/
```

`scorigami/server.py` is kept around but no longer required — the deployed
viewer reads only from `tally/*.json` and `stats.json`.
