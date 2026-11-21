"""Merge build_heavy.py runs (one per hash seed) into maps/heavy.json.gz.

  python pack.py tpcds0.json tpcds1.json ... > maps/heavy.json.gz

Layout:
  shared  {file: [line, ...]}        lines that ran before any query: a change there keeps the test
  lines   {file: {line: [O, E, P, D]}}  bit i set: query i's optimize / execute / parse / duckdb step ran it
  queries [{exec, P, O, E, D, tables, Pw, Ow, Ew, Dw}]  fingerprints (None when unstable
          across runs) and warm CPU seconds of each step
"""
import gzip
import hashlib
import json
import sys

runs = [json.load(open(p)) for p in sys.argv[1:]]
base = runs[0]
shared = {}
for f, l in base["shared"]:
    shared.setdefault(f, []).append(l)
lines = {}
queries = []
for i, q in enumerate(base["queries"]):
    if "error" in q:
        raise SystemExit(f"query {i}: {q['error']}")
    out = {"exec": q["exec"], "sql": q["sql"]}
    for k in "POED":
        vals = {r["queries"][i].get(k) for r in runs}
        v = vals.pop() if len(vals) == 1 else None
        if k == "D" and v is not None:
            v = hashlib.sha1(v.encode()).hexdigest()
        out[k] = v
    out["tables"] = q.get("tables", [])
    for k in ("Pw", "Ow", "Ew", "Dw"):
        out[k] = round(max(r["queries"][i].get(k, 0) for r in runs), 4)
    queries.append(out)
    for bit, key in ((0, "Ol"), (1, "El"), (2, "Pl"), (3, "Dl")):
        for f, l in q.get(key, []):
            m = lines.setdefault(f, {}).setdefault(str(l), [0, 0, 0, 0])
            m[bit] |= 1 << i
sys.stdout.buffer.write(gzip.compress(json.dumps(
    {"shared": shared, "lines": lines, "queries": queries}, separators=(",", ":")).encode(), 9))
