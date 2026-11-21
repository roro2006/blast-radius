"""Score the selector on labeled mutants (any that are labeled so far).

    python tools/mutants/score.py build/mut [--show-misses]

Runs selector.py in-process against <dir>/patches with sample/tests.json, compares with
<dir>/labels/*.json, and reports misses, cost and oracle cost by kind.
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
os.environ.setdefault("SRC", str(root / "build" / "sqlglot"))
import selector as S  # noqa: E402

d = Path(sys.argv[1])
show = "--show-misses" in sys.argv
tests = json.loads((root / "sample/tests.json").read_text())["tests"]
num = {t["id"]: t["number"] for t in tests}
by_file = defaultdict(set)
for t in tests:
    by_file[t["file"]].add(t["number"])
kinds = json.loads((d / "patches/kinds.json").read_text())
sel = S.Selector(tests)
every = sel.all_tests()


def cost(workers):
    if not workers:
        return 0.0
    return (max(sum(sel.ms[n] for n in w) for w in workers) + 2000 * len(workers)) / 1000


rows = []
for lab in sorted((d / "labels").glob("*.json")):
    cid = lab.stem
    failing = set()
    for i in json.loads(lab.read_text())["failing"]:
        if i.endswith("::*"):
            failing |= by_file.get(i[:-3], set())
        elif i in num:
            failing.add(num[i])
        elif "::" in i and i.split("[")[0] in num:
            failing.add(num[i.split("[")[0]])
    text = (d / "patches" / f"{cid}.patch").read_text(encoding="utf8")
    try:
        picked, why = sel.select(text), "ok"
    except S.RunAll as e:
        picked, why = every, f"ALL: {e}"
    missed = failing - picked
    c = cost(sel.pack(picked)) + (300 if missed else 0)
    rows.append((cid, kinds.get(cid, "?"), len(failing), len(picked), len(missed), c,
                 cost(sel.pack(failing)), why, missed, text))

agg = defaultdict(lambda: [0, 0, 0.0, 0.0])
for cid, kind, nf, npk, nm, c, oc, why, missed, text in rows:
    a = agg[kind]
    a[0] += 1; a[1] += bool(nm); a[2] += c; a[3] += oc
print(f"{'kind':10} {'n':>4} {'miss':>5} {'cost/commit':>12} {'oracle/commit':>14}")
tot = [0, 0, 0.0, 0.0]
for k, a in sorted(agg.items()):
    print(f"{k:10} {a[0]:4} {a[1]:5} {a[2] / a[0]:12.2f} {a[3] / a[0]:14.2f}")
    for i in range(4):
        tot[i] += a[i]
if tot[0]:
    print(f"{'ALL':10} {tot[0]:4} {tot[1]:5} {tot[2] / tot[0]:12.2f} {tot[3] / tot[0]:14.2f}"
          f"   (miss rate {tot[1] / tot[0]:.1%}, non-miss cost/commit {(tot[2] - 300 * tot[1]) / tot[0]:.2f})")
if show:
    for cid, kind, nf, npk, nm, c, oc, why, missed, text in rows:
        if nm:
            ids = [t["id"] for t in tests if t["number"] in missed][:5]
            body = [l for l in text.splitlines() if l.startswith(("+++", "-", "+")) and not l.startswith("---")]
            print(f"\n== {cid} [{kind}] missed {nm}/{nf} picked {npk} ({why})")
            print("   " + "\n   ".join(body[:8]))
            print("   missed: " + ", ".join(ids))
