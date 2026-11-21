set -e
printf '[run]\nsource = sqlglot\ndata_file = /out/.coverage\n' > /tmp/.coveragerc
OUT=/out PYTHONPATH=/sqlglot:/cov coverage run --rcfile=/tmp/.coveragerc -m pytest -q -p ctxplugin -p no:cacheprovider tests > /out/pytest.log 2>&1 || true
tail -3 /out/pytest.log
python /cov/dump.py /sqlglot /out
