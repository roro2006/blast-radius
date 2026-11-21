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
WORKER_MS = 2000
MAX_WORKERS = 8


class RunAll(Exception):
    """This change is outside what the maps can explain."""


def log(*a):
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- maps

def load_maps():
    with gzip.open(MAPS, "rt") as f:
        doc = json.load(f)
    sets = []
    for packed in doc["sets"]:
        s, acc = [], 0
        for i, d in enumerate(packed):
            acc = d if i == 0 else acc + d
            s.append(acc)
        sets.append(s)
    return doc, sets


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
            cur["hunks"].append([int(m.group(1)), []])
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
    try:
        if ast.dump(ast.parse(old_text)) == ast.dump(ast.parse(new_text)):
            return True
    except SyntaxError:
        return False
    return code_key(old_code) == code_key(new_code)


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
        self.by_file = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])
        self._words = None
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
            self._trees[f] = stmt_index(ast.parse(text))
        return self._trees[f]

    def words(self):
        if self._words is None:
            self._words = {}
            for p in (SRC / "sqlglot").rglob("*.py"):
                rel = p.relative_to(SRC).as_posix()
                for i, line in enumerate(p.read_text(encoding="utf8").splitlines(), 1):
                    code = line.split("#", 1)[0]
                    for w in set(re.findall(r"[A-Za-z_]\w*", code)):
                        self._words.setdefault(w, []).append((rel, i))
        return self._words

    def readers(self, names, skip):
        res = set()
        for n in names:
            for f, l in self.words().get(n, ()):
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
        if is_new or is_del:
            raise RunAll(f"added/deleted {path}")
        if path.startswith("sqlglot/") and path.endswith(".py"):
            return self.sqlglot_py(path, hunks)
        if path.startswith("tests/") and path.endswith(".py"):
            return self.test_py(path, hunks)
        raise RunAll(f"unmapped {path}")

    def select(self, patch_text):
        files = parse_patch(patch_text)
        if not files:
            return set()
        sel = set()
        for fd in files:
            if not (fd["old"] or fd["new"]):
                raise RunAll("unnamed file")
            sel |= self.select_file(fd)
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
        return best[1]


def main():
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    sel = Selector(tests)
    every = sel.all_tests()
    for patch in sorted((DATA / "commits").glob("*.patch")):
        cid = patch.stem
        try:
            nums = sel.select(patch.read_text(encoding="utf8", errors="replace"))
            why = f"{len(nums)} tests"
        except RunAll as e:
            nums, why = every, f"ALL ({e})"
        except Exception as e:  # never crash: an unexplained change runs everything
            nums, why = every, f"ALL (error {type(e).__name__}: {e})"
        print(json.dumps({"commit": cid, "workers": sel.pack(nums)}), flush=True)
        log(f"{cid}: {why}")


if __name__ == "__main__":
    main()
