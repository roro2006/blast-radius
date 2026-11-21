"""Static import graph of the pinned sqlglot package -> maps/imports.json.gz.

    python tools/build_imports.py build/sqlglot maps/imports.json.gz

{"eager": [files sqlglot/__init__.py imports at import time, transitively],
 "importers": {file: [files that import it anywhere, directly]}}
Imports under `if TYPE_CHECKING:` never run and are left out.
"""
import ast
import gzip
import json
import sys
from pathlib import Path

src, out = Path(sys.argv[1]), sys.argv[2]
files = {p.relative_to(src).as_posix() for p in (src / "sqlglot").rglob("*.py")}


def mod_file(mod):
    p = mod.replace(".", "/")
    for c in (p + ".py", p + "/__init__.py"):
        if c in files:
            return c
    return None


def targets(f, node):
    pkg = f[:-3].split("/")
    pkg = pkg[:-1]  # the module's package (for __init__ the package itself)
    res = set()
    if isinstance(node, ast.Import):
        for a in node.names:
            parts = a.name.split(".")
            for i in range(1, len(parts) + 1):  # `import a.b.c` imports a, a.b, a.b.c
                m = mod_file(".".join(parts[:i]))
                if m:
                    res.add(m)
    else:
        if node.level:
            base = pkg[:len(pkg) - node.level + 1]
            mod = ".".join(base + ([node.module] if node.module else []))
        else:
            mod = node.module or ""
        parts = mod.split(".")
        for i in range(1, len(parts) + 1):
            m = mod_file(".".join(parts[:i]))
            if m:
                res.add(m)
        for a in node.names:  # `from pkg import submodule`
            m = mod_file(f"{mod}.{a.name}")
            if m:
                res.add(m)
    return res


def is_type_checking(test):
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or \
        (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")


top, every = {}, {}
for f in sorted(files):
    tree = ast.parse((src / f).read_text(encoding="utf8"))
    top[f], every[f] = set(), set()

    def walk(node, in_func):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.If) and is_type_checking(c.test):
                for e in c.orelse:
                    walk_one(e, in_func)
                continue
            walk_one(c, in_func)

    def walk_one(c, in_func):
        if isinstance(c, (ast.Import, ast.ImportFrom)):
            t = targets(f, c)
            every[f] |= t
            if not in_func:
                top[f] |= t
        walk(c, in_func or isinstance(c, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))

    walk(tree, False)

eager, todo = set(), ["sqlglot/__init__.py"]
while todo:
    f = todo.pop()
    if f in eager:
        continue
    eager.add(f)
    # importing a.b.c runs a/__init__ and a/b/__init__ first
    parts = f.split("/")
    for i in range(1, len(parts)):
        init = "/".join(parts[:i]) + "/__init__.py"
        if init in files:
            todo.append(init)
    todo.extend(top[f])

importers = {}
for f, ts in every.items():
    for t in ts:
        if t != f:
            importers.setdefault(t, set()).add(f)
# a package's __init__ runs whenever any of its submodules is imported
for f in files:
    parts = f.split("/")
    for i in range(1, len(parts)):
        init = "/".join(parts[:i]) + "/__init__.py"
        if init in files and init != f:
            importers.setdefault(init, set()).add(f)
doc = {"eager": sorted(eager), "importers": {k: sorted(v) for k, v in importers.items()}}
with gzip.open(out, "wt") as fh:
    json.dump(doc, fh, separators=(",", ":"))
print(f"{len(files)} files, {len(eager)} eager from sqlglot/__init__", file=sys.stderr)
