# Blast Radius entry

Picks the sqlglot tests a patch can break, using maps built once from the pinned
commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`.

## How it selects

| Change | Tests selected |
| --- | --- |
| Same program (comments, whitespace, formatting) | none |
| Line inside a function | tests whose run executed that statement |
| Module- or class-level line (runs at import) | tests that execute lines reading the names it binds, limited to tests that instantiate that class |
| Docstring | adds `tests/test_docs.py` (doctests) |
| `sqlglot/typing/*.py` | adds `tests/test_typing.py` (it inspects that source) |
| Fixture under `tests/` | tests that opened that file |
| Test file | the edited tests, their class, or their module |
| Anything else (renames, new files, binary, config, shared test helpers) | every test |

Tests are packed longest-first onto 1–8 workers, keeping the count with the lowest
cost (busiest worker + 2 s per worker).

## Maps

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
