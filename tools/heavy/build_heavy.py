"""Per-query profile of test_execute_tpcds on the pinned tree (run from the sqlglot root).

The test parses every TPC-DS query, runs the executable ones on sqlglot's Python executor in
pool workers, and compares each result with duckdb's result for transpile(sql, "duckdb").
For each query and each phase of that work, this records which sqlglot lines ran (in a fresh
fork, so lazily built caches count for every query that needs them) and a fingerprint of the
phase's output:

  P  parse_one(sql), pickled and unpickled as the pool does   -> tree fingerprint
  E  find_tables + execute on the parsed tree, result pickled  -> rows fingerprint
  D  transpile(sql, write="duckdb")                            -> the duckdb SQL text

Lines that ran before any query (imports, setUpClass, pickling the tables for the pool) are
"shared": a change there keeps the test. Output: JSON on stdout.

  python build_heavy.py > tpcds.json
"""
import json
import os
import pickle
import sys
import time

ROOT = os.getcwd()
sys.path.insert(0, ROOT)
PKG = os.path.join(ROOT, "sqlglot") + os.sep

mon = sys.monitoring
TOOL = 4
mon.use_tool_id(TOOL, "heavy")
cur = set()


def on_line(code, line):
    fn = code.co_filename
    if fn.startswith(PKG):
        cur.add((fn[len(ROOT) + 1:], line))
    return mon.DISABLE


mon.register_callback(TOOL, mon.events.LINE, on_line)
mon.set_events(TOOL, mon.events.LINE)

from multiprocessing.reduction import ForkingPickler  # noqa: E402

from tests.test_executor import TestExecutor  # noqa: E402
from tests.helpers import TPCDS_SCHEMA  # noqa: E402
from sqlglot import find_tables, parse_one, transpile  # noqa: E402
from sqlglot.executor import execute  # noqa: E402

from heavy_steps import optimize_step, rows_fp, run_step, tree_fp  # noqa: E402

TestExecutor.setUpClass()
tables = pickle.loads(ForkingPickler.dumps((TPCDS_SCHEMA, TestExecutor.tpcds_tables)))[1]
shared = set(cur)


def phase(fn):
    global cur
    cur = set()
    mon.restart_events()
    t = time.process_time()
    out = fn()
    return out, time.process_time() - t, cur


def one(meta, sql):
    res = {"exec": bool(meta.get("execute"))}
    e, t, lines = phase(lambda: ForkingPickler.loads(ForkingPickler.dumps(parse_one(sql))))
    res["P"], res["Pt"], res["Pl"] = tree_fp(e), t, sorted(lines)
    if res["exec"]:
        state = {}

        def opt():
            ts = {x.name: tables[x.name] for x in find_tables(e)}
            o, tabs = optimize_step(e, ts)
            state.update(o=o, tabs=tabs)
            return tree_fp(o), sorted(ts)

        def run():
            r = ForkingPickler.loads(ForkingPickler.dumps(run_step(state["o"], state["tabs"])))
            return rows_fp(r)
        (res["O"], res["tables"]), res["Ot"], lines = phase(opt)
        res["Ol"] = sorted(lines)
        res["E"], res["Et"], lines = phase(run)
        res["El"] = sorted(lines)
        res["D"], res["Dt"], lines = phase(lambda: transpile(sql, write="duckdb")[0])
        res["Dl"] = sorted(lines)
    return res


queries = []
for meta, sql, _ in TestExecutor.tpcds_sqls:
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r)
        try:
            data = json.dumps(one(meta, sql)).encode()
        except Exception as ex:
            data = json.dumps({"error": repr(ex)}).encode()
        with os.fdopen(w, "wb") as f:
            f.write(data)
        os._exit(0)
    os.close(w)
    with os.fdopen(r, "rb") as f:
        buf = f.read()
    os.waitpid(pid, 0)
    q = json.loads(buf)
    q["sql"] = sql
    queries.append(q)
    print(len(queries), q.get("error") or round(q.get("Et", 0), 3), file=sys.stderr)

# warm costs, as the runtime check pays them: one process, queries in order, no tracing
mon.set_events(TOOL, 0)
for q, (meta, sql, _) in zip(queries, TestExecutor.tpcds_sqls):
    t = time.process_time()
    e = ForkingPickler.loads(ForkingPickler.dumps(parse_one(sql)))
    q["Pw"] = time.process_time() - t
    if q["exec"]:
        t = time.process_time()
        o, tabs = optimize_step(e, {x.name: tables[x.name] for x in find_tables(e)})
        q["Ow"] = time.process_time() - t
        t = time.process_time()
        run_step(o, tabs)
        q["Ew"] = time.process_time() - t
        t = time.process_time()
        transpile(sql, write="duckdb")
        q["Dw"] = time.process_time() - t

json.dump({"shared": sorted(shared), "queries": queries}, sys.stdout)
