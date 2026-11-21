"""TPC-DS fixture rows exactly as TestExecutor.setUpClass builds them, pickled as plain tuples
(no sqlglot classes), so the runtime check skips parsing 3.5 MB of csv. Run at image build:

  python tables.py /app/sqlglot /app/heavy_tables.pkl
"""
import ast
import csv
import gzip
import pickle
import sys

src, out = sys.argv[1], sys.argv[2]
sys.path.insert(0, src)
from tests.helpers import TPCDS_SCHEMA  # noqa: E402


def open_file(file_name):
    with open(file_name, "rb") as f:
        gzipped = f.read(2) == b"\x1f\x8b"
    if gzipped:
        return gzip.open(file_name, "rt", newline="")
    return open(file_name, encoding="utf-8", newline="")


data = {}
for table in TPCDS_SCHEMA:
    reader = csv.reader(open_file(f"{src}/tests/fixtures/optimizer/tpc-ds/{table}.csv.gz"), delimiter="|")
    rows, ctypes = [], []
    next(reader)
    for row in reader:
        if not ctypes:
            for v in row:
                try:
                    ctypes.append(type(ast.literal_eval(v)))
                except (ValueError, SyntaxError):
                    ctypes.append(str)
        rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
    data[table] = pickle.dumps(rows, protocol=pickle.HIGHEST_PROTOCOL)  # unpickled per table on use
with open(out, "wb") as f:
    pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
