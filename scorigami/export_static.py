"""Pre-generate static JSON for every C(13,3)=286 axis-stat combo.

Produces a self-contained `public/` directory that can be deployed to any
static host (Vercel, GitHub Pages, Netlify, S3, ...). The viewer fetches
these files directly — no backend, no Python at runtime.

Layout written:
    scorigami/public/
        index.html      (copy of cube.html)
        stats.json      (axis metadata for the dropdowns)
        tally/
            <a>_<b>_<c>.json   (alphabetically-sorted keys; 286 files)

Run:
    python3 scorigami/export_static.py
"""
import json
import shutil
import sys
import time
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server import STATS, compute_payload  # noqa: E402

OUT_DIR   = HERE / "public"
TALLY_DIR = OUT_DIR / "tally"
TALLY_DIR.mkdir(parents=True, exist_ok=True)

stats_keys = list(STATS.keys())

# 1) stats.json — drives the X/Y/Z dropdowns in the UI
stats_payload = {
    "stats": [
        {"key": k, "label": STATS[k]["label"], "color": STATS[k]["color"]}
        for k in stats_keys
    ]
}
(OUT_DIR / "stats.json").write_text(json.dumps(stats_payload, separators=(",", ":")))
print(f"-> public/stats.json  ({len(stats_keys)} stats)")

# 2) Copy cube.html -> public/index.html so the deploy serves the viewer at /
src = HERE / "cube.html"
dst = OUT_DIR / "index.html"
shutil.copyfile(src, dst)
print(f"-> public/index.html  (copied from {src.name})")

# 3) All C(n,3) combos, with each filename in canonical alphabetical order.
combos = list(combinations(sorted(stats_keys), 3))
print(f"\ngenerating {len(combos)} combo files ...")

t0 = time.time()
total_bytes = 0
for i, (a, b, c) in enumerate(combos, 1):
    payload = compute_payload(a, b, c)
    out_path = TALLY_DIR / f"{a}_{b}_{c}.json"
    out_path.write_text(payload)
    total_bytes += len(payload)
    if i % 25 == 0 or i == len(combos):
        print(f"  [{i:>3}/{len(combos)}] {a}_{b}_{c}.json  ({len(payload):>9,} bytes)")

elapsed = time.time() - t0
print(
    f"\nwrote {len(combos)} files / {total_bytes/1e6:.1f} MB total in {elapsed:.1f}s"
)
print(f"deploy folder: {OUT_DIR}")
