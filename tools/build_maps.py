"""Compact the raw map-build output into maps/maps.json.gz.

    python tools/build_maps.py build/raw sample/tests.json maps/maps.json.gz

Every map is "key -> set of tests". Test sets repeat a lot, so each distinct set is
stored once and referenced by index.

Setup-phase contexts ("<nodeid>|setup") include setUpClass and class/module fixtures,
which pytest runs inside the first test of a class or module. A failure there errors
every test in that class (or module), so those contexts are widened accordingly.
"""
import gzip
import json
import sys
from collections import defaultdict

raw, tests_path, out_path = sys.argv[1:4]
tests = json.load(open(tests_path))["tests"]
ids = [t["id"] for t in tests]
index = {i: n for n, i in enumerate(ids)}

by_class = defaultdict(list)
by_module = defaultdict(list)
first_in_module = set()
for i in ids:
    module = i.split("::", 1)[0]
    if not by_module[module]:
        first_in_module.add(i)
    by_module[module].append(index[i])
    by_class[i.rsplit("::", 1)[0]].append(index[i])

_expand_cache = {}


def expand(ctx):
    """Context name -> test indices it stands for."""
    if ctx in _expand_cache:
        return _expand_cache[ctx]
    res = ()
    if ctx:
        nodeid, _, phase = ctx.partition("|")
        if nodeid in index:
            if phase == "setup":
                group = set(by_class[nodeid.rsplit("::", 1)[0]])
                if nodeid in first_in_module:
                    group |= set(by_module[nodeid.split("::", 1)[0]])
                res = tuple(group)
            else:
                res = (index[nodeid],)
    _expand_cache[ctx] = res
    return res


sets, set_ids = [], {}


def intern(ctxs):
    s = set()
    for c in ctxs:
        s.update(expand(c))
    key = tuple(sorted(s))
    if key not in set_ids:
        set_ids[key] = len(sets)
        sets.append(list(key))
    return set_ids[key]


linemap = json.load(open(f"{raw}/linemap.json"))
lines = {}
for f, m in linemap.items():
    lines[f] = {l: intern(c) for l, c in m.items()}
del linemap

fixtures = defaultdict(set)
for ctx, files in json.load(open(f"{raw}/reads.json")).items():
    for f in files:
        fixtures[f if f.startswith("tests/") else "tests/" + f].add(ctx)
fixtures = {f: intern(c) for f, c in fixtures.items()}

uses = defaultdict(set)
for ctx, names in json.load(open(f"{raw}/uses.json")).items():
    for n in names:
        if n.startswith("sqlglot."):
            uses[n].add(ctx)
uses = {n: intern(c) for n, c in uses.items()}

# delta-encode each sorted set: smaller after gzip
packed = [[s[0]] + [b - a for a, b in zip(s, s[1:])] if s else [] for s in sets]
doc = {"tests": ids, "sets": packed, "lines": lines, "fixtures": fixtures, "uses": uses}
with gzip.open(out_path, "wt", compresslevel=9) as f:
    json.dump(doc, f, separators=(",", ":"))
print(f"tests {len(ids)} sets {len(sets)} files {len(lines)} fixtures {len(fixtures)} classes {len(uses)}",
      file=sys.stderr)
