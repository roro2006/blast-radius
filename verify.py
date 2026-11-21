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
POOL_CTX = os.environ.get("VERIFY_POOL_CTX", "fork") or None

# CPU model (seconds), fitted on the sample set in the scoring image.
FORK_OVERHEAD = 0.12     # child: pytest session + sqlglot import
PER_FILE = 0.03          # importing / collecting one dialect test module
HEAVY_FILES = {          # modules with costly imports or class setup (duckdb, pools)
    "tests/test_optimizer.py": 0.6,
    "tests/test_executor.py": 0.6,
}
RUN_FACTOR = 0.35        # recorded test seconds -> our CPU seconds (recorded runs were slower)
# The board's CPU figure ran ~3x our local measurement for the plain selector; plan with 2x.
CPU_MULT = float(os.environ.get("VERIFY_CPU_MULT", "2"))
# Chance a source-code commit's candidates all pass (5 of 7 on the sample). Fixture and
# test-file edits nearly always break their tests (0 of 9), so they are never verified.
P_PASS_CODE = 0.6
TIME_BUDGET = float(os.environ.get("VERIFY_BUDGET", "100"))  # wall seconds for all verification
BASELINE = os.environ.get("BASELINE", "/app/baseline_fail.json")  # written at build time


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
    """pytest plugin: which node ids cleanly passed. Stops the session at the first failure
    of a test that passes on the unpatched tree (once one candidate really fails the commit
    needs a worker anyway); tests already failing here before the patch never stop it."""

    def __init__(self, limits, baseline=()):
        self.limits, self.bad, self.passed = limits, set(), set()
        self.baseline, self.session = set(baseline), None
        self.why = {}

    def pytest_sessionstart(self, session):
        self.session = session

    def pytest_runtest_logreport(self, report):
        nid = report.nodeid
        if report.failed or report.skipped:
            self.bad.add(nid)
            self.why.setdefault(nid, f"{report.when} {report.outcome}: {str(report.longrepr)[-300:]}")
            if report.failed and nid not in self.baseline and self.session is not None:
                self.session.shouldstop = "candidate failed"
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
            ctx = mp.get_context(POOL_CTX) if POOL_CTX else mp_context
            super().__init__(min(max_workers or POOL_CAP, POOL_CAP), ctx, *a, **k)

    cf.ProcessPoolExecutor = CappedPool


def _child(w, ids, limits, baseline):
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)  # stdout must carry only answer lines
    os.dup2(devnull, 2)
    # A runaway patch must not let the container's OOM killer take the selector with it:
    # make this process its first choice (a killed child keeps every candidate). An address
    # space limit would break duckdb, which reserves large mappings.
    try:
        with open("/proc/self/oom_score_adj", "w") as f:
            f.write("1000")
    except OSError:
        pass
    os.chdir(FARM)
    sys.path.insert(0, str(FARM))
    import pytest
    col = Collector(limits, baseline)
    code = pytest.main(["-q", "--tb=no", "-p", "no:cacheprovider", "-p", "no:xdist", "-p", "no:randomly",
                        "--rootdir", str(FARM), *ids], plugins=[col])
    ok = sorted(col.passed - col.bad)
    why = dict(list(col.why.items())[:5]) if os.environ.get("VERIFY_DEBUG") else {}
    os.write(w, json.dumps({"ok": ok, "code": int(code), "why": why}).encode())


class Verifier:
    def __init__(self, src, ms, file_of, id_of, apply):
        self.src, self.ms, self.file_of, self.id_of, self.apply = src, ms, file_of, id_of, apply
        self.spent = 0.0
        self.ready = False
        try:
            self.baseline = set(json.loads(Path(BASELINE).read_text()))
        except (OSError, ValueError):
            self.baseline = set()
        log(f"verify: {len(self.baseline)} tests already fail before any patch")
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
                    _child(w, ids, limits, self.baseline)
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
            for k, v in res.get("why", {}).items():
                log(f"  kept {k}: {v}")
            ok = set(res["ok"])
            # tests failing here before the patch are never verified away
            return {n for n in nums if self.id_of[n] not in ok or self.id_of[n] in self.baseline}
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


def baseline(src, out, workers="4"):
    """Build time: run the whole suite on the unpatched tree and record what fails here."""
    import pytest
    build_farm(src)
    os.chdir(FARM)
    sys.path.insert(0, str(FARM))
    col = Collector({})
    col.pytest_sessionstart = lambda session: None  # never stop: record every failure
    code = pytest.main(["-q", "--tb=no", "-p", "no:cacheprovider", "-n", workers, "--timeout", "300",
                 "--rootdir", str(FARM), "tests"], plugins=[col])
    if code not in (0, 1):  # 0 all passed, 1 some failed; anything else is a broken run
        sys.exit(f"baseline: pytest exited {code}")
    failed = sorted(set(col.why) - {k for k, v in col.why.items() if " skipped:" in v})
    Path(out).write_text(json.dumps(failed))
    print(f"baseline: {len(failed)} failing: {failed[:10]}")


if __name__ == "__main__" and sys.argv[1:2] == ["baseline"]:
    baseline(*sys.argv[2:])
