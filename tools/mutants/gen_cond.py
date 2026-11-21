"""Validation mutants for the condition profile: and<->or flips and </<=, >/>=, </> swaps
at random profiled sites, kept only when the condition rule picks fewer tests than plain
line coverage would (so labeling them checks exactly what the rule drops).

    python tools/mutants/gen_cond.py <sqlglot_checkout> <out_dir> <count> [seed]
"""
import ast
import difflib
import gzip
import json
import os
import random
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
src, out, count = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
os.environ.setdefault("SRC", str(src))
import selector as S  # noqa: E402

rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 11)
tests = json.loads((root / "sample/tests.json").read_text())["tests"]
sel = S.Selector(tests)
cond = json.load(gzip.open(root / "maps/cond.json.gz", "rt"))
SWAP = {ast.Lt: [" < ", " <= ", " > "], ast.LtE: [" <= ", " < ", " >= "],
        ast.Gt: [" > ", " >= ", " < "], ast.GtE: [" >= ", " > ", " <= "]}
cands = []
for f, keys in cond["sites"].items():
    text = (src / f).read_text(encoding="utf8")
    for n in ast.walk(ast.parse(text)):
        if getattr(n, "lineno", None) is None or n.lineno != n.end_lineno:
            continue
        if isinstance(n, ast.BoolOp) and len(n.values) == 2 and f"B:{n.lineno}:{n.col_offset}" in keys:
            a, b = n.values[0].end_col_offset, n.values[1].col_offset
            old = " and " if isinstance(n.op, ast.And) else " or "
            cands.append((f, n.lineno, a, b, old, " or " if old == " and " else " and "))
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in SWAP and \
                f"C:{n.lineno}:{n.col_offset}" in keys:
            ops = SWAP[type(n.ops[0])]
            cands.append((f, n.lineno, n.left.end_col_offset, n.comparators[0].col_offset, ops[0],
                          rng.choice(ops[1:])))
rng.shuffle(cands)
out.mkdir(parents=True, exist_ok=True)
kinds, k = {}, 0
for f, line, a, b, old, new in cands:
    if k == count:
        break
    lines = (src / f).read_text(encoding="utf8").splitlines(keepends=True)
    raw = lines[line - 1].encode()
    seg = raw[a:b].decode()
    if seg.count(old.strip()) != 1 or old not in seg:
        continue
    new_line = (raw[:a] + seg.replace(old, new).encode() + raw[b:]).decode()
    new_lines = lines[:line - 1] + [new_line] + lines[line:]
    patch = f"diff --git a/{f} b/{f}\nindex 0000000..1111111 100644\n" + \
        "".join(difflib.unified_diff(lines, new_lines, f"a/{f}", f"b/{f}", n=3))
    # keep only mutants where the condition rule drops tests that coverage would pick
    old_text = "".join(lines)
    picked = sel.cond_affected(f, old_text, "".join(new_lines))
    if picked is None:
        continue
    covered = sel.covering(f, line, line)
    if not (covered - picked):
        continue
    cid = f"c{k:04d}"
    (out / f"{cid}.patch").write_text(patch, encoding="utf8", newline="\n")
    kinds[cid] = "cond"
    k += 1
(out / "kinds.json").write_text(json.dumps(kinds, indent=1))
print(f"{len(cands)} candidate sites, wrote {k}", file=sys.stderr)
