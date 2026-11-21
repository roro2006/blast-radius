#!/bin/sh
# Rebuild maps/heavy.json.gz (the per-query profile of test_execute_tpcds), about 2 minutes:
#
#   docker build --build-arg VERIFY=1 -t blast-radius-entry .
#   sh tools/heavy/build.sh
#
# Two runs under different hash seeds; a fingerprint that differs between them is dropped
# (that query then always keeps the test).
set -e
cd "$(dirname "$0")/../.."
ROOT="$(pwd -W 2>/dev/null || pwd)"
export MSYS_NO_PATHCONV=1
mkdir -p build/heavy
for seed in 0 1; do
  docker run --rm -e PYTHONHASHSEED=$seed -v "$ROOT/tools/heavy:/h:ro" -w /app/sqlglot \
    blast-radius-entry python /h/build_heavy.py > build/heavy/tpcds$seed.json
done
docker run --rm -v "$ROOT:/w" -w /w python:3.13-slim \
  python tools/heavy/pack.py build/heavy/tpcds0.json build/heavy/tpcds1.json > maps/heavy.json.gz
