"""Compact the condition profile (tools/cov/condplugin.py) into maps/cond.json.gz.

    python tools/build_cond.py build/cond sample/tests.json maps/cond.json.gz

{"tests": [ids], "sets": [delta-encoded test-index sets],
 "sites": {file: {"B:line:col" | "C:line:col": {state: set_id}}},
 "bad": {file: [site keys evaluated during an import, or sharing a start position]}}
States: BoolOp "(k, t)" terminal states and "v" (a non-bool operand was evaluated);
ordering compares "e" / "n" / "x" (equal, unequal, not a plain value); is/== compares "d"
(the two operators disagreed). Setup-phase contexts are widened to the class (and the
module, for a module's first test) exactly as in build_maps.py.
"""
import glob
import gzip
import json
import sys
from collections import defaultdict

raw, tests_path, out_path = sys.argv[1:4]
ids = [t["id"] for t in json.load(open(tests_path))["tests"]]
index = {i: n for n, i in enumerate(ids)}
by_class, by_module, first_in_module = defaultdict(list), defaultdict(list), set()
for i in ids:
    module = i.split("::", 1)[0]
    if not by_module[module]:
        first_in_module.add(i)
    by_module[module].append(index[i])
    by_class[i.rsplit("::", 1)[0]].append(index[i])


def expand(ctx):
    nodeid, _, phase = ctx.partition("|")
    if nodeid not in index:
        return ()
    if phase == "setup":
        group = set(by_class[nodeid.rsplit("::", 1)[0]])
        if nodeid in first_in_module:
            group |= set(by_module[nodeid.split("::", 1)[0]])
        return tuple(group)
    return (index[nodeid],)


acc = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))  # file -> key -> state -> tests
bad = defaultdict(set)
for p in sorted(glob.glob(f"{raw}/cond-*.json")):
    d = json.load(open(p))
    for site in d["dup"]:
        f, key = site.split("|", 1)
        bad[f].add(key)
    for ctx, sites in d["rec"].items():
        tests = None if ctx == "<import>" else expand(ctx)
        for site, states in sites.items():
            f, key = site.split("|", 1)
            if tests is None:
                bad[f].add(key)
                continue
            for st in states:
                acc[f][key][st].update(tests)

sets, set_ids = [], {}


def intern(s):
    k = tuple(sorted(s))
    if k not in set_ids:
        set_ids[k] = len(sets)
        sets.append(k)
    return set_ids[k]


sites = {}
for f, keys in acc.items():
    for key, states in keys.items():
        if key in bad[f]:
            continue
        sites.setdefault(f, {})[key] = {st: intern(t) for st, t in states.items() if t}
packed = [[s[0]] + [b - a for a, b in zip(s, s[1:])] if s else [] for s in sets]
doc = {"tests": ids, "sets": packed, "sites": sites, "bad": {f: sorted(k) for f, k in bad.items()}}
with gzip.open(out_path, "wt") as fh:
    json.dump(doc, fh, separators=(",", ":"))
print(f"sites {sum(map(len, sites.values()))}, bad {sum(map(len, bad.values()))}, sets {len(sets)}",
      file=sys.stderr)
