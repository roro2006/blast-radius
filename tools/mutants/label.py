"""Label patches by running the full sqlglot suite on each (inside the map-build image).

    docker run ... -v <patch_dir>:/patches:ro -v <out_dir>:/labels br-maps \
        python /mut/label.py /patches /labels [workers]

Writes /labels/<id>.json = {"failing": [nodeid, ...], "seconds": wall}. Already-labeled
ids are skipped, so the run can be resumed. SHARD=i/n labels every n-th patch from i.
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/sel")
from selector import apply, parse_patch  # noqa: E402

patches, labels = Path(sys.argv[1]), Path(sys.argv[2])
workers = sys.argv[3] if len(sys.argv) > 3 else "8"
work = Path("/tmp/work")

shard, nshards = map(int, os.environ.get("SHARD", "0/1").split("/"))
for k, p in enumerate(sorted(patches.glob("*.patch"))):
    if k % nshards != shard:
        continue
    out = labels / f"{p.stem}.json"
    if out.exists():
        continue
    t0 = time.time()
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree("/sqlglot", work, symlinks=True)
    for fd in parse_patch(p.read_text(encoding="utf8")):
        f = fd["new"] or fd["old"]
        target = work / f
        if fd["new"] is None:
            target.unlink()
            continue
        old = target.read_text(encoding="utf8") if fd["old"] else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(apply(old, fd["hunks"]), encoding="utf8")
    res = work / "failed.json"
    env = dict(os.environ, PYTHONPATH=f"{work}:/mut", FAILED_OUT=str(res), SKIP_INTEGRATION="0",
               TEST_DURATIONS="/sel/sample/tests.json")
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-n", workers, "-p", "no:cacheprovider",
                    "-p", "failplugin", "--dist", "loadscope", "--timeout", "300", "tests"],
                   cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    failing = json.loads(res.read_text()) if res.exists() else ["<run produced no report>"]
    out.write_text(json.dumps({"failing": failing, "seconds": round(time.time() - t0, 1)}))
    print(f"{p.stem}: {len(failing)} failing in {time.time() - t0:.0f}s", flush=True)
