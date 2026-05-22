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

## Deploying to Vercel

The static bundle in `scorigami/public/` is what gets deployed. Vercel
serves directories with zero config — no build step, no framework adapter.

### 1. Initialize git in the repo root

```
cd /Users/orionhoch/Desktop/NBA_Cube
git init
git add .
git status                   # eyeball the staged list — should NOT include
                             # scorigami.sqlite or nba_api/
git commit -m "Initial scorigami cube + static export"
```

### 2. Push to a new GitHub repo

Either via the GitHub UI (create a new empty repo, then):
```
git remote add origin git@github.com:<your-user>/nba-scorigami-cube.git
git branch -M main
git push -u origin main
```

…or use the `gh` CLI:
```
gh repo create nba-scorigami-cube --public --source=. --push
```

### 3. Connect Vercel

1. Go to https://vercel.com/new
2. Import the GitHub repo you just pushed.
3. **Root Directory:** set to `scorigami/public` (this is the key step —
   it tells Vercel where the deployable bundle lives).
4. Framework Preset: **Other** (or leave on auto-detect, which should
   resolve to "Other" since there's no framework).
5. Build & Output Settings: leave everything blank/default.
   - Build Command: *empty*
   - Output Directory: *empty* (Root Directory already points at the files)
   - Install Command: *empty*
6. Click **Deploy**.

Vercel will copy `scorigami/public/` to its edge network and give you a URL
like `https://nba-scorigami-cube.vercel.app/`. `index.html` loads at `/`,
each combo file at `/tally/<a>_<b>_<c>.json`. Gzip is on by default.

### 4. Updating the data

Whenever a new game has finished and you want the cube fresh:
```
python3 scorigami/collect.py                  # appends new player_games
python3 scorigami/export_static.py            # rebuilds public/ (~5 min)
git add scorigami/public/
git commit -m "Refresh data through YYYY-MM-DD"
git push
```

Vercel auto-redeploys on push.

## Repo size note

`scorigami/public/` holds 286 JSON files totaling ~150–250 MB. GitHub
accepts this fine (per-file limit is 100 MB; none of ours come close), but
it makes the repo chunky. If you'd rather keep the repo light, you can
generate the public folder during a CI job that produces a build artifact,
but committing it directly is the simplest workflow.
