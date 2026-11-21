"""Validation mutants for the comparison profile: swap `is` <-> `==` / `is not` <-> `!=` at
random sites maps/cmp.json.gz marks safe. Labeled with the full suite, every one should
break nothing.

    python tools/mutants/gen_swaps.py <sqlglot_checkout> <out_dir> <count> [seed]
"""
import ast
import difflib
import gzip
import json
import random
import sys
from pathlib import Path

src, out, count = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 7)
safe = json.load(gzip.open(Path(__file__).resolve().parents[2] / "maps/cmp.json.gz", "rt"))
SWAP = {ast.Is: (" is ", " == "), ast.IsNot: (" is not ", " != "),
        ast.Eq: (" == ", " is "), ast.NotEq: (" != ", " is not ")}
cands = []
for f, keys in safe.items():
    text = (src / f).read_text(encoding="utf8")
    for n in ast.walk(ast.parse(text)):
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in SWAP \
                and f"{n.lineno}:{n.col_offset}" in keys and n.lineno == n.end_lineno \
                and n.left.end_lineno == n.lineno:
            cands.append((f, n.lineno, n.left.end_col_offset, n.comparators[0].col_offset, type(n.ops[0])))
rng.shuffle(cands)
out.mkdir(parents=True, exist_ok=True)
kinds, k = {}, 0
for f, line, a, b, op in cands:
    if k == count:
        break
    lines = (src / f).read_text(encoding="utf8").splitlines(keepends=True)
    l = lines[line - 1].encode()  # col offsets are utf-8 byte offsets
    old, new = SWAP[op]
    seg = l[a:b].decode()
    if seg.count(old.strip()) != 1 or old not in seg:
        continue
    l2 = l[:a] + seg.replace(old, new).encode() + l[b:]
    new_lines = lines[:line - 1] + [l2.decode()] + lines[line:]
    body = "".join(difflib.unified_diff(lines, new_lines, f"a/{f}", f"b/{f}", n=3))
    cid = f"s{k:04d}"
    (out / f"{cid}.patch").write_text(f"diff --git a/{f} b/{f}\nindex 0000000..1111111 100644\n" + body,
                                      encoding="utf8", newline="\n")
    kinds[cid] = "swap"
    k += 1
(out / "kinds.json").write_text(json.dumps(kinds, indent=1))
print(f"{len(cands)} candidate sites, wrote {k}", file=sys.stderr)
