# BoxScorigami

Scorigami, but for box scores. Pick any three stats (points/rebounds/assists, sacks/tackles/interceptions, goals/assists/shots) and every player game in league history becomes a voxel in a 3D grid. Red cells happened exactly once, green cells happened more than once. You can peel the cube open, filter by year or player, and click any cell to see who did it last. Covers the NBA, WNBA, NFL, MLB, and NHL back to each league's first season.

## Where the data comes from

No API will give you every player box score ever. What exists is a patchwork. Some leagues have decent free endpoints with awkward limits, some have nothing at all. So each sport gets its own collector that pulls or scrapes into a local SQLite database, and everything downstream of that is uniform.

| League | Source | Approach |
|---|---|---|
| NBA / WNBA | stats.nba.com (`nba_api`) | one game log request per season |
| NFL | Pro Football Reference | scraped, one page per game, HTML cached |
| MLB | MLB StatsAPI | bulk gameLog, 100 players per request |
| NHL | NHL stats API | whole season report queries, sliced by month |

The one that is particularly difficult:

**NFL.** No public API covers historical box scores, so they get scraped from Pro Football Reference, which sits behind Cloudflare and limits you to 20 requests a minute. The collector launches a real Chrome once to pass the challenge, lifts the clearance cookie, and replays it over plain HTTP with `curl_cffi` impersonating Chrome, so the crawl itself never needs a browser. Every page it fetches gets gzipped into the database. When a new stat table becomes worth parsing later, that is a `reparse` over cached HTML instead of scraping the whole site again.


## Static site choice (I am poor and didn't want to store GBs of data offsite)

The first version precomputed a JSON file per axis combination. Each stat line belongs to C(n-1,2) combos, which is 91 files for NBA's 15 stats, so a 255 MB database exploded into about 1 GB of duplicated JSON.

The replacement stores every distinct stat line exactly once, with its count and its five most recent occurrences, and lets the browser do the grouping. Choosing axes is a single pass over the dump: bucket by the three chosen values, sum the counts. That is cheap enough to rerun on every dropdown change with no server involved.

The dump itself is a custom columnar binary. Stat values live in one flat Int16Array, counts in another, and strings like names and dates are dictionary encoded into index columns. The client never runs JSON.parse, which for NBA alone took around 800ms. It points typed array views straight into the fetched buffer. Files are brotli compressed on disk and served with `Content-Encoding: br`, so the browser inflates them natively while streaming. The biggest sport is a 15 MB download that unpacks into a few flat arrays, and the whole site is flat files behind Caddy. No backend, no database, nothing to keep alive.

Some of the features fall straight out of that layout:

- **Recent games.** The five most recent occurrences ride along with every line. Assembling the recent list for every cell during rollup turned out to be about 73% of its cost, for lists nobody sees until they click a cell. So the rollup keeps only the single most recent occurrence per cell, and the full list is rebuilt on click by rescanning the dump for that one cell, then cached.
- **Min games toggle.** The games played qualifier is folded into each line's key, so one dump serves both states of the toggle. No second download.
- **Instant tab switches.** Dumps are cached per sport and group, and sibling groups (MLB positions, NFL and NHL units) prefetch during idle frames after the first cube renders.

## Getting the data yourself

Every collector is resumable and checkpointed, so you can rebuild any of them from scratch:

```
# NBA and WNBA
python3 nba/collect.py
python3 nba/collect.py --league wnba

# NFL (needs Chrome once for the Cloudflare cookie)
python3 nfl/collect.py enumerate
python3 nfl/collect.py scrape

# MLB (one season-batch per season, then the backfills)
python3 mlb/collect.py season-batch --season 2024
python3 mlb/collect.py backfill-names
python3 mlb/collect.py backfill-positions

# NHL
python3 nhl/collect.py enumerate
python3 nhl/collect.py season-batch
```

Then regenerate the dumps for whatever changed:

```
python3 export_dumps.py --sport nhl          # MLB instead: python3 mlb/collect.py build
node dump_to_binary.js public/nhl/*/*.json.br && rm public/nhl/*/*.json.br
```

`public/` is the whole deployed site. Railway builds the Dockerfile and Caddy serves it. To run it locally use `SITE_ROOT=public caddy run --config Caddyfile`, since the dumps need the `Content-Encoding: br` header and a plain `http.server` won't set it.

## GenAI

The NFL data was the wall this project hit. Every other league has some kind of API; for historical NFL box scores there is nothing, and I spent a while looking. The way through was scraping Pro Football Reference, and I used GenAI to get a working scraper built: figuring out how to get past Cloudflare (launch a real Chrome once for the clearance cookie, then replay it with `curl_cffi` impersonating Chrome so the crawl never needs a browser), staying under the 20 requests/minute limit, and parsing the box score tables out of PFR's comment-wrapped HTML. It also pushed the design of caching every fetched page gzipped in SQLite, so adding a new stat later is a reparse over cached HTML instead of a second crawl of the whole site.

## Layout

```
├── public/            # the deployed static site: index.html plus per sport dumps
├── nba/  nfl/  mlb/  nhl/  wnba/
│   ├── collect.py     # league specific collection (see above)
│   └── server.py      # stat definitions, sanity filters, era cutoffs, dev API
├── export_dumps.py    # turns SQLite into the dump files (all sports but MLB)
├── dump_to_binary.js  # turns brotli JSON dumps into the columnar binary
└── stats.py           # shared query layer for the per sport dev servers
```
