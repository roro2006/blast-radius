import coverage, json, os, sys
root, outd = sys.argv[1], sys.argv[2]
d = coverage.CoverageData(os.path.join(outd, ".coverage")); d.read()
out = {}
for f in d.measured_files():
    rel = os.path.relpath(f, root).replace(os.sep, "/")
    m = d.contexts_by_lineno(f)
    out[rel] = {str(l): sorted(c for c in cs if c) for l, cs in m.items()}
json.dump(out, open(os.path.join(outd, "linemap.json"), "w"))
print("files", len(out), "contexts", len(d.measured_contexts()))
