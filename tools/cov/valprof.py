"""pytest plugin used only when building the value profile (never at selection time).

sqlglot is imported through an AST rewrite that records, per test, values the coverage map
cannot see. Every probe returns exactly what the original expression returns.

* dict literals assigned at class or module level become TrackDict instances. Per test
  (and for import time, test None) we record which entry keys were read for their value
  (d[k], d.get(k)), which were only membership-tested (k in d), lookups of keys that were
  missing, and whether the dict was used as a whole (iterated, copied, compared, mutated,
  len). Copies made at import (dict(X), {**X}) count as whole-dict use at import.
* single-operator relational comparisons (<, <=, >, >=, ==, !=) between numbers, strings,
  bools, None or tuples of those: the outcome of all six operators on the same operands, as
  a 6-bit mask. Any other operand type records "complex".
* two-operand `and` / `or` used as a condition (if / while / ternary / assert / not): the
  truthiness of both sides, or that the second side was not evaluated.

Output: $OUT/valprof-<worker>.json, merged by tools/build_valprof.py.
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
CTX = [None]  # current test node id; None while importing / collecting

DSITES = []   # sid -> (file, "line:col")
CSITES = []
BSITES = []
# (kind, sid, ctx) -> record
DREC, CREC, BREC = {}, {}, {}


def _ctx():
    return CTX[0]


# ---------------------------------------------------------------- dicts

def _drec(sid):
    key = (sid, _ctx())
    r = DREC.get(key)
    if r is None:
        r = DREC[key] = {"get": set(), "in": set(), "miss": set(), "len": False, "keys": False,
                         "vals": False, "mut": False}
    return r


def _ksrc(d, k):
    try:
        return dict.get(d._src, k, "<inherited>")
    except TypeError:  # unhashable key: the lookup raises in the original too
        return "<unhashable>"


def _miss_repr(k):
    if isinstance(k, (str, int, float, bool, type(None), tuple)):
        return repr(k)
    if isinstance(k, type):
        return f"<type {k.__module__}.{k.__qualname__}>"
    return "<other>"


class TrackDict(dict):
    __slots__ = ("_sid", "_src")

    def __getitem__(self, k):
        r = _drec(self._sid)
        if dict.__contains__(self, k):
            r["get"].add(_ksrc(self, k))
        else:
            r["miss"].add(_miss_repr(k))
        return dict.__getitem__(self, k)

    def get(self, k, default=None):
        r = _drec(self._sid)
        if dict.__contains__(self, k):
            r["get"].add(_ksrc(self, k))
        else:
            r["miss"].add(_miss_repr(k))
        return dict.get(self, k, default)

    def __contains__(self, k):
        r = _drec(self._sid)
        if dict.__contains__(self, k):
            r["in"].add(_ksrc(self, k))
        else:
            r["miss"].add(_miss_repr(k))
        return dict.__contains__(self, k)

    def __reduce_ex__(self, proto):
        r = _drec(self._sid)
        r["keys"] = r["vals"] = True
        return (dict, (dict(dict.items(self)),))  # unpickles as a plain dict (untracked)


def _flag_wrapper(name, flags):
    base = getattr(dict, name)

    def f(self, *a, **k):
        r = _drec(self._sid)
        for fl in flags:
            r[fl] = True
        return base(self, *a, **k)

    f.__name__ = name
    return f


# len / truthiness only sees the size; key iteration only the keys (a value read that follows
# goes through __getitem__); anything that exposes values marks "vals"; mutation marks "mut".
for _n, _fl in [("__len__", ("len",)), ("__iter__", ("keys",)), ("keys", ("keys",)),
                ("__reversed__", ("keys",)),
                ("values", ("vals",)), ("items", ("keys", "vals")), ("copy", ("keys", "vals")),
                ("__eq__", ("keys", "vals")), ("__ne__", ("keys", "vals")),
                ("__repr__", ("keys", "vals")), ("__str__", ("keys", "vals")),
                ("__format__", ("keys", "vals")), ("__or__", ("keys", "vals")),
                ("__ror__", ("keys", "vals")), ("__sizeof__", ("len",)),
                ("__ior__", ("mut",)), ("__setitem__", ("mut",)), ("__delitem__", ("mut",)),
                ("pop", ("mut",)), ("popitem", ("mut",)), ("setdefault", ("mut",)),
                ("update", ("mut",)), ("clear", ("mut",))]:
    setattr(TrackDict, _n, _flag_wrapper(_n, _fl))
TrackDict.__hash__ = None


def _vp_dict(sid, entries):
    """entries: [(key, value, src)] or [(None, mapping, None)] for ** in literal order."""
    d = TrackDict()
    src = {}
    for k, v, s in entries:
        if s is None and k is None:
            for kk, vv in (v.items() if hasattr(v, "items") else v):
                dict.__setitem__(d, kk, vv)
                src.pop(kk, None)
        else:
            dict.__setitem__(d, k, v)
            try:
                src[k] = s
            except TypeError:
                pass
    d._sid, d._src = sid, src
    return d


# ---------------------------------------------------------------- comparisons

SIMPLE = (int, float, str, bool, type(None))
REL = ("<", "<=", ">", ">=", "==", "!=")


def _simple(x):
    return isinstance(x, SIMPLE) or (isinstance(x, tuple) and all(_simple(y) for y in x))


def _vp_cmp(sid, op, a, b):
    try:
        r = _vp_op(op, a, b)
    except BaseException:
        CREC.setdefault((sid, _ctx()), set()).add(-1)  # another operator might not raise
        raise
    return _vp_rec(sid, a, b, r)


def _vp_op(op, a, b):
    if op == 0:
        r = a < b
    elif op == 1:
        r = a <= b
    elif op == 2:
        r = a > b
    elif op == 3:
        r = a >= b
    elif op == 4:
        r = a == b
    else:
        r = a != b
    return r


def _vp_rec(sid, a, b, r):
    key = (sid, _ctx())
    s = CREC.get(key)
    if s is None:
        s = CREC[key] = set()
    if len(s) < 64 and _simple(a) and _simple(b) and type(r) is bool:
        try:
            m = (a < b) | (a <= b) << 1 | (a > b) << 2 | (a >= b) << 3 | (a == b) << 4 | (a != b) << 5
            s.add(int(m))
        except TypeError:
            s.add(-1)
    else:
        s.add(-1)  # complex operands (or too many shapes): unknown
    return r


# ---------------------------------------------------------------- and / or in conditions

STACK = {}


def _vp_begin(sid):
    STACK.setdefault(sid, []).append([None, None])
    return None


def _vp_side(sid, i, v):
    st = STACK.get(sid)
    if st:
        st[-1][i] = bool(v)
    return v


def _vp_end(sid, v):
    st = STACK.get(sid)
    key = (sid, _ctx())
    s = BREC.get(key)
    if s is None:
        s = BREC[key] = set()
    if st:
        a, b = st.pop()
        s.add((a, b))
    else:
        s.add(("?", "?"))
    return v


builtins.__vp_dict__ = _vp_dict
builtins.__vp_cmp__ = _vp_cmp
builtins.__vp_begin__ = _vp_begin
builtins.__vp_side__ = _vp_side
builtins.__vp_end__ = _vp_end

OPCODE = {ast.Lt: 0, ast.LtE: 1, ast.Gt: 2, ast.GtE: 3, ast.Eq: 4, ast.NotEq: 5}


def _call(name, *args):
    return ast.Call(ast.Name(name, ast.Load()), list(args), [])


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel
        self.depth = 0  # function nesting: dict literals are tracked only outside functions

    def _func(self, node):
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _func

    def _dict_value(self, value):
        if self.depth or not isinstance(value, ast.Dict):
            return value
        sid = len(DSITES)
        DSITES.append((self.rel, f"{value.lineno}:{value.col_offset}"))
        entries = []
        for k, v in zip(value.keys, value.values):
            if k is None:
                entries.append(ast.Tuple([ast.Constant(None), v, ast.Constant(None)], ast.Load()))
            else:
                entries.append(ast.Tuple([k, v, ast.Constant(ast.unparse(k))], ast.Load()))
        return ast.copy_location(_call("__vp_dict__", ast.Constant(sid), ast.List(entries, ast.Load())), value)

    def visit_Assign(self, node):
        self.generic_visit(node)
        node.value = self._dict_value(node.value)
        return node

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        if node.value is not None:
            node.value = self._dict_value(node.value)
        return node

    def visit_Compare(self, node):
        key = f"{node.lineno}:{node.col_offset}"
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPCODE:
            return node
        sid = len(CSITES)
        CSITES.append((self.rel, key))
        return ast.copy_location(_call("__vp_cmp__", ast.Constant(sid), ast.Constant(OPCODE[type(node.ops[0])]),
                                       node.left, node.comparators[0]), node)

    def _cond(self, test):
        """Rewrite a 2-operand and/or used as a condition."""
        if not (isinstance(test, ast.BoolOp) and len(test.values) == 2):
            return test
        sid = len(BSITES)
        BSITES.append((self.rel, f"{test.lineno}:{test.col_offset}"))
        a, b = test.values
        inner = ast.BoolOp(test.op, [_call("__vp_side__", ast.Constant(sid), ast.Constant(0), a),
                                     _call("__vp_side__", ast.Constant(sid), ast.Constant(1), b)])
        wrapped = ast.BoolOp(ast.Or(), [_call("__vp_begin__", ast.Constant(sid)), inner])
        return ast.copy_location(_call("__vp_end__", ast.Constant(sid), wrapped), test)

    def visit_If(self, node):
        node.test = self._cond_first(node.test)
        self.generic_visit(node)
        return node

    def _cond_first(self, test):
        # rewrite operands first (comparisons inside), then the and/or itself
        if isinstance(test, ast.BoolOp) and len(test.values) == 2:
            test.values = [self.visit(v) for v in test.values]
            return self._cond_marked(test)
        return test

    def _cond_marked(self, test):
        test._vp_done = True
        return self._cond(test)

    def visit_While(self, node):
        node.test = self._cond_first(node.test)
        self.generic_visit(node)
        return node

    def visit_IfExp(self, node):
        node.test = self._cond_first(node.test)
        self.generic_visit(node)
        return node

    def visit_Assert(self, node):
        node.test = self._cond_first(node.test)
        self.generic_visit(node)
        return node

    def visit_UnaryOp(self, node):
        if isinstance(node.op, ast.Not):
            node.operand = self._cond_first(node.operand)
        self.generic_visit(node)
        return node

    def visit_Call(self, node):
        # our own probe calls: do not descend into their marker arguments again
        if isinstance(node.func, ast.Name) and node.func.id.startswith("__vp_"):
            return node
        self.generic_visit(node)
        return node

    def visit_BoolOp(self, node):
        if getattr(node, "_vp_done", False):
            return node
        self.generic_visit(node)
        return node


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


assert "sqlglot" not in sys.modules, "valprof must load before sqlglot"
sys.meta_path.insert(0, _Finder)


class SyncExecutor(cf.Executor):
    """Pool work runs in-process (still pickled both ways) so it is attributed to the test."""

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

FAILED = set()


def pytest_runtest_setup(item):
    CTX[0] = "S:" + item.nodeid  # setup: credited to the whole class/module at build


def pytest_runtest_call(item):
    CTX[0] = item.nodeid


def pytest_runtest_teardown(item):
    CTX[0] = "S:" + item.nodeid


def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    CTX[0] = None


def pytest_sessionfinish(session):
    def ctxs(rec, conv):
        out = {}
        for (sid, ctx), v in rec.items():
            out.setdefault(str(sid), {})["" if ctx is None else ctx] = conv(v)
        return out

    out = {
        "dsites": DSITES, "csites": CSITES, "bsites": BSITES,
        "dicts": ctxs(DREC, lambda r: {k: sorted(v) if isinstance(v, set) else v for k, v in r.items()}),
        "cmps": ctxs(CREC, sorted),
        "bools": ctxs(BREC, lambda s: sorted([list(x) for x in s], key=repr)),
        "failed": sorted(FAILED),
        "leftover": sorted(str(k) for k, v in STACK.items() if v),
    }
    w = os.environ.get("PYTEST_XDIST_WORKER", "main")
    with open(os.path.join(os.environ["OUT"], f"valprof-{w}.json"), "w") as fh:
        json.dump(out, fh)
