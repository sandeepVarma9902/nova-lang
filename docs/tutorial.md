# Learn Nova in 30 minutes

You already know React and JavaScript — this tour maps Nova onto what you know. Every snippet runs right here in your browser: hit **Edit & run**, then **Run**. Break things on purpose; the error messages will teach you.

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

That's the whole deal: `let` never changes (like `const`), types are usually figured out for you, and there's no `any` — every value has a known type. Function parameters always need types, because the compiler can't guess your API.

## (b) Functions are pure by default

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

A pure function that tries to `print` is a compile error — you say what a function may do in its signature (`effect`, `effect io`, `effect net`). It's React's pure-render vs `useEffect` split, except the compiler enforces it. Capabilities like `IO` are passed as parameters, so side effects can't sneak in.

## (c) No null — Option and Result instead

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

`null` and `undefined` don't exist. `match` must handle every case (the compiler checks). `?` is early-return for errors — like `try/catch` without the invisible jumps.

## (d) Components: props, state, events

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

Props are immutable. State is declared, not hooked — no rules-of-hooks, no stale closures. The click handler is just an assignment; Nova re-renders after it. (In the playground above, component output also renders as a live browser preview.)

## (e) Classes without the boilerplate

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

`interface`, `implements`, `extends` work like Java. `data class` is Java's `record` with `==` that just works.

## (f) Modules

```nova-norun
// nova_helpers.nova
fn greet(name: String) -> String {
  return "Hello, " + name + "!"
}
```

```nova-norun
// app.nova (entry)
import nova_helpers
print(nova_helpers.greet("Ada"))   // namespaced — no classpath, no collisions
```

Imports are plain file references (like ES modules). Names starting with `_` stay private to their module. `nova lock` pins every dependency by hash so builds are reproducible:

```bash
nova lock app.nova            # write hashes into nova.lock
nova run --locked app.nova    # refuse to run if anything changed
```

## (g) Async done right: spawn vs await

```nova
fn fib(n: Int) -> Int {
  if n < 2 { return n }
  return fib(n - 1) + fib(n - 2)
}

// IO-bound -> async: coordinate with await
async fn doubleAll(xs: List[Int]) -> List[Int] {
  let out: List[Int] = []
  for x in xs {
    let s = spawn { x * 2 }
    out.push(s.await())
  }
  return out
}

// CPU-bound -> parallel: spawn runs on a worker thread
let t = spawn { fib(20) }
print(t.await())

// IO-bound -> async: coordinate with await
print(doubleAll([1, 2, 3]))
```

Two tools, one rule: **`spawn { }` for CPU work** (real parallelism), **`async fn` + `await` for IO** (cooperative, never blocks the loop). The compiler rejects blocking IO inside `async fn` — wrap it in `spawn { }` instead.

**Streams** (typed pub/sub):

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
main()
```

## (h) AI functions, secrets, deploy

```nova
// The return TYPE is the output schema — the provider must conform to it.
data class Summary(headline: String, score: Float)

ai fn summarize(text: String) -> Summary
  model = "nova-mock-1"
  temperature = 0.3
{
  prompt("Summarize in one sentence: " + text)
}

effect net fn run(net: Net) {
  let s = summarize("Nova is an AI-native language.")
  print(s.headline)
}
run(net)
```

An `ai fn` is a typed function whose body is a prompt — the return type is the output schema. It needs the `net` effect and a `Net` capability (AI calls are network calls). This runs against a deterministic mock: no key, no network, same output every time.

Secrets never live in source:

```nova
effect io fn dbPass(io: IO) -> Result[String, String] {
  return secret("db-password", io)   // resolves NOVA_SECRET_DB_PASSWORD at runtime
}
```

Ship it with a manifest (`app.nova` next to your entry file):

```nova-norun
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
nova build                    # type-checks, stages dist/
nova deploy --target local    # runs as a process; secrets resolve at runtime
nova deploy --target docker   # docker build of the staged image
```

Full walkthrough: [deployment.md](./deployment.md).

## (i) Naming conventions — 2 minutes

Types look like Java, functions look like JavaScript. One rule each — no debates:

| Name kind | Style | Example |
|---|---|---|
| Classes, interfaces, data classes, components | PascalCase | `UserCard`, `HttpRequest` |
| Functions, methods, variables | camelCase | `getUser`, `retryCount` |
| Constants (`let`, immutable) | SCREAMING_SNAKE_CASE | `MAX_RETRIES` |
| Module files | snake_case | `helpers.nova` |
| Test files | `*_test.nova` | `user_test.nova` |

```nova-norun
// ✅ Do
class UserService { ... }
component UserCard(user: User) { ... }
fn getUser(id: Int) -> Option[User] { ... }
let MAX_RETRIES = 3               // immutable: SCREAMING_SNAKE_CASE by convention

// ❌ Don't
class userService { ... }      // types are PascalCase
fn GetUser(id: Int) { ... }    // functions are camelCase
```

Two extras: `_helper` is module-private by convention; acronyms read as words (`HttpRequest`, `userId`). `nova fmt` fixes whitespace, not names.

---

## Where next

- **Why Nova, and how it compares:** [Why Nova for your domain →](./domains.md)
- **Deep dives:** SPEC §1–§3 (syntax + types) · §4 (effects) · §6 (concurrency) · §8 (components) · §11 (AI) · §14 (deploy)
- **Migrating existing code:** [migration-guide.md](./migration-guide.md)
- **Skeptical questions:** [faq.md](./faq.md)
