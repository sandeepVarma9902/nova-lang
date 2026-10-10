# Nova Performance — the honest story

**TL;DR:** The reference implementation is Python. It is portable, not fast.
v2.1 made the interpreter ~22% faster and the VM dispatch ~24% faster, but
no Python tuning will beat Java. The path to beating Java is the native
NovaVM in Rust — the bytecode spec is done, and the plan is below. We are
not pretending otherwise.

## What "fast" means here

Nova has two execution paths:

| Path | What it is | Status |
|---|---|---|
| Tree-walking interpreter | `nova run` — direct AST evaluation | Reference implementation, portable |
| NovaVM (Python) | `nova build --emit-bytecode` → `.nvb` → `vm.py` | Experimental, ~10× slower than the interpreter was; now ~24% faster |

Neither is fast by Java standards. A Java `for` loop does ~50M iterations/sec;
the Nova interpreter does ~100K "Nova iterations"/sec on a tight loop. That
is a ~500× gap, and it is architectural: Python method calls, `isinstance`
chains, and allocation per operation. Profiling and fixing hot spots narrows
it; it does not close it.

## v2.1 profiling: what was actually hot

Profiled with `cProfile` on a loop-heavy program (200K-iteration integer
loop + `fib(20)` + 20K string appends) and on the VM (50K-iteration loop).

### Interpreter hot spots (fixed)

1. **`eval_expr` dispatch** — every expression evaluation did
   `"_" + type(node).__name__` string concatenation followed by `getattr`.
   2.6M calls in the benchmark. Fixed: a dispatch table built once at
   import (`_EVAL_DISPATCH`: AST class → evaluator function), so each
   evaluation is one dict lookup.

2. **`_eval_Binary` type checks** — `isinstance(l, int) and isinstance(r, int)
   and not isinstance(l, bool)` on every arithmetic op. Fixed: `type(l) is int`
   is a single pointer comparison and excludes `bool` by construction.

3. **Method-lookup caching** — the dispatch table above is also the
   "method lookup caching" fix: no per-call `getattr`.

### VM hot spots (fixed)

4. **Opcode dispatch** — the main loop was a 39-way `if/elif` chain on the
   opcode, re-compared for every instruction (550K dispatches in the
   benchmark). Fixed: table dispatch — each opcode is a closure defined once
   per `run()`, and the loop does one tuple index (`handlers[op]`) per
   instruction.

### Deliberately not "fixed"

- **String building** (`s = s + "x"` in a loop) is O(n²) by Nova's value
  semantics — each `+` allocates. That is the language's semantics, not an
  interpreter bug. A `StringBuilder` type would be the real fix; it is not
  in v2.1.
- **`isinstance` chains** (5.4M calls) and **env-chain variable lookup**
  (2.5M calls) remain. They are intrinsic to a tree-walking interpreter in
  Python. The Rust VM eliminates them.

## Before / after numbers

Machine: Linux VM, Python 3.12, `time` wall-clock, best of 3.

**Interpreter** (`/tmp/bench.nova`: 200K-iteration Int loop + `fib(20)` +
20K string appends):

| Version | Time |
|---|---|
| v2.0 (before) | 2.27s |
| v2.1 (after) | 1.78s |
| **Improvement** | **~22%** |

**NovaVM (Python)** (`/tmp/vmbench.nova`: 50K-iteration Int loop):

| Version | Time |
|---|---|
| v2.0 (before) | 0.500s |
| v2.1 (after) | 0.382s |
| **Improvement** | **~24%** |

Small examples (`hello`, `api`, `effects`) are startup-dominated (~0.15–0.27s,
mostly Python import + parse + check) and show no meaningful change — honest
reporting means saying so.

## The path to beating Java: native NovaVM (Rust)

This is the plan, not a promise. The bytecode spec (`docs/bytecode.md`,
`nova_lang/bytecode.py`) is the contract; a Rust VM implements it.

**What the Rust VM needs:**

1. **`.nvb` loader** — the file format is specified and stable: header,
   constant pool, function table, instruction stream. Rust reads it with
   `nom` or hand-rolled parsing. (~1 week)
2. **Value representation** — tagged union (`enum Value { Int(i64),
   Float(f64), Str(Rc<String>), List(...), Option(...), Result(...), ... }`).
   Int overflow checks mirror the spec (no silent wrapping). (~1 week)
3. **Dispatch loop** — computed goto (via `match` on opcode, which LLVM
   turns into a jump table) over the 39 opcodes. This is where the 500× gap
   closes: no Python call overhead, no `isinstance`, no allocation for
   Int arithmetic. (~2 weeks)
4. **Builtins** — reimplement the ~60 builtins used by the subset the
   compiler emits (JSON, strings, lists; IO/Net via `std`). (~2 weeks)
5. **Interop** — the effect system and capability discipline are
   compile-time; the VM just needs `print`/exit/file primitives. AI
   provider calls go through the same HTTP hook. (~1 week)
6. **Parity harness** — `tests/test_vm.py` already asserts VM/interpreter
   output parity; extend it to run the Rust binary and diff. (~1 week)

**What it explicitly does NOT need:** a garbage collector on day one
(`Rc` + pools suffice for the subset), JIT compilation (AOT jump tables
beat Java's interpreter; JIT is phase two), or the full stdlib.

**Honest timeline:** ~8 weeks for one engineer to reach "runs the examples
faster than the Python interpreter", longer to beat `java`. We are not
starting it in v2.1 — this task is the plan, not the VM.

## How to reproduce

```bash
# interpreter benchmark
time ./nova run /tmp/bench.nova
# VM benchmark
python3 -c "
from nova_lang.bytecode import compile_source
from nova_lang.vm import VM
import time
prog = compile_source(open('/tmp/vmbench.nova').read(), 'b')
t0 = time.perf_counter(); VM().run(prog); t1 = time.perf_counter()
print('%.3fs' % (t1 - t0))"
```
