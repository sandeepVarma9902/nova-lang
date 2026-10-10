# cli.py — the `nova` command line: run, check, fmt, lock, migrate,
# build, deploy.
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time


def _load(path):
    from .lexer import Lexer, LexError
    from .parser import Parser, ParseError, JsxError
    with open(path) as f:
        source = f.read()
    try:
        toks = Lexer(source, path).tokenize()
    except LexError as e:
        return None, [_diag("E_LEXER", str(e), e.line, e.col, "")]
    try:
        prog = Parser(toks, source, path).parse_program()
    except ParseError as e:
        return None, [_diag("E_SYNTAX", str(e), e.line, e.col, e.hint)]
    except JsxError as e:
        return None, [_diag("E_SYNTAX", "JSX: " + str(e), e.line, e.col, "")]
    return (source, prog), []


def _diag(code, message, line, col, hint):
    d = {"code": code, "message": message, "line": line, "col": col}
    if hint:
        d["hint"] = hint
    return d


def cmd_check(path):
    from .checker import check_source
    with open(path) as f:
        source = f.read()
    _, diags = check_source(source, path, os.path.dirname(
        os.path.abspath(path)), is_entry=True)
    print(json.dumps([d.to_dict() for d in diags], indent=2))
    return 1 if diags else 0


def cmd_run(path, locked=False):
    from .checker import check_source
    from .interpreter import Interpreter, NovaError
    from .manifest import resolve_threading, ManifestError
    if path.endswith(".nvb"):
        # Bytecode: execute on NovaVM (no type-check; it was checked at
        # compile time).
        from .vm import run_bytecode, VMError
        try:
            for line in run_bytecode(path):
                print(line)
        except VMError as e:
            sys.stderr.write(json.dumps([_diag(e.code, str(e), e.line, 0,
                                               "")]) + "\n")
            return 1
        return 0
    if locked and not _verify_lock(path):
        return 1
    with open(path) as f:
        source = f.read()
    basedir = os.path.dirname(os.path.abspath(path))
    _, diags = check_source(source, path, basedir, is_entry=True)
    if diags:
        sys.stderr.write(json.dumps([d.to_dict() for d in diags], indent=2)
                         + "\n")
        return 1
    try:
        threading = resolve_threading(path)
    except ManifestError as e:
        sys.stderr.write(json.dumps([_diag("E_MANIFEST", str(e), 0, 0,
                                           "")]) + "\n")
        return 1
    # parse again for the interpreter (check_source already validated it)
    from .lexer import Lexer
    from .parser import Parser
    toks = Lexer(source, path).tokenize()
    prog = Parser(toks, source, path).parse_program()
    interp = Interpreter(basedir, is_entry=True, threading=threading)
    try:
        output = interp.run(prog, path)
    except NovaError as e:
        sys.stderr.write(json.dumps([_diag(e.code, str(e), e.line, e.col,
                                           e.hint)], indent=2) + "\n")
        return 1
    for line in output:
        print(line)
    return 0


def cmd_fmt(path):
    from .lexer import Lexer, LexError
    from .parser import Parser, ParseError, JsxError
    from .formatter import Formatter
    with open(path) as f:
        source = f.read()
    try:
        toks = Lexer(source, path).tokenize()
        prog = Parser(toks, source, path).parse_program()
    except (LexError, ParseError, JsxError) as e:
        line = getattr(e, "line", 0)
        col = getattr(e, "col", 0)
        code = "E_LEXER" if isinstance(e, LexError) else "E_SYNTAX"
        sys.stderr.write(json.dumps([_diag(code, str(e), line, col,
                                           getattr(e, "hint", ""))]) + "\n")
        return 1
    sys.stdout.write(Formatter().fmt_program(prog))
    return 0


def _transitive_imports(path, seen=None):
    from .lexer import Lexer
    from .parser import Parser
    from . import ast as A
    if seen is None:
        seen = set()
    real = os.path.realpath(path)
    if real in seen:
        return []
    seen.add(real)
    out = [real]
    with open(path) as f:
        source = f.read()
    prog = Parser(Lexer(source, path).tokenize(), source, path).parse_program()
    basedir = os.path.dirname(real)
    from .modpath import resolve
    for stmt in prog.stmts:
        if isinstance(stmt, A.Import):
            ip = resolve(stmt.name, basedir)
            if ip is not None:
                out.extend(_transitive_imports(ip, seen))
    return out


def cmd_lock(path):
    # nova lock: pin every transitive import by sha256 into nova.lock.
    files = _transitive_imports(os.path.abspath(path))
    lock = {"entry": os.path.abspath(path), "modules": {}}
    for f in files:
        with open(f, "rb") as fh:
            lock["modules"][f] = hashlib.sha256(fh.read()).hexdigest()
    lock_path = os.path.join(os.path.dirname(os.path.abspath(path)),
                             "nova.lock")
    with open(lock_path, "w") as fh:
        json.dump(lock, fh, indent=2)
    print("locked %d module(s) -> %s" % (len(files), lock_path))
    return 0


def _verify_lock(path):
    lock_path = os.path.join(os.path.dirname(os.path.abspath(path)),
                             "nova.lock")
    if not os.path.isfile(lock_path):
        sys.stderr.write(json.dumps([_diag(
            "E_LOCK", "no nova.lock found; run `nova lock` first",
            0, 0, "")]) + "\n")
        return False
    with open(lock_path) as fh:
        lock = json.load(fh)
    ok = True
    for f, pinned in lock.get("modules", {}).items():
        if not os.path.isfile(f):
            sys.stderr.write("lock mismatch: missing %s\n" % f)
            ok = False
            continue
        with open(f, "rb") as fh:
            actual = hashlib.sha256(fh.read()).hexdigest()
        if actual != pinned:
            sys.stderr.write("lock mismatch: %s changed since locking\n" % f)
            ok = False
    if not ok:
        sys.stderr.write("supply-chain check failed: modules differ from "
                         "nova.lock\n")
    return ok


def cmd_migrate(args):
    from .migrate import migrate
    flavor = None
    path = None
    i = 0
    while i < len(args):
        if args[i] == "--from" and i + 1 < len(args):
            flavor = args[i + 1]
            i += 2
        else:
            path = args[i]
            i += 1
    if flavor not in ("java", "jsx") or not path:
        sys.stderr.write("usage: nova migrate --from java|jsx <file>\n")
        return 2
    with open(path) as f:
        source = f.read()
    sys.stderr.write("EXPERIMENTAL: mechanical subset only — "
                     "review all output.\n")
    out, warnings = migrate(source, os.path.basename(path), flavor)
    sys.stdout.write(out)
    if warnings:
        sys.stderr.write("\n%d warning(s):\n" % len(warnings))
        for w in warnings:
            sys.stderr.write("  ! %s\n" % w)
    return 0


USAGE = """usage: nova <command> [options] <file>
  nova run [--locked] <file.nova|.nvb>  run a Nova program (.nvb = bytecode)
  nova check <file.nova>            type-check; diagnostics as JSON
  nova fmt <file.nova>             print canonical formatting
  nova lock <file.nova>             pin imports by hash into nova.lock
  nova migrate --from java|jsx <f>  EXPERIMENTAL mechanical migrator
  nova build [--manifest app.nova] [--emit-bytecode]
                                type-check + stage dist/ (+ Dockerfile);
                                --emit-bytecode also writes dist/<entry>.nvb
  nova dom <file.nova> -o <out.html>
                                EXPERIMENTAL: components -> self-contained HTML
  nova eval <suite.json> [--provider mock|http] [--out report.json]
                                eval harness: prompts -> predicates -> JSON
  nova lsp                      EXPERIMENTAL LSP server (JSON-RPC over stdio)
  nova db pull --dsn sqlite:///app.db [--out models.nova]
                                inspect schema -> Nova model types
  nova data export --from sqlite:///app.db --query "SELECT ..." --format jsonl
                                export fine-tuning datasets (see docs/ai-training.md)
  nova test [dir]               discover *_test.nova, run assertions, report
  nova deploy [--target local|docker|aws|gcp] [--env NAME] [--manifest app.nova]
"""


def cmd_build(args):
    from .manifest import load_manifest, ManifestError
    man_path = None
    emit_bytecode = "--emit-bytecode" in args
    i = 0
    while i < len(args):
        if args[i] == "--manifest" and i + 1 < len(args):
            man_path = args[i + 1]
            i += 2
        else:
            i += 1
    if man_path is None:
        man_path = os.path.join(os.getcwd(), "app.nova")
    if not os.path.isfile(man_path):
        sys.stderr.write("no app.nova found (pass --manifest <path>)\n")
        return 2
    try:
        man = load_manifest(man_path)
    except ManifestError as e:
        sys.stderr.write("app.nova: %s\n" % e)
        return 1
    appdir = os.path.dirname(os.path.abspath(man_path))
    entry = os.path.join(appdir, man.entry)
    if not os.path.isfile(entry):
        sys.stderr.write("entry not found: %s\n" % man.entry)
        return 1
    # The spine of every build: the program must type-check clean.
    from .checker import check_source
    with open(entry) as f:
        source = f.read()
    _, diags = check_source(source, entry, appdir, is_entry=True)
    if diags:
        sys.stderr.write(json.dumps([d.to_dict() for d in diags], indent=2)
                         + "\n")
        return 1
    dist = os.path.join(appdir, "dist")
    os.makedirs(dist, exist_ok=True)
    sources = []
    for f in _transitive_imports(os.path.abspath(entry)):
        shutil.copy2(f, dist)
        sources.append(os.path.basename(f))
    # Dockerfile: v1 image = python + the Nova toolchain (stdlib only).
    docker_base = man.docker.get("base", "python:3.12-slim")
    docker_port = man.docker.get("port", 8080)
    entry_base = os.path.basename(man.entry)
    dockerfile = """# Generated by `nova build` for %(name)s %(version)s.
# v1 deploy targets: local process + Docker image.
FROM %(base)s
WORKDIR /app
# Nova toolchain (pure Python, stdlib only — no pip installs)
COPY nova_lang /opt/nova-lang/nova_lang
COPY nova /opt/nova-lang/nova
ENV PATH="/opt/nova-lang:${PATH}"
# App sources (entry + transitive imports)
COPY %(sources)s /app/
ENV NOVA_THREADING=%(threading)s
EXPOSE %(port)s
CMD ["nova", "run", "%(entry)s"]
""" % {"name": man.name, "version": man.version, "base": docker_base,
       "sources": " ".join(sources), "threading": man.threading,
       "port": docker_port, "entry": entry_base}
    with open(os.path.join(dist, "Dockerfile"), "w") as fh:
        fh.write(dockerfile)
    print("build ok: %s %s" % (man.name, man.version))
    print("  entry:     %s" % man.entry)
    print("  threading: %s" % man.threading)
    print("  resources: %s" % (", ".join(
        "%s %s" % (k, n) for k, n, _ in man.resources) or "none"))
    print("  staged:    %s/" % dist)
    if emit_bytecode:
        # v2: compile the entry to NovaVM bytecode alongside the sources.
        from .bytecode import compile_to_file, CompileError
        nvb_name = os.path.splitext(entry_base)[0] + ".nvb"
        nvb_path = os.path.join(dist, nvb_name)
        try:
            compile_to_file(entry, nvb_path)
        except CompileError as e:
            sys.stderr.write("bytecode: %s\n" % e)
            return 1
        print("  bytecode:  %s" % nvb_path)
    return 0


def cmd_deploy(args):
    from .manifest import load_manifest, ManifestError
    target, env_name, man_path = None, "dev", None
    i = 0
    while i < len(args):
        if args[i] == "--target" and i + 1 < len(args):
            target = args[i + 1]
            i += 2
        elif args[i] == "--env" and i + 1 < len(args):
            env_name = args[i + 1]
            i += 2
        elif args[i] == "--manifest" and i + 1 < len(args):
            man_path = args[i + 1]
            i += 2
        else:
            i += 1
    if man_path is None:
        man_path = os.path.join(os.getcwd(), "app.nova")
    if not os.path.isfile(man_path):
        sys.stderr.write("no app.nova found (pass --manifest <path>)\n")
        return 2
    try:
        man = load_manifest(man_path)
    except ManifestError as e:
        sys.stderr.write("app.nova: %s\n" % e)
        return 1
    if target is None:
        target = man.deploy_target
    if target not in ("local", "docker", "aws", "gcp"):
        sys.stderr.write("deploy target must be local|docker|aws|gcp\n")
        return 2
    if target == "local":
        return _deploy_local(man, man_path, env_name)
    if target == "docker":
        return _deploy_docker(man, man_path)
    # v2 cloud targets: generate real deployment artifacts.
    from .deploy_cloud import generate_aws, generate_gcp
    out_dir = os.path.join(os.path.dirname(os.path.abspath(man_path)),
                           "deploy", target)
    if target == "aws":
        generate_aws(man, man_path, out_dir)
    else:
        generate_gcp(man, man_path, out_dir)
    print("deploy artifacts: %s/ (see docs/v2-deploy-cloud.md)" % out_dir)
    return 0


def _deploy_local(man, man_path, env_name):
    appdir = os.path.dirname(os.path.abspath(man_path))
    entry = os.path.join(appdir, man.entry)
    # Non-secret config from the env section becomes environment
    # variables. Secrets are NEVER in the manifest — they resolve by
    # name from the capability-gated store at runtime.
    for k, v in man.env(env_name).items():
        os.environ.setdefault(k.upper(), str(v))
    os.environ.setdefault("NOVA_THREADING", man.threading)
    print("deploy %s -> local  (env=%s, threading=%s)"
          % (man.name, env_name, man.threading))
    # v2: cron resources are real scheduled jobs, not logged intent.
    # The scheduler runs in a background thread while the entry runs in
    # the foreground; if the entry exits, the scheduler keeps the
    # process alive until Ctrl-C.
    from .scheduler import jobs_from_manifest, Scheduler
    cron_jobs = []
    try:
        cron_jobs = jobs_from_manifest(man, man_path)
    except ValueError as e:
        sys.stderr.write("app.nova: %s\n" % e)
        return 1
    sched = None
    if cron_jobs:
        sched = Scheduler(cron_jobs)
        for j in cron_jobs:
            print("  cron '%s': schedule '%s' run '%s' — scheduled"
                  % (j.name, j.schedule.expr, j.run_file))
        sched.start()
    for kind, name, opts in man.resources:
        if kind == "secret":
            print("  secret '%s': resolves from NOVA_SECRET_%s at runtime"
                  % (name, name.upper().replace("-", "_")))
        elif kind == "stream":
            print("  stream '%s': in-memory provider (v1)" % name)
        elif kind == "cron":
            pass  # already printed above as scheduled
        elif kind == "bucket":
            print("  bucket '%s': v2 target (no v1 runtime)" % name)
    rc = cmd_run(entry)
    if sched is not None:
        print("entry exited; cron scheduler keeps running "
              "(Ctrl-C to stop)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            sched.stop()
    return rc


def _deploy_docker(man, man_path):
    rc = cmd_build(["--manifest", man_path])
    if rc != 0:
        return rc
    appdir = os.path.dirname(os.path.abspath(man_path))
    dist = os.path.join(appdir, "dist")
    tag = "%s:%s" % (man.name.lower().replace(" ", "-"), man.version)
    if shutil.which("docker") is None:
        print("docker not found — Dockerfile written to %s/Dockerfile"
              % dist)
        print("build it with: docker build -t %s %s" % (tag, dist))
        return 0
    print("docker build -t %s %s" % (tag, dist))
    return subprocess.run(["docker", "build", "-t", tag, dist]).returncode


def cmd_dom(args):
    # EXPERIMENTAL: Nova components -> self-contained HTML+JS.
    src, out = None, None
    i = 0
    while i < len(args):
        if args[i] == "-o" and i + 1 < len(args):
            out = args[i + 1]
            i += 2
        elif not args[i].startswith("--"):
            src = args[i]
            i += 1
        else:
            i += 1
    if src is None or out is None:
        sys.stderr.write("usage: nova dom <file.nova> -o <out.html>\n")
        return 2
    from .render_dom import render_to_html, NovaRenderError
    try:
        render_to_html(src, out)
    except NovaRenderError as e:
        sys.stderr.write("render_dom: %s\n" % e)
        return 1
    print("dom: %s -> %s" % (src, out))
    return 0


def cmd_eval(args):
    # Eval harness: suite JSON -> provider -> predicates -> JSON report.
    suite, provider, out = None, None, None
    i = 0
    while i < len(args):
        if args[i] == "--provider" and i + 1 < len(args):
            provider = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out = args[i + 1]
            i += 2
        elif not args[i].startswith("--"):
            suite = args[i]
            i += 1
        else:
            i += 1
    if suite is None:
        sys.stderr.write(
            "usage: nova eval <suite.json> [--provider mock|http] "
            "[--out report.json]\n")
        return 2
    from .evalharness import run_eval
    return run_eval(suite, provider_override=provider, out_path=out)


def cmd_lsp(args):
    # EXPERIMENTAL: LSP server over stdio.
    from .lsp import main as lsp_main
    lsp_main(args)
    return 0


def cmd_db(args):
    # nova db pull --dsn sqlite:///app.db [--out models.nova]
    if not args or args[0] != "pull":
        sys.stderr.write("usage: nova db pull --dsn sqlite:///app.db "
                         "[--out models.nova]\n")
        return 2
    dsn = None
    out = "models.nova"
    i = 1
    while i < len(args):
        if args[i] == "--dsn" and i + 1 < len(args):
            dsn = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out = args[i + 1]
            i += 2
        else:
            i += 1
    if not dsn:
        sys.stderr.write("nova db pull needs --dsn sqlite:///<path>\n")
        return 2
    from .db import pull_sqlite, DbError
    try:
        tables = pull_sqlite(dsn, out)
    except DbError as e:
        sys.stderr.write("db pull failed: %s\n" % e)
        return 1
    schema_path = os.path.join(os.path.dirname(os.path.abspath(out)),
                               ".nova-schema.json")
    print("pulled %d table(s) -> %s (+ %s)"
          % (len(tables), out, schema_path))
    return 0


def cmd_data(args):
    # nova data export --from sqlite:///app.db --query "SELECT ..."     #     --format jsonl [--out out.jsonl]
    # Produces fine-tuning datasets. Training happens outside Nova;
    # see docs/ai-training.md for the full loop.
    if not args or args[0] != "export":
        sys.stderr.write(
            "usage: nova data export --from sqlite:///app.db "
            '--query "SELECT ..." --format jsonl [--out out.jsonl]\n')
        return 2
    dsn = query = out = None
    fmt = "jsonl"
    i = 1
    while i < len(args):
        if args[i] == "--from" and i + 1 < len(args):
            dsn = args[i + 1]
            i += 2
        elif args[i] == "--query" and i + 1 < len(args):
            query = args[i + 1]
            i += 2
        elif args[i] == "--format" and i + 1 < len(args):
            fmt = args[i + 1]
            i += 2
        elif args[i] == "--out" and i + 1 < len(args):
            out = args[i + 1]
            i += 2
        else:
            i += 1
    if not dsn or not query:
        sys.stderr.write("nova data export needs --from and --query\n")
        return 2
    if fmt != "jsonl":
        sys.stderr.write("unsupported format '%s' — only jsonl\n" % fmt)
        return 2
    import json as _json
    import sqlite3
    from .db import parse_dsn, DbError
    try:
        driver, path = parse_dsn(dsn)
    except DbError as e:
        sys.stderr.write("data export failed: %s\n" % e)
        return 1
    if driver != "sqlite":
        sys.stderr.write("data export failed: only sqlite is supported\n")
        return 1
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(query).fetchall()]
    except Exception as e:
        sys.stderr.write("query failed: %s\n" % e)
        return 1
    lines = [_json.dumps(r, separators=(",", ":")) for r in rows]
    if out:
        with open(out, "w") as fh:
            fh.write("\n".join(lines) + ("\n" if lines else ""))
        print("exported %d row(s) -> %s (jsonl)" % (len(rows), out))
    else:
        sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    return 0


def cmd_test(args):
    # nova test [dir] — discover *_test.nova, run test_* fns + top-level
    # asserts, report per file.
    import glob
    root = args[0] if args and not args[0].startswith("--") else "."
    files = sorted(glob.glob(os.path.join(root, "**", "*_test.nova"),
                             recursive=True))
    if not files:
        print("no *_test.nova files found under %s" % root)
        return 0
    from .interpreter import Interpreter, NovaError, TOP_EFF, FuncValue
    from . import ast as A
    total = passed = failed = 0
    failures = []
    for path in files:
        # check first
        from .checker import check_source
        with open(path) as fh:
            src = fh.read()
        _, diags = check_source(src, path,
                                os.path.dirname(os.path.abspath(path)),
                                is_entry=True)
        errs = diags  # check_source only reports errors
        if errs:
            failed += 1
            failures.append((path, None,
                             "; ".join("%s: %s" % (d.code, d.message)
                                       for d in errs[:3])))
            print("FAIL %s (check: %d error(s))" % (path, len(errs)))
            continue
        # run the program (top-level asserts execute here)
        from .lexer import Lexer
        from .parser import Parser
        basedir = os.path.dirname(os.path.abspath(path))
        prog = Parser(Lexer(src, path).tokenize(), src, path).parse_program()
        interp = Interpreter(basedir, is_entry=True)
        try:
            interp.run(prog, path)
        except NovaError as e:
            failed += 1
            failures.append((path, None, "%s: %s" % (e.code, e)))
            print("FAIL %s (%s)" % (path, e.code))
            continue
        # then each test_* function
        test_fns = [(n, c.value)
                    for n, c in interp.global_env.cells.items()
                    if n.startswith("test_")
                    and isinstance(c.value, FuncValue)]
        file_ok = True
        for tname, fv in sorted(test_fns):
            total += 1
            saved = interp.fx
            interp.fx = TOP_EFF
            try:
                interp.call_fn(fv, [], A.Node(), "test")
                passed += 1
            except NovaError as e:
                file_ok = False
                failures.append((path, tname, "%s: %s" % (e.code, e)))
            finally:
                interp.fx = saved
        if file_ok and not test_fns:
            total += 1
            passed += 1  # top-level asserts only
        if file_ok:
            print("ok   %s (%d test(s))" % (path, len(test_fns) or 1))
        else:
            failed += 1
            print("FAIL %s" % path)
    print("----")
    print("%d passed, %d failed, %d files" % (passed, failed, len(files)))
    for path, tname, msg in failures:
        where = "%s::%s" % (path, tname) if tname else path
        print("  FAIL %s: %s" % (where, msg))
    return 1 if failed else 0


def main(argv):
    if len(argv) < 2:
        sys.stderr.write(USAGE)
        return 2
    cmd = argv[1]
    if cmd == "run":
        locked = "--locked" in argv
        files = [a for a in argv[2:] if not a.startswith("--")]
        if not files:
            sys.stderr.write(USAGE)
            return 2
        return cmd_run(files[0], locked=locked)
    if cmd == "check":
        if len(argv) < 3:
            sys.stderr.write(USAGE)
            return 2
        return cmd_check(argv[2])
    if cmd == "fmt":
        if len(argv) < 3:
            sys.stderr.write(USAGE)
            return 2
        return cmd_fmt(argv[2])
    if cmd == "lock":
        if len(argv) < 3:
            sys.stderr.write(USAGE)
            return 2
        return cmd_lock(argv[2])
    if cmd == "migrate":
        return cmd_migrate(argv[2:])
    if cmd == "build":
        return cmd_build(argv[2:])
    if cmd == "deploy":
        return cmd_deploy(argv[2:])
    if cmd == "dom":
        return cmd_dom(argv[2:])
    if cmd == "eval":
        return cmd_eval(argv[2:])
    if cmd == "lsp":
        return cmd_lsp(argv[2:])
    if cmd == "db":
        return cmd_db(argv[2:])
    if cmd == "data":
        return cmd_data(argv[2:])
    if cmd == "test":
        return cmd_test(argv[2:])
    sys.stderr.write(USAGE)
    return 2
