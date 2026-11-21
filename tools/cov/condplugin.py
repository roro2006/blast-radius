"""pytest plugin used only when building the condition profile (never at selection time).

Per test (same context names as ctxplugin: "<nodeid>|setup" for the setup phase, "<nodeid>"
for call/teardown), records for sqlglot's conditions:

  * BoolOp `a and b and ...` / `a or b or ...`: the terminal state of each evaluation,
    (k, t) = index of the last operand evaluated and its truth. With and/or short-circuit
    this fixes every operand's truth (earlier ones are all True for `and`, all False for
    `or`; later ones were not evaluated). Also whether every evaluated operand was a bool.
  * single-operator comparisons:
      ordering (<, <=, >, >=): whether the operands were ever equal ("e") / unequal ("n");
      identity/equality (is, is not, ==, !=): whether `is` and `==` ever disagreed ("d").

Each operand or comparison evaluates exactly as before; the probes only look at values.
Output: $OUT/cond-<worker>.json = {"<ctx>": {"<site>": [states...]}}, where site is
"B:line:col" or "C:line:col" in file-relative form "file|B:line:col".
"""
import ast
import builtins
import concurrent.futures as cf
import importlib.machinery
import json
import os
import pickle
import sys

import pytest

ROOT = os.environ.get("SQLGLOT_ROOT", "/sqlglot")
SITES = []      # sid -> "file|kind:line:col"
BOOLS = []      # sid -> (is_and, n) for BoolOp sites, None for compares
CUR = [""]
REC = {}        # ctx -> {sid: set(states)}
LT, LE, GT, GE, IS, ISNOT, EQ, NE = range(8)
CMP_OPS = {ast.Lt: LT, ast.LtE: LE, ast.Gt: GT, ast.GtE: GE,
           ast.Is: IS, ast.IsNot: ISNOT, ast.Eq: EQ, ast.NotEq: NE}


IMPORTING = [0]   # >0 while any sqlglot module body is executing


def _put(sid, state):
    # evaluations during an import feed every later user of the module, not just the test
    # that happened to import it: such sites are marked unusable
    ctx = "<import>" if IMPORTING[0] else CUR[0]
    d = REC.get(ctx)
    if d is None:
        d = REC[ctx] = {}
    s = d.get(sid)
    if s is None:
        d[sid] = {state}
    else:
        s.add(state)


def _has(sid, state):
    d = REC.get("<import>" if IMPORTING[0] else CUR[0])
    return d is not None and state in d.get(sid, ())


OPEN = {}   # (ctx, sid) -> evaluations started minus finished


def _bop(sid, i, x):
    is_and, n = BOOLS[sid]
    t = bool(x)
    end = i == n - 1 or t != is_and  # this operand ends the evaluation
    if i == 0 and not end:
        k = ("<import>" if IMPORTING[0] else CUR[0], sid)
        OPEN[k] = OPEN.get(k, 0) + 1
    if end:
        _put(sid, (i, t))
        if i:
            k = ("<import>" if IMPORTING[0] else CUR[0], sid)
            OPEN[k] = OPEN.get(k, 0) - 1
    if not (x is True or x is False):
        _put(sid, "v")  # a non-bool operand was evaluated (value context matters)
    return x


def _cmp(sid, op, a, b):
    if op <= GE:
        if _plain(a) and _plain(b):
            _put(sid, "e" if a == b else "n")
        else:
            _put(sid, "x")  # objects with their own operators: equality unknown
        return (a < b) if op == LT else (a <= b) if op == LE else (a > b) if op == GT else (a >= b)
    if op == IS or op == ISNOT:
        r = (a is b) if op == IS else (a is not b)
        if _has(sid, "d"):
            return r  # already known to disagree in this test: skip the costly ==
        try:
            e = (a == b) if op == IS else (a != b)
            if not (e is True or e is False) or e != r:
                _put(sid, "d")
        except BaseException:
            _put(sid, "d")
        return r
    try:
        r = (a == b) if op == EQ else (a != b)
    except BaseException:
        _put(sid, "d")
        raise
    alt = (a is b) if op == EQ else (a is not b)
    if not (r is True or r is False) or r != alt:
        _put(sid, "d")
    return r


PLAIN = {int, float, str, bytes, bool, type(None)}


def _plain(x):
    t = type(x)
    if t in PLAIN:
        return True
    return t is tuple and len(x) <= 8 and all(type(y) in PLAIN for y in x)  # version tuples

builtins.__brbop__ = _bop
builtins.__brcmp2__ = _cmp


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        if os.environ.get("COND_NO_BOOL"):
            return node
        sid = len(SITES)
        SITES.append(f"{self.rel}|B:{node.lineno}:{node.col_offset}")
        BOOLS.append((isinstance(node.op, ast.And), len(node.values)))
        node.values = [ast.copy_location(ast.Call(ast.Name("__brbop__", ast.Load()),
                                                  [ast.Constant(sid), ast.Constant(i), v], []), v)
                       for i, v in enumerate(node.values)]
        return node

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in CMP_OPS or os.environ.get("COND_NO_CMP"):
            return node
        only = os.environ.get("COND_ORDER_ONLY")
        if only is not None and CMP_OPS[type(node.ops[0])] <= GE and not self.rel.startswith(only):
            return node
        sid = len(SITES)
        SITES.append(f"{self.rel}|C:{node.lineno}:{node.col_offset}")
        BOOLS.append(None)
        call = ast.Call(ast.Name("__brcmp2__", ast.Load()),
                        [ast.Constant(sid), ast.Constant(CMP_OPS[type(node.ops[0])]),
                         node.left, node.comparators[0]], [])
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        IMPORTING[0] += 1
        try:
            super().exec_module(module)
        finally:
            IMPORTING[0] -= 1

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        tree = ast.parse(self.get_data(path), path)
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


assert "sqlglot" not in sys.modules, "condplugin must load before sqlglot"
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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    CUR[0] = item.nodeid + "|setup"
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    CUR[0] = item.nodeid
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    CUR[0] = item.nodeid
    yield
    CUR[0] = ""


def pytest_sessionfinish(session):
    # an operand raised mid-evaluation: the edited operator might not have reached it
    for (ctx, sid), c in OPEN.items():
        if c:
            CUR_SAVE, CUR[0] = CUR[0], ctx
            d = REC.setdefault(ctx, {})
            d.setdefault(sid, set()).add("r")
            CUR[0] = CUR_SAVE
    count = {}
    for s in SITES:
        count[s] = count.get(s, 0) + 1
    dup = sorted(s for s, c in count.items() if c > 1)  # two sites share a start: unusable
    out = {"dup": dup, "rec": {}}
    for ctx, d in REC.items():
        out["rec"][ctx] = {SITES[sid]: sorted(map(str, st)) for sid, st in d.items()}
    w = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(os.environ["OUT"], f"cond-{w}.json"), "w") as fh:
        json.dump(out, fh)
