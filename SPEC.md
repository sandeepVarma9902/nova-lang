# Nova Language Specification — v1

Nova is an AI-native programming language: a hybrid of Java and React,
authored by AI, for AI. Static typing with inference, classes and
interfaces, components with type-checked JSX-like views, explicit effects,
no null, structured concurrency, first-class AI calls, and a platform
layer (streams, manifest, deploy) around a small language core.

v1 runs on a tree-walking interpreter (`nova`). The portable bytecode
story (NovaVM) is specified in §18; v2 compiles to it.

---

## 1. Lexical structure

- Significant newlines: a newline ends a statement, except inside
  `(` `)` `[` `]`.
- Comments: `//` to end of line.
- Keywords: `let var fn effect class interface extends implements data
  init this super component state view import if else while for in match
  return requires ensures result and or not true false Some None Ok Err
  override ai spawn stream async`
- `null`, `nil`, `undefined` do not exist: using one is a dedicated
  diagnostic `E_NO_NULL` ("Nova has no null — use `Option[T]`").
- Integer literals are decimal; range-checked to signed 64-bit
  (`E_INT_RANGE`). Float literals contain `.`. String literals are
  `"..."` with `\` escapes.

## 2. Type system

| Type | Meaning |
|---|---|
| `Int` | signed 64-bit. **Arithmetic is checked**: overflow is a runtime error (`R_INT_OVERFLOW`), never silent wraparound. |
| `Float` | 64-bit float |
| `String` | UTF-8 text |
| `Bool` | `true` / `false` |
| `Unit` | `()` — the "no value" value |
| `List[T]` | growable list; `push` mutates |
| `Option[T]` | `Some(v)` / `None` — the only "missing value" |
| `Result[T, E]` | `Ok(v)` / `Err(e)` — the only error value |
| `Task[T]` | a spawned concurrent task; `task.await() : T` |
| `Stream[T]` | a typed pub/sub topic handle |
| `Subscription[T]` | a consumer-group subscription on a stream |
| `Message[T]` | one consumed record: `topic: String`, `offset: Int`, `key: Option[String]`, `value: T` |
| `IO`, `Net` | **capabilities**: unconstructible, unforgeable; received as parameters |
| classes / interfaces / components | nominal, Java-like |

No primitive/object duality: `Int` etc. are full types. No implicit
conversions, ever (`E_TYPE_MISMATCH` suggests no coercion — convert
explicitly with `str()` / `int()`). No operator overloading: `+ - * /
% == != < <= > >=` mean exactly one thing. Boolean operators are the
keywords `and` `or` `not`.

Type inference: `let` declarations usually omit the annotation; the
compiler infers it. Annotations are required where inference cannot know:
function/method return types (optional but recommended), `state`
declarations, `stream` declarations.

## 3. Declarations and statements

```
let x = 1            // immutable binding, type inferred (Int)
let y: Float = 2.5   // explicit annotation
var n = 0            // mutable binding
n = n + 1            // assignment is an EXPRESSION (returns the value)
```

Statements: `let`/`var`, `stream`, expression statements, `return`,
`if`/`else` (statement and expression forms), `while`, `for x in list`,
`match` (expression; arms must agree in type and be exhaustive —
unhandled variants are a compile error).

## 4. Functions, purity, effects

Functions are **pure by default**. Side effects are explicit:

```
fn add(a: Int, b: Int) -> Int { return a + b }   // pure

effect fn greet(name: String) {                  // may print
  print("hello, " + name)
}

effect io fn load(path: String, io: IO) -> Result[String, String] {
  return readFile(path, io)                      // may do file IO
}

effect net fn get(url: String, net: Net) -> Result[String, String] {
  return fetch(url, net)                         // may do network IO
}
```

Effect lattice: `pure < effect(console) < effect io / effect net`.
A function may only call functions whose effect is a subset of its own
(`E_EFFECT` otherwise). `effect io` / `effect net` functions **must**
receive their capability as a parameter (`E_CAPABILITY`) — capabilities
are never ambient, except the trust root: the entry module receives
ambient `io` / `net` bindings. Imported modules never do.

Optional contracts (checked at runtime, part of the signature):

```
fn divide(a: Int, b: Int) -> Int
  requires b != 0
  ensures result * b == a
{
  return a / b
}
```

Violations fail fast: `R_CONTRACT_PRE` / `R_CONTRACT_POST`.

Errors without ceremony: `Result[T, E]` + postfix `?` (propagate `Err`
/ `None` to the enclosing function, which must return a matching
`Result`/`Option`), and `match` for handling. `try`/`catch` does not
exist; `try` as an expression is `match`.

## 5. No null: `Option[T]`

There is no null. Absence is `Option[T]` = `Some(v)` | `None`, and the
compiler enforces handling: using an `Option[T]` where `T` is expected
is `E_UNHANDLED_OPTION` (handle it with `match`, or `?` inside a
function returning `Option`). Unwrapping without handling —
`.unwrap()` / `.expect(msg)` — fails fast at runtime (`R_UNWRAP_NONE`)
instead of corrupting state ten frames later.

## 6. Concurrency

Default model: **single-threaded event loop** — the React/JS mental
model. No data races, no locks in user code. Two opt-in tools:

```
spawn { block }     // run block on a worker thread -> Task[T]
task.await()        // collect the Task's result (blocks the caller)
```

Rules, enforced by the compiler:

- `spawn` needs an effect context (spawning a thread is an effect).
- The spawn block is checked with the spawner's effect plus `async`:
  it may `await`, but it never gains new capabilities.
- `?` may not appear directly in a spawn block (`E_QUESTION`) — it
  would escape the task boundary. Handle `Result`/`Option` with `match`
  inside the block.
- `await` needs an effect context (it blocks the calling thread).

**Async discipline** — the compiler tells you what should be async:

```
async fn renderAll(ids: List[Int]) -> List[String] { ... }

async effect net fn fetchLen(url: String, net: Net) -> Int {
  let body = fetch(url, net)   // E_ASYNC_BLOCKING: blocking IO directly
  return 0                     // in an async fn stalls the event loop
}

async effect net fn fetchOk(url: String, net: Net) -> Bool {
  let t = spawn { fetch(url, net) }   // explicitly marked off-thread: OK
  return match t.await() {
    Ok(body) => true,
    Err(e) => false
  }
}
```

An `async fn` may `await` and print; calling anything with `io`/`net`
effect directly inside it is `E_ASYNC_BLOCKING` ("wrap it in
`spawn { ... }"). `async` composes with `io`/`net` (`async effect io
fn`) so offloaded IO stays expressible. Calling an `async fn` is
allowed from any effect context — `async` marks await-capability, not
a caller barrier.

**Guidance the language enforces:**

| Workload | Construct |
|---|---|
| IO-bound (fetch, read, AI calls) | `async fn` + `await` |
| CPU-bound (fib, image ops, parsing) | `spawn { }` worker pool |

Tasks are **structured**: a task's children complete before it does;
every outstanding task is joined before the program exits; an
un-awaited task failure surfaces at join time, never silently.

Threading models (`app.nova`, or `NOVA_THREADING`):

| Model | Meaning |
|---|---|
| `single` | `spawn` runs inline, deterministically. The event-loop model, exactly. |
| `multi` / `auto` | `spawn` runs on a worker thread pool (v1: sized to CPU count). |

v2 runs the whole program on a cooperative event loop (`nova_lang/loop.py`):
the root program is the root tasklet, `spawn_async { }` creates tasklets
that genuinely suspend (timer/IO/future awaitables) and resume, and blocking
builtins (`readFile`/`writeFile`/`fetch`/AI calls) are offloaded to an IO
pool when called from a tasklet so the scheduler never stalls. `spawn { }`
keeps its worker-pool parallelism. The v1 *discipline* (`E_ASYNC_BLOCKING`)
is unchanged — now the loop honors it for real.

## 6b. Structured nurseries

A `nursery { }` block scopes child tasks: every `spawn`/`spawn_async` inside
is joined before the block exits — on ALL exits. If the block raises, tasks
are cancelled (best-effort: `spawn_async` tasklets observe cancellation at
their next park point; `spawn` pool threads cannot be preempted and run to
completion) and joined suppressively; the original error propagates.

```
nursery {
  let a = spawn_async { sleep(0.2); print("a") }
  let b = spawn_async { sleep(0.1); print("b") }
}  // both joined here; "b" prints before "a"
```

## 6c. Send checking

The compiler proves task blocks only share `Send` data across `spawn`
boundaries — `E_NOT_SEND` at check time, no data-race caveat anymore.
`Send`: primitives, `Option`/`Result` of `Send`, `Task` of `Send`, streams,
`fn` references (no closures exist in Nova), and class instances whose
fields are all immutable and `Send` (data classes qualify; one `var`
field disqualifies). Not `Send`: `List` (mutable), capabilities, component
instances. Assigning to a captured variable inside `spawn { }` is also
`E_NOT_SEND`.

## 6d. Typed context

React-style dependency injection without prop drilling — explicit, typed,
compiler-visible:

```
context theme: String = "light"   // declaration with required default

effect fn show() {
  print(consume theme)            // nearest provider, else the default
}

provide theme = "dark" {          // pushes a frame for the block
  show()                          // prints "dark"
}
show()                            // prints "light" again
```

`provide` frames are kept across tasklet parks; `spawn_async` tasklets
inherit the spawner's context stack lexically.

## 7. Streams

A `stream` declaration binds a typed, first-class pub/sub topic:

```
stream events: String              // topic "events"
stream audit: String = "prod.audit" // explicit topic name
```

API (Kafka-like: topics, consumer groups, offsets):

```
events.publish("hello")                 // effect
events.publish(key: "k1", value: "hi")  // keyed publish
let sub = events.subscribe(group: "workers")   // Subscription[String]
match sub.poll() {                      // Option[Message[String]]
  None => ...,
  Some(msg) => {
    print(msg.value)                    // String
    print(msg.offset)                   // Int
    sub.commit(msg)                     // effect; offset bookkeeping
  }
}
```

Each consumer group tracks its own cursor, so groups make independent
progress over the same log. v1 ships the **in-memory provider**
(thread-safe; local dev). The provider interface
(`publish`/`poll`/`commit`) is swappable — a Kafka provider drops in
for v2 without changing user code (`NOVA_STREAMS` selects it).

## 8. Components

Everything UI is a component. Props are immutable; state is a
first-class declaration (order-independent, compiler-managed — no hook
rules). The view is a type-checked JSX-like block:

```
component Counter(title: String) {
  state count: Int = 0

  view {
    <div>
      <h1>{title}</h1>
      <p>{count}</p>
      <button onClick={count = count + 1}>Increment</button>
    </div>
  }
}
```

- `{expr}` interpolates; `<for x in items>` repeats;
  `<if cond>...</if>` conditions.
- Event handlers are assignments (`onClick={count = count + 1}`) —
  assignment is an expression, so handlers are just expressions.
  Handlers run with full effects and re-render afterwards.
- `render(Counter(title: "Clicks"))` prints the virtual tree (indented)
  and its HTML; `inst.fire("onClick")` dispatches an event.
- Props are immutable (`E_IMMUTABLE` on assignment); state mutation
  needs an effect context.
- No prop drilling: typed context (v2 — see ROADMAP.md).

## 9. Classes and interfaces

```
interface Shape {
  fn area() -> Float
}

class Circle implements Shape {
  let radius: Float
  init(radius: Float) { this.radius = radius }  // let-fields assigned in init
  fn area() -> Float { return 3.14159 * this.radius * this.radius }
}

data class Point(x: Float, y: Float)   // concise value type
```

Nominal typing with `extends` (single inheritance) and `implements`;
`init` must call `super(...)` when the parent has one (`E_SUPER`).
Methods default to pure; mark `effect` as needed. Encapsulation via
`let` (immutable) vs `var`, and `_private` naming for modules.

## 10. Modules

```
import helpers                    // helpers.nova, same directory
helpers.greet("Ada")              // functions, classes, components
```

Explicit imports only — no classpath. `_names` are module-private
(`E_IMPORT` on access). `nova lock` pins every transitive import by
sha256 into `nova.lock`; `nova run --locked` verifies before running
(supply-chain story, §15).

## 11. AI integration

First-class `ai` blocks — the return **type** is the output schema:

```
data class Summary(headline: String, score: Float)

ai fn summarize(text: String) -> Summary
  model = "nova-mock-1"
  temperature = 0.3
  max_tokens = 256
{
  prompt("Summarize in one sentence: " + text)
}
```

- Config: `model`, `temperature`, `max_tokens`, `stream` (validated;
  `E_AI_CONFIG` otherwise).
- The body must call `prompt(...)` (`R_AI_NO_PROMPT` otherwise).
- The provider must return structured data conforming to the schema;
  JSON coercion is automatic (`schema_json` derives JSON Schema from
  Nova types).
- v1 runs against a deterministic **mock provider** (no network, no
  key, reproducible). `NOVA_AI_PROVIDER=http` selects the experimental
  HTTP hook (`NOVA_AI_BASE_URL`, `NOVA_AI_KEY`) — roadmap, not stable.
- Calling an `ai fn` needs an effect context; it has `net` effect, so
  from `async` code it must be `spawn`-wrapped like any network IO.

## 12. Capabilities and secrets

`IO` / `Net` are unconstructible capability types. `secret(name, io)`:

```
effect io fn dbPass(io: IO) -> Result[String, String] {
  return secret("db-password", io)
}
```

Secrets are referenced **by name** and resolved by a capability-gated
store — never in source. v1 store: `NOVA_SECRET_<NAME>` environment
variables. Managed stores (AWS/GCP) are v2.

## 13. Security pillar

Nova treats security as a language feature, not a library concern:

1. **No eval, no dynamic code execution primitive.** There is no
   `eval`, no `Function(...)`, no runtime code loading. What runs is
   what the compiler saw. (Threat defeated: prompt-injection-adjacent
   codegen — an AI generating Nova cannot smuggle executable strings
   past review; there is no string-to-code primitive to target.)
2. **No ambient authority.** IO/network/secrets require explicit
   capabilities as parameters (§4, §12). A function's signature is its
   authority manifest — auditable by humans and AI alike.
3. **Checked integer arithmetic.** Overflow is `R_INT_OVERFLOW`, never
   wraparound. (Threat defeated: the classic memory-safety-adjacent
   integer bug class.)
4. **Pinned supply chain.** `nova lock` hashes every transitive import;
   `--locked` runs refuse tampered modules. The manifest story extends
   this to deployments.
5. **Structured diagnostics.** Every failure — compile or runtime —
   is a stable error code with a message, location and suggested fix
   as JSON: built for AI consumers to act on, not just humans to read.
6. **Async discipline as safety.** Blocking IO cannot silently stall
   shared execution contexts (§6).

## 14. App manifest and deployment

`app.nova` (optional, built-in) — convention over configuration:

```
app "shop" {
  version = "0.1.0"
  entry = "main.nova"
  threading = "auto"              // single | multi | auto
  env prod { log_level = "info" } // non-secret config -> env vars
  resource stream "events" { partitions = 4 }
  resource secret "db-password" { store = "env" }
  resource cron "nightly" { schedule = "0 2 * * *", run = "report.nova" }
  docker { base = "python:3.12-slim", port = 8080 }
  deploy target = "local"         // local | docker
}
```

- `nova build` — validates the manifest, type-checks the entry,
  stages `dist/` (entry + transitive imports + generated Dockerfile).
- `nova deploy --target local` — runs the entry as a process with the
  env section applied; secrets resolve from the store at runtime.
  `resource cron` entries run on their schedules in a background
  scheduler thread (minute granularity).
- `nova deploy --target docker` — `docker build`s the staged image
  (or prints the exact command if docker is absent).
- v1 targets: local process + Docker image. Cloud targets (AWS/GCP)
  and managed secret stores are v2 (ROADMAP.md).

Full walkthrough: `docs/deployment.md`.

## 15. Toolchain

One command, no toolchain maze:

```
nova run [--locked] file.nova|.nvb  run (auto-discovers app.nova for threading;
                                .nvb executes on NovaVM)
nova check file.nova            type-check; diagnostics as JSON on stdout
nova fmt file.nova              canonical formatting (the ONE true style)
nova lock file.nova             pin imports into nova.lock
nova migrate --from java|jsx f  EXPERIMENTAL mechanical migrator
nova build [--manifest app.nova] [--emit-bytecode]
                                type-check + stage dist/ (+ Dockerfile);
                                --emit-bytecode writes dist/<entry>.nvb
nova dom file.nova -o out.html  EXPERIMENTAL: components -> self-contained HTML
nova eval suite.json [--provider mock|http] [--out report.json]
                                eval harness: prompts -> predicates -> JSON
nova lsp                        EXPERIMENTAL LSP server (JSON-RPC over stdio)
nova deploy [--target local|docker|aws|gcp] [--env NAME]
```

`nova fmt` is canonical — there is exactly one way to format Nova
code, enforced by the toolchain (AI-native: no style debates, no
style tokens wasted).

## 16. Diagnostics

Every diagnostic is JSON: `{"code", "message", "line", "col", "hint?"}`.
Stable codes (selection):

| Code | Meaning |
|---|---|
| `E_NO_NULL` | `null`/`nil`/`undefined` used — use `Option[T]` |
| `E_TYPE_MISMATCH` | types don't unify; no implicit conversions |
| `E_UNHANDLED_OPTION` | `Option[T]` used where `T` expected |
| `E_EFFECT` | effect context insufficient |
| `E_ASYNC_BLOCKING` | blocking IO directly in an `async fn` — wrap in `spawn` |
| `E_QUESTION` | `?` misused (incl. directly in `spawn` blocks) |
| `E_CAPABILITY` | `effect io`/`net` fn missing its capability parameter; capabilities constructed or shadowed |
| `E_IMMUTABLE` | assignment to immutable binding/prop/let-field |
| `E_INT_RANGE` / `R_INT_OVERFLOW` | literal out of i64 range / arithmetic overflow |
| `E_NON_EXHAUSTIVE` | `match` misses a variant |
| `E_IMPORT` / `E_LOCK` | module problems / lockfile mismatch |
| `E_AI_SCHEMA` / `E_AI_CONFIG` / `R_AI_NO_PROMPT` | AI block problems |
| `R_UNWRAP_NONE` / `R_UNWRAP_ERR` | unwrap on `None` / `Err` |
| `R_CONTRACT_PRE` / `R_CONTRACT_POST` | contract violation |
| `R_TASK_FAILED` / `R_TASK_LEAK` | task failure / `?` escaping a task |
| `E_NOT_SEND` | mutable/shared state captured across a `spawn` boundary |
| `E_TASKLET_CONTEXT` | `spawn_async` outside the event loop (e.g. in a pool thread) |
| `R_DEADLOCK` | tasklets parked with no possible waker |
| `R_CANCELLED` | tasklet cancelled (e.g. nursery exit) |

## 17. What Nova deliberately omits (v1)

No first-class functions, no operator overloading, no implicit
conversions, no exceptions, no `try`/`catch`, no null, no eval, no
macros, no inheritance of implementation beyond single `extends`, no
method overloading, no global mutable state (module-level `var` is
scoped to the module and discouraged).

## 18. NovaVM (v2 — built)

v1's tree-walking interpreter is the reference semantics. The
portability story is NovaVM: a small, portable bytecode
(`nova_lang/bytecode.py`: compiler, assembler/disassembler, `.nvb`
format with magic `NVB1`) executed by a dispatch-loop VM
(`nova_lang/vm.py`) — still Python, stdlib only: the point is
portability of the artifact, not speed.

- `nova build --emit-bytecode` compiles the entry to `dist/<entry>.nvb`
  (type-check runs first; anything outside the subset is a clear
  `CompileError`, never silently miscompiled).
- `nova run file.nvb` executes on the VM. Checked 64-bit int
  arithmetic, floats, strings, bools, lists, Option/Result, closures,
  recursion — with interpreter↔VM output parity on the supported
  subset.
- Subset (honest): arithmetic/vars/functions/control-flow/lists/
  Option/Result/print. Out: effects, capabilities, spawn, streams,
  classes, components, AI — compile-time rejections.

Full opcode set, file format, and limits: `docs/v2-bytecode.md`.
