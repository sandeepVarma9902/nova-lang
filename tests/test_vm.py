#!/usr/bin/env python3
# test_vm.py — NovaVM (bytecode compiler + VM) test suite.
#
# Compiles small Nova programs with nova_lang.bytecode and runs them with
# nova_lang.vm, asserting exact output. Also checks: .nvb file round
# trips, assembly round trips, runtime error codes, compile-time
# rejection of out-of-subset features, and output parity with the
# tree-walking interpreter on examples/hello.nova.
#
# Run: python3 tests/test_vm.py   (from the nova-lang directory)

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nova_lang.bytecode import (compile_source, compile_to_file, dump, load,
                                disassemble, assemble, CompileError)
from nova_lang.vm import VM, VMError, run_bytecode

FAILURES = []


def run_src(src):
    """Compile Nova source and run it on the VM. Returns output lines."""
    prog = compile_source(src, "<test>")
    return VM().run(prog)


def check(name, got, want):
    if got == want:
        print("ok   %s" % name)
    else:
        FAILURES.append((name, "want %r, got %r" % (want, got)))


def check_vm_error(name, src, want_code):
    try:
        run_src(src)
    except VMError as e:
        if e.code == want_code:
            print("ok   %s [%s]" % (name, want_code))
        else:
            FAILURES.append((name, "want error %s, got %s (%s)"
                             % (want_code, e.code, e.message)))
    except CompileError as e:
        FAILURES.append((name, "want runtime %s, got compile error: %s"
                         % (want_code, e)))
    else:
        FAILURES.append((name, "want runtime %s, no error raised" % want_code))


def check_compile_error(name, src, want_substr):
    try:
        compile_source(src, "<test>")
    except CompileError as e:
        if want_substr in str(e):
            print("ok   %s [rejected: %s]" % (name, want_substr))
        else:
            FAILURES.append((name, "wrong rejection message: %s" % e))
    else:
        FAILURES.append((name, "expected CompileError containing %r"
                         % want_substr))


# ------------------------------------------------------------- basics ----
def test_arithmetic():
    check("arith/precedence",
          run_src("print(2 + 3 * 4)\nprint((2 + 3) * 4)\n"
                  "print(10 - 2 - 3)\nprint(2 * 3 % 4)\n"),
          ["14", "20", "5", "2"])
    check("arith/float",
          run_src("print(1.5 + 2.25)\nprint(5.0 / 2.0)\nprint(2.5 * 4.0)\n"),
          ["3.75", "2.5", "10.0"])
    check("arith/neg-div",
          run_src("print(0 - 7 / 2)\nprint(0 - 7 % 2)\nprint(7 / -2)\n"
                  "print(0 - 5)\n"),
          ["-3", "-1", "-3", "-5"])
    check("arith/unary",
          run_src("let x = 5\nprint(-x)\nprint(-3.5)\nprint(not true)\n"
                  "print(not false)\n"),
          ["-5", "-3.5", "false", "true"])
    check("arith/comparison",
          run_src("print(1 < 2)\nprint(2 <= 2)\nprint(3 > 4)\nprint(3 >= 4)\n"
                  "print(1 == 1)\nprint(1 != 2)\nprint(1.5 == 1.5)\n"),
          ["true", "true", "false", "false", "true", "true", "true"])
    # Nova has no implicit conversions: the checker rejects 1 == 1.0
    # outright (E_TYPE_MISMATCH), so the VM never sees it.
    check("arith/str-eq", run_src("print(\"a\" == \"a\")\nprint(\"a\" != \"b\")\n"),
          ["true", "true"])
    check("arith/logic",
          run_src("print(true and false)\nprint(true or false)\n"
                  "print(true and true)\nprint(false or false)\n"),
          ["false", "true", "true", "false"])
    check_vm_error("arith/div-zero", "print(1 / 0)\n", "R_DIV_ZERO")
    check_vm_error("arith/mod-zero", "print(1 % 0)\n", "R_DIV_ZERO")
    check_vm_error("arith/fdiv-zero", "print(1.0 / 0.0)\n", "R_DIV_ZERO")
    check_vm_error("arith/overflow-add", "print(9223372036854775807 + 1)\n",
                   "R_INT_OVERFLOW")
    check_vm_error("arith/overflow-mul", "print(3037000500 * 3037000500)\n",
                   "R_INT_OVERFLOW")
    # INT_MIN cannot be written as a literal (the lexer range-checks),
    # so compute it: 0 - 9223372036854775807 - 1.
    check_vm_error("arith/overflow-neg",
                   "let m = 0 - 9223372036854775807 - 1\nprint(-m)\n",
                   "R_INT_OVERFLOW")
    check_vm_error("arith/min-div-neg1",
                   "let m = 0 - 9223372036854775807 - 1\nprint(m / -1)\n",
                   "R_INT_OVERFLOW")


def test_strings():
    check("str/concat",
          run_src("print(\"foo\" + \"bar\")\nlet a = \"x\"\nprint(a + a + a)\n"),
          ["foobar", "xxx"])
    check("str/str-builtin",
          run_src("print(str(42))\nprint(str(1.5))\nprint(str(true))\n"
                  "print(str([1, 2]))\nprint(str(None))\n"
                  "print(str(Some(3)))\nprint(str(Ok(4)))\n"
                  "print(str(Err(\"e\")))\n"),
          ["42", "1.5", "true", "[1, 2]", "None", "Some(3)", "Ok(4)",
           "Err(e)"])
    check("str/print-multi", run_src("print(\"a\", 1, true, [2])\n"),
          ["a 1 true [2]"])
    check("str/print-empty", run_src("print()\n"), [""])


def test_variables():
    check("vars/let-var",
          run_src("let x = 1\nvar y = 2\ny = y + x\nprint(y)\n"
                  "print(x)\n"),
          ["3", "1"])
    check("vars/assign-expr",
          run_src("var n = 1\nprint(n = 41 + 1)\nprint(n)\n"),
          ["42", "42"])
    check("vars/shadow",
          run_src("let x = 1\nif true {\n  let x = 2\n  print(x)\n}\n"
                  "print(x)\n"),
          ["2", "1"])
    # The checker enforces immutability statically, so the VM never sees it.
    check_compile_error("vars/immut", "let x = 1\nx = 2\n", "E_IMMUTABLE")


def test_functions():
    check("fn/basic",
          run_src("fn add(a: Int, b: Int) -> Int {\n  return a + b\n}\n"
                  "print(add(20, 22))\n"),
          ["42"])
    check("fn/recursion",
          run_src("fn fact(n: Int) -> Int {\n"
                  "  if n <= 1 { return 1 }\n"
                  "  return n * fact(n - 1)\n}\n"
                  "print(fact(0))\nprint(fact(5))\nprint(fact(10))\n"),
          ["1", "120", "3628800"])
    check("fn/forward-ref",
          run_src("print(f())\nfn f() -> Int { return 7 }\n"),
          ["7"])
    check("fn/named-args",
          run_src("fn sub(a: Int, b: Int) -> Int { return a - b }\n"
                  "print(sub(b: 1, a: 10))\nprint(sub(10, 1))\n"),
          ["9", "9"])
    check("fn/no-return-value",
          run_src("effect fn greet(name: String) {\n"
                  "  print(\"hi \" + name)\n}\n"
                  "greet(\"nova\")\nprint(\"done\")\n"),
          ["hi nova", "done"])
    check("fn/closure-over-global",
          run_src("let prefix = \">>\"\n"
                  "fn tag(s: String) -> String { return prefix + s }\n"
                  "print(tag(\"a\"))\nprint(tag(\"b\"))\n"),
          [">>a", ">>b"])
    check("fn/if-expr-value",
          run_src("fn pick(c: Bool) -> Int {\n"
                  "  return if c { 1 } else { 2 }\n}\n"
                  "print(pick(true))\nprint(pick(false))\n"
                  "let x = if 1 < 2 { 10 } else { 20 }\nprint(x)\n"),
          ["1", "2", "10"])
    # Arity and callability are also checked statically; the VM keeps
    # runtime guards as defense-in-depth (see vm/argc-runtime below).
    check_compile_error("fn/argc",
                        "fn f(a: Int) -> Int { return a }\nprint(f())\n",
                        "E_ARGC")
    check_compile_error("fn/not-callable", "let x = 1\nprint(x())\n",
                        "E_NOT_CALLABLE")


def test_control_flow():
    check("flow/if-else",
          run_src("var x = 0\nif 1 < 2 {\n  x = 1\n} else {\n  x = 2\n}\n"
                  "print(x)\n"
                  "if 1 > 2 {\n  x = 3\n}\nprint(x)\n"),
          ["1", "1"])
    check("flow/else-if",
          run_src("fn f(n: Int) -> String {\n"
                  "  var r = \"many\"\n"
                  "  if n == 1 { r = \"one\" }\n"
                  "  else if n == 2 { r = \"two\" }\n"
                  "  return r\n}\n"
                  "print(f(1))\nprint(f(2))\nprint(f(9))\n"),
          ["one", "two", "many"])
    check("flow/while",
          run_src("var i = 0\nvar s = 0\nwhile i < 10 {\n"
                  "  s = s + i\n  i = i + 1\n}\nprint(s)\nprint(i)\n"),
          ["45", "10"])
    check("flow/for",
          run_src("var s = 0\nfor x in [1, 2, 3, 4] {\n  s = s + x\n}\n"
                  "print(s)\n"),
          ["10"])
    check("flow/for-empty", run_src("for x in [] {\n  print(x)\n}\n"
                                    "print(\"done\")\n"),
          ["done"])
    check("flow/for-nested",
          run_src("var n = 0\nfor a in [1, 2] {\n  for b in [10, 20] {\n"
                  "    n = n + a * b\n  }\n}\nprint(n)\n"),
          ["90"])
    check("flow/while-false", run_src("while false {\n  print(\"no\")\n}\n"
                                      "print(\"yes\")\n"),
          ["yes"])


def test_lists():
    check("list/basic",
          run_src("let xs = [1, 2, 3]\nprint(xs)\nprint(xs[0])\n"
                  "print(xs[2])\nprint(len(xs))\n"),
          ["[1, 2, 3]", "1", "3", "3"])
    check("list/mutate",
          run_src("let xs = [1, 2]\nxs.push(3)\nprint(xs)\n"
                  "xs[0] = 9\nprint(xs)\nprint(len(xs))\n"),
          ["[1, 2, 3]", "[9, 2, 3]", "3"])
    check("list/nested",
          run_src("let m = [[1, 2], [3]]\nprint(m)\nprint(m[1][0])\n"
                  "print(m == [[1, 2], [3]])\n"),
          ["[[1, 2], [3]]", "3", "true"])
    check("list/strings", run_src("let xs = [\"a\", \"b\"]\nprint(xs[1])\n"),
          ["b"])
    check_vm_error("list/oob", "print([1][5])\n", "R_INDEX_OUT_OF_BOUNDS")
    check_vm_error("list/oob-neg", "print([1][-1])\n", "R_INDEX_OUT_OF_BOUNDS")
    check_vm_error("list/oob-store", "let xs = [1]\nxs[2] = 9\n",
                   "R_INDEX_OUT_OF_BOUNDS")


def test_option_result():
    # Nova == on Option/Result is identity, not structural (matches the
    # tree-walking interpreter exactly — `match` is the way to compare).
    check("opt/basic",
          run_src("let a = Some(1)\nlet c = a\nprint(a)\n"
                  "print(a == Some(1))\nprint(a == c)\n"),
          ["Some(1)", "false", "true"])
    check("res/basic",
          run_src("let a = Ok(1)\nlet b = a\n"
                  "print(a)\nprint(a == Ok(1))\nprint(a == b)\n"),
          ["Ok(1)", "false", "true"])
    # Note: bare `None`/`Some(..)` at a call site infers Option[Any] and
    # the checker does not unify it there (v1 inference limitation), so
    # realistic code binds with an annotation first.
    check("opt/match",
          run_src("fn describe(o: Option[Int]) -> String {\n"
                  "  return match o {\n"
                  "    Some(v) => \"some \" + str(v),\n"
                  "    None => \"none\",\n"
                  "  }\n}\n"
                  "fn noInt() -> Option[Int] { return None }\n"
                  "print(describe(Some(5)))\nprint(describe(noInt()))\n"),
          ["some 5", "none"])
    check("res/match",
          run_src("fn code(r: Result[Int, String]) -> Int {\n"
                  "  return match r {\n"
                  "    Ok(v) => v,\n"
                  "    Err(e) => -1,\n"
                  "  }\n}\n"
                  "fn ok41() -> Result[Int, String] { return Ok(41) }\n"
                  "fn bad() -> Result[Int, String] { return Err(\"bad\") }\n"
                  "print(code(ok41()))\nprint(code(bad()))\n"),
          ["41", "-1"])
    check("match/lit-wild",
          run_src("fn f(n: Int) -> String {\n"
                  "  return match n {\n"
                  "    0 => \"zero\",\n"
                  "    1 => \"one\",\n"
                  "    _ => \"many\",\n"
                  "  }\n}\n"
                  "print(f(0))\nprint(f(1))\nprint(f(42))\n"),
          ["zero", "one", "many"])
    check("match/var",
          run_src("print(match 7 { v => v * 2 })\n"),
          ["14"])
    check("opt/question",
          run_src("fn get(xs: List[Int]) -> Option[Int] {\n"
                  "  if len(xs) == 0 { return None }\n"
                  "  return Some(xs[0] * 10)\n}\n"
                  "fn use() -> Option[Int] {\n"
                  "  let v = get([3])?\n"
                  "  return Some(v + 1)\n}\n"
                  "fn useNone() -> Option[Int] {\n"
                  "  let v = get([])?\n"
                  "  return Some(v + 1)\n}\n"
                  "print(use())\nprint(useNone())\n"),
          ["Some(31)", "None"])
    check("res/question",
          run_src("fn div(a: Int, b: Int) -> Result[Int, String] {\n"
                  "  if b == 0 { return Err(\"zero\") }\n"
                  "  return Ok(a / b)\n}\n"
                  "fn calc() -> Result[Int, String] {\n"
                  "  let x = div(20, 4)?\n"
                  "  let y = div(x, 2)?\n"
                  "  return Ok(y)\n}\n"
                  "fn calcErr() -> Result[Int, String] {\n"
                  "  let x = div(20, 0)?\n"
                  "  return Ok(x)\n}\n"
                  "print(calc())\nprint(calcErr())\n"),
          ["Ok(2)", "Err(zero)"])
    check("opt/unwrap",
          run_src("print(Some(1).unwrap())\nprint(Ok(2).unwrap())\n"),
          ["1", "2"])
    check_vm_error("opt/unwrap-none", "print(None.unwrap())\n",
                   "R_UNWRAP_NONE")
    check_vm_error("res/unwrap-err", "print(Err(\"x\").unwrap())\n",
                   "R_UNWRAP_ERR")
    check_vm_error("opt/expect-msg",
                   "print(None.expect(\"need it\"))\n", "R_UNWRAP_NONE")


def test_builtins():
    check("blt/int",
          run_src("print(int(3.9))\nprint(int(-3.9))\nprint(int(7.0))\n"),
          ["3", "-3", "7"])
    check("blt/assert",
          run_src("assert(1 + 1 == 2)\nassert(true, \"ok\")\n"
                  "print(\"passed\")\n"),
          ["passed"])
    check_vm_error("blt/assert-fail", "assert(1 == 2)\n", "R_ASSERT")
    check_vm_error("blt/assert-msg", "assert(false, \"nope\")\n", "R_ASSERT")
    check_vm_error("blt/int-range",
                   "print(int(100000000000000000000.0))\n", "R_INT_OVERFLOW")


def compile_no_check(src):
    """Parse and compile, skipping the type-checker: exercises the
    compiler's own subset boundary (the checker would reject some of
    these first)."""
    from nova_lang.lexer import Lexer
    from nova_lang.parser import Parser
    from nova_lang.bytecode import Compiler
    prog = Parser(Lexer(src, "<test>").tokenize(), src, "<test>",
                  ).parse_program()
    return Compiler("<test>").compile_program(prog)


def check_compiler_rejection(name, src, want_substr):
    try:
        compile_no_check(src)
    except CompileError as e:
        if want_substr in str(e):
            print("ok   %s [rejected: %s]" % (name, want_substr))
        else:
            FAILURES.append((name, "wrong rejection message: %s" % e))
    else:
        FAILURES.append((name, "expected CompileError containing %r"
                         % want_substr))


def test_rejections():
    check_compile_error("rej/spawn", "spawn { print(1) }\n", "Spawn")
    check_compile_error("rej/class", "class A {\n}\n", "ClassDecl")
    check_compile_error("rej/io-effect",
                        "effect io fn f(io: IO) {\n}\n", "not supported")
    check_compile_error("rej/net-effect",
                        "effect net fn f(net: Net) {\n}\n", "not supported")
    check_compile_error("rej/contract",
                        "fn f() -> Int requires true { return 1 }\n",
                        "contracts")
    check_compile_error("rej/readfile", "print(readFile(\"x\", io))\n",
                        "not supported")
    check_compile_error("rej/async",
                        "async fn f() {\n}\n", "not supported")
    # These two are rejected by the checker first (E_IMPORT / E_NO_METHOD),
    # so exercise the compiler boundary directly.
    check_compiler_rejection("rej/import", "import foo\n", "not supported")
    check_compiler_rejection("rej/method", "let s = \"a\"\nprint(s.foo())\n",
                             "not supported")
    check_compiler_rejection("rej/field", "let s = \"a\"\nprint(s.foo)\n",
                             "not supported")


def test_vm_guards():
    # Hand-assembled bytecode: the VM's own runtime guards, which the
    # checker normally makes unreachable.
    asm = """
.func <main> params=0 slots=1
.const 0 = unit
  MAKE_FUNCTION 1
  STORE_VAR 0
  LOAD_VAR 0
  CALL 0
  POP
  LOAD_CONST 0
  RETURN
.func f params=1 slots=1
.const 0 = unit
  LOAD_CONST 0
  RETURN
"""
    try:
        VM().run(assemble(asm))
    except VMError as e:
        if e.code == "E_ARGC":
            print("ok   vm/argc-runtime [E_ARGC]")
        else:
            FAILURES.append(("vm/argc-runtime", "got %s" % e.code))
    else:
        FAILURES.append(("vm/argc-runtime", "no error raised"))

    asm2 = """
.func <main> params=0 slots=0
.const 0 = int 1
.const 1 = unit
  LOAD_CONST 0
  CALL 0
  POP
  LOAD_CONST 1
  RETURN
"""
    try:
        VM().run(assemble(asm2))
    except VMError as e:
        if e.code == "E_NOT_CALLABLE":
            print("ok   vm/not-callable-runtime [E_NOT_CALLABLE]")
        else:
            FAILURES.append(("vm/not-callable-runtime", "got %s" % e.code))
    else:
        FAILURES.append(("vm/not-callable-runtime", "no error raised"))


def test_checker_still_applies():
    # compile_source runs the full checker first.
    check_compile_error("chk/type", "let x: Int = \"s\"\n", "E_TYPE_MISMATCH")
    check_compile_error("chk/undefined", "print(nope)\n", "E_UNDEFINED")


# ------------------------------------------------------- file round trip ----
def test_nvb_round_trip():
    src = ("fn fib(n: Int) -> Int {\n"
           "  if n < 2 { return n }\n"
           "  return fib(n - 1) + fib(n - 2)\n}\n"
           "print(fib(15))\nprint([1, 2] == [1, 2])\n")
    with tempfile.TemporaryDirectory() as d:
        src_path = os.path.join(d, "t.nova")
        out_path = os.path.join(d, "t.nvb")
        with open(src_path, "w") as fh:
            fh.write(src)
        got_path = compile_to_file(src_path, out_path)
        assert got_path == out_path
        with open(out_path, "rb") as fh:
            magic = fh.read(4)
        assert magic == b"NVB1", magic
        check("nvb/run", run_bytecode(out_path), ["610", "true"])


def test_nvb_bad_file():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "bad.nvb")
        with open(p, "wb") as fh:
            fh.write(b"NOPE")
        try:
            run_bytecode(p)
        except CompileError:
            print("ok   nvb/bad-magic [rejected]")
        else:
            FAILURES.append(("nvb/bad-magic", "expected CompileError"))


def test_asm_round_trip():
    src = ("let xs = [3, 1, 2]\nvar s = 0\nfor x in xs { s = s + x }\n"
           "print(s)\nprint(match Some(s) { Some(v) => v, None => 0 })\n")
    prog = compile_source(src, "<test>")
    text = disassemble(prog)
    assert ".func <main>" in text
    assert "LOAD_CONST" in text
    prog2 = assemble(text)
    check("asm/round-trip", VM().run(prog2), VM().run(prog))
    # binary round trip of the re-assembled program
    prog3 = load(dump(prog2))
    check("asm/binary-round-trip", VM().run(prog3), VM().run(prog))


def test_interpreter_parity():
    # examples/hello.nova must print byte-identical output on both
    # backends.
    from nova_lang.lexer import Lexer
    from nova_lang.parser import Parser
    from nova_lang.checker import check_source
    from nova_lang.interpreter import Interpreter
    path = os.path.join(ROOT, "examples", "hello.nova")
    with open(path) as f:
        source = f.read()
    _, diags = check_source(source, path, os.path.dirname(path),
                            is_entry=True)
    assert not diags, diags
    prog_ast = Parser(Lexer(source, path).tokenize(), source,
                      path).parse_program()
    interp_out = Interpreter(os.path.dirname(path), is_entry=True,
                             threading="single").run(prog_ast, path)
    vm_out = run_src(source)
    check("parity/hello.nova", vm_out, interp_out)


def main():
    print("== NovaVM tests ==")
    test_arithmetic()
    test_strings()
    test_variables()
    test_functions()
    test_control_flow()
    test_lists()
    test_option_result()
    test_builtins()
    test_rejections()
    test_vm_guards()
    test_checker_still_applies()
    test_nvb_round_trip()
    test_nvb_bad_file()
    test_asm_round_trip()
    test_interpreter_parity()
    print()
    if FAILURES:
        print("%d FAILURE(S):" % len(FAILURES))
        for name, msg in FAILURES:
            print("FAIL %s\n  %s\n" % (name, msg))
        return 1
    print("all green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
