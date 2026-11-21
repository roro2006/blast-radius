"""Where the selector's cost over the oracle goes, on labeled mutants.

    python tools/mutants/waste.py build/mut [build/novel ...]
"""
import json, os, sys
from collections import Counter, defaultdict
from pathlib import Path
root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root)); os.environ.setdefault("SRC", str(root / "build/sqlglot"))
import selector as S
tests = json.loads((root / "sample/tests.json").read_text())["tests"]
num = {t["id"]: t["number"] for t in tests}; idof = {t["number"]: t["id"] for t in tests}
byf = defaultdict(set)
for t in tests: byf[t["file"]].add(t["number"])
sel = S.Selector(tests)
def cost(ws): return (max(sum(sel.ms[n] for n in w) for w in ws) + 2000 * len(ws)) / 1000 if ws else 0
rows = []
for d in map(Path, sys.argv[1:]):
    edits = json.loads((d / "patches/edits.json").read_text()) if (d / "patches/edits.json").exists() else {}
    kinds = json.loads((d / "patches/kinds.json").read_text())
    for lab in sorted((d / "labels").glob("*.json")):
        fail = set()
        for i in json.loads(lab.read_text())["failing"]:
            if i.endswith("::*"): fail |= byf[i[:-3]]
            elif i in num: fail.add(num[i])
            elif i.split("[")[0] in num: fail.add(num[i.split("[")[0]])
        text = (d / "patches" / f"{lab.stem}.patch").read_text(encoding="utf8")
        try: picked = sel.select(text)
        except S.RunAll: picked = sel.all_tests()
        c, oc = cost(sel.pack(picked)), cost(sel.pack(fail))
        rows.append(dict(id=lab.stem, kind=edits.get(lab.stem, kinds.get(lab.stem)), c=c, oc=oc,
                         fail=fail, picked=picked, extra=picked - fail, text=text))
n = len(rows)
W = sum(r["c"] - r["oc"] for r in rows)
print(f"{n} mutants: cost {sum(r['c'] for r in rows)/n:.2f}/commit, oracle {sum(r['oc'] for r in rows)/n:.2f}, waste {W/n:.2f}")
nofail = [r for r in rows if not r["fail"] and r["picked"]]
print(f"no-fail commits that still pick tests: {len(nofail)}/{n}, waste {sum(r['c'] for r in nofail)/n:.2f}/commit")
print(f"failing commits' waste: {sum(r['c']-r['oc'] for r in rows if r['fail'])/n:.2f}/commit")
# which unneeded tests carry the waste (attributed by their seconds)
heavy = Counter()
for r in rows:
    for t in r["extra"]:
        heavy[t] += sel.ms[t] / 1000
print("\nunneeded tests picked most (total seconds over all mutants, times picked):")
cnt = Counter(t for r in rows for t in r["extra"])
for t, s in heavy.most_common(12):
    print(f"  {s:7.1f}s  x{cnt[t]:3}  {idof[t]}")
print("\nworst mutants:")
for r in sorted(rows, key=lambda r: r["c"] - r["oc"], reverse=True)[:25]:
    body = [l for l in r["text"].splitlines() if l.startswith(("+++", "-", "+")) and not l.startswith("---")]
    print(f"  {r['c']-r['oc']:5.1f}s {r['id']} {r['kind']} picked {len(r['picked'])} fail {len(r['fail'])} | " + " ".join(body[:3])[:170])
