#!/usr/bin/env bash
# Build the Nova website: vendor interpreter sources, extract playground
# examples, convert docs to HTML. Stdlib-only. Safe to re-run.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$(dirname "$HERE")"

# 1. vendor nova_lang/*.py for the Pyodide playground
mkdir -p "$HERE/nova_py"
for f in "$SRC"/nova_lang/*.py; do
  cp "$f" "$HERE/nova_py/$(basename "$f")"
done
python3 - "$HERE/nova_py" <<'EOF'
import json, os, sys
d = sys.argv[1]
files = sorted(f for f in os.listdir(d) if f.endswith(".py"))
with open(os.path.join(d, "files.json"), "w") as fh:
    json.dump(files, fh)
print("vendored", len(files), "python files")
EOF

# 2. extract playground examples (source + expected output) from examples/
python3 - "$SRC/examples" "$HERE/js/examples.json" <<'EOF'
import json, os, sys
srcdir, outpath = sys.argv[1], sys.argv[2]
want = {
    "hello": "Hello \u2014 values, inference, pure functions",
    "counter": "Counter \u2014 components and state",
    "api": "Result \u2014 errors without ceremony",
}
examples = []
for slug, desc in want.items():
    path = os.path.join(srcdir, slug + ".nova")
    with open(path) as fh:
        text = fh.read()
    # split source from the trailing // EXPECT: block
    if "\n// EXPECT:" in text:
        src, expect = text.split("\n// EXPECT:", 1)
        lines = []
        for ln in expect.strip().split("\n"):
            if "//" in ln:
                ln = ln.split("//", 1)[1]
                if ln.startswith(" "):
                    ln = ln[1:]
            lines.append(ln.rstrip())
        expected = "\n".join(lines).strip("\n")
    else:
        src, expected = text, ""
    examples.append({"slug": slug, "title": slug + ".nova",
                     "desc": desc, "source": src.rstrip() + "\n",
                     "expected": expected})
os.makedirs(os.path.dirname(outpath), exist_ok=True)
with open(outpath, "w") as fh:
    json.dump(examples, fh)
print("extracted", len(examples), "playground examples")
EOF

# 3. docs -> HTML
python3 "$HERE/build_docs.py"

echo "website build complete"
