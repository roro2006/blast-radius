import sys, time, os
sys.path.insert(0, os.getcwd())
t0=time.process_time()
from tests.test_executor import TestExecutor, mp_execute, initializer
import tests.test_executor as te
from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs
from sqlglot import parse_one, find_tables, transpile
from sqlglot.executor import execute
print("import", time.process_time()-t0)
t0=time.process_time()
class C: pass
TestExecutor.setUpClass()
print("setup", time.process_time()-t0)
tabs=TestExecutor.tpcds_tables
tot=0
for i,(meta,sql,_) in enumerate(TestExecutor.tpcds_sqls):
    t=time.process_time()
    e=parse_one(sql)
    tp=time.process_time()-t
    if meta.get("execute"):
        tables={x.name: tabs[x.name] for x in find_tables(e)}
        r=execute(e, schema=TPCDS_SCHEMA, tables=tables)
        d=transpile(sql, write="duckdb")[0]
    dt=time.process_time()-t; tot+=dt
    print(i, meta.get("execute",False), round(tp,3), round(dt,3))
print("total", tot)
