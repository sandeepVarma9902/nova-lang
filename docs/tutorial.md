# Learn Nova in 30 minutes

A guided tour for a React developer. Every snippet below is runnable with `nova run <file>.nova` — type it out, run it, break it, read the error. Nova's diagnostics are designed to teach you (each is a stable code + hint, JSON-readable; see SPEC §16).

## (a) Hello + type inference — 2 minutes

```nova
let greeting: String = "Hello"
let target = "Nova"          // inferred: String — no annotation needed
let year = 2026              // inferred: Int (signed 64-bit)
let pi = 3.14159             // inferred: Float

fn add(a: Int, b: Int) -> Int {
  return a + b
}

fn greet(who: String) -> String {
  return greeting + ", " + who + "!"
}

print(greet(target))
```

**Notice:** `let` is immutable; types are usually inferred, but function *parameters* always need types (the compiler can't guess your API). There is no `any` — Nova never lets a value escape without a type.

**Analogy:** `let` ≈ `const`, `var` ≈ `let`. Unlike TypeScript, you almost never write `as`; there are no implicit conversions — `str(pi)` converts a Float to a String explicitly.

## (b) Functions, purity, effects

```nova
fn double(x: Int) -> Int {   // pure: no side effects allowed
  return x * 2
}

effect fn shout(msg: String) {  // may print — declared up front
  print(msg + "!")
}

effect io fn load(path: String, io: IO) -> Result[String, String] {
  return readFile(path, io)   // needs the IO capability as a parameter
}
```

**Notice:** Functions are pure by default. A pure function calling `print` is a compile error (`E_EFFECT`). Effect levels nest: `pure < effect < effect io / effect net`. A function's signature is its authority manifest — you can see exactly what a function is allowed to do without reading its body.

**Analogy:** Think of React's separation of pure render vs `useEffect` — except the compiler enforces it, not convention. Capabilities (`IO`, `Net`) are like requiring a prop for side effects: you can't sneak one in.

## (c) Option, Result, `?`, match — the no-null story

```nova
// JS: function getUser(id) { return db.find(id) ?? undefined }
// Nova: absence is a TYPE, not a forgotten value.

fn findItem(items: List[String], query: String) -> Option[String] {
  for item in items {
    if item == query {
      return Some(item)
    }
  }
  return None
}

match findItem(["Buy milk", "Read book"], "Gym") {
  Some(item) => print("Found: " + item),
  None => print("Not on the list")
}

// Errors work the same way — Result instead of throw:
effect fn fetchUser(id: Int) -> Result[String, String] {
  if id == 1 {
    return Ok("Ada")
  }
  return Err("user " + str(id) + " not found")
}

effect fn greetUser(id: Int) -> Result[String, String] {
  let name = fetchUser(id)?   // Err returns early — the happy path stays linear
  return Ok("Hello, " + name + "!")
}
```

**Notice:** `null`, `nil`, `undefined` don't exist — using one is `E_NO_NULL`. `match` arms must be *exhaustive* (missing `None` is a compile error, `E_NON_EXHAUSTIVE`). `?` propagates `Err`/`None` to the enclosing function, which must itself return `Result`/`Option` — the compiler connects the dots for you.

**Analogy:** `Option<T>` ≈ TypeScript's `T | undefined` but enforced: you literally cannot touch the value without handling both arms. `Result` + `?` ≈ `try/catch` flattened — no invisible jumps.

## (d) Your first component: props, state, events, re-render

```nova
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

let app = render(Counter(title: "Clicks"))
app.fire("onClick")   // state → 1, re-renders automatically
```

**Notice:** Props are immutable (`title = "x"` is `E_IMMUTABLE`). State is a first-class declaration — order-independent, no rules-of-hooks: you cannot call it "in a loop by accident" because there is no hook call at all. Assignment is an *expression*, so the handler is just `count = count + 1` — no `setCount(c => c + 1)` wrapper. Handlers run with full effects and re-render afterwards.

**Analogy:** `component Counter(title: String)` ≈ `function Counter({ title })`. `state count: Int = 0` ≈ `useState(0)` minus the footguns. `<for x in items>` ≈ `.map`, `<if cond>` ≈ ternary.

## (e) Classes, interfaces, data classes

```nova
interface Shape {
  fn area() -> Float
}

class Circle implements Shape {
  let radius: Float
  init(radius: Float) { this.radius = radius }
  override fn area() -> Float {
    return 3.14159 * this.radius * this.radius
  }
}

// Java's POJO + getters + setters + equals + toString — in one line:
data class Point(x: Float, y: Float)

let p = Point(x: 1.0, y: 2.0)
print(p == Point(x: 1.0, y: 2.0))   // true — structural equality built in
```

**Notice:** `let` fields are assigned once in `init`. Methods are pure unless marked `effect`. `data class` gives you fields, construction, `==`, and a readable print — the mechanical parts of a POJO without the boilerplate.

**Analogy:** `data class` ≈ Java `record`, but with structural `==` out of the box. `interface`/`implements`/`extends` work exactly like Java.

## (f) Modules + `nova lock`

```nova
// nova_helpers.nova
fn greet(name: String) -> String {
  return "Hello, " + name + "!"
}
```

```nova
// app.nova (entry)
import nova_helpers
print(nova_helpers.greet("Ada"))   // namespaced — no classpath, no collisions
```

`_private` names (leading underscore) are invisible outside their module — `E_IMPORT` if you try. Then pin the supply chain:

```bash
nova lock app.nova        # hashes every transitive import into nova.lock
nova run --locked app.nova  # refuses to run if anything was tampered with
```

**Notice:** No `node_modules` abyss: imports are explicit file references, and `nova.lock` makes them content-addressed.

**Analogy:** `import x` ≈ ES modules, `nova.lock` ≈ `package-lock.json` — except it hashes *source files*, not registry tarballs.

## (g) async / spawn / streams

```nova
fn fib(n: Int) -> Int {
  if n < 2 { return n }
  return fib(n - 1) + fib(n - 2)
}

effect fn main() {
  // CPU-bound -> parallel: spawn runs on a worker thread
  let t = spawn { fib(30) }

  // IO-bound -> async: coordinate with await
  async fn doubleAll(xs: List[Int]) -> List[Int] {
    let out: List[Int] = []
    for x in xs {
      let s = spawn { x * 2 }
      out.push(s.await())
    }
    return out
  }

  print(t.await())
}
```

**The rule the compiler enforces:**

| Workload | Construct |
|---|---|
| IO-bound (fetch, read, AI calls) | `async fn` + `await` |
| CPU-bound (fib, image ops, parsing) | `spawn { }` worker pool |

**Notice:** Calling blocking IO *directly* inside an `async fn` is a compile error — `E_ASYNC_BLOCKING` ("wrap it in `spawn { }`"). This is Nova teaching you the architecture: the async function would stall the event loop. `?` may not appear directly in a `spawn` block (`E_QUESTION`) — handle `Result` with `match` inside the task.

**Analogy:** This is Node's event-loop model with teeth. Nova's `async`/`spawn`/`await` are not three spellings of JS's `async/await` — Nova has no `await` keyword floating around promises; `async fn` marks *await-capability*, and `Task[T].await()` blocks the caller. There's nothing like a JS `Promise` — a `Task` is a structured, join-guaranteed computation.

**Streams** (typed pub/sub, Kafka-shaped):

```nova
stream events: String

effect fn main() {
  let sub = events.subscribe(group: "workers")
  events.publish("hello")
  match sub.poll() {
    Some(msg) => print(msg.value),
    None => print("(empty)")
  }
}
```

## (h) ai fn + secret + app.nova + build/deploy — 5 minutes

```nova
// The return TYPE is the output schema — the provider must conform to it.
data class Summary(headline: String, score: Float)

ai fn summarize(text: String) -> Summary
  model = "nova-mock-1"
  temperature = 0.3
{
  prompt("Summarize in one sentence: " + text)
}

effect fn run() {
  let s = summarize("Nova is an AI-native language.")
  print(s.headline)
}
```

**Notice:** v1 runs against a deterministic **mock provider** — no network, no key, reproducible output. `ai fn` has `net` effect: from `async` code, spawn-wrap it like any network IO. `prompt(...)` is mandatory in the body (`R_AI_NO_PROMPT` otherwise).

**Secrets** are named, never in source:

```nova
effect io fn dbPass(io: IO) -> Result[String, String] {
  return secret("db-password", io)   // resolves NOVA_SECRET_DB_PASSWORD at runtime
}
```

**Manifest** — `app.nova` sits next to your entry file:

```nova
app "myapp" {
  version = "0.1.0"
  entry = "main.nova"
  threading = "auto"              // single | multi | auto
  env prod { log_level = "info" }
  resource secret "db-password" { store = "env" }
  docker { base = "python:3.12-slim", port = 8080 }
  deploy target = "local"         // local | docker
}
```

```bash
nova build                    # validates manifest, type-checks, stages dist/
nova deploy --target local    # runs as a process; secrets resolve at runtime
nova deploy --target docker   # docker build of the staged image
```

Full walkthrough: [deployment.md](./deployment.md).

## (i) Naming conventions — 2 minutes

Nova takes its naming from the two languages it fuses: types look like
Java, functions look like JavaScript. One rule per kind of name — no
code-review debates.

| Name kind | Style | Example |
|---|---|---|
| Classes, interfaces, enums, data classes | PascalCase | `User`, `HttpRequest` |
| Components | PascalCase | `UserCard`, `Counter` |
| Functions, methods | camelCase | `getUser`, `calculateTotal` |
| Variables, parameters | camelCase | `userName`, `retryCount` |
| Module-level constants | SCREAMING_SNAKE_CASE | `MAX_RETRIES`, `DEFAULT_PORT` |
| Type parameters | single capital letter | `T`, `E`, `K`, `V` |
| Module files | snake_case | `helpers.nova` → `import helpers` |
| Test files | `*_test.nova` | `user_test.nova` (found by `nova test`) |

```nova
// ✅ Do
class UserService { ... }
component UserCard(user: User) { ... }
fn getUser(id: Int) -> Option[User] { ... }
let retryCount = 0
const MAX_RETRIES = 3

// ❌ Don't
class userService { ... }      // types are PascalCase
fn GetUser(id: Int) { ... }    // functions are camelCase
let MAX = 3                    // screaming case is for constants only
```

Three special rules:

1. **Private is `_`-prefixed** — `_helper`, `_CACHE` are module-private by
   convention, like Python.
2. **View tags are CamelCase** — inside a component view, `<UserCard>`
   works; `<user-card>` is a compile error. The parser enforces this one.
3. **Acronyms stay readable** — `HttpRequest`, not `HTTPRequest`;
   `userId`, not `userID`. Treat an acronym like a word.

`nova fmt` fixes your whitespace, not your names — naming is a human
(and code-review) job. The full normative list lives in SPEC §1.

---

## Where next

- **Syntax + type system deep-dive:** SPEC §1–§3
- **Effects lattice, contracts, `?`:** SPEC §4
- **Concurrency & async discipline:** SPEC §6
- **Components:** SPEC §8 · **Classes:** SPEC §9
- **AI integration:** SPEC §11 · **Secrets/capabilities:** SPEC §12
- **Manifest + deployment:** SPEC §14, [deployment.md](./deployment.md)
- **Migrating existing code:** [migration-guide.md](./migration-guide.md) (v1 deliverable)
- **Skeptical questions answered:** [faq.md](./faq.md)
- **What's coming in v2:** ROADMAP.md (NovaVM bytecode, cloud deploy targets, managed secrets, cron scheduler, typed context for components)
