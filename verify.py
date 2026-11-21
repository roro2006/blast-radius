"""Run the selector's candidate tests on the patched source and keep only the ones that fail.

The map-based selector is safe but over-selects: most commits where nothing fails still get
a worker or two. When running the candidates costs less CPU than scheduling them, we run
them here and print only the tests that did not cleanly pass.

How:
  * /tmp/w mirrors the pinned sqlglot tree with symlinks; a commit's touched files are written
    as real files, then put back. Bytecode (and pytest's assert-rewritten test modules) for the
    unpatched tree is precompiled at build time under PYTHONPYCACHEPREFIX, so imports are cheap.
  * The parent imports pytest once and forks one child per commit; the child imports sqlglot
    from /tmp/w fresh, runs the candidates, and reports through a pipe.

A candidate is dropped only when its setup, call and teardown all passed, no subtest failed,
it was not skipped, and its call stayed well within its recorded time. Anything else (failure,
error, skip, not collected, slow, crash, timeout) keeps it.
"""
import json
import os
import select
import signal
import sys
import time
from pathlib import Path

FARM = Path(os.environ.get("FARM", "/tmp/w"))
POOL_CAP = 2

# CPU model (seconds), fitted on the sample set in the scoring image.
FORK_OVERHEAD = 0.12     # child: pytest session + sqlglot import
PER_FILE = 0.03          # importing / collecting one dialect test module
HEAVY_FILES = {          # modules with costly imports or class setup (duckdb, pools)
    "tests/test_optimizer.py": 0.6,
    "tests/test_executor.py": 0.6,
}
RUN_FACTOR = 0.6         # recorded test seconds -> our CPU seconds (recorded runs were slower)
# The board's CPU figure ran ~3x our local measurement for the plain selector; plan with 2x.
CPU_MULT = float(os.environ.get("VERIFY_CPU_MULT", "2"))
# Chance a source-code commit's candidates all pass (5 of 7 on the sample). Fixture and
# test-file edits nearly always break their tests (0 of 9), so they are never verified.
P_PASS_CODE = 0.6
TIME_BUDGET = float(os.environ.get("VERIFY_BUDGET", "100"))  # wall seconds for all verification


def log(*a):
    print(*a, file=sys.stderr)


def build_farm(src):
    """Mirror src under FARM: real directories, symlinked files."""
    for root, dirs, files in os.walk(src):
        rel = os.path.relpath(root, src)
        dst = FARM / rel if rel != "." else FARM
        dst.mkdir(parents=True, exist_ok=True)
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for f in files:
            link = dst / f
            if not link.exists() and not link.is_symlink():
                os.symlink(os.path.join(root, f), link)


class Overlay:
    """Writes a patch's files into the farm and restores the symlinks afterwards."""

    def __init__(self, src, apply, files):
        self.src, self.undo = Path(src), []
        for fd in files:
            path = fd["new"] or fd["old"]
            target = FARM / path
            if fd["new"] is None:
                self._remove(path, target)
                continue
            old = (self.src / fd["old"]).read_text(encoding="utf8") if fd["old"] else ""
            text = apply(old, fd["hunks"])
            self._mkdirs(target.parent)
            self._remove(path, target)
            target.write_text(text, encoding="utf8")

    def _mkdirs(self, d):
        missing = []
        while not d.exists():
            missing.append(d)
            d = d.parent
        for m in reversed(missing):
            m.mkdir()
            self.undo.append(("rmdir", m, None))

    def _remove(self, path, target):
        if target.is_symlink() or target.exists():
            target.unlink()
        orig = self.src / path
        self.undo.append(("restore", target, orig if orig.exists() else None))

    def restore(self):
        for kind, p, orig in reversed(self.undo):
            if kind == "rmdir":
                try:
                    p.rmdir()
                except OSError:
                    pass
            else:
                if p.is_symlink() or p.exists():
                    p.unlink()
                if orig is not None:
                    os.symlink(orig, p)


class Collector:
    """pytest plugin: which node ids cleanly passed."""

    def __init__(self, limits):
        self.limits, self.bad, self.passed = limits, set(), set()

    def pytest_runtest_logreport(self, report):
        nid = report.nodeid
        if report.failed or report.skipped:
            self.bad.add(nid)
        elif report.when == "call":
            if report.duration > self.limits.get(nid, 2.0):
                self.bad.add(nid)
            else:
                self.passed.add(nid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.bad.add(report.nodeid)


def _cap_pools():
    import concurrent.futures as cf
    base = cf.ProcessPoolExecutor

    import multiprocessing as mp

    class CappedPool(base):
        """At most POOL_CAP workers, forked from the test process instead of spawned: a
        spawned worker re-imports sqlglot from scratch (~0.7 s CPU each). Arguments and
        results are still pickled, so what the tests compute is unchanged."""

        def __init__(self, max_workers=None, mp_context=None, *a, **k):
            super().__init__(min(max_workers or POOL_CAP, POOL_CAP), mp.get_context("fork"), *a, **k)

    cf.ProcessPoolExecutor = CappedPool


def _child(w, ids, limits):
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)  # stdout must carry only answer lines
    os.dup2(devnull, 2)
    # A runaway patch must not let the container's OOM killer take the selector with it;
    # hitting this fails the run, which keeps every candidate.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (2_500_000_000, 2_500_000_000))
    os.chdir(FARM)
    sys.path.insert(0, str(FARM))
    import pytest
    col = Collector(limits)
    # -x: once one candidate fails the commit needs a worker anyway, so stop paying for the rest
    code = pytest.main(["-q", "-x", "--tb=no", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly",
                        "--rootdir", str(FARM), *ids], plugins=[col])
    ok = sorted(col.passed - col.bad)
    os.write(w, json.dumps({"ok": ok, "code": int(code)}).encode())


class Verifier:
    def __init__(self, src, ms, file_of, id_of, apply):
        self.src, self.ms, self.file_of, self.id_of, self.apply = src, ms, file_of, id_of, apply
        self.spent = 0.0
        self.ready = False
        try:
            t = time.time()
            build_farm(src)
            _cap_pools()
            import pytest  # noqa: F401  (imported once, shared by every forked child)
            self.ready = True
            log(f"verify: farm ready in {time.time() - t:.2f}s")
        except Exception as e:  # verification is an optimisation; never fail the run
            log(f"verify: disabled ({type(e).__name__}: {e})")

    def est_cpu(self, nums):
        files = {self.file_of[n] for n in nums}
        return (FORK_OVERHEAD + sum(HEAVY_FILES.get(f, PER_FILE) for f in files)
                + RUN_FACTOR * sum(self.ms[n] for n in nums) / 1000)

    def worth_it(self, files, nums, cost_ms):
        """Verify when the expected saving beats the (inflated) CPU it costs. Only commits
        that touch nothing but sqlglot/*.py qualify; see P_PASS_CODE."""
        if not (self.ready and nums):
            return False
        if not all((fd["new"] or "").startswith("sqlglot/") and fd["new"].endswith(".py")
                   and fd["old"] == fd["new"] for fd in files):
            return False
        return P_PASS_CODE * cost_ms / 1000 > CPU_MULT * self.est_cpu(nums)

    def run(self, files, nums, timeout):
        """Numbers that did not cleanly pass, or None when verification could not decide."""
        ids = sorted(self.id_of[n] for n in nums)
        limits = {self.id_of[n]: max(2.0, 5 * self.ms[n] / 1000) for n in nums}
        overlay = None
        t0 = time.time()
        try:
            overlay = Overlay(self.src, self.apply, files)
            sys.stdout.flush()
            sys.stderr.flush()
            r, w = os.pipe()
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(r)
                    os.setpgid(0, 0)
                    _child(w, ids, limits)
                finally:
                    os._exit(0)
            os.close(w)
            buf, deadline = b"", t0 + timeout
            while True:
                left = deadline - time.time()
                if left <= 0:
                    log(f"verify: timeout after {timeout:.1f}s")
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
            try:  # spawned pool workers may outlive the child
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                pass
            if not buf:
                return None
            res = json.loads(buf)
            ok = set(res["ok"])
            return {n for n in nums if self.id_of[n] not in ok}
        except Exception as e:
            log(f"verify: error ({type(e).__name__}: {e})")
            return None
        finally:
            if overlay is not None:
                overlay.restore()
            self.spent += time.time() - t0

    def refine(self, files, nums, cost_ms):
        if not self.worth_it(files, nums, cost_ms) or self.spent > TIME_BUDGET:
            return nums, "kept"
        import resource
        est = self.est_cpu(nums)
        t = time.time()
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        res = self.run(files, nums, timeout=max(2.5, 4 * est + 1))  # a timeout only keeps tests
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = r1.ru_utime + r1.ru_stime - r0.ru_utime - r0.ru_stime
        tfiles = sorted({self.file_of[n].rsplit("/", 1)[-1] for n in nums})
        if res is None:
            return nums, "kept (verify failed)"
        return res, (f"verified {len(nums)}->{len(res)} wall {time.time() - t:.2f}s cpu {cpu:.2f}s "
                     f"(est {est:.2f}) {tfiles}")
