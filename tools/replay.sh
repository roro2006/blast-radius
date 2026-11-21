#!/bin/sh
# Replay the sample commits under the scorer's limits and report what the board would add up:
# simulated seconds, misses, and the CPU of every process the image ran (make sample does not
# measure CPU). Needs only Docker; scoring runs in a python container.
#
#   sh tools/replay.sh [--no-build] [extra docker run flags, e.g. --env VERIFY=0]
set -e
cd "$(dirname "$0")/.."
ROOT="$(pwd -W 2>/dev/null || pwd)"
IMAGE=${IMAGE:-blast-radius-static}
if [ "$1" = "--no-build" ]; then shift; else
  docker build --quiet --platform linux/amd64 --tag "$IMAGE" . > /dev/null
fi
export MSYS_NO_PATHCONV=1
docker run --rm --platform linux/amd64 --network none --cpus 2 --memory 4096m \
  --memory-swap 4096m --pids-limit 256 --read-only --tmpfs /tmp:rw,size=64m,mode=1777 \
  --cap-drop ALL --security-opt no-new-privileges --user 65534:65534 --env HOME=/tmp "$@" \
  --volume "$ROOT/sample/commits:/data/commits:ro" --volume "$ROOT/sample/tests.json:/data/tests.json:ro" \
  "$IMAGE" python -c "
import resource, subprocess, sys, time
t = time.time()
subprocess.run(['python', '-s', '/app/selector.py'])
r = resource.getrusage(resource.RUSAGE_CHILDREN)
print(f'CPU {r.ru_utime + r.ru_stime:.2f}s wall {time.time() - t:.1f}s', file=sys.stderr)
" > "$ROOT/output.jsonl"
docker run --rm -v "$ROOT:/w:ro" -w /w python:3.13-slim \
  python score.py sample sample/answers.json output.jsonl | grep -E "Simulated|Misses"
