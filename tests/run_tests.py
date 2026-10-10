#!/usr/bin/env python3
# run_tests.py — Nova's v1 test suite.
#
# 1. Every example under examples/ is executed via `nova run` and its exact
#    stdout is asserted against the `// EXPECT:` block in the file.
# 2. Every file under tests/errors/ is type-checked via `nova check` and
#    must produce the `// ERROR: <code>` diagnostic.
#
# All green is the v1 bar. Run: python3 tests/run_tests.py  (or ./nova test
# once the CLI alias lands).

import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NOVA = os.path.join(ROOT, "nova")


def run_nova(args, env=None):
    e = dict(os.environ)
    if env:
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
    p = subprocess.run([NOVA] + args, cwd=ROOT, capture_output=True,
                       text=True, env=e, timeout=120)
    return p


def expected_output(path):
    """Parse the `// EXPECT:` block: subsequent `// ...` lines."""
    lines = []
    with open(path) as f:
        src = f.read().splitlines()
    in_block = False
    for line in src:
        s = line.strip()
        if s.startswith("// EXPECT"):
            in_block = True
            continue
        if in_block:
            if s.startswith("//"):
                # strip exactly one space: the comment marker's own
                content = s[2:]
                if content.startswith(" "):
                    content = content[1:]
                lines.append(content)
            elif s == "":
                continue
            else:
                break
    return "\n".join(lines)


def expected_error(path):
    with open(path) as f:
        for line in f:
            s = line.strip()
            if s.startswith("// ERROR:"):
                return s[len("// ERROR:"):].strip()
    return None


def test_examples():
    failures = []
    examples = sorted(
        f for f in os.listdir(os.path.join(ROOT, "examples"))
        if f.endswith(".nova"))
    # deploy-demo is a directory; nova_helpers.nova is a helper module.
    examples = [f for f in examples if f != "nova_helpers.nova"]
    demo_main = os.path.join(ROOT, "examples", "deploy-demo", "main.nova")
    if os.path.isfile(demo_main):
        examples.append(os.path.join("deploy-demo", "main.nova"))
    for rel in examples:
        path = os.path.join(ROOT, "examples", rel)
        name = "examples/" + rel
        want = expected_output(path)
        # deterministic threading for the suite; scrub secrets for demo
        env = {"NOVA_THREADING": "single"}
        for k in list(os.environ):
            if k.startswith("NOVA_SECRET_"):
                env[k] = None
        p = run_nova(["run", path], env=env)
        got = p.stdout
        # nova run prints each output line with print(); normalize trailing NL
        if got.endswith("\n"):
            got = got[:-1]
        if p.returncode != 0:
            failures.append((name, "non-zero exit %d\n%s"
                             % (p.returncode, p.stderr.strip()[:500])))
        elif got != want:
            failures.append((name, "output mismatch\n--- want ---\n%s\n"
                                   "--- got ---\n%s" % (want, got)))
        else:
            print("ok   %s" % name)
    return failures


def test_errors():
    failures = []
    errdir = os.path.join(ROOT, "tests", "errors")
    files = sorted(f for f in os.listdir(errdir) if f.endswith(".nova"))
    for f in files:
        path = os.path.join(errdir, f)
        want = expected_error(path)
        if not want:
            failures.append(("errors/" + f, "missing // ERROR: <code>"))
            continue
        p = run_nova(["check", path])
        try:
            diags = json.loads(p.stdout)
        except Exception:
            failures.append(("errors/" + f,
                             "check did not emit JSON: " + p.stdout[:200]))
            continue
        codes = [d.get("code") for d in diags]
        if want in codes:
            print("ok   errors/%s [%s]" % (f, want))
        else:
            failures.append(("errors/" + f,
                             "want %s, got %s" % (want, codes)))
    return failures


def test_python():
    failures = []
    testdir = os.path.join(ROOT, "tests")
    files = sorted(f for f in os.listdir(testdir)
                   if f.startswith("test_") and f.endswith(".py"))
    for f in files:
        path = os.path.join(testdir, f)
        p = subprocess.run([sys.executable, path], cwd=ROOT,
                           capture_output=True, text=True, timeout=120)
        if p.returncode != 0:
            failures.append(("tests/" + f, "exit %d\n%s"
                             % (p.returncode,
                                (p.stderr or p.stdout)[:800])))
        else:
            print("ok   tests/%s" % f)
    return failures


def main():
    print("== examples ==")
    failures = test_examples()
    print("== error diagnostics ==")
    failures += test_errors()
    print("== python unit tests ==")
    failures += test_python()
    print()
    if failures:
        print("%d FAILURE(S):" % len(failures))
        for name, msg in failures:
            print("FAIL %s\n%s\n" % (name, msg))
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
