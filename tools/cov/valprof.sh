#!/bin/sh
# Rebuild maps/valprof/ (the value profile) from the pinned commit, about 2 minutes:
#
#   docker build --build-arg VERIFY=1 -t blast-radius-entry .
#   sh tools/cov/valprof.sh
#
# Runs the whole suite once with tools/cov/valprof.py rewriting sqlglot on import, then
# merges the per-worker records with tools/build_valprof.py. Every test must still pass
# under the instrumentation (the probes return what the original expressions return).
set -e
cd "$(dirname "$0")/../.."
ROOT="$(pwd -W 2>/dev/null || pwd)"
export MSYS_NO_PATHCONV=1
mkdir -p build/vp && rm -f build/vp/*.json
docker run --rm --cpus 4 -v "$ROOT/tools/cov:/cov:ro" -v "$ROOT/build/vp:/out" -e OUT=/out \
  -e SQLGLOT_ROOT=/app/sqlglot -e PYTHONPATH=/app/sqlglot:/cov -w /app/sqlglot blast-radius-entry \
  python -m pytest -q -n 4 -p valprof -p no:cacheprovider --timeout 600 tests | tail -1
docker run --rm -v "$ROOT:/w" -w /w python:3.13-slim \
  python tools/build_valprof.py build/vp sample/tests.json maps/valprof
