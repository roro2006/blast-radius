set -e
OUT=/out PYTHONPATH=/sqlglot:/cov python -m pytest -q -n ${WORKERS:-4} -p cmpplugin -p no:cacheprovider tests > /out/cmp-pytest.log 2>&1 || true
tail -3 /out/cmp-pytest.log
