"""List the mutants in a patch directory that contain an edit gen.py could never produce
(so labeling them tests something our own corpus doesn't).

    python tools/mutants/novel.py build/indep/patches     # writes <dir>/novel.json
"""
import collections
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from selector import parse_patch  # noqa: E402

OPS = [(" == ", " != "), (" != ", " == "), (" is not ", " is "), (" is ", " is not "),
       (" and ", " or "), (" or ", " and "), (" not ", " "), (" < ", " <= "), (" <= ", " < "),
       (" > ", " >= "), (" >= ", " > "), (" + ", " - "), (" - ", " + "), (" in ", " not in "),
       ("True", "False"), ("False", "True")]


def ours_code(line):
    """Every line gen.py's mutate_code_line can turn `line` into."""
    s, out = line.lstrip(), set()
    if not s or s.startswith(("#", "def ", "class ", "@", "import ", "from ", '"', "'")):
        return out
    if '"' in line or "'" in line:
        cands = [(a, b) for a, b in OPS if a in line.split('"')[0].split("'")[0]]
    else:
        cands = [(a, b) for a, b in OPS if a in line]
    for a, b in cands:
        i = line.find(a)
        out.add(line[:i] + b + line[i + len(a):])
    if '"' not in line and "'" not in line:
        for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line.split("#")[0]):
            n = int(m.group(1))
            for v in ([n - 1, n + 1] if n else [1]):
                out.add(line[:m.start(1)] + str(v) + line[m.end(1):])
    return out


def ours_fix(f, line):
    out = set()
    if not f.startswith("tests/fixtures/optimizer/") or not line.strip() or line.startswith(("#", "--")):
        return out
    words = list(re.finditer(r"\b[A-Za-z_]\w*\b", line))
    if len(words) < 3:
        return out
    for w in words[1:]:
        for p in ["x ", "1 ", ""]:
            for s in ["", "a", " "]:
                out.add(line[:w.start()] + p + line[w.start():w.end()] + s + line[w.end():])
    return out


d = Path(sys.argv[1])
edits = json.loads((d / "edits.json").read_text())
novel = {}
for p in sorted(d.glob("*.patch")):
    new_edit = False
    for fd in parse_patch(p.read_text(encoding="utf8")):
        f = fd["new"]
        for _, body in fd["hunks"]:
            minus = [l[1:] for l in body if l[0] == "-"]
            plus = [l[1:] for l in body if l[0] == "+"]
            if not minus and len(plus) == 1 and plus[0].lstrip().startswith("#") and f.endswith(".py"):
                continue  # inserted comment line: gen.py makes these
            if len(minus) == 1 and len(plus) == 1:
                reach = ours_code(minus[0]) if f.endswith(".py") else ours_fix(f, minus[0])
                if plus[0] in reach:
                    continue
            new_edit = True
    if new_edit:
        novel[p.stem] = edits.get(p.stem, "?")
print(len(novel), "novel of", len(list(d.glob("*.patch"))))
for k, v in collections.Counter(novel.values()).most_common():
    print(f"  {v:3} {k}")
(d / "novel.json").write_text(json.dumps(sorted(novel)))
