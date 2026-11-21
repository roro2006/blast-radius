"""Merge the value profile (tools/cov/valprof.py) into maps/valprof/ (index.json and one
shard per source file).

    python tools/build_valprof.py <raw dir with valprof-*.json> <tests.json> maps/valprof

Layout, per file and site "line:col" (positions in the pinned source):
  dicts[file][site] = {"imp": rec | None, "t": [[test numbers], rec], ...]}
      rec = [get keys, in keys, missed key reprs, flags]  flags: 1 len, 2 keys, 4 vals, 8 mut
  cmps[file][site]  = {"imp": [masks], "t": [[[test numbers], [masks]], ...]}
      mask: bit i = outcome of (<, <=, >, >=, ==, !=)[i]; -1 = operands not simple
  bools[file][site] = {"imp": [pairs], "t": [[[test numbers], [pairs]], ...]}
      pair: [truth of left, truth of right or null when not evaluated]
Records made during a test's setup/teardown are credited to every test of its class (or
module, for plain functions), since class fixtures are shared.
"""
import glob
import gzip
import json
import os
import sys


def shard_name(f):
    return f.replace("/", "__") + ".json.gz"


raw, tests_json, out = sys.argv[1:4]
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
group = {}  # class or module prefix -> numbers
for t in tests:
    parts = t["id"].split("::")
    key = "::".join(parts[:2]) if len(parts) > 2 else parts[0]
    group.setdefault(key, []).append(t["number"])


def owners(ctx):
    """Test numbers a context is credited to; None for import time."""
    if ctx == "":
        return None
    if ctx.startswith("S:"):
        nid = ctx[2:]
        parts = nid.split("::")
        key = "::".join(parts[:2]) if len(parts) > 2 else parts[0]
        return group.get(key, [num[nid]] if nid in num else [])
    nid = ctx.split("[", 1)[0] if ctx not in num else ctx
    return [num[nid]] if nid in num else []


FLAGS = {"len": 1, "keys": 2, "vals": 4, "mut": 8}
dicts, cmps, bools, failed, unbalanced = {}, {}, {}, set(), set()


def merge_into(store, sites, data, init, add):
    for sid, per_ctx in data.items():
        f, key = sites[int(sid)]
        site = store.setdefault(f, {}).setdefault(key, {"imp": None, "t": {}})
        for ctx, rec in per_ctx.items():
            who = owners(ctx)
            if who is None:
                site["imp"] = add(site["imp"] if site["imp"] is not None else init(), rec)
            else:
                for n in who:
                    site["t"][n] = add(site["t"].get(n, init()), rec)


def dict_init():
    return [set(), set(), set(), 0]


def dict_add(acc, rec):
    acc[0] |= set(rec["get"])
    acc[1] |= set(rec["in"])
    acc[2] |= set(rec["miss"])
    acc[3] |= sum(bit for name, bit in FLAGS.items() if rec.get(name))
    return acc


def set_add(acc, rec):
    acc |= {tuple(x) if isinstance(x, list) else x for x in rec}
    return acc


for p in sorted(glob.glob(f"{raw}/valprof-*.json")):
    d = json.load(open(p))
    merge_into(dicts, d["dsites"], d["dicts"], dict_init, dict_add)
    merge_into(cmps, d["csites"], d["cmps"], set, set_add)
    merge_into(bools, d["bsites"], d["bools"], set, set_add)
    failed |= set(d["failed"])
    for sid in d["leftover"]:
        unbalanced.add(tuple(d["bsites"][int(sid)]))
    if d["leftover"]:
        print(f"{p}: unbalanced and/or probes at {len(d['leftover'])} sites", file=sys.stderr)


def pack(store, conv):
    """Group tests with identical records: {"imp": .., "t": [[numbers], rec], ...]}."""
    out = {}
    for f, sites in store.items():
        for key, site in sites.items():
            groups = {}
            for n, rec in site["t"].items():
                c = json.dumps(conv(rec), sort_keys=True)
                groups.setdefault(c, []).append(n)
            out.setdefault(f, {})[key] = {
                "imp": None if site["imp"] is None else conv(site["imp"]),
                "t": [[sorted(ns), json.loads(c)] for c, ns in groups.items()],
            }
    return out


doc = {
    "dicts": pack(dicts, lambda r: [sorted(r[0]), sorted(r[1]), sorted(r[2]), r[3]]),
    "cmps": pack(cmps, lambda s: sorted(s)),
    "bools": pack(bools, lambda s: sorted([list(x) for x in s], key=repr)),
    # tests that failed under instrumentation: their records may not reflect a normal run
    "failed": sorted(num[f] for f in failed if f in num),
    # and/or sites where an operand raised mid-evaluation: a swap could skip or add the raise
    "unbalanced": sorted(list(x) for x in unbalanced),
}
# One shard per source file (a patch touches one or two), plus an index.
os.makedirs(out, exist_ok=True)
for old in glob.glob(os.path.join(out, "*.json.gz")):
    os.remove(old)
files = sorted(set(doc["dicts"]) | set(doc["cmps"]) | set(doc["bools"]))
for f in files:
    shard = {k: doc[k].get(f, {}) for k in ("dicts", "cmps", "bools")}
    with gzip.open(os.path.join(out, shard_name(f)), "wt") as fh:
        json.dump(shard, fh, separators=(",", ":"))
with open(os.path.join(out, "index.json"), "w") as fh:
    json.dump({"files": files, "failed": doc["failed"], "unbalanced": doc["unbalanced"]}, fh)
n = {k: sum(len(v) for v in doc[k].values()) for k in ("dicts", "cmps", "bools")}
print(f"sites {n} in {len(files)} shards, failed under instrumentation {len(doc['failed'])}", file=sys.stderr)
