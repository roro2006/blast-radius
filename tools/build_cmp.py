"""Merge the comparison profile into maps/cmp.json.gz: file -> ["line:col", ...] of
`is`/`==` comparisons that were evaluated and never disagreed with the swapped operator
anywhere in the suite on the pinned commit (see tools/cov/cmpplugin.py).

    python tools/build_cmp.py build/cmp maps/cmp.json.gz
"""
import glob
import gzip
import json
import sys

raw, out = sys.argv[1:3]
seen, bad = {}, {}
for p in glob.glob(f"{raw}/cmp-*.json"):
    d = json.load(open(p))
    for f, keys in d["seen"].items():
        seen.setdefault(f, set()).update(keys)
    for f, keys in d["bad"].items():
        bad.setdefault(f, set()).update(keys)
safe = {f: sorted(k - bad.get(f, set())) for f, k in seen.items()}
safe = {f: k for f, k in safe.items() if k}
with gzip.open(out, "wt") as fh:
    json.dump(safe, fh, separators=(",", ":"))
print(f"safe sites {sum(map(len, safe.values()))}, unsafe {sum(map(len, bad.values()))}", file=sys.stderr)
