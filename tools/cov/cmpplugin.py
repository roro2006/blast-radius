"""pytest plugin used only when building the comparison profile (never at selection time).

Every single-operator `is` / `is not` / `==` / `!=` comparison in sqlglot is rewritten
into a probe that returns exactly what the original returns and also evaluates the
swapped operator (`is` <-> `==`, `is not` <-> `!=`) on the same operands. A site is
"swap-safe" when, over the whole suite on the pinned commit, every evaluation gave the
same bool both ways and neither side raised. Swapping the operator at such a site leaves
every value the program computes unchanged.

Output: $OUT/cmp-<worker>.json = {"seen": {file: ["line:col", ...]}, "bad": {...}}.
"""
import ast
import builtins
import concurrent.futures as cf
import importlib.machinery
import json
import os
import pickle
import sys

ROOT = os.environ.get("SQLGLOT_ROOT", "/sqlglot")
SITES = []        # sid -> (file, key)
SEEN = set()
BAD = set()
IS, ISNOT, EQ, NE = range(4)
OPS = {ast.Is: IS, ast.IsNot: ISNOT, ast.Eq: EQ, ast.NotEq: NE}


def _probe(sid, op, a, b):
    if op == IS or op == ISNOT:
        r = (a is b) if op == IS else (a is not b)
        if sid not in BAD:
            try:
                e = (a == b) if op == IS else (a != b)
                if not (e is True or e is False) or e != r:
                    BAD.add(sid)
            except BaseException:
                BAD.add(sid)
        SEEN.add(sid)
        return r
    try:
        r = (a == b) if op == EQ else (a != b)
    except BaseException:
        BAD.add(sid)
        SEEN.add(sid)
        raise
    if sid not in BAD:
        alt = (a is b) if op == EQ else (a is not b)
        if not (r is True or r is False) or r != alt:
            BAD.add(sid)
    SEEN.add(sid)
    return r


builtins.__brcmp__ = _probe


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        sid = len(SITES)
        SITES.append((self.rel, f"{node.lineno}:{node.col_offset}"))
        call = ast.Call(ast.Name("__brcmp__", ast.Load()),
                        [ast.Constant(sid), ast.Constant(OPS[type(node.ops[0])]),
                         node.left, node.comparators[0]], [])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = self.get_filename(fullname)
        src = self.get_data(path)
        tree = ast.parse(src, path)
        rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
        tree = ast.fix_missing_locations(_Rewrite(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)


class _Finder:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin and spec.origin.endswith(".py"):
            spec.loader = _Loader(name, spec.origin)
        return spec


assert "sqlglot" not in sys.modules, "cmpplugin must load before sqlglot"
sys.meta_path.insert(0, _Finder)


class SyncExecutor(cf.Executor):
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        f = cf.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:
            f.set_exception(e)
        return f


cf.ProcessPoolExecutor = SyncExecutor


def pytest_sessionfinish(session):
    out = {"seen": {}, "bad": {}}
    # two compares can share a start (e.g. `(a is b) == c`): such keys are never safe
    count = {}
    for site in SITES:
        count[site] = count.get(site, 0) + 1
    for sid, (f, key) in enumerate(SITES):
        if sid in SEEN or count[(f, key)] > 1:
            bad = sid in BAD or count[(f, key)] > 1
            out["bad" if bad else "seen"].setdefault(f, []).append(key)
    w = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(os.environ["OUT"], f"cmp-{w}.json"), "w") as fh:
        json.dump(out, fh)
