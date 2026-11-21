"""Drop the two slowest tests when the patched source provably leaves them passing.

test_max_nodes (~29 s) and test_execute_tpcds (~21 s) cover thousands of core lines, so the
line maps select them for most parser, optimizer and executor edits, though few such edits
break them. When one of them decides a commit's cost, a forked child imports the patched
sqlglot (from the /tmp/w symlink farm) and checks it far more cheaply than the test runs:

* test_max_nodes only asserts that a few parses succeed or raise the max-nodes error with
  the right message. The check runs the same assertions with the wide query and both limits
  scaled down by 50 (same depth, same nodes-to-limit ratios).
* test_execute_tpcds parses all 99 TPC-DS queries, executes 57 of them on the Python executor
  and compares each with duckdb's result for transpile(sql, "duckdb"). maps/heavy.json.gz holds
  fingerprints of each step's output on the pinned tree and which sqlglot lines each query's
  optimize and execute steps ran (tools/heavy). The check re-parses and re-transpiles every
  query (cheap), and optimizes / executes only the queries whose steps ran a changed function
  or whose input changed. Identical parse trees, optimized trees, result rows and duckdb SQL
  mean every subtest sees what it saw on the pinned tree, where the test passes.

The tpcds check applies only when the patch edits nothing but function bodies of existing
sqlglot/*.py files, none of which ran before the first query (imports, class setup). Any
mismatch, error, timeout or budget overrun keeps the test.
"""
import ast
import gzip
import json
import os
import pickle
import select
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAP = Path(os.environ.get("HEAVY_MAP", HERE / "maps" / "heavy.json.gz"))
TABLES = Path(os.environ.get("HEAVY_TABLES", HERE / "heavy_tables.pkl"))
CPU_MULT = float(os.environ.get("HEAVY_CPU_MULT", "3"))  # board CPU / our local CPU
IMPORT_CPU = 0.5       # child: import patched sqlglot, optimizer, executor, duckdb dialect
TABLES_CPU = 0.25      # unpickling the fixture rows and building Tables
MAXN_CPU = 0.5
MARGIN = 1.3           # run a check only if the saving beats MARGIN x its board CPU
CPU_BUDGET = float(os.environ.get("HEAVY_CPU_BUDGET", "80"))  # local CPU seconds, whole run
WALL_STOP = float(os.environ.get("HEAVY_WALL_STOP", "100"))   # no new checks after this (limit 180 s)
START = time.time()

MAXN_ID = "tests/test_parser.py::TestParser::test_max_nodes"
TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"
# functions the check calls in a split form (tools/heavy/heavy_steps.py): edits keep the test
SPLIT = {("sqlglot/executor/__init__.py", "execute")}


def log(*a):
    print(*a, file=sys.stderr)


def _defs(tree):
    """Outermost function definitions, in order, with their qualified names."""
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                out.append((prefix + child.name, child))
            elif isinstance(child, ast.ClassDef):
                walk(child, prefix + child.name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return out


def _blank(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            node.body = [ast.Pass()]
    return ast.dump(tree)


def changed_functions(old_text, new_text):
    """[(qualname, first line, last line)] of the old file's functions whose bodies differ,
    or None when anything outside function bodies differs."""
    try:
        old, new = ast.parse(old_text), ast.parse(new_text)
        if _blank(ast.parse(old_text)) != _blank(ast.parse(new_text)):
            return None
    except (SyntaxError, ValueError, RecursionError):
        return None
    a, b = _defs(old), _defs(new)
    if [n for n, _ in a] != [n for n, _ in b]:
        return None
    # body lines only: the def line itself runs at import (and its signature is compared above)
    return [(name, x.body[0].lineno, x.end_lineno) for (name, x), (_, y) in zip(a, b)
            if ast.dump(x) != ast.dump(y)]


class Heavy:
    def __init__(self, sel, src, apply):
        self.sel, self.src, self.apply = sel, Path(src), apply
        self.maxn = sel.num_by_id.get(MAXN_ID)
        self.tpcds = sel.num_by_id.get(TPCDS_ID)
        self.doc = None
        self.farm = None
        self.spent = 0.0

    # -- planning
    def load(self):
        if self.doc is None:
            with gzip.open(MAP, "rt") as f:
                self.doc = json.load(f)
        return self.doc

    def tpcds_plan(self, files):
        """Queries whose optimize / execute steps need re-running, or None (keep the test)."""
        doc = self.load()
        need_o = need_e = need_p = need_d = 0
        for fd in files:
            f = fd["new"]
            old_text = (self.src / f).read_text(encoding="utf8")
            funcs = changed_functions(old_text, self.apply(old_text, fd["hunks"]))
            if funcs is None:
                return None
            shared = set(doc["shared"].get(f, ()))
            lines = doc["lines"].get(f, {})
            for name, lo, hi in funcs:
                if (f, name) in SPLIT or (f, name.rsplit(".", 1)[-1]) in SPLIT:
                    return None
                for ln in range(lo, hi + 1):
                    if ln in shared:
                        return None
                    m = lines.get(str(ln))
                    if m:
                        need_o |= m[0]
                        need_e |= m[1]
                        need_p |= m[2]
                        need_d |= m[3]
        qs = doc["queries"]

        def bits(mask):
            return [i for i in range(len(qs)) if mask >> i & 1]
        o, e = bits(need_o | need_e), bits(need_e)
        p, d = bits(need_p | need_o | need_e), bits(need_d)
        cpu = (IMPORT_CPU + sum(qs[i]["Pw"] for i in p) + sum(qs[i].get("Dw", 0) for i in d)
               + sum(qs[i]["Ow"] for i in o) + sum(qs[i]["Ew"] for i in e)
               + (TABLES_CPU if o else 0))
        return {"O": o, "E": e, "P": p, "D": d, "cpu": cpu}

    def refine(self, files, nums, pack, cost):
        """(numbers, note): nums without the heavy tests the checks cleared."""
        cand = [n for n in (self.maxn, self.tpcds) if n is not None and n in nums]
        if not cand:
            return nums, ""
        if not all(fd["old"] and fd["old"] == fd["new"] and fd["new"].startswith("sqlglot/")
                   and fd["new"].endswith(".py") and not fd["flags"] for fd in files):
            return nums, "heavy: not sqlglot-only"
        if time.time() - START > WALL_STOP or self.spent > CPU_BUDGET:
            return nums, "heavy: budget spent"
        base = cost(pack(nums))
        plan, est = {}, 0.0
        if self.tpcds in cand:
            p = self.tpcds_plan(files)
            if p is not None:
                plan["tpcds"], est = p, p["cpu"]
        if self.maxn in cand:
            plan["max_nodes"] = True
            est += MAXN_CPU if est else IMPORT_CPU + MAXN_CPU
        if not plan:
            return nums, "heavy: tpcds not checkable"
        drop = {self.maxn if k == "max_nodes" else self.tpcds for k in plan}
        saving = base - cost(pack(nums - drop))
        if saving / 1000 < MARGIN * CPU_MULT * est:
            # maybe one of them alone pays
            if len(plan) == 2:
                for k, n in (("max_nodes", self.maxn), ("tpcds", self.tpcds)):
                    s = base - cost(pack(nums - {n}))
                    e = (IMPORT_CPU + MAXN_CPU) if k == "max_nodes" else plan["tpcds"]["cpu"]
                    if s / 1000 >= MARGIN * CPU_MULT * e:
                        plan = {k: plan[k]}
                        est = e
                        break
                else:
                    return nums, f"heavy: not worth it ({saving / 1000:.1f}s vs {est:.1f}s cpu)"
            else:
                return nums, f"heavy: not worth it ({saving / 1000:.1f}s vs {est:.1f}s cpu)"
        if "tpcds" in plan:
            # execute steps may run at most what the drop is worth
            plan["tpcds"]["budget"] = max(0.5, saving / 1000 / CPU_MULT / MARGIN
                                          - IMPORT_CPU - MAXN_CPU)
        res = self.run(files, plan, timeout=2 * est + 2)  # a slow patched query only keeps tests
        kept = set(nums)
        notes = []
        for k, n in (("max_nodes", self.maxn), ("tpcds", self.tpcds)):
            if k in plan:
                ok = res.get(k) is True
                notes.append(f"{k} {'dropped' if ok else 'kept'}"
                             + ("" if ok else f" ({res.get(k)})"))
                if ok:
                    kept.discard(n)
        return kept, "heavy: " + ", ".join(notes) + f" [{res.get('cpu', 0):.2f}s]"

    # -- running
    def run(self, files, plan, timeout):
        import resource

        sys.path.insert(0, str(HERE))
        import verify  # farm and overlay helpers (verify.py)

        if self.farm is None:
            t = time.time()
            verify.build_farm(self.src)
            self.farm = verify.FARM
            log(f"heavy: farm ready in {time.time() - t:.2f}s")
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        overlay = None
        res = {}
        try:
            overlay = verify.Overlay(self.src, self.apply, files)
            sys.stdout.flush()
            sys.stderr.flush()
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(r)
                    os.setpgid(0, 0)
                    _child(w, plan, self.doc, str(self.farm))
                finally:
                    os._exit(0)
            os.close(w)
            buf, deadline = b"", time.time() + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    res = {"error": "timeout"}
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except OSError:
                        pass
                    break
                ready, _, _ = select.select([r], [], [], left)
                if ready:
                    chunk = os.read(r, 1 << 16)
                    if not chunk:
                        break
                    buf += chunk
            os.close(r)
            os.waitpid(pid, 0)
            if buf:
                res = json.loads(buf)
        except Exception as e:
            res = {"error": f"{type(e).__name__}: {e}"}
        finally:
            if overlay is not None:
                overlay.restore()
            r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu = r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime
            self.spent += cpu
            res["cpu"] = cpu
        return res


# --------------------------------------------------------------------------- child

def _child(w, plan, doc, farm):
    res = {}
    try:
        sys.path.insert(0, farm)
        os.chdir(farm)
        # sqlglot validates more when it runs under tests (expressions/core.py:
        # UNITTEST = "unittest" in sys.modules or "pytest" in sys.modules); so must we
        import unittest  # noqa: F401
        os.environ.setdefault("SKIP_INTEGRATION", "0")
        if plan.get("max_nodes"):
            try:
                res["max_nodes"] = _max_nodes()
            except BaseException as e:  # noqa: BLE001  (anything unexpected keeps the test)
                res["max_nodes"] = f"{type(e).__name__}: {e}"[:200]
        if plan.get("tpcds"):
            try:
                res["tpcds"] = _tpcds(plan["tpcds"], doc["queries"])
            except BaseException as e:  # noqa: BLE001
                res["tpcds"] = f"{type(e).__name__}: {e}"[:200]
    finally:
        os.write(w, json.dumps(res).encode())
        os.close(w)


def _max_nodes():
    """test_max_nodes with width and limits / 50 (10000 -> 200 columns, 1M -> 20k, 50k -> 1k)."""
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    if not isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select):
        return "SELECT 1"
    cols = [f"col{i}" for i in range(500)]
    if not isinstance(parse_one(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000), exp.Select):
        return "500 columns"
    expr = "x" + "=x" * 100
    sql = f"SELECT {','.join(expr for _ in range(200))}"
    for limit in (20_000, 1_000):
        try:
            parse_one(sql, max_nodes=limit)
            return f"no error at {limit}"
        except ParseError as e:
            msg = str(e)
        if "Maximum number of AST nodes" not in msg or f"({limit})" not in msg:
            return f"message {msg[:80]!r}"
    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
        return "no error at 5"
    except ParseError:
        pass
    if not isinstance(parse_one("SELECT 1, 2, 3"), exp.Select):
        return "after limit"
    return True


def _tpcds(plan, queries):
    import hashlib
    from multiprocessing.reduction import ForkingPickler

    from sqlglot import find_tables, parse_one, transpile

    sys.path.insert(1, str(HERE))
    from heavy_steps import optimize_step, rows_fp, run_step, tree_fp

    t0 = time.process_time()
    trees = {}
    need_o, need_e = set(plan["O"]), set(plan["E"])
    # a step that ran none of the changed lines on the pinned tree runs the same code now:
    # parse only the queries some changed line's parse (or later step) ran, transpile likewise
    for i in plan["P"]:
        q = queries[i]
        e = ForkingPickler.loads(ForkingPickler.dumps(parse_one(q["sql"])))
        if q["exec"]:
            trees[i] = e
            if q["P"] is None or tree_fp(e) != q["P"]:
                need_o.add(i)
    for i in plan["D"]:
        q = queries[i]
        if q["exec"]:
            d = transpile(q["sql"], write="duckdb")[0]
            if q["D"] is None or hashlib.sha1(d.encode()).hexdigest() != q["D"]:
                return f"duckdb sql of query {i + 1} changed"
    if not (need_o or need_e):
        return True
    from sqlglot.executor.table import Table
    from tests.helpers import TPCDS_SCHEMA

    with open(TABLES, "rb") as f:
        packed = pickle.load(f)  # {table: pickled rows}
    tables = {}

    def table(name):
        if name not in tables:
            tables[name] = Table(columns=TPCDS_SCHEMA[name], rows=pickle.loads(packed[name]))
        return tables[name]

    # cheapest queries first, each optimized and (if needed) executed before the next, so a
    # failing commit usually stops early
    todo = [i for i in need_o | need_e if queries[i]["exec"]]
    todo.sort(key=lambda i: queries[i]["Ow"] + (queries[i]["Ew"] if i in need_e else 0))
    for i in todo:
        q = queries[i]
        if time.process_time() - t0 > plan["budget"]:
            return "budget spent"
        e = trees[i]
        ts = {x.name: table(x.name) for x in find_tables(e)}
        o, tabs = optimize_step(e, ts)
        if q["O"] is None or tree_fp(o) != q["O"]:
            need_e.add(i)
        if i in need_e:
            if time.process_time() - t0 > plan["budget"]:
                return "execute budget spent"
            r = ForkingPickler.loads(ForkingPickler.dumps(run_step(o, tabs)))
            if q["E"] is None or rows_fp(r) != q["E"]:
                return f"rows of query {i + 1} changed"
    return True
