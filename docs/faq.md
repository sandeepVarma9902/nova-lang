# Nova FAQ — for the skeptical React/Java developer

## 1. Why no null?
Null is a billion-dollar mistake: it's the absence of a value hiding behind a type that *claims* to have one, so every dereference is a gamble. Nova makes absence explicit — `Option[T]` = `Some(v)` | `None` — and the compiler refuses to let you use it without handling both cases (`E_UNHANDLED_OPTION`). The JS pattern `value?.nested?.field ?? "default"` becomes a `match` the compiler checks for exhaustiveness. You trade one line of `?.` for never shipping a `TypeError: Cannot read properties of undefined`.

## 2. Why no exceptions?
Exceptions create invisible control flow — `fetchUser(7)` can return from anywhere, caught (or not) frames away. Nova's `Result[T, E]` keeps failure in the type signature: a function that can fail *says so*. `?` gives you the flat happy path of `try/catch` without the hidden jumps, and `match` is your explicit catch block. What Java does with checked exceptions Nova does with `Result` + `?` — minus the `throws` ceremony.

## 3. Why no `async`/`await` like JS?
JS's `async/await` is syntactic sugar over Promises with no opinion about *what* runs where. Nova splits the two workloads and enforces the split: `spawn { }` runs a block on a worker thread (CPU-bound → parallel), `async fn` marks a function that may `await` (IO-bound → coordinate). Calling blocking IO directly inside an `async fn` is a compile error (`E_ASYNC_BLOCKING`), not a slow Tuesday. There's no `Promise` type — a `Task[T]` is a structured, join-guaranteed computation, and `task.await()` blocks the caller explicitly. The mental model: `async` marks *await-capability*, not a caller barrier — any effect context can call an `async fn`, and `async` composes with `io`/`net` (`async effect net fn`) so offloaded IO stays expressible. Note the v1 honesty clause: `async` currently runs on the calling thread and `await` blocks it — the *discipline* is fully enforced by the compiler, while the true non-blocking event-loop scheduler arrives with NovaVM in v2 (SPEC §6).

## 4. Is the interpreter slow?
v1 is a tree-walking interpreter — the *reference semantics*, not the performance story. The production story is **NovaVM**: a small, portable bytecode VM the v2 compiler targets (SPEC §18). Think of v1 like TypeScript's `tsc --noEmit` + `ts-node` phase: correctness and tooling first, codegen second. For today's workloads, `threading = "multi"` parallelizes CPU-bound work on a worker pool; the interpreter is not the bottleneck for IO-bound code, which is most code. Concretely: if your program spends its time waiting on network/AI/streaming, you won't feel the interpreter. If you're doing heavy number-crunching in Nova itself, that's what `spawn` + NovaVM are for.

## 5. How do I do `useState`?
`state count: Int = 0` inside a component. No hook call, no rules-of-hooks, order-independent, compiler-managed. State annotations are required (the compiler can't infer from nothing), updates are assignments, and firing an event re-renders.

## 6. How do I do `useEffect`?
You don't have implicit render-side-effects. Side effects live in explicit `effect fn`s, and component event handlers run with full effects and re-render afterwards. Lifecycle hooks are a v2 conversation; v1 wants effects called deliberately, not as render fallout.

## 7. How do I avoid prop drilling?
v1: pass props explicitly (they're type-checked, immutable). Typed context is a v2 item (ROADMAP.md) — the SPEC flags it explicitly in §8, so don't build your own context hack; wait for the typed one.

## 8. How do I do `fetch().then(...)`?
```nova
effect net fn load(url: String, net: Net) -> Result[String, String] {
  return fetch(url, net)
}
effect net fn run(net: Net) -> Result[String, String] {
  let body = load("https://api.example.com", net)?  // Err propagates
  return Ok(body)
}
```
The `net` capability arrives as a parameter (never ambient). From `async` code, spawn-wrap it: `let t = spawn { fetch(url, net).unwrap() }; t.await()` — otherwise `E_ASYNC_BLOCKING`.

## 9. What replaces Java's checked exceptions?
`Result[T, E]` + `?`. Where Java writes `throws IOException`, Nova writes `-> Result[String, String]`. Where Java writes `catch`, Nova writes `match`. The exhaustiveness check replaces the compiler's "unhandled exception" error — with less paperwork.

## 10. What replaces `Optional<T>`?
`Option[T]`. Same shape, deeper enforcement: Java lets you call `get()` and blow up at runtime; Nova's `.unwrap()` fails fast (`R_UNWRAP_NONE`) and the compiler pushes you toward `match` or `?` instead. `Optional.map` chains become `match` arms — more verbose, but every branch is visible.

## 11. What replaces the classpath / Maven?
Explicit `import module_name` (file-based, namespaced) plus `nova lock`, which pins every transitive import by sha256 into `nova.lock`. `nova run --locked` refuses tampered modules. No dependency resolution algorithm, no version ranges — v1 is deliberately small; a package registry is a v2 question.

## 12. What's the threading model?
Default: **single-threaded event loop** (the React/JS mental model — no data races, no locks in your code). Opt-in: `spawn { }` for worker-thread parallelism. `threading = "single"` in app.nova makes `spawn` run inline and deterministic — your tests' best friend (the test suite itself runs with `NOVA_THREADING=single`). `multi`/`auto` use a worker pool sized to CPU count. Tasks are structured: children complete before their parent, everything is joined before exit. Two rules the compiler enforces so you can't misuse this: spawning needs an effect context (starting a thread *is* an effect), and the spawn block never gains new capabilities beyond the spawner's — capabilities can't leak across the thread boundary.

## 13. Can I use npm packages?
No — v1. Nova has no JS interop and no package registry yet. The stdlib-only repo keeps the supply chain tiny and auditable (SPEC §13). If you need a capability that doesn't exist, that's a v2/roadmap conversation, not a `require()` hack.

## 14. How much do AI calls cost? Are they deterministic?
v1 runs against a **deterministic mock provider** — zero cost, zero network, reproducible output. Cost only enters with `NOVA_AI_PROVIDER=http` (experimental), where your own key and base URL apply and cost is whatever your provider charges. Determinism in production is your provider's contract to honor; Nova's contract is the *schema* — the return type is the output schema and JSON coercion is automatic. Practical advice: develop and test against the mock (it's deterministic, so tests are stable), and treat the HTTP hook as integration-testing territory, not a v1 deployment path.

## 15. Why no operator overloading?
Because `a + b` should mean exactly one thing everywhere. Overloading turns reading code into archaeology — Nova is AI-native, and both humans and models read code faster when operators are fixed. Need custom behavior? Name a function.

## 16. Why is assignment an expression?
So event handlers stay trivial: `onClick={count = count + 1}` is just an expression the handler runs — no `setState` wrapper, no callback form. It also means `let x = (y = 5)` works, though you should rarely write it. The one caution: `if x = 5` *assigns* and tests the result — comparison is always `==`.

## 17. What happens on integer overflow?
It's a runtime error — `R_INT_OVERFLOW` — never silent wraparound, and literals outside i64 range are rejected at compile time (`E_INT_RANGE`). Checked arithmetic is a security feature (SPEC §13), not a performance choice. If you need wrapping or big integers, that's explicit API, not default behavior.

## 18. How do errors surface to AI agents?
Every diagnostic — compile or runtime — is structured JSON: `{"code", "message", "line", "col", "hint?"}` with a *stable* code (`E_NO_NULL`, `R_TASK_FAILED`, …). `nova check` prints diagnostics as JSON on stdout. No regex-parsing log lines: an agent can pattern-match on the code and follow the hint. This is a deliberate design pillar (§13.5).

## 19. What happens on an un-awaited task failure?
Nothing silent. Tasks are structured: every outstanding task is joined before the program exits, and an un-awaited task failure surfaces **at join time** (`R_TASK_FAILED`), never swallowed. There is no fire-and-forget that can lose an error — that's the point of structured concurrency. Compare with JS, where an unhandled promise rejection may log a warning and vanish: in Nova the program's exit join is the backstop, and it fails loudly. If you need "I genuinely don't care about the result," you still have to `await` it and `match` away the outcome explicitly — the language makes you sign for it.

## 20. Where does `?` *not* work?
Directly inside a `spawn { }` block (`E_QUESTION`) — it would escape the task boundary, and the task boundary is where structured concurrency draws its line. Handle `Result`/`Option` with `match` inside the block, then return the handled value. `?` also requires the enclosing function to return a matching `Result`/`Option`; the compiler tells you when it doesn't.
