#!/usr/bin/env python3
# test_nova_test.py — `nova test`: discovery of *_test.nova, assertions,
# per-file reporting, and exit codes.

import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOVA = os.path.join(ROOT, "nova")


def run(args, cwd):
    return subprocess.run([NOVA] + args, capture_output=True, text=True,
                          timeout=60, cwd=cwd)


def main():
    tmp = tempfile.mkdtemp(prefix="nova-test-test-")
    failures = []

    def check(name, cond, detail=""):
        if cond:
            print("  ok %s" % name)
        else:
            failures.append("%s %s" % (name, detail))

    # a passing suite: two files, one with test_ fns, one top-level asserts
    with open(os.path.join(tmp, "math_test.nova"), "w") as fh:
        fh.write('''
fn add(a: Int, b: Int) -> Int {
  return a + b
}

effect fn test_add() {
  assert(add(2, 3) == 5, "2+3")
}

effect fn test_add_zero() {
  assert(add(0, 0) == 0)
}
''')
    with open(os.path.join(tmp, "top_test.nova"), "w") as fh:
        fh.write('''
let x = 40 + 2
assert(x == 42, "top-level assert runs")
''')
    p = run(["test", tmp], cwd=ROOT)
    check("pass exit 0", p.returncode == 0, p.stdout + p.stderr)
    check("pass count", "3 passed, 0 failed, 2 files" in p.stdout,
          p.stdout)

    # a failing suite: exit 1, names the failing test
    with open(os.path.join(tmp, "bad_test.nova"), "w") as fh:
        fh.write('''
effect fn test_broken() {
  assert(1 == 2, "deliberate")
}
''')
    p = run(["test", tmp], cwd=ROOT)
    check("fail exit 1", p.returncode == 1, p.stdout)
    check("fail names test", "test_broken" in p.stdout and
          "deliberate" in p.stdout, p.stdout)

    # check errors fail the file too
    with open(os.path.join(tmp, "oops_test.nova"), "w") as fh:
        fh.write('effect fn test_x() {\n  assert(undefined_name)\n}\n')
    p = run(["test", tmp], cwd=ROOT)
    check("check-error fails", p.returncode == 1
          and "oops_test.nova" in p.stdout, p.stdout)

    # no test files: exit 0, clear message
    empty = tempfile.mkdtemp(prefix="nova-test-empty-")
    p = run(["test", empty], cwd=ROOT)
    check("empty dir", p.returncode == 0 and "no *_test.nova" in p.stdout,
          p.stdout)

    # recursion into subdirectories
    sub = os.path.join(tmp, "sub")
    os.makedirs(sub)
    with open(os.path.join(sub, "deep_test.nova"), "w") as fh:
        fh.write('effect fn test_deep() {\n  assert(true)\n}\n')
    os.remove(os.path.join(tmp, "bad_test.nova"))
    os.remove(os.path.join(tmp, "oops_test.nova"))
    p = run(["test", tmp], cwd=ROOT)
    check("recursive", p.returncode == 0 and "4 passed" in p.stdout,
          p.stdout)

    if failures:
        print("FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("nova test: all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
