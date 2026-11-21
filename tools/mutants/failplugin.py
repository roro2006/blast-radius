"""Labeling plugin.

* Collects every test that fails, errors, times out, or has a failing subtest; a file that
  cannot be collected fails all of its tests (recorded as '<file>::*').
* Caps the process pools the optimizer/executor tests create at POOL_CAP workers (default 4). They still run
  in real spawned processes with pickling, so results are unchanged, but 6 xdist workers
  no longer start ~70 interpreters and swap.
* Runs the slowest classes first, each kept together (use with --dist loadscope), so
  class setup runs once and the long tail is short.
"""
import concurrent.futures as cf
import json
import os

import pytest

_failed = set()
_Pool = cf.ProcessPoolExecutor
POOL_CAP = int(os.environ.get("POOL_CAP", "4"))


class CappedPool(_Pool):
    def __init__(self, max_workers=None, *a, **k):
        super().__init__(min(max_workers or POOL_CAP, POOL_CAP), *a, **k)


cf.ProcessPoolExecutor = CappedPool


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    path = os.environ.get("TEST_DURATIONS")
    if not path or not os.path.exists(path):
        return
    secs = {t["id"]: t["seconds"] for t in json.load(open(path))["tests"]}
    # Keep each class (or module, for plain functions) contiguous so class setup runs once;
    # order the groups slowest-first for --dist loadscope.
    def scope(it):
        return it.nodeid.rsplit("::", 1)[0] if it.cls else it.nodeid.split("::", 1)[0]
    total = {}
    for it in items:
        total[scope(it)] = total.get(scope(it), 0.0) + secs.get(it.nodeid, 0.0)
    pos = {it.nodeid: i for i, it in enumerate(items)}
    items.sort(key=lambda it: (-total[scope(it)], scope(it), pos[it.nodeid]))


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        _failed.add(report.nodeid + "::*")


def pytest_sessionfinish(session):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return  # the controller receives every worker's reports
    with open(os.environ["FAILED_OUT"], "w") as f:
        json.dump(sorted(_failed), f)
