"""pytest plugin used only when building the maps (never at selection time).

Records, per test:
  * coverage contexts: "<nodeid>" for the call/teardown phase, "<nodeid>|setup" for the
    setup phase (which also holds setUpClass / module fixtures for the first test of a
    class or module; build_maps.py widens those to the whole class/module);
  * the non-Python files under tests/ it opens (fixtures are invisible to coverage);
  * the sqlglot classes it instantiates, by MRO (dialects, tokenizers, parsers,
    generators, expression nodes).

Process pools are replaced by an in-process executor that still pickles arguments and
results, so code the tests send to spawn workers (and the serde path pickling runs) is
traced in the test's own context.
"""
import builtins
import concurrent.futures as cf
import io
import json
import os
import pathlib
import pickle

import coverage
import pytest


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

_CUR = [""]
_READS: dict = {}
_USES: dict = {}
_SEEN: dict = {}
_open = builtins.open


def _set(ctx):
    _CUR[0] = ctx
    cov = coverage.Coverage.current()
    if cov:
        cov.switch_context(ctx)


def _rec(p):
    try:
        s = os.fspath(p)
    except TypeError:
        return
    if isinstance(s, str):
        s = s.replace(os.sep, "/")
        if "/tests/" in s and not s.endswith((".py", ".pyc")) and "/__pycache__/" not in s:
            _READS.setdefault(_CUR[0], set()).add("tests/" + s.split("/tests/", 1)[1])


def _o(file, *a, **k):
    _rec(file)
    return _open(file, *a, **k)


builtins.open = _o
io.open = _o
_popen = pathlib.Path.open


def _po(self, *a, **k):
    _rec(self)
    return _popen(self, *a, **k)


pathlib.Path.open = _po


def _track(cls):
    orig = cls.__init__

    def __init__(self, *a, **k):
        seen = _SEEN.setdefault(_CUR[0], set())
        t = type(self)
        if t not in seen:
            seen.add(t)
            _USES.setdefault(_CUR[0], set()).update(c.__module__ + "." + c.__qualname__ for c in t.__mro__)
        orig(self, *a, **k)

    cls.__init__ = __init__


def pytest_configure(config):
    import sqlglot.dialects.dialect as d
    import sqlglot.expressions.core as core
    import sqlglot.generator as g
    import sqlglot.parser as p
    import sqlglot.tokens as tk

    for c in (core.Expr, p.Parser, g.Generator, tk.Tokenizer, d.Dialect):
        _track(c)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _set(item.nodeid + "|setup")
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _set(item.nodeid)
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    _set(item.nodeid)
    yield
    _set("")


def pytest_unconfigure(config):
    out = os.environ["OUT"]
    with _open(out + "/reads.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _READS.items()}, f)
    with _open(out + "/uses.json", "w") as f:
        json.dump({k: sorted(v) for k, v in _USES.items()}, f)
