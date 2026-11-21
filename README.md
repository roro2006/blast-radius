# Blast Radius entry

Picks the sqlglot tests a patch can break, using maps built once from the pinned
commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## How it selects

| Change | Tests selected |
| --- | --- |
| Same program (comments, whitespace, formatting) | none |
| Only annotations / `if TYPE_CHECKING:` blocks, in a `from __future__ import annotations` module that never reads annotations at run time | none |
| Only the order of undecorated methods in a class body | none |
| Only lambda bodies | tests whose run executed those bodies |
| One relational operator (`<` `<=` `>` `>=` `==` `!=`) | tests where the two operators ever disagreed on the values they saw (value profile) |
| One `and` ↔ `or` in a condition | tests where the two sides ever differed in truthiness, or the right side was not evaluated |
| Entries of one class- or module-level dict literal | tests that read a changed key's value (or used the dict whole, or probed an added/removed key) |
| Line inside a function | tests whose run executed that statement |
| Module- or class-level line (runs at import) | tests that execute lines reading the names it binds, limited to tests that instantiate that class |
| Docstring | adds `tests/test_docs.py` (doctests) |
| `sqlglot/typing/*.py` | adds `tests/test_typing.py` (it inspects that source) |
| Fixture under `tests/` | tests that opened that file |
| Test file | the edited tests, their class, or their module |
| New `sqlglot/` or `tests/` module whose name appears nowhere in the pinned tree | none itself; each other patched file that mentions it adds every test that runs any of its functions |
| Anything else (renames, deletions, binary, config, shared test helpers) | every test |

Each of the value-profile rules falls back to the rows above when the profile cannot decide
(the value was used at import, operands were not plain values, an operand raised, ...).

Optional (`docker build --build-arg VERIFY=1`, off by default): for commits that touch only
`sqlglot/*.py`, `verify.py` runs the candidate tests on the patched source when the expected
saving beats three times their estimated CPU, and keeps only the tests that did not cleanly
pass (fail, error, skip, slow, timeout, crash and not-collected all keep a test). It mirrors the
pinned tree into `/tmp/w` with symlinks, overwrites the patched files, and forks a child per
commit from a parent that already imported pytest. On the practice board it cost ~2.5 s CPU
and saved ~0.01 s, hence off.

### The two slowest tests (`heavy.py`)

`test_max_nodes` (~29 s) and `test_execute_tpcds` (~21 s) cover thousands of core parser,
optimizer and executor lines, so the maps pick them for most core edits, though few of those
edits break them. When dropping them would lower a commit's cost by more than the check's
CPU (counted at three times our local CPU, plus a margin), a forked child imports the
patched sqlglot from a symlinked mirror of the pinned tree (`/tmp/w`, bytecode precompiled
at build time) and checks them directly:

* **test_max_nodes** only asserts that a few parses succeed or raise the max-nodes error with
  the right message. The child runs the same assertions with the wide query and both limits
  divided by 50 (same nesting depth, same nodes-to-limit ratios): about 0.2 s instead of 29.
* **test_execute_tpcds** parses 99 TPC-DS queries, executes 57 on the Python executor and
  compares each with duckdb's result for `transpile(sql, write="duckdb")`.
  `maps/heavy.json.gz` (`tools/heavy/`) holds, from the pinned tree, a fingerprint of every
  step's output (parse tree, optimized tree, result rows, duckdb SQL) and which lines each
  query's optimize and execute steps ran, recorded per query in a fresh process so lazily
  built caches count for every query that needs them. The child re-parses and re-transpiles
  every query, optimizes only the queries that ran a changed function (or whose parse
  changed), and executes only those whose optimized tree changed or whose execute step
  ran a changed function. If every fingerprint matches, every subtest sees exactly what it
  saw on the pinned tree, where the test passes.

The tpcds check applies only when the patch edits nothing but function bodies of existing
`sqlglot/*.py` files, none of which ran before the first query (imports, class setup,
pickling tables for the pool). Any mismatch, exception, timeout or budget overrun keeps the
test. Rebuild the map with `sh tools/heavy/build.sh`.

Tests are packed longest-first onto 1–8 workers, keeping the count with the lowest
cost (busiest worker + 2 s per worker).

## Maps

`maps/valprof/` is the value profile (`tools/cov/valprof.py`, rebuilt with
`sh tools/cov/valprof.sh`): one run of the suite with sqlglot rewritten on import so that, per
test, it records the outcome of all six relational operators at every comparison between plain
values, the truthiness of both sides of every two-operand `and`/`or` condition, and which keys
of every class- or module-level dict literal were read (and whether the dict was copied,
iterated or mutated). One shard per source file; the selector loads only the touched ones.

`maps/maps.json.gz` holds three maps, each "key → set of tests":

* **lines**: per-test line coverage (`coverage.py` with one context per test).
* **fixtures**: non-Python files under `tests/` each test opens.
* **uses**: sqlglot classes (by MRO) each test instantiates.

While the maps are built, process pools run in-process but still pickle their
arguments and results, so work the optimizer and executor tests send to worker
processes is traced. Setup-phase coverage (`setUpClass`, class and module fixtures)
is credited to the whole class or module.

Rebuild with `make maps` (Docker, about 6 minutes). The selector itself never runs tests.

## Commands

```sh
make sample   # build the image, run on the 30 sample commits, score
make local    # same without Docker (needs build/sqlglot at the pinned commit)
make maps     # rebuild maps/maps.json.gz
```
