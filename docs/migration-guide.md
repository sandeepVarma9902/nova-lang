# Nova Migration Guide (v1)

This is the v1 migration deliverable: two construct-mapping tables plus mechanical recipes with worked examples. The companion tool — `nova migrate --from java|jsx <file>` — is an **experimental scaffold** (see "What the scaffold handles" below); these tables are the source of truth.

## Table A — Java → Nova

| Java | Nova | Notes |
|---|---|---|
| `class Foo { ... }` | `class Foo { ... }` | Same shape; `init` replaces constructors |
| `interface I { ... }` | `interface I { ... }` | Method signatures as `fn` declarations |
| POJO + getters/setters | `data class Point(x: Float, y: Float)` | Fields, init, `==`, readable print — one line |
| `record` | `data class` | Structural equality included |
| `null` / `Optional<T>` | `Option[T]` (`Some`/`None`) | Compiler-enforced handling; no `.get()` roulette |
| checked exceptions (`throws`) | `Result[T, E]` + `?` | Failure in the signature, no ceremony |
| `try`/`catch` | `match` on `Result` | Exhaustive arms = your catch blocks |
| `synchronized` / locks | `spawn` + structured tasks | No user-space locks; default is single-threaded |
| `static` field/method | module-level `let` / `fn` | Modules replace the static-holder pattern |
| `package` / classpath | `import module_name` | Explicit, file-based; no resolution algorithm |
| Streams API (`.map/.filter`) | `List` methods + `for` loops | Plain iteration; v1 keeps it simple |
| `CompletableFuture<T>` | `Task[T]` + `task.await()` | Structured: joined before exit, failures surface |
| `int/long` | `Int` (i64, checked) | Overflow is `R_INT_OVERFLOW`, never wraparound |
| `double/float` | `Float` | No implicit numeric conversions — ever |

## Table B — React/JSX → Nova

| React | Nova | Notes |
|---|---|---|
| `function C(props)` | `component C(title: String)` | Props are typed parameters, immutable |
| `useState(0)` | `state count: Int = 0` | No hooks, no rules-of-hooks; order-independent |
| props | component params (immutable) | Assignment to a prop is `E_IMMUTABLE` |
| `useEffect` | explicit `effect fn` + event handlers | No implicit render side-effects; lifecycle is v2 |
| `onClick={() => setX(x+1)}` | `onClick={count = count + 1}` | Assignment is an expression — handlers are just expressions |
| conditional rendering (ternary/`&&`) | `<if cond>...</if>` | Type-checked JSX-like block |
| `items.map(...)` | `<for item in items>...</for>` | Repeats a template over a list |
| Context API | v2 typed context (ROADMAP.md) | v1: pass props explicitly |
| `fetch().then(...)` | `effect net fn` + `?` | `net` capability as parameter; `Err` propagates |
| `undefined` / optional chaining | `Option` / `None` + `match` | Exhaustiveness replaces `?.` guesswork |

---

## Recipe 1 — Java POJO with getters/setters → data class

**Before (Java):**
```java
public class Point {
  private double x;
  private double y;
  public Point(double x, double y) { this.x = x; this.y = y; }
  public double getX() { return x; }
  public double getY() { return y; }
  // + equals + hashCode + toString (~40 lines total)
}
```

**After (Nova):**
```nova
data class Point(x: Float, y: Float)

let p = Point(x: 1.0, y: 2.0)
print(p)                              // Point(x: 1.0, y: 2.0)
print(p == Point(x: 1.0, y: 2.0))     // true
```

**Recipe:** fields become constructor params; delete getters/setters/equals/hashCode/toString — the `data class` generates them. Mutable POJOs: declare `var` fields in a regular `class` with `init` instead.

## Recipe 2 — try/catch → Result + match

**Before (Java):**
```java
String greetUser(int id) {
  try {
    String name = fetchUser(id);   // throws UserNotFoundException
    return "Hello, " + name + "!";
  } catch (UserNotFoundException e) {
    return "Error: " + e.getMessage();
  }
}
```

**After (Nova):**
```nova
effect fn fetchUser(id: Int) -> Result[String, String] {
  if id == 1 { return Ok("Ada") }
  return Err("user " + str(id) + " not found")
}

effect fn greetUser(id: Int) -> Result[String, String] {
  let name = fetchUser(id)?        // Err returns early — no catch block
  return Ok("Hello, " + name + "!")
}

match greetUser(7) {
  Ok(msg) => print(msg),
  Err(e) => print("Error: " + e)
}
```

**Recipe:** (1) change the throwing method's return to `Result[T, String]` (or a typed error); (2) replace `throw` with `return Err(...)`; (3) replace `try` with `?` on each fallible call inside a `Result`-returning function; (4) replace `catch` with `match` arms at the handling site. Each step is mechanical.

## Recipe 3 — React counter → Nova component

**Before (React):**
```jsx
function Counter({ title }) {
  const [count, setCount] = useState(0);
  return (
    <div>
      <h1>{title}</h1>
      <p>{count}</p>
      <button onClick={() => setCount(c => c + 1)}>Increment</button>
    </div>
  );
}
```

**After (Nova):**
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
```

**Recipe:** (1) props destructure → typed component params; (2) each `useState` → a `state` declaration (annotation required); (3) `setX(...)` → assignment `x = ...`; (4) ternaries/`&&` → `<if>`, `.map` → `<for>`. Note: hooks with cleanup (`useEffect` return) have no v1 equivalent — flag for manual rewrite.

## Recipe 4 — null-check chain → Option + match

**Before (JS):**
```js
const city = user?.address?.city ?? "unknown";
```

**After (Nova):** model each nullable level as `Option` and handle explicitly:
```nova
data class Address(city: String)
data class User(name: String, address: Option[Address])

fn cityOf(u: User) -> String {
  return match u.address {
    Some(a) => a.city,
    None => "unknown"
  }
}
```

**Recipe:** (1) every `T | null | undefined` becomes `Option[T]`; (2) every `?.` becomes a `match` arm or `?` (inside an `Option`-returning function); (3) every `?? default` becomes the `None` arm. The compiler's exhaustiveness check (`E_NON_EXHAUSTIVE`) is your test for missed branches.

---

## What the scaffold handles vs. what needs hands

`nova migrate --from java|jsx <file>` is an **experimental starting scaffold, not a transpiler**. Honest accounting:

**Handled mechanically:**
- Getter/setter pairs → `data class` fields (Recipe 1 pattern)
- `null` returns / `Optional<T>` → `Option[T]`; `?.` / `??` → `match` skeleton (Recipe 4)
- `try/catch` → `Result` + `?` + `match` skeleton (Recipe 2)
- Java primitive/wrapper types → `Int`/`Float`/`String`/`Bool`/`Unit`; `List<X>` → `List[X]`
- `useState` → `state` declarations; `setX` calls → assignments; simple JSX → view blocks (Recipe 3)

**Emitted as WARNINGs (human review required):**
- Everything it can't convert: reflection, annotations, generics beyond `List`, inheritance hierarchies needing `extends`/`super` decisions, `useEffect` cleanup, Context providers, `synchronized` blocks, static initializers, checked-exception hierarchies (it flattens to `Result[T, String]` — you pick the error type)
- Effect placement: the scaffold can't know which functions need `effect`/`effect io`/`effect net` — expect `E_EFFECT` errors on first `nova check` and annotate deliberately
- Capability threading: every `effect io`/`net` function needs its `IO`/`Net` parameter wired from the entry module — a design decision, not a mechanical one

**Workflow:** run the scaffold → `nova fmt` → `nova check` → fix each diagnostic (they're JSON with hints — built for this) → `nova lock` when imports are stable. A full transpiler is v2; v1's deliverable is these tables plus a scaffold that never silently guesses.
