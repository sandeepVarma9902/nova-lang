# NovaVM — bytecode compiler + VM (v2, item 4)

SPEC.md §18 names the production story: the tree-walking interpreter is
the *reference semantics*; NovaVM is the *"write once, run anywhere"*
portable bytecode. This document describes what shipped: a compact
bytecode, an assembler/disassembler, a `.nvb` file format, and a VM —
all Python stdlib only.

## The honest one-paragraph summary

`nova_lang/bytecode.py` compiles the **portable subset** of Nova to a
~40-opcode stack bytecode; `nova_lang/vm.py` executes it in a plain
Python dispatch loop. The win is **portability, not speed**: any host
that implements 39 opcodes can run `.nvb` files, but this particular VM
is slower than the tree-walking interpreter (CPython does the real
work in both). Anything outside the subset — effects with
capabilities, spawn/streams, classes, components, AI — is rejected at
compile time with a clear error, never silently miscompiled.

## What's supported

| Feature | Status |
|---|---|
| Integer arithmetic (`+ - * / %`, unary `-`), **checked 64-bit** — overflow is `R_INT_OVERFLOW`, never wraps | ✅ |
| Integer division truncates toward zero; `%` keeps the dividend's sign (matches the interpreter) | ✅ |
| Float arithmetic (`+ - * /`, unary `-`); `/` by zero is `R_DIV_ZERO`; `%` on floats is a type error | ✅ |
| String concatenation (`+`), `str()` | ✅ |
| `let` / `var`, assignment-as-expression, block shadowing | ✅ |
| Functions incl. recursion, forward references, named args (on direct calls), closures over top-level bindings | ✅ |
| `if`/`else` (statement + expression forms), `while`, `for x in list` | ✅ |
| Lists: literals, indexing, index-assign, `push`, `len`, nesting, structural `==` | ✅ |
| `Option`: `Some`/`None`, `match` arms, `?`, `.unwrap()`/`.expect(msg)` | ✅ |
| `Result`: `Ok`/`Err`, `match` arms, `?` (propagates `Err` as the call's return), `.unwrap()`/`.expect(msg)` | ✅ |
| `match` patterns: `Some/None/Ok/Err` binders, literals, `_`, variable | ✅ |
| `print` (multi-arg), `int()` (truncate + range check), `len()`, `assert` | ✅ |
| `and`/`or` (short-circuit, real `Bool` result), `not`, all comparisons | ✅ |

### Deliberate semantic notes (parity with the interpreter)

- `==` on `Option`/`Result` is **identity**, not structural — `Some(1) == Some(1)` is `false` in both backends (the interpreter's deep-compare branches are unreachable dead code; `match` is the intended comparison). The VM replicates this exactly.
- `1 == 1.0` never reaches either backend: the checker rejects mixed-type comparison (`E_TYPE_MISMATCH`).
- The VM **trusts the checker's static guarantees** and does not re-enforce them: immutability (`let`), effect discipline (`pure` vs `effect`), exhaustiveness of `match`, and arity of direct calls are all proven before compilation. Arity and callability *are* re-checked at runtime as defense-in-depth.
- A `for` loop evaluates its iterable once and walks it by index; appends during iteration are observed, like the interpreter's live list walk.
- `int()` accepts only `Float` (the checker rejects `int(7)`); at runtime it also accepts `Int` harmlessly.

## What's rejected (clear `CompileError`, never silent)

Effects/capabilities (`effect io`/`effect net`/`async` functions),
`readFile`/`writeFile`/`fetch`/`secret`/`prompt`/`sleep`/`render`,
contracts (`requires`/`ensures`), `import`, classes, interfaces,
components/JSX, `stream`, `ai` declarations, `spawn`/`spawn_async`,
nurseries, contexts (`context`/`provide`/`consume`), field access,
methods other than `push`/`unwrap`/`expect`, and named arguments on
indirect calls (through a variable holding a function — the VM keeps no
runtime parameter metadata).

Nested `fn` declarations are rejected by the *checker* (`E_NESTED_FN`
— Nova has no nested functions); closures in the VM therefore only ever
capture top-level bindings, which the `(depth, slot)` scheme handles.

## The opcode set (39 ops)

```
Stack:      LOAD_CONST POP DUP
Variables:  LOAD_VAR STORE_VAR            ; operand = depth<<8 | slot
Arithmetic: ADD SUB MUL DIV MOD NEG NOT    ; checked Int | Float | String+String
Compare:    EQ NE LT LE GT GE
Jumps:      JUMP JUMP_IF_FALSE JUMP_IF_TRUE ; (popping)
Functions:  MAKE_FUNCTION CALL RETURN      ; closures capture the frame
Lists:      BUILD_LIST INDEX STORE_INDEX LEN LIST_PUSH
Variants:   MAKE_VARIANT TAG_OF VARIANT_VALUE UNWRAP_OR_RETURN UNWRAP_MSG
Builtins:   PRINT STR INT_OF ASSERT
Trap:       FAIL                           ; compiler-generated unreachable
```

Variables compile to `(depth, slot)`: `depth` = function frames to walk
outward, `slot` = cell index. Block scopes share their function's cells,
so shadowing, loop variables, and closures over globals all work by
construction. `?` (`UNWRAP_OR_RETURN`) unwinds exactly one call frame,
like the interpreter's `EarlyReturn`.

## The `.nvb` file format

Big-endian binary, magic `NVB1`, format version `1`:

```
magic        4s   "NVB1"
version      u16
source       u16 len + utf-8          (informational: original file path)
nfuncs       u32
per function:
  name       u16 len + utf-8
  nparams    u16
  nslots     u16
  nconsts    u32
  per const: u8 tag + payload
               0 = ()                1 = bool u8
               2 = int i64           3 = float f64
               4 = str u32 len + bytes
  code_len   u32                      (multiple of 3)
  code       u8 opcode, u16 operand per instruction
  nlines     u32
  lines      u16 source line per instruction (0 = unknown)
entry        u32                      (function index; always 0)
```

`load()` validates magic, version, opcode numbers, and the entry index,
and raises `CompileError` on anything malformed.

## API (for the CLI wiring)

```python
from nova_lang.bytecode import compile_to_file, compile_source
from nova_lang.vm import run_bytecode

compile_to_file("app.nova", "app.nvb")  # parse + check + compile; -> path
run_bytecode("app.nvb")                 # -> [printed lines]; raises
                                        # CompileError (bad file) or VMError
```

- `compile_source(source, filename, basedir)` — parse + **full
  type-check** + compile to a `Program`. Raises `CompileError` on any
  lexer/parser/checker diagnostic or out-of-subset construct.
- `VMError` carries the Nova error code (`R_INT_OVERFLOW`, …), message,
  and source line (from the per-instruction line table).
- `disassemble(program) -> str` / `assemble(text) -> Program` — the
  human-readable assembly round-trips (used for debugging; see below).
- `dump(program) -> bytes` / `load(bytes) -> Program` — the `.nvb`
  codec.

`nova build --emit-bytecode` emits the `.nvb`; `nova run` executes it
(wired in `cli.py` by the parent — `cli.py` itself was not touched).

## Debugging: assembly text

```
.func <main> params=0 slots=2  # [0]
.const 0 = int 41
  LOAD_CONST       0         ; @0
  LOAD_CONST       0         ; @1
  ADD              0         ; @2
  PRINT            1         ; @3
  ...
```

Labels (`L0:`) mark jump targets; `;`-comments are ignored by the
assembler. `disassemble → assemble → dump → load → run` is covered by
tests and produces identical output.

## Honest limits

1. **It's a Python-loop VM — portability story, not a speed story.**
   Expect roughly an order of magnitude slower than the interpreter on
   arithmetic-heavy code (every opcode is several Python operations).
   A future native (Rust/C/WASM) NovaVM can consume the same `.nvb`.
2. **Subset only.** Anything effectful, concurrent, object-oriented, or
   AI-shaped stays on the interpreter. The compiler says so loudly.
3. **No cross-module code.** `import` is rejected; each `.nvb` is one
   compilation unit.
4. **Limits are documented, not hidden:** 255 slots per function,
   255 scope-nesting depth, 65535 constants/instructions per function —
   all raise `CompileError`, never corrupt.
5. **Execution budget:** 10⁸ instructions, then `R_BUDGET` (infinite-loop
   guard; the interpreter has none).
6. **Recursion is iterative** — the VM never hits Python's recursion
   limit, unlike the tree-walker.
7. **Error fidelity:** runtime errors carry Nova codes and source lines,
   but there are no column numbers and no multi-frame stack traces yet.
