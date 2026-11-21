"""Blast Radius selector: for each patch, pick the sqlglot tests that can catch it.

Maps (built once from the pinned commit, see tools/):
  lines    file -> line -> tests whose run executed that line
  fixtures data file under tests/ -> tests that opened it
  uses     sqlglot class (qualified name) -> tests that instantiated it or a subclass

Per changed file:
  * same compiled code (comments, whitespace)          -> nothing
  * line inside a function                             -> tests covering its statement
  * module/class-level line (runs at import time)      -> tests covering lines that read
                                                          the names it binds, limited to
                                                          users of the class
  * fixture                                            -> tests that read it
  * test file                                          -> the edited tests / class / module
  * anything not understood                            -> every test
Stdout carries only answer lines; logs go to stderr.
"""
import ast
import gzip
import json
import os
import re
import sys
from pathlib import Path

DATA = Path(os.environ.get("DATA", "/data"))
SRC = Path(os.environ.get("SRC", "/app/sqlglot"))
MAPS = Path(os.environ.get("MAPS", Path(__file__).resolve().parent / "maps" / "maps.json.gz"))
CMP = Path(os.environ.get("CMP", Path(__file__).resolve().parent / "maps" / "cmp.json.gz"))
WORKER_MS = 2000
MAX_WORKERS = 8
LAMBDA_RULE = os.environ.get("LAMBDA_RULE", "1") != "0"
ANNOTATION_RULE = os.environ.get("ANNOTATION_RULE", "1") != "0"
REORDER_RULE = os.environ.get("REORDER_RULE", "1") != "0"  # 12 labeled reorder mutants: 0 misses
VALPROF_RULE = os.environ.get("VALPROF_RULE", "1") != "0"
HEAVY_RULE = os.environ.get("HEAVY_RULE", "1") != "0"  # heavy.py: check the two slowest tests
ADDED_RULE = os.environ.get("ADDED_RULE", "1") != "0"  # new .py files nothing imports yet
VALPROF = Path(os.environ.get("VALPROF", Path(__file__).resolve().parent / "maps" / "valprof"))


class RunAll(Exception):
    """This change is outside what the maps can explain."""


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- maps

class Sets:
    """The maps' test sets, delta-packed in the file; each is decoded on first use (a run
    touches a few hundred of them)."""

    def __init__(self, packed):
        self.packed, self.done = packed, {}

    def __getitem__(self, i):
        s = self.done.get(i)
        if s is None:
            s, acc = [], 0
            for k, d in enumerate(self.packed[i]):
                acc = d if k == 0 else acc + d
                s.append(acc)
            self.done[i] = s
        return s


def load_maps():
    with gzip.open(MAPS, "rt") as f:
        doc = json.load(f)
    return doc, Sets(doc["sets"])


class Source:
    """sqlglot/*.py text, for finding the lines that mention a name."""

    def __init__(self):
        self.files = None

    def occurrences(self, name):
        """(file, line) of every line where `name` appears as an identifier before any `#`.
        A superset of tokenizing each comment-stripped line: a name glued to a leading digit
        also counts."""
        if self.files is None:
            self.files = []
            for p in sorted((SRC / "sqlglot").rglob("*.py")):
                text = p.read_text(encoding="utf8")
                self.files.append((p.relative_to(SRC).as_posix(), text))
        pat = re.compile(r"(?<![A-Za-z_])" + re.escape(name) + r"(?!\w)")
        out = []
        for rel, text in self.files:
            last, pos, line = -1, 0, 1
            for m in pat.finditer(text):
                line += text.count("\n", pos, m.start())  # running count: one pass per file
                pos = m.start()
                start = text.rfind("\n", 0, m.start()) + 1
                if "#" in text[start:m.start()]:
                    continue
                if line != last:
                    out.append((rel, line))
                    last = line
        return out


def load_cmp():
    try:
        with gzip.open(CMP, "rt") as f:
            return {k: set(v) for k, v in json.load(f).items()}
    except FileNotFoundError:
        return {}


# --------------------------------------------------------------------------- patches

def parse_patch(text):
    """[(old_path|None, new_path|None, hunks, flags)]; hunks are [old_start, [lines]]."""
    files, cur = [], None
    for line in text.splitlines():
        if line.startswith("diff --git "):
            cur = {"old": None, "new": None, "hunks": [], "flags": set()}
            files.append(cur)
            continue
        if cur is None:
            if line.startswith("--- "):
                cur = {"old": None, "new": None, "hunks": [], "flags": set()}
                files.append(cur)
            else:
                continue
        if line.startswith("--- ") and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ ") and not cur["hunks"]:
            p = line[4:].split("\t")[0]
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)", line)
            if not m:
                raise RunAll("bad hunk header")
            # "-N,0" (a pure insertion without context) inserts after old line N
            cur["hunks"].append([int(m.group(1)) + (m.group(2) == "0"), []])
        elif cur["hunks"] and line[:1] in (" ", "+", "-"):
            cur["hunks"][-1][1].append(line)
        elif cur["hunks"] and line.startswith("\\"):
            continue  # "\ No newline at end of file"
        elif line.startswith(("new file mode", "deleted file mode", "rename ", "copy ",
                              "similarity", "old mode", "new mode", "Binary files", "GIT binary")):
            cur["flags"].add(line.split()[0])
    return files


def apply(old_text, hunks):
    old = old_text.splitlines(keepends=True)
    new, pos = [], 0
    for start, body in hunks:
        start = max(start, 1)
        if start - 1 < pos:
            raise RunAll("overlapping hunks")
        new += old[pos:start - 1]
        pos = start - 1
        for l in body:
            if l[0] in " -":
                if pos >= len(old) or old[pos].rstrip("\r\n") != l[1:]:
                    raise RunAll("patch does not apply")
                if l[0] == " ":
                    new.append(old[pos])
                pos += 1
            else:
                new.append(l[1:] + "\n")
    return "".join(new + old[pos:])


def changed_old_lines(hunks):
    """Old-file line numbers touched; an insertion touches the lines on either side."""
    res = set()
    for start, body in hunks:
        o = start
        for l in body:
            if l[0] == "-":
                res.add(o)
                o += 1
            elif l[0] == "+":
                res.update({o - 1, o})
            else:
                o += 1
    return {l for l in res if l > 0}


# --------------------------------------------------------------------------- python analysis

def code_key(code):
    """Identity of a code object ignoring line numbers; docstrings stay (they're constants)."""
    consts = tuple(code_key(c) if hasattr(c, "co_code") else (type(c).__name__, repr(c))
                   for c in code.co_consts)
    return (code.co_code, consts, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_flags, code.co_argcount, code.co_kwonlyargcount)


def same_program(old_text, new_text, old_code, new_code):
    """No behavioural change: identical AST (positions ignored) or identical compiled code.
    The AST check is needed because 3.13 stores each class's first line as a constant."""
    if old_code is None:
        return False
    if code_key(old_code) == code_key(new_code):
        return True
    try:
        return same_ast(parse_shared(old_text), ast.parse(new_text))
    except SyntaxError:
        return False


_POS = {"lineno", "col_offset", "end_lineno", "end_col_offset"}
_SHARED = {}


def parse_shared(text):
    """Parse of the pinned (unpatched) source, shared by every rule. Never mutate it: rules
    only change the tree of the patched text, which they parse themselves."""
    tree = _SHARED.get(text)
    if tree is None:
        tree = _SHARED[text] = ast.parse(text)
    return tree


def same_ast(a, b):
    """ast.dump(a) == ast.dump(b) (positions ignored), stopping at the first difference."""
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for f in a._fields:
            if f not in _POS and not same_ast(getattr(a, f, None), getattr(b, f, None)):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(same_ast(x, y) for x, y in zip(a, b))
    return a == b and repr(a) == repr(b)  # repr: 1 vs True, 0.0 vs -0.0


def compiles(text, name):
    try:
        return compile(text, name, "exec", dont_inherit=True)
    except (SyntaxError, ValueError):
        return None


class Stmt:
    __slots__ = ("lo", "hdr", "end", "in_func", "node", "cls", "doc")

    def __init__(self, lo, hdr, end, in_func, node, cls, doc):
        self.lo, self.hdr, self.end = lo, hdr, end
        self.in_func, self.node, self.cls, self.doc = in_func, node, cls, doc


def stmt_index(tree):
    out = []

    def is_doc(parent, child):
        body = getattr(parent, "body", None)
        return (isinstance(body, list) and body and body[0] is child and isinstance(child, ast.Expr)
                and isinstance(child.value, ast.Constant) and isinstance(child.value.value, str))

    def walk(node, in_func, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                body = getattr(child, "body", None)
                hdr = (body[0].lineno - 1) if isinstance(body, list) and body and isinstance(body[0], ast.stmt) \
                    else child.end_lineno
                lo = min([d.lineno for d in getattr(child, "decorator_list", [])] + [child.lineno])
                out.append(Stmt(lo, max(hdr, child.lineno), child.end_lineno, in_func, child, cls,
                                is_doc(node, child)))
                is_func = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                sub_cls = ((cls + ".") if cls else "") + child.name if isinstance(child, ast.ClassDef) else cls
                walk(child, in_func or is_func, sub_cls)
            else:
                walk(child, in_func, cls)

    walk(tree, False, None)
    return out


def innermost(index, line):
    best = None
    for s in index:
        if s.lo <= line <= s.end and (best is None or s.lo >= best.lo):
            best = s
    return best


FAMILY = {ast.Is: ast.Eq, ast.Eq: ast.Eq, ast.IsNot: ast.NotEq, ast.NotEq: ast.NotEq}


def eq_compares(tree):
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in FAMILY]


def only_safe_swaps(old_text, new_text, safe):
    """True when the new file differs from the old only by `is` <-> `==` / `is not` <-> `!=`
    at comparisons the profile saw agree both ways on every evaluation (so every value the
    program computes is unchanged)."""
    if not safe:
        return False
    try:
        old, new = parse_shared(old_text), ast.parse(new_text)
    except SyntaxError:
        return False
    a, b = eq_compares(old), eq_compares(new)
    if len(a) != len(b):
        return False
    swapped = False
    for x, y in zip(a, b):
        tx, ty = type(x.ops[0]), type(y.ops[0])
        if tx is ty:
            continue
        if FAMILY[tx] is not FAMILY[ty] or f"{x.lineno}:{x.col_offset}" not in safe:
            return False
        y.ops = [tx()]
        swapped = True
    # with the swaps undone the trees must be identical (positions ignored), which also
    # means the pairing above lined up the right comparisons
    return swapped and ast.dump(old) == ast.dump(new)


RUNTIME_ANNOTATIONS = ("dataclass", "NamedTuple", "TypedDict", "get_type_hints", "__annotations__",
                       "annotationlib", "signature(", "pydantic", "attrs")


def _strip_annotations(tree):
    """Drop what `from __future__ import annotations` never evaluates: parameter, return and
    variable annotations, and the bodies of `if TYPE_CHECKING:` blocks."""
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            n.returns = None
        elif isinstance(n, ast.arg):
            n.annotation = None
        elif isinstance(n, ast.AnnAssign):
            n.annotation = ast.Constant(None)
        elif isinstance(n, ast.If):
            test = n.test
            name = test.id if isinstance(test, ast.Name) else test.attr if isinstance(test, ast.Attribute) else None
            if name == "TYPE_CHECKING":
                n.body = [ast.Pass()]
    return tree


def annotations_only(old_text, new_text):
    """True when the files differ only in annotations / TYPE_CHECKING imports, in a module where
    annotations are never evaluated and nothing reads them at run time."""
    if any(w in old_text or w in new_text for w in RUNTIME_ANNOTATIONS):
        return False
    try:
        old, new = ast.parse(old_text), ast.parse(new_text)
    except SyntaxError:
        return False

    def future(tree):
        return any(isinstance(s, ast.ImportFrom) and s.module == "__future__"
                   and any(a.name == "annotations" for a in s.names) for s in tree.body)

    return future(old) and future(new) and same_ast(_strip_annotations(old), _strip_annotations(new))


def methods_reordered(old_text, new_text):
    """True when the files differ only in the order of methods inside class bodies: each class
    keeps the same undecorated methods (unique names, identical bodies, no default values,
    which are evaluated at definition) and its other statements in the same order."""
    try:
        old, new = parse_shared(old_text), ast.parse(new_text)
    except SyntaxError:
        return False
    a = [n for n in ast.walk(old) if isinstance(n, ast.ClassDef)]
    b = [n for n in ast.walk(new) if isinstance(n, ast.ClassDef)]
    if len(a) != len(b):
        return False
    moved = False
    for x, y in zip(a, b):
        def movable(s):
            return (isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and not s.decorator_list
                    and not s.args.defaults and not any(s.args.kw_defaults))
        fx = [s for s in x.body if movable(s)]
        fy = [s for s in y.body if movable(s)]
        names = [s.name for s in fx]
        if len(set(names)) != len(names) or sorted(names) != sorted(s.name for s in fy):
            return False
        # every name bound in the class body must be bound once, or order could matter
        bound = [t.id for s in x.body if isinstance(s, (ast.Assign, ast.AnnAssign))
                 for t in (s.targets if isinstance(s, ast.Assign) else [s.target]) if isinstance(t, ast.Name)]
        bound += [s.name for s in x.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
        if len(set(bound)) != len(bound):
            return False
        if [s.name for s in fx] != [s.name for s in fy]:
            moved = True
            # put the new tree's methods back in the old order; same_ast below then also
            # checks that every method body is unchanged
            by_name = {s.name: s for s in fy}
            it = iter(by_name[s.name] for s in fx)
            y.body = [next(it) if movable(s) else s for s in y.body]
    return moved and same_ast(old, new)


# --------------------------------------------------------------------------- value profile edits
# Each returns a description of the one edit when old and new differ by exactly that edit, else
# None. Sites are "line:col" of the node in the old (pinned) file, as tools/cov/valprof.py
# records them.

REL_OPS = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3, ast.Eq: 4, ast.NotEq: 5}


def _only_difference(old, new, nodes_old, nodes_new, differs, undo):
    """Pair nodes in walk order; exactly one pair may differ (per `differs`); after `undo` the
    trees must be identical. Returns the old node of that pair or None."""
    if len(nodes_old) != len(nodes_new):
        return None
    hit = None
    for x, y in zip(nodes_old, nodes_new):
        if same_ast(x, y):
            continue
        if hit is not None or not differs(x, y):
            return None
        hit = (x, y)
    if hit is None:
        return None
    undo(*hit)
    return hit[0] if same_ast(old, new) else None


def relational_op_change(old, new):
    """(site, old op index, new op index) for one changed relational operator."""
    def cmps(t):
        return [n for n in ast.walk(t) if isinstance(n, ast.Compare)]

    def differs(x, y):
        return (len(x.ops) == len(y.ops) == 1 and type(x.ops[0]) in REL_OPS
                and type(y.ops[0]) in REL_OPS and type(x.ops[0]) is not type(y.ops[0])
                and same_ast(x.left, y.left) and same_ast(x.comparators, y.comparators))

    new_op = []

    def undo(x, y):
        new_op.append(REL_OPS[type(y.ops[0])])  # read before the undo overwrites it
        y.ops = list(x.ops)

    a, b = cmps(old), cmps(new)
    x = _only_difference(old, new, a, b, differs, undo)
    if x is None:
        return None
    return f"{x.lineno}:{x.col_offset}", REL_OPS[type(x.ops[0])], new_op[0]


def _condition_boolops(tree):
    """Two-operand and/or nodes in condition position (if / while / ternary / assert / not),
    the ones the profiler instruments."""
    out = []
    for n in ast.walk(tree):
        test = (n.test if isinstance(n, (ast.If, ast.While, ast.IfExp, ast.Assert))
                else n.operand if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) else None)
        if isinstance(test, ast.BoolOp) and len(test.values) == 2:
            out.append(test)
    return out


def boolop_swap(old, new):
    """site of one `and` <-> `or` swap in a condition."""
    bo = [n for n in ast.walk(old) if isinstance(n, ast.BoolOp)]
    bn = [n for n in ast.walk(new) if isinstance(n, ast.BoolOp)]
    cond = {id(n) for n in _condition_boolops(old)}

    def differs(x, y):
        return (id(x) in cond and len(x.values) == len(y.values) == 2
                and type(x.op) is not type(y.op) and same_ast(x.values, y.values))

    def undo(x, y):
        y.op = x.op

    x = _only_difference(old, new, bo, bn, differs, undo)
    return None if x is None else f"{x.lineno}:{x.col_offset}"


def _assigned_dicts(tree):
    """Dict literals assigned (Assign / AnnAssign value) outside any function: the ones the
    profiler turns into tracking dicts."""
    out = []

    def walk(node, in_func):
        for c in ast.iter_child_nodes(node):
            f = in_func or isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
            if not f and isinstance(c, (ast.Assign, ast.AnnAssign)) and isinstance(c.value, ast.Dict):
                out.append(c.value)
            walk(c, f)

    walk(tree, False)
    return out


def dict_entry_change(old, new):
    """(site, changed-value keys, added keys, removed keys, added key reprs) when one tracked
    dict literal changed only in its entries; keys are their source text."""
    do, dn = _assigned_dicts(old), _assigned_dicts(new)
    if len(do) != len(dn):
        return None
    hit = None
    for x, y in zip(do, dn):
        if not same_ast(x, y):
            if hit is not None:
                return None
            hit = (x, y)
    if hit is None:
        return None
    x, y = hit

    def entries(d):
        out = []
        for k, v in zip(d.keys, d.values):
            out.append(("**" + ast.unparse(v), None) if k is None else (ast.unparse(k), v))
        return out

    eo, en = entries(x), entries(y)
    ko, kn = [k for k, _ in eo], [k for k, _ in en]
    if len(set(ko)) != len(ko) or len(set(kn)) != len(kn):
        return None  # duplicate keys: last one wins, order games
    common_o = [k for k in ko if k in set(kn)]
    common_n = [k for k in kn if k in set(ko)]
    if common_o != common_n:
        return None  # reordered entries
    vo, vn = dict(eo), dict(en)
    if any(k.startswith("**") for k in set(ko) ^ set(kn)):
        return None  # a ** splat added or removed
    if any(k.startswith("**") and not same_ast(vo[k], vn[k]) for k in common_o):
        return None
    changed = {k for k in common_o if not k.startswith("**") and not same_ast(vo[k], vn[k])}
    added, removed = set(kn) - set(ko), set(ko) - set(kn)
    reprs = set()
    for k in added:
        node = next(kk for kk in y.keys if kk is not None and ast.unparse(kk) == k)
        try:
            value = ast.literal_eval(node)
        except (ValueError, SyntaxError, TypeError):
            return None  # a computed key: we cannot match it against recorded misses
        reprs.add(repr(value))
    y.keys, y.values = list(x.keys), list(x.values)  # undo; the rest must be identical
    if not same_ast(old, new):
        return None
    return f"{x.lineno}:{x.col_offset}", changed, added, removed, reprs


_IN_FUNC = {}


def in_function_sites(tree):
    """"line:col" of every Compare / BoolOp inside a function or lambda body (comprehensions at
    module or class level run at import and do not count)."""
    key = id(tree)
    if key not in _IN_FUNC:
        out = set()

        def walk(node, in_func):
            for c in ast.iter_child_nodes(node):
                f = in_func or isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                if f and isinstance(c, (ast.Compare, ast.BoolOp)):
                    out.add(f"{c.lineno}:{c.col_offset}")
                walk(c, f)

        walk(tree, False)
        _IN_FUNC[key] = (tree, out)  # keep the tree alive so its id is not reused
    return _IN_FUNC[key][1]


class ValueProfile:
    """maps/valprof/ (tools/build_valprof.py): an index and one shard per source file, each
    loaded on first use."""

    def __init__(self, path):
        self.path, self.index, self.shards = Path(path), None, {}

    def load(self):
        if self.index is None:
            try:
                self.index = json.loads((self.path / "index.json").read_text())
            except (OSError, ValueError):
                self.index = {}
        return self.index

    def site(self, kind, f, key):
        if f not in self.shards:
            shard = {}
            if f in set(self.load().get("files", ())):
                try:
                    with gzip.open(self.path / (f.replace("/", "__") + ".json.gz"), "rt") as fh:
                        shard = json.load(fh)
                except (OSError, ValueError):
                    shard = None  # a listed shard that cannot be read: decide nothing
            self.shards[f] = shard
        shard = self.shards[f]
        if shard is None:
            raise LookupError(f)
        return shard.get(kind, {}).get(key)

    def select_cmp(self, f, key, a, b):
        """Tests where operator a and b ever disagreed, or None (unknown: use other rules)."""
        site = self.site("cmps", f, key)
        if not self.load():
            return None
        if site is None:
            return set()  # never evaluated, at import or in any test

        def flips(masks):
            return any(m < 0 or ((m >> a) & 1) != ((m >> b) & 1) for m in masks)

        if site["imp"] is not None and flips(site["imp"]):
            return None
        return {n for ns, masks in site["t"] if flips(masks) for n in ns}

    def select_bool(self, f, key):
        site = self.site("bools", f, key)
        if not self.load() or [f, key] in self.load().get("unbalanced", []):
            return None
        if site is None:
            return set()

        def flips(pairs):
            return any(not (isinstance(p, list) and len(p) == 2 and isinstance(p[0], bool)
                            and isinstance(p[1], bool) and p[0] == p[1]) for p in pairs)

        if site["imp"] is not None and flips(site["imp"]):
            return None
        return {n for ns, pairs in site["t"] if flips(pairs) for n in ns}

    def select_dict(self, f, key, changed, added, removed, reprs):
        site = self.site("dicts", f, key)
        if not self.load() or site is None:
            return None  # a tracked dict is built at import: absent means not profiled
        touched = added | removed
        # copied, iterated or mutated anywhere: class creation (which sqlglot may run inside
        # the first test that imports a dialect) and caches build on such copies, so the
        # tests that later use the copy are not in the records
        recs = ([site["imp"]] if site["imp"] is not None else []) + [rec for _, rec in site["t"]]
        if any(rec[3] & (2 | 4 | 8) for rec in recs):
            return None

        def hit(rec):
            get, inn, miss, flags = set(rec[0]), set(rec[1]), set(rec[2]), rec[3]
            if flags & (4 | 8):  # values exposed or mutated
                return True
            if touched and flags & (1 | 2):  # size or key set observed
                return True
            if changed & get or touched & (get | inn) or reprs & miss:
                return True
            return "<unhashable>" in get or "<other>" in miss and bool(added)

        if site["imp"] is not None and hit(site["imp"]):
            return None  # read at import (copied, iterated, looked up): effects are global
        return {n for ns, rec in site["t"] if hit(rec) for n in ns}


def lambda_body_changes(old_text, new_text):
    """[(first, last) old line span of each changed lambda body] when the two files differ only
    inside lambda bodies (same lambdas in the same places, same parameters and defaults),
    else None. Such a change can only matter when a changed lambda is called."""
    try:
        old, new = parse_shared(old_text), ast.parse(new_text)
    except SyntaxError:
        return None
    a = [n for n in ast.walk(old) if isinstance(n, ast.Lambda)]
    b = [n for n in ast.walk(new) if isinstance(n, ast.Lambda)]
    if not a or len(a) != len(b):
        return None
    spans = []
    for x, y in zip(a, b):
        if same_ast(x.body, y.body):
            continue
        if not same_ast(x.args, y.args):
            return None
        spans.append((x.body.lineno, x.body.end_lineno))
        y.body = x.body  # undo the change; the rest of the file must then be identical
    return spans if spans and same_ast(old, new) else None


def s_has_doc(node):
    b = node.body[0] if node.body else None
    return isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant) and isinstance(b.value.value, str)


def bound_names(node):
    names = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
            names.add(n.attr)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in n.names)
    return names


# --------------------------------------------------------------------------- selector

class Selector:
    def __init__(self, tests):
        self.tests = tests
        self.num_by_id = {t["id"]: t["number"] for t in tests}
        self.ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
        doc, self.sets = load_maps()
        # map-test index -> number in this run's tests.json
        self.num_of = [self.num_by_id.get(i) for i in doc["tests"]]
        self.lines = doc["lines"]
        self.fixtures = doc["fixtures"]
        self.uses = doc["uses"]
        self.cmp = load_cmp()
        self.vp = ValueProfile(VALPROF)
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self.source = Source()
        self._runtime = {}
        self._trees = {}

    # -- helpers
    def nums(self, set_id):
        return {n for n in (self.num_of[i] for i in self.sets[set_id]) if n is not None}

    def covering(self, f, lo, hi):
        m = self.lines.get(f, {})
        res = set()
        for l in range(lo, hi + 1):
            sid = m.get(str(l))
            if sid is not None:
                res |= self.nums(sid)
        return res

    def tree(self, f, text):
        if f not in self._trees:
            self._trees[f] = stmt_index(parse_shared(text))
        return self._trees[f]

    def readers(self, names, skip):
        res = set()
        for n in names:
            for f, l in self.source.occurrences(n):
                if (f, l) not in skip:
                    sid = self.lines.get(f, {}).get(str(l))
                    if sid is not None:
                        res |= self.nums(sid)
        return res

    def runtime_tests(self, f):
        """Tests that run any statement inside a function of f."""
        if f not in self._runtime:
            res = set()
            p = SRC / f
            if p.exists():
                for s in self.tree(f, p.read_text(encoding="utf8")):
                    if s.in_func:
                        res |= self.covering(f, s.lo, s.lo)
            self._runtime[f] = res
        return self._runtime[f]

    def class_users(self, f, cls):
        qual = f[:-3].replace("/", ".") + "." + cls
        if qual.endswith(".__init__." + cls):
            qual = qual.replace(".__init__.", ".")
        sid = self.uses.get(qual)
        if sid is not None:
            return self.nums(sid)
        return None  # never instantiated (mixin, holder): caller stays unscoped

    def all_tests(self):
        return {t["number"] for t in self.tests}

    # -- per-file rules
    def sqlglot_py(self, f, hunks):
        old_text = (SRC / f).read_text(encoding="utf8")
        new_text = apply(old_text, hunks)
        old_code, new_code = compiles(old_text, f), compiles(new_text, f)
        if new_code is None:
            raise RunAll(f"{f} no longer compiles")
        if same_program(old_text, new_text, old_code, new_code):
            return set()
        sel = set()
        if f.startswith("sqlglot/typing/"):
            sel |= set(self.by_file.get("tests/test_typing.py", []))  # compares source/code objects
        removed = [l[1:] for _, body in hunks for l in body if l[0] == "-"]
        added = [l[1:] for _, body in hunks for l in body if l[0] == "+"]
        changed = removed + added
        # cheap necessary conditions first: every changed code line of an annotation-only edit
        # carries a `:`, `->` or an import (multi-line annotations just skip the rule)
        if ANNOTATION_RULE and all(re.search(r":|->|\bimport\b|TYPE_CHECKING", l) or not l.strip()
                                   or l.lstrip().startswith("#") for l in changed) \
                and annotations_only(old_text, new_text):
            return sel  # annotations are never evaluated here
        if REORDER_RULE and any(l.lstrip().startswith(("def ", "async def ")) for l in removed)                 and any(l.lstrip().startswith(("def ", "async def ")) for l in added)                 and methods_reordered(old_text, new_text):
            return sel  # same methods, same bodies, other order
        # a swap needs `is`/`==`/`!=` on a changed line; skip the two-tree walk otherwise
        swap_tokens = any(re.search(r"\bis\b|==|!=", l[1:])
                          for _, body in hunks for l in body if l[0] in "+-")
        if swap_tokens and only_safe_swaps(old_text, new_text, self.cmp.get(f)):
            return sel
        old_lines = old_text.splitlines()
        if LAMBDA_RULE and any("lambda" in l for start, body in hunks
                               for l in old_lines[max(0, start - 30):start + len(body) + 1]):
            # only lambda bodies changed: the tests whose run executed those bodies
            spans = lambda_body_changes(old_text, new_text)
            if spans is not None:
                index = self.tree(f, old_text)
                hits = []
                for lo, hi in spans:
                    cov = self.covering(f, lo, hi)
                    s = innermost(index, lo)
                    if not cov and not (s and s.in_func):
                        # a top-level lambda no test ran may still run at import (collection
                        # time, not credited to any test): leave it to the general rules
                        break
                    hits.append(cov)
                else:
                    for cov in hits:
                        sel |= cov
                    return sel
        if VALPROF_RULE:
            vp = self.valprof_select(f, old_text, new_text, changed)
            if vp is not None:
                return sel | vp
        index = self.tree(f, old_text)
        for line in changed_old_lines(hunks):
            s = innermost(index, line)
            if s is None:
                continue
            if s.doc:
                sel |= set(self.by_file.get("tests/test_docs.py", []))  # doctests run docstrings
            if s.in_func:
                hi = s.hdr if line <= s.hdr else s.end
                sel |= self.covering(f, s.lo, hi)
                continue
            node = s.node
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if line > s.hdr:
                    continue  # body lines are handled by their own statements
                # signature/decorators/bases run at import; its callers are its readers
                names = {node.name}
                if not isinstance(node, ast.ClassDef):
                    # whoever runs it (the docstring is not an executed line: skip it)
                    body = node.body[1:] if s_has_doc(node) and len(node.body) > 1 else node.body
                    sel |= self.covering(f, body[0].lineno, body[0].lineno)
            elif s.doc:
                continue
            else:
                names = bound_names(node)
            skip = {(f, i) for i in range(s.lo, s.end + 1)}
            r = self.readers(names, skip)
            if s.cls:
                users = self.class_users(f, s.cls)
                if users is not None:
                    r &= users
            # the defining statement's own import-time coverage says nothing; readers do
            sel |= r
            if not names:
                sel |= self.covering(f, s.lo, s.end)
        return sel

    def valprof_select(self, f, old_text, new_text, changed=None):
        """Exact selections for single-operator, and/or and dict-entry edits from the value
        profile, or None when the edit is not one of those or the profile cannot decide."""
        res = None
        # gates on the changed text: a skipped rule only falls back to the general rules
        text = "\n".join(changed) if changed is not None else None
        try:
            old = parse_shared(old_text)
            r = None
            if text is None or re.search(r"[<>]|==|!=", text):
                r = relational_op_change(old, ast.parse(new_text))
            if r is not None:
                # code outside functions runs once, at import, and sqlglot imports dialects
                # lazily: inside whichever test first needs them, so the profile credits that
                # one test. Only sites in function bodies have per-test records we can trust.
                res = self.vp.select_cmp(f, *r) if r[0] in in_function_sites(old) else None
            else:
                if text is None or re.search(r"\b(and|or)\b", text):
                    r = boolop_swap(old, ast.parse(new_text))
                if r is not None:
                    res = self.vp.select_bool(f, r) if r in in_function_sites(old) else None
                else:
                    r = dict_entry_change(old, ast.parse(new_text))
                    if r is not None:
                        res = self.vp.select_dict(f, *r)
        except (SyntaxError, RecursionError, ValueError, LookupError):
            return None
        if res is None:
            return None
        nums = {n for n in (self.vp_num(i) for i in res) if n is not None}
        # tests that failed under instrumentation have unreliable records: always keep them
        return nums | {n for n in (self.vp_num(i) for i in self.vp.load().get("failed", []))
                       if n is not None}

    def vp_num(self, n):
        return n if n in self.ms else None

    def test_py(self, f, hunks):
        old_text = (SRC / f).read_text(encoding="utf8")
        new_text = apply(old_text, hunks)
        old_code, new_code = compiles(old_text, f), compiles(new_text, f)
        module_tests = set(self.by_file.get(f, []))
        if new_code is None:
            if module_tests:
                return module_tests  # import error fails the module only
            raise RunAll(f"{f} no longer compiles")
        if same_program(old_text, new_text, old_code, new_code):
            return set()
        if not module_tests:
            # helpers / conftest / __init__ / scripts: whoever imports it
            stem = Path(f).stem
            if stem in ("conftest", "__init__") or Path(f).name == "helpers.py":
                raise RunAll(f"shared test module {f}")
            importers = [tf for tf in self.by_file
                         if re.search(rf"\b{re.escape(stem)}\b", (SRC / tf).read_text(encoding="utf8"))]
            if importers:
                raise RunAll(f"imported test helper {f}")
            return set()
        index = self.tree(f, old_text)
        sel = set()
        ids = {t["id"]: t["number"] for t in self.tests if t["file"] == f}
        for line in changed_old_lines(hunks):
            s = innermost(index, line)
            if s is None:
                continue
            # climb to the outermost function and its class
            chain = [x for x in index if x.lo <= line <= x.end]
            funcs = [x for x in chain if isinstance(x.node, (ast.FunctionDef, ast.AsyncFunctionDef))]
            top = min(funcs, key=lambda x: x.lo) if funcs else None
            if top is not None and top.node.name.startswith("test") and (top.cls or "").count(".") == 0:
                prefix = f"{f}::{top.cls}::{top.node.name}" if top.cls else f"{f}::{top.node.name}"
                hit = {n for i, n in ids.items() if i == prefix or i.startswith(prefix + "[")}
                if hit:
                    sel |= hit
                    continue
            if top is not None and top.cls:
                prefix = f"{f}::{top.cls.split('.')[0]}::"
                sel |= {n for i, n in ids.items() if i.startswith(prefix)}
                continue
            return module_tests
        return sel

    def fixture(self, f, is_new):
        sid = self.fixtures.get(f)
        if sid is not None:
            return self.nums(sid)
        if is_new:
            folder = f.rsplit("/", 1)[0] + "/"
            sibling = [s for k, s in self.fixtures.items() if k.startswith(folder)]
            if sibling:
                res = set()
                for s in sibling:
                    res |= self.nums(s)
                return res
        return set()  # nobody opens it

    def select_file(self, fd):
        old, new, hunks, flags = fd["old"], fd["new"], fd["hunks"], fd["flags"]
        path = new or old
        if flags & {"Binary", "GIT", "rename", "copy"} or (old and new and old != new):
            raise RunAll(f"rename/binary {path}")
        is_new, is_del = old is None, new is None
        if path.endswith((".md", ".rst", ".txt")) and not path.startswith(("sqlglot/", "tests/")):
            return set()
        if path.startswith(("posts/", "benchmarks/", "pdoc/", "docs/", ".github/")) or path in (
                "CHANGELOG.md", "README.md", "CONTRIBUTING.md", "LICENSE", "AGENTS.md", "CLAUDE.md",
                ".gitignore", ".pre-commit-config.yaml", "sqlglot.png"):
            return set()
        if path.startswith("sqlglotc/"):
            return set(self.by_file.get("tests/test_sqlglotc.py", []))
        if path.startswith("tests/") and not path.endswith(".py"):
            return self.fixture(path, is_new)
        if is_new and ADDED_RULE and path.endswith(".py") and path.startswith(("sqlglot/", "tests/")):
            return self.added_py(path)
        if is_new or is_del:
            raise RunAll(f"added/deleted {path}")
        if path.startswith("sqlglot/") and path.endswith(".py"):
            return self.sqlglot_py(path, hunks)
        if path.startswith("tests/") and path.endswith(".py"):
            return self.test_py(path, hunks)
        raise RunAll(f"unmapped {path}")

    def added_py(self, path):
        """A new module runs only if something imports it. Nothing in the pinned tree can
        (its name must not appear anywhere there), so only this patch's other files can: each
        of them that mentions it gets every test that runs any of its functions (an import
        that fails or misbehaves breaks the whole importer)."""
        p = Path(path)
        if p.stem in ("__init__", "conftest") or p.name == "helpers.py" or p.stem.startswith("_"):
            raise RunAll(f"added {path}")
        pat = re.compile(r"(?<![A-Za-z_])" + re.escape(p.stem) + r"(?!\w)")
        for root in ("sqlglot", "tests"):
            for q in (SRC / root).rglob("*.py"):
                if pat.search(q.read_text(encoding="utf8")):
                    raise RunAll(f"added {path}: its name is already used")
        self._added.append(pat)
        return set()

    def select(self, patch_text):
        files = parse_patch(patch_text)
        if not files:
            return set()
        sel = set()
        self._added = []
        for fd in files:
            if not (fd["old"] or fd["new"]):
                raise RunAll("unnamed file")
            sel |= self.select_file(fd)
        for pat in self._added:
            for fd in files:
                f = fd["old"]
                if f and fd["new"] and any(l[0] == "+" and pat.search(l) for _, b in fd["hunks"] for l in b):
                    if f.startswith("sqlglot/"):
                        sel |= self.runtime_tests(f)
                    elif f.startswith("tests/"):
                        sel |= set(self.by_file.get(f, ()))
                    else:
                        raise RunAll(f"{f} uses an added module")
        return sel

    # -- packing
    def pack(self, nums):
        if not nums:
            return []
        order = sorted(nums, key=lambda n: -self.ms[n])
        best = None
        for w in range(1, MAX_WORKERS + 1):
            loads, bins = [0] * w, [[] for _ in range(w)]
            for n in order:
                i = loads.index(min(loads))
                loads[i] += self.ms[n]
                bins[i].append(n)
            cost = max(loads) + WORKER_MS * w
            if best is None or cost < best[0]:
                best = (cost, [b for b in bins if b])
            # longest-first is an approximation: search for a strictly smaller busiest worker
            if w > 1 and len(order) > w:
                try:
                    exact = self.exact_bins(order, w, max(loads))
                except Exception as e:  # never worse than longest-first
                    log(f"exact packing failed: {type(e).__name__}: {e}")
                    exact = None
                if exact is not None:
                    cost = max(sum(self.ms[n] for n in b) for b in exact) + WORKER_MS * w
                    if cost < best[0]:
                        best = (cost, exact)
        return best[1]

    def exact_bins(self, order, w, limit, budget=20000):
        """Branch and bound: w bins whose busiest is strictly below `limit`, the smallest found
        within `budget` search nodes, or None. `order` is longest first. Past 60 tests only the
        24 longest are searched and the rest are placed longest-first (the caller keeps the
        result only when it is strictly better)."""
        total = sum(self.ms[n] for n in order)
        lower = max(self.ms[order[0]], -(-total // w))
        if lower >= limit:
            return None  # longest-first is already optimal
        head, tail = (order, []) if len(order) <= 60 else (order[:24], order[24:])
        if tail:
            limit = 10 ** 12  # the head alone has no useful bound; the caller compares
        ms = [self.ms[n] for n in head]
        loads, assign = [0] * w, [0] * len(ms)
        state = {"best": limit, "bins": None, "nodes": 0}

        def go(i):
            if state["nodes"] > budget or state["best"] <= lower:
                return
            state["nodes"] += 1
            if i == len(ms):
                state["best"] = max(loads)
                state["bins"] = list(assign)
                return
            seen = set()
            for b in sorted(range(w), key=lambda b: loads[b]):
                if loads[b] in seen:  # bins with equal loads are interchangeable
                    continue
                seen.add(loads[b])
                if loads[b] + ms[i] >= state["best"]:
                    continue
                loads[b] += ms[i]
                assign[i] = b
                go(i + 1)
                loads[b] -= ms[i]

        go(0)
        if state["bins"] is None:
            return None
        bins, loads = [[] for _ in range(w)], [0] * w
        for n, b in zip(head, state["bins"]):
            bins[b].append(n)
            loads[b] += self.ms[n]
        for n in tail:
            i = loads.index(min(loads))
            loads[i] += self.ms[n]
            bins[i].append(n)
        return [b for b in bins if b]


    def cost(self, bins):
        return max(sum(self.ms[n] for n in b) for b in bins) + WORKER_MS * len(bins) if bins else 0


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    sel = Selector(tests)
    every = sel.all_tests()
    heavy = None
    if HEAVY_RULE:
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from heavy import Heavy
            heavy = Heavy(sel, SRC, apply)
        except Exception as e:
            log(f"heavy: unavailable ({type(e).__name__}: {e})")
    verifier = None
    if os.environ.get("VERIFY", "1") != "0":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from verify import Verifier
        verifier = Verifier(SRC, sel.ms, {t["number"]: t["file"] for t in tests},
                            {t["number"]: t["id"] for t in tests}, apply)
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        text = patch.read_text(encoding="utf8", errors="replace")
        verifiable = False
        try:
            nums = sel.select(text)
            why = f"{len(nums)} tests"
            verifiable = True
        except RunAll as e:
            nums, why = every, f"ALL ({e})"
        except Exception as e:  # never crash: an unexplained change runs everything
            nums, why = every, f"ALL (error {type(e).__name__}: {e})"
        bins = sel.pack(nums)
        if heavy is not None and verifiable and nums:
            try:
                files = parse_patch(text)
                kept, note = heavy.refine(files, nums, sel.pack, sel.cost)
                if kept != nums:
                    nums, bins = kept, sel.pack(kept)
                if note:
                    why += f", {note}"
            except Exception as e:
                why += f", heavy error {type(e).__name__}: {e}"
        if verifier is not None and verifiable and nums:
            try:
                kept, note = verifier.refine(parse_patch(text), nums, sel.cost(bins))
                if kept is not nums:
                    nums, bins = kept, sel.pack(kept)
                why += f", {note}"
            except Exception as e:
                why += f", verify error {type(e).__name__}: {e}"
        print(json.dumps({"commit": cid, "workers": bins}), flush=True)
        log(f"{cid}: {why}")


if __name__ == "__main__":
    main()
