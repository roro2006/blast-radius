#!/usr/bin/env python3
"""Generate synthetic single-edit patches against a sqlglot checkout.

usage: python gen_indep.py <sqlglot_checkout> <out_dir> <count> [seed]

Writes <out_dir>/iNNNN.patch, kinds.json (code|fixture|multi|comment) and
edits.json (short edit-type labels).
"""
import difflib
import hashlib
import io
import json
import keyword
import os
import random
import re
import sys
import tokenize
from collections import Counter, defaultdict

# --------------------------------------------------------------------------
# file discovery
# --------------------------------------------------------------------------

PY_GROUPS = {  # directory prefix -> weight (mirrors the examples' file mix)
    "sqlglot/parsers/": 20,
    "sqlglot/generators/": 20,
    "sqlglot/dialects/": 15,
    "sqlglot/optimizer/": 18,
    "sqlglot/expressions/": 8,
    "sqlglot/typing/": 6,
    "sqlglot/executor/": 4,
    "sqlglot/": 9,  # remaining top-level modules (serde, transforms, helper, ...)
}


def list_files(root, sub, suffix):
    out = []
    for dp, dn, fn in os.walk(os.path.join(root, sub)):
        dn[:] = sorted(d for d in dn if d not in ("__pycache__",))
        for f in sorted(fn):
            if f.endswith(suffix):
                out.append(os.path.relpath(os.path.join(dp, f), root).replace("\\", "/"))
    return out


def group_of(path):
    for pre in PY_GROUPS:
        if pre != "sqlglot/" and path.startswith(pre):
            return pre
    return "sqlglot/"


def read(root, rel):
    with open(os.path.join(root, rel), "rb") as fh:
        raw = fh.read()
    raw = raw.replace(b"\r\n", b"\n")  # working tree may be CRLF; patches target LF blobs
    if b"\r" in raw or not raw.endswith(b"\n"):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------------------
# diff output
# --------------------------------------------------------------------------

def blob_hash(text):
    b = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(b) + b).hexdigest()[:7]


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@$")
FUNC_RE = re.compile(r"^[A-Za-z_$]")


def make_diff(path, old, new):
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    out = [
        f"diff --git a/{path} b/{path}\n",
        f"index {blob_hash(old)}..{blob_hash(new)} 100644\n",
        f"--- a/{path}\n",
        f"+++ b/{path}\n",
    ]
    diff = list(difflib.unified_diff(a, b, n=3, lineterm=""))[2:]
    for line in diff:
        m = HUNK_RE.match(line)
        if m:
            start = int(m.group(1))
            ctx = ""
            for j in range(start - 2, -1, -1):
                if j < len(a) and FUNC_RE.match(a[j]):
                    ctx = a[j].rstrip()[:80].rstrip()
                    break
            out.append(line + (" " + ctx if ctx else "") + "\n")
        else:
            out.append(line if line.endswith("\n") else line + "\n")
    return "".join(out)


# --------------------------------------------------------------------------
# python token-level candidates
# --------------------------------------------------------------------------

CMP_ALTS = {"<": ["<=", ">"], "<=": ["<", ">="], ">": [">=", "<"], ">=": [">", "<="]}
CALL_SWAPS = {"min": "max", "max": "min", "any": "all", "all": "any",
              "startswith": "endswith", "endswith": "startswith",
              "lower": "upper", "upper": "lower", "lstrip": "rstrip", "rstrip": "lstrip",
              "isupper": "islower", "islower": "isupper"}
CAT_WEIGHTS = {"cmp": 14, "boolop": 8, "is": 8, "not": 6, "bool": 14, "num": 20,
               "str": 8, "arith": 3, "call": 3, "enum": 4, "brk": 1}
INT_RE = re.compile(r"^(0[xX][0-9a-fA-F]+|\d+)$")


class Cand:
    __slots__ = ("cat", "label", "line", "c0", "c1", "repl")

    def __init__(self, cat, label, line, c0, c1, repl):
        self.cat, self.label, self.line, self.c0, self.c1, self.repl = cat, label, line, c0, c1, repl


def py_candidates(src):
    try:
        toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline)
                if t.type not in (tokenize.COMMENT, tokenize.NL)]
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return []
    n = len(toks)
    for_lines = {t.start[0] for t in toks if t.type == tokenize.NAME and t.string == "for"}
    ctx = [None] * n  # innermost bracket kind
    skip = [False] * n
    stack = []
    fdepth = 0
    for i, t in enumerate(toks):
        if t.type == tokenize.FSTRING_START:
            fdepth += 1
        if fdepth:
            skip[i] = True
        if t.type == tokenize.FSTRING_END:
            fdepth -= 1
        if t.type == tokenize.OP and t.string in "([{":
            prev = toks[i - 1] if i else None
            isname = prev is not None and ((prev.type == tokenize.NAME and not keyword.iskeyword(prev.string))
                                           or prev.string in (")", "]"))
            if t.string == "(":
                if isname:
                    kind = "call:" + (prev.string if prev.type == tokenize.NAME else "")
                else:
                    kind = "tuple"
            elif t.string == "[":
                kind = "subscript" if isname else "list"
            else:
                kind = "brace"
            stack.append(kind)
        ctx[i] = stack[-1] if stack else None
        if t.type == tokenize.OP and t.string in ")]}" and stack:
            stack.pop()
            ctx[i] = stack[-1] if stack else None

    members = defaultdict(set)
    for i in range(2, n):
        if (toks[i].type == tokenize.NAME and toks[i - 1].string == "." and toks[i - 2].type == tokenize.NAME
                and toks[i].string.isupper() and len(toks[i].string) > 1 and not skip[i]):
            members[toks[i - 2].string].add(toks[i].string)

    cands = []

    def add(*a):
        cands.append(Cand(*a))

    for i, t in enumerate(toks):
        if skip[i] or t.type not in (tokenize.NAME, tokenize.OP, tokenize.NUMBER, tokenize.STRING):
            continue
        s = t.string
        ln, c0 = t.start
        c1 = t.end[1]
        if t.end[0] != ln:
            continue
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < n else None
        first_on_line = prev is None or prev.type in (tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)
        k = ctx[i] or ""
        if t.type == tokenize.NAME:
            if s == "is":
                if nxt and nxt.string == "not" and nxt.start[0] == ln:
                    add("is", "identity op swap is not->is", ln, c0, nxt.end[1], "is")
                    add("is", "identity op swap is not->!=", ln, c0, nxt.end[1], "!=")
                else:
                    add("is", "identity op swap is->is not", ln, c0, c1, "is not")
                    add("is", "identity op swap is->==", ln, c0, c1, "==")
            elif s in ("and", "or"):
                add("boolop", f"bool op swap {s}->{'or' if s == 'and' else 'and'}", ln, c0, c1,
                    "or" if s == "and" else "and")
            elif s == "not":
                if prev and prev.string == "is":
                    pass
                elif nxt and nxt.string == "in" and nxt.start[0] == ln:
                    add("not", "not in -> in", ln, c0, nxt.end[1], "in")
                elif nxt and nxt.start[0] == ln:
                    add("not", "not removal", ln, c0, nxt.start[1], "")
            elif s == "in":
                if not (prev and prev.string == "not") and ln not in for_lines:
                    add("not", "in -> not in", ln, c0, c1, "not in")
            elif s in ("True", "False"):
                new = "False" if s == "True" else "True"
                p = prev.string if prev else ""
                if p == ":" and k == "brace":
                    lab = "dict value bool flip"
                elif p == "=" and k.startswith("call"):
                    lab = "kwarg bool flip"
                elif p == "=" and k == "":
                    lab = "assign bool flip"
                elif p == "return":
                    lab = "return bool flip"
                else:
                    lab = "bool literal flip"
                add("bool", lab, ln, c0, c1, new)
            elif s in ("break", "continue") and first_on_line:
                add("brk", "break/continue swap", ln, c0, c1, "continue" if s == "break" else "break")
            elif s in CALL_SWAPS and nxt and nxt.string == "(":
                add("call", f"call name swap {s}->{CALL_SWAPS[s]}", ln, c0, c1, CALL_SWAPS[s])
            elif (s.isupper() and len(s) > 1 and prev and prev.string == "." and i >= 2
                  and toks[i - 2].type == tokenize.NAME):
                others = sorted(members.get(toks[i - 2].string, set()) - {s})
                if others:
                    for o in others[:: max(1, len(others) // 2)][:2]:
                        add("enum", "enum member swap", ln, c0, c1, o)
        elif t.type == tokenize.OP:
            if s in ("==", "!="):
                add("cmp", "compare op swap ==/!=", ln, c0, c1, "!=" if s == "==" else "==")
            elif s in CMP_ALTS:
                for alt in CMP_ALTS[s]:
                    add("cmp", f"compare op swap {s}->{alt}", ln, c0, c1, alt)
            elif s in ("+", "-") and prev and (
                    (prev.type == tokenize.NAME and not keyword.iskeyword(prev.string))
                    or prev.type == tokenize.NUMBER or prev.string in (")", "]")):
                if nxt and nxt.type != tokenize.STRING:
                    add("arith", f"arith op swap {s}->{'-' if s == '+' else '+'}", ln, c0, c1,
                        "-" if s == "+" else "+")
        elif t.type == tokenize.NUMBER:
            if not INT_RE.match(s):
                continue
            is_hex = s.lower().startswith("0x")
            v = int(s, 16) if is_hex else int(s)
            news = [v + 1] + ([v - 1] if v >= 1 else [])
            p = prev.string if prev else ""
            nx = nxt.string if nxt else ""
            cmpops = ("==", "!=", "<", ">", "<=", ">=")
            if k == "tuple":
                lab = "tuple number change"
            elif k.startswith("call:seq_get"):
                lab = "seq_get index change"
            elif k.startswith("call"):
                lab = "call arg number change" if p != "=" else "kwarg number change"
            elif k == "subscript":
                lab = "subscript index change"
            elif k == "list":
                lab = "list number change"
            elif k == "brace":
                lab = "dict value number change" if p == ":" else "dict/set number change"
            elif p in cmpops or nx in cmpops:
                lab = "compare constant change"
            elif p == "=":
                lab = "constant number change"
            else:
                lab = "number change"
            for nv in news:
                rep = hex(nv) if is_hex else str(nv)
                if is_hex and s[2:].isupper():
                    rep = "0x" + format(nv, "X")
                add("num", lab, ln, c0, c1, rep)
        elif t.type == tokenize.STRING:
            m = re.match(r"^(['\"])(.*)\1$", s)
            if not m or "\\" in s or s.startswith(('"""', "'''")) or len(m.group(2)) < 2:
                continue
            if first_on_line and nxt is not None and nxt.type == tokenize.NEWLINE:
                continue  # docstring-ish statement
            if (nxt is not None and nxt.type == tokenize.STRING) or (prev and prev.type == tokenize.STRING):
                continue  # implicit concatenation
            body = m.group(2)
            p = prev.string if prev else ""
            nx = nxt.string if nxt else ""
            if k == "brace" and nx == ":":
                lab = "dict key string change"
            elif k == "brace" and p == ":":
                lab = "dict value string change"
            elif k.startswith("call") or k == "tuple" or k == "list":
                lab = "string arg change"
            else:
                lab = "string literal change"
            q = m.group(1)
            letters = [j for j, ch in enumerate(body) if ch.isalpha()]
            if not letters:
                continue
            opts = [body[:-1]]
            j = letters[len(letters) // 2]
            ch = body[j]
            alt = chr((ord(ch.lower()) - 97 + 1) % 26 + 97)
            opts.append(body[:j] + (alt.upper() if ch.isupper() else alt) + body[j + 1:])
            opts.append(body + ("S" if body.isupper() else "s"))
            for o in opts:
                add("str", lab, ln, c0, c1, q + o + q)
    return cands


def apply_cand(lines, c):
    l = lines[c.line - 1]
    out = list(lines)
    out[c.line - 1] = l[:c.c0] + c.repl + l[c.c1:]
    return out


def compiles(path, text):
    try:
        compile(text, path, "exec", dont_inherit=True)
        return True
    except (SyntaxError, ValueError):
        return False


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------

LITERALS = ["42", "0", "1", "NULL", "x", "a", "1", "42"]
TOKEN_RE = re.compile(r"\w+|[^\w\s]")


def fixture_line_info(lines):
    """Return [(idx, role)] for SQL lines; role is 'input' or 'expected output'."""
    stmt = 0
    out = []
    for i, l in enumerate(lines):
        s = l.strip()
        if not s:
            stmt = 0
            continue
        if s.startswith("#") or s.startswith("--"):
            continue
        out.append((i, "input" if stmt == 0 else "expected output"))
        if s.endswith(";"):
            stmt += 1
    return out


def fixture_edit(rng, line):
    toks = list(TOKEN_RE.finditer(line))
    if len(toks) < 3:
        return None
    words = [m for m in toks if m.group().replace("_", "").isalnum()]
    op = rng.choices(["insert", "delete_word", "delete_char", "append", "typo", "swap"],
                     [30, 15, 15, 15, 10, 5])[0]
    if op == "insert":
        m = rng.choice(toks)
        lit = rng.choice(LITERALS)
        if rng.random() < 0.5:
            return line[:m.start()] + lit + " " + line[m.start():], "fixture literal insert"
        return line[:m.end()] + " " + lit + line[m.end():], "fixture literal insert"
    if op == "delete_word" and words:
        kw = [m for m in words if m.group().isupper() and len(m.group()) > 1] or words
        m = rng.choice(kw)
        return line[:m.start()] + line[m.end():], "fixture word delete"
    if op == "delete_char":
        ps = [m for m in toks if not m.group().replace("_", "").isalnum() and m.group() != ";"]
        if not ps:
            return None
        m = rng.choice(ps)
        return line[:m.start()] + line[m.end():], "fixture punctuation delete"
    if op == "append" and words:
        m = rng.choice(words)
        return line[:m.end()] + rng.choice(["42", "1", "a", "0"]) + line[m.end():], "fixture identifier append"
    if op == "typo" and words:
        m = rng.choice(words)
        return line[:m.end()] + rng.choice("abxyz") + " " + line[m.end():], "fixture typo insert"
    if op == "swap" and len(words) >= 2:
        j = rng.randrange(len(words) - 1)
        a, b = words[j], words[j + 1]
        if a.group() == b.group():
            return None
        return (line[:a.start()] + b.group() + line[a.end():b.start()] + a.group() + line[b.end():],
                "fixture adjacent word swap")
    return None


FIXTURE_WEIGHTS = [("tests/fixtures/optimizer/", 85), ("tests/fixtures/identity.sql", 5),
                   ("tests/fixtures/pretty.sql", 5), ("tests/fixtures/optimizer/tpc", 5)]

# --------------------------------------------------------------------------
# comments
# --------------------------------------------------------------------------

COMMENTS = [
    "Intentionally not cached", "Cheap check first", "Keep in sync with the generator",
    "Kept for backwards compatibility", "Mirrors the behavior of the reference implementation",
    "Fast path for the common case", "Order matters here", "Normalized upstream",
    "Handled separately below", "Preserve the original behavior", "Avoid mutating the input expression",
    "See the dialect documentation for details", "Only reached for well-formed input",
    "This mirrors the upstream implementation",
]


def multiline_string_lines(src):
    bad = set()
    try:
        for t in tokenize.generate_tokens(io.StringIO(src).readline):
            if t.type in (tokenize.STRING, tokenize.FSTRING_MIDDLE) and t.end[0] > t.start[0]:
                bad.update(range(t.start[0] + 1, t.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return bad


# --------------------------------------------------------------------------
# generator
# --------------------------------------------------------------------------

class Gen:
    def __init__(self, root, seed):
        self.root = root
        self.rng = random.Random(seed)
        self.bygroup = defaultdict(list)
        for p in list_files(root, "sqlglot", ".py"):
            self.bygroup[group_of(p)].append(p)
        self.sql = list_files(root, "tests/fixtures", ".sql")
        self.cache = {}
        self.used = set()

    def src(self, p):
        if p not in self.cache:
            self.cache[p] = read(self.root, p)
        return self.cache[p]

    def pick_py(self):
        r = self.rng
        groups = [g for g in PY_GROUPS if self.bygroup.get(g)]
        g = r.choices(groups, [PY_GROUPS[x] for x in groups])[0]
        return r.choice(self.bygroup[g])

    def pick_sql(self):
        r = self.rng
        pre = r.choices([w[0] for w in FIXTURE_WEIGHTS], [w[1] for w in FIXTURE_WEIGHTS])[0]
        if pre.endswith(".sql"):
            return pre
        if pre.endswith("/tpc"):
            files = [f for f in self.sql if f.startswith(pre)]
        else:
            files = [f for f in self.sql if f.startswith(pre) and "/tpc" not in f]
        return r.choice(files)

    def code_edit(self, exclude=()):
        r = self.rng
        for _ in range(60):
            p = self.pick_py()
            if p in exclude:
                continue
            src = self.src(p)
            if not src:
                continue
            key = ("cands", p)
            if key not in self.cache:
                self.cache[key] = py_candidates(src)
            cs = self.cache[key]
            if not cs:
                continue
            cats = sorted({c.cat for c in cs})
            cat = r.choices(cats, [CAT_WEIGHTS[c] for c in cats])[0]
            c = r.choice([x for x in cs if x.cat == cat])
            sig = (p, c.line, c.c0, c.repl)
            if sig in self.used:
                continue
            new = "\n".join(apply_cand(src.split("\n"), c))
            if new == src or not compiles(p, new):
                continue
            self.used.add(sig)
            return p, src, new, c.label
        return None

    def fixture_edit(self, exclude=()):
        r = self.rng
        for _ in range(60):
            p = self.pick_sql()
            if p in exclude:
                continue
            src = self.src(p)
            if not src:
                continue
            lines = src.split("\n")
            info = fixture_line_info(lines)
            if not info:
                continue
            i, role = r.choice(info)
            if len(lines[i].strip()) < 8:
                continue
            res = fixture_edit(r, lines[i])
            if not res or res[0] == lines[i] or not res[0].strip():
                continue
            sig = (p, i, res[0])
            if sig in self.used:
                continue
            self.used.add(sig)
            lines[i] = res[0]
            return p, src, "\n".join(lines), f"{res[1]} ({role})"
        return None

    def comment_edit(self, exclude=()):
        r = self.rng
        for _ in range(60):
            p = self.pick_py()
            if p in exclude:
                continue
            src = self.src(p)
            if not src:
                continue
            key = ("bad", p)
            if key not in self.cache:
                self.cache[key] = multiline_string_lines(src)
            bad = self.cache[key]
            if bad is None:
                continue
            lines = src.split("\n")
            i = r.randrange(0, len(lines) - 1)
            if (i + 1) in bad or not lines[i].strip() or (i > 0 and lines[i - 1].rstrip().endswith("\\")):
                continue
            if lines[i].lstrip().startswith(("#!", "# -*-")):
                continue
            indent = re.match(r"\s*", lines[i]).group()
            text = r.choice(COMMENTS)
            sig = (p, i, text)
            if sig in self.used:
                continue
            self.used.add(sig)
            new_src = "\n".join(lines[:i] + [f"{indent}# {text}"] + lines[i:])
            if not compiles(p, new_src):
                continue
            return p, src, new_src, "comment insert"
        return None

    def build(self, kind):
        r = self.rng
        if kind in ("code", "fixture", "comment"):
            e = getattr(self, kind + "_edit")()
            return ([e], e[3]) if e else (None, None)
        shape = r.choices(["cf", "ccf", "cdf"], [4, 3, 3])[0]
        seq = {"cf": ["comment", "fixture"], "ccf": ["comment", "comment", "fixture"],
               "cdf": ["comment", "code", "fixture"]}[shape]
        parts, ex = [], set()
        for k in seq:
            e = getattr(self, k + "_edit")(exclude=ex)
            if e is None:
                return None, None
            ex.add(e[0])
            parts.append((k, e))
        parts.sort(key=lambda x: x[1][0])
        label = "multi: " + " + ".join(
            "comment" if k == "comment" else ("code[" + e[3] + "]" if k == "code" else "fixture")
            for k, e in parts)
        return [e for _, e in parts], label


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        sys.exit(2)
    root, out, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    seed = int(sys.argv[4]) if len(sys.argv) > 4 else 0
    os.makedirs(out, exist_ok=True)
    g = Gen(root, seed)
    # kind mix of the examples: 16 code / 8 fixture / 3 multi / 3 comment (of 30)
    kinds = []
    for k, w in {"code": 16, "fixture": 8, "multi": 3, "comment": 3}.items():
        kinds += [k] * round(count * w / 30)
    while len(kinds) < count:
        kinds.append("code")
    kinds = kinds[:count]
    g.rng.shuffle(kinds)
    kinds_out, edits_out = {}, {}
    for idx, kind in enumerate(kinds):
        parts, label = None, None
        for _ in range(50):
            parts, label = g.build(kind)
            if parts:
                break
        if not parts:
            raise SystemExit(f"could not build a {kind} patch")
        pid = f"i{idx:04d}"
        text = "".join(make_diff(p, old, new) for p, old, new, _ in sorted(parts, key=lambda e: e[0]))
        with open(os.path.join(out, pid + ".patch"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        kinds_out[pid] = kind
        edits_out[pid] = label
    with open(os.path.join(out, "kinds.json"), "w") as fh:
        json.dump(kinds_out, fh, indent=1)
    with open(os.path.join(out, "edits.json"), "w") as fh:
        json.dump(edits_out, fh, indent=1)
    print("kinds:", dict(Counter(kinds_out.values())))


if __name__ == "__main__":
    main()
