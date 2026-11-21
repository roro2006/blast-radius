"""sqlglot.executor.execute() in two steps, so the optimized tree can be compared on its own.

Mirrors execute() at the pinned commit for a non-empty schema and no dialect. The selector
treats any change to execute() itself as a change to every query (see SPLIT_FUNCS there).
"""
from sqlglot.errors import ExecuteError
from sqlglot.executor import ensure_tables
from sqlglot.executor.python import PythonExecutor
from sqlglot.optimizer import optimize
from sqlglot.planner import Plan
from sqlglot.schema import ensure_schema


def optimize_step(expression, tables, schema=None):
    if schema is None:
        from tests.helpers import TPCDS_SCHEMA as schema
    tables_ = ensure_tables(tables, dialect=None)
    schema = ensure_schema(schema, dialect=None)
    if tables_.supported_table_args and tables_.supported_table_args != schema.supported_table_args:
        raise ExecuteError("Tables must support the same table args as schema")
    expression = optimize(expression, schema, leave_tables_isolated=True, dialect=None)
    expression.sql(pretty=True)  # execute() logs it (argument built eagerly)
    return expression, tables_


def run_step(expression, tables_):
    return PythonExecutor(tables=tables_).execute(Plan(expression))


def tree_fp(node):
    """Structure of a parsed tree, read through .args only (no sqlglot code runs)."""
    import hashlib

    out = []

    def walk(x):
        args = getattr(x, "args", None)
        if isinstance(args, dict) and hasattr(x, "comments"):
            out.append("(" + type(x).__name__)
            for k in sorted(args):
                out.append(k)
                walk(args[k])
            out.append(repr(getattr(x, "comments", None)))
            meta = getattr(x, "_meta", None)  # positions, optimizer flags (nonnull, FINAL, ...)
            if meta:
                out.append("{")
                for k in sorted(meta, key=str):
                    out.append(str(k))
                    walk(meta[k])
                out.append("}")
            t = getattr(x, "_type", None)
            if t is not None:
                walk(t)
            out.append(")")
        elif isinstance(x, list):
            out.append("[")
            for v in x:
                walk(v)
            out.append("]")
        else:
            out.append(type(x).__name__ + ":" + repr(x))

    walk(node)
    return hashlib.sha1("\x00".join(out).encode()).hexdigest()


def rows_fp(table):
    import hashlib

    cols = list(table.columns)
    rows = [tuple(r) for r in table.rows]
    return hashlib.sha1(repr((cols, rows)).encode()).hexdigest()
