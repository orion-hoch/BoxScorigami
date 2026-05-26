"""Pre-generate static JSON for every C(10,3)=120 MLB axis-stat combo.

Writes into the unified deploy folder at <repo>/public/mlb/.
Run after collect.py.
"""
import json
import sys
import time
from itertools import combinations
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from server import STATS, compute_payload  # noqa: E402

OUT_DIR   = HERE.parent / "public" / "mlb"
TALLY_DIR = OUT_DIR / "tally"
TALLY_DIR.mkdir(parents=True, exist_ok=True)

stats_keys = list(STATS.keys())
stats_payload = {
    "stats": [{"key": k, "label": STATS[k]["label"], "color": STATS[k]["color"]}
              for k in stats_keys]
}
(OUT_DIR / "stats.json").write_text(json.dumps(stats_payload, separators=(",", ":")))
print(f"-> public/mlb/stats.json  ({len(stats_keys)} stats)")

combos = list(combinations(sorted(stats_keys), 3))
print(f"\ngenerating {len(combos)} combo files ...")

t0 = time.time()
total_bytes = 0
for i, (a, b, c) in enumerate(combos, 1):
    payload = compute_payload(a, b, c)
    out_path = TALLY_DIR / f"{a}_{b}_{c}.json"
    out_path.write_text(payload)
    total_bytes += len(payload)
    if i % 10 == 0 or i == len(combos):
        print(f"  [{i:>3}/{len(combos)}] {a}_{b}_{c}.json  ({len(payload):>9,} bytes)")

print(f"\nwrote {len(combos)} files / {total_bytes/1e6:.1f} MB total in {time.time()-t0:.1f}s")
print(f"deploy folder: {OUT_DIR}")
