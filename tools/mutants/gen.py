"""Generate sample-style one-line mutants of the pinned sqlglot checkout (evaluation only;
nothing derived from them ships in the selector).

    python tools/mutants/gen.py <sqlglot_checkout> <out_dir> <count> [seed]

Kinds: code (operator/constant/keyword swaps), comment (inserted comment line),
fixture (token typo in an optimizer .sql fixture), testedit (changed literal in a test),
docstring (changed doctest output), multi (2-3 of the above in different files).
"""
import difflib
import io
import json
import random
import re
import sys
import tokenize
from pathlib import Path

src, out = Path(sys.argv[1]), Path(sys.argv[2])
count = int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
out.mkdir(parents=True, exist_ok=True)

PY = sorted(p.relative_to(src).as_posix() for p in (src / "sqlglot").rglob("*.py") if p.stat().st_size > 200)
FIX = sorted(p.relative_to(src).as_posix() for p in (src / "tests/fixtures/optimizer").glob("*.sql"))
TESTS = sorted(p.relative_to(src).as_posix() for p in (src / "tests").rglob("test_*.py"))
text_cache = {}


def text(f):
    if f not in text_cache:
        text_cache[f] = (src / f).read_text(encoding="utf8")
    return text_cache[f]


OPS = [(" == ", " != "), (" != ", " == "), (" is not ", " is "), (" is ", " is not "),
       (" and ", " or "), (" or ", " and "), (" not ", " "), (" < ", " <= "), (" <= ", " < "),
       (" > ", " >= "), (" >= ", " > "), (" + ", " - "), (" - ", " + "), (" in ", " not in "),
       ("True", "False"), ("False", "True")]


def code_tokens(line):
    """Spans of the line outside strings and comments."""
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(line.strip() + "\n").readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return toks


def mutate_code_line(line):
    stripped = line.lstrip()
    if not stripped or stripped.startswith(("#", "def ", "class ", "@", "import ", "from ", '"', "'")):
        return None
    if '"' in line or "'" in line:  # keep it simple: avoid touching string contents
        cands = [(a, b) for a, b in OPS if a in line.split('"')[0].split("'")[0]]
    else:
        cands = [(a, b) for a, b in OPS if a in line]
    nums = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line.split("#")[0]))
    if '"' not in line and "'" not in line:
        cands += [("NUM", m) for m in nums]
    if not cands:
        return None
    a, b = rng.choice(cands)
    if a == "NUM":
        n = int(b.group(1))
        return line[:b.start(1)] + str(n + rng.choice([-1, 1]) if n else "1") + line[b.end(1):]
    i = line.find(a)
    return line[:i] + b + line[i + len(a):]


def try_compile(t, f):
    try:
        compile(t, f, "exec")
        return True
    except SyntaxError:
        return False


def edit_code():
    for _ in range(200):
        f = rng.choice(PY)
        lines = text(f).splitlines(keepends=True)
        i = rng.randrange(len(lines))
        new = mutate_code_line(lines[i])
        if new and new != lines[i]:
            t = "".join(lines[:i] + [new] + lines[i + 1:])
            if try_compile(t, f):
                return f, t
    raise RuntimeError("no code mutant")


def edit_comment():
    f = rng.choice(PY)
    lines = text(f).splitlines(keepends=True)
    for _ in range(100):
        i = rng.randrange(len(lines))
        if lines[i].strip() and not lines[i].rstrip().endswith(("(", "[", "{", ",", "\\")) and \
                not lines[i - 1].rstrip().endswith(("(", "[", "{", ",", "\\")):
            ind = lines[i][:len(lines[i]) - len(lines[i].lstrip())]
            note = rng.choice(["Cheap check first", "Kept for backwards compatibility",
                               "Intentionally not cached", "TODO: revisit", "See issue tracker"])
            t = "".join(lines[:i] + [f"{ind}# {note}\n"] + lines[i:])
            if try_compile(t, f):
                return f, t
    raise RuntimeError("no comment mutant")


def edit_fixture():
    for _ in range(200):
        f = rng.choice(FIX)
        lines = text(f).splitlines(keepends=True)
        i = rng.randrange(len(lines))
        l = lines[i]
        if not l.strip() or l.startswith(("#", "--")):
            continue
        words = list(re.finditer(r"\b[A-Za-z_]\w*\b", l))
        if len(words) < 3:
            continue
        w = rng.choice(words[1:])
        new = l[:w.start()] + rng.choice(["x ", "1 ", ""]) + l[w.start():w.end()] + \
            rng.choice(["", "a", " "]) + l[w.end():]
        if new != l:
            return f, "".join(lines[:i] + [new] + lines[i + 1:])
    raise RuntimeError("no fixture mutant")


def edit_test():
    for _ in range(300):
        f = rng.choice(TESTS)
        lines = text(f).splitlines(keepends=True)
        i = rng.randrange(len(lines))
        l = lines[i]
        m = re.search(r'"([A-Za-z][^"\\]{3,})"', l)
        if not m or not l.startswith("        "):
            continue
        s = m.group(1)
        j = rng.randrange(len(s))
        s2 = s[:j] + rng.choice("xyz") + s[j + 1:]
        new = l[:m.start(1)] + s2 + l[m.end(1):]
        t = "".join(lines[:i] + [new] + lines[i + 1:])
        if new != l and try_compile(t, f):
            return f, t
    raise RuntimeError("no test mutant")


def edit_docstring():
    cands = [(f, i) for f in PY for i, l in enumerate(text(f).splitlines(keepends=True))
             if l.lstrip().startswith(">>>")]
    for _ in range(200):
        f, i = rng.choice(cands)
        lines = text(f).splitlines(keepends=True)
        if i + 1 < len(lines) and lines[i + 1].strip() and not lines[i + 1].lstrip().startswith((">>>", '"""')):
            l = lines[i + 1]
            k = len(l) - len(l.lstrip())
            new = l[:k] + "x" + l[k:]
            t = "".join(lines[:i + 1] + [new] + lines[i + 2:])
            if try_compile(t, f):
                return f, t
    raise RuntimeError("no docstring mutant")


EDITS = {"code": edit_code, "comment": edit_comment, "fixture": edit_fixture,
         "testedit": edit_test, "docstring": edit_docstring}
WEIGHTS = {"code": 55, "fixture": 15, "comment": 8, "multi": 12, "testedit": 6, "docstring": 4}


def diff(f, new_text):
    a = text(f).splitlines(keepends=True)
    b = new_text.splitlines(keepends=True)
    body = "".join(difflib.unified_diff(a, b, f"a/{f}", f"b/{f}", n=3))
    return f"diff --git a/{f} b/{f}\nindex 0000000..1111111 100644\n" + body


meta = {}
kinds = list(WEIGHTS)
for k in range(count):
    kind = rng.choices(kinds, [WEIGHTS[x] for x in kinds])[0]
    if kind == "multi":
        parts, files = [], set()
        for sub in rng.sample(["code", "comment", "fixture", "code"], rng.choice([2, 3])):
            f, t = EDITS[sub]()
            if f not in files:
                files.add(f)
                parts.append(diff(f, t))
        patch = "".join(parts)
    else:
        f, t = EDITS[kind]()
        patch = diff(f, t)
    cid = f"m{k:04d}"
    (out / f"{cid}.patch").write_text(patch, encoding="utf8", newline="\n")
    meta[cid] = kind
(out / "kinds.json").write_text(json.dumps(meta, indent=1))
print(f"wrote {count} mutants to {out}", file=sys.stderr)
