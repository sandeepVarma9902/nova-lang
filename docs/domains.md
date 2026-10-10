# Why Nova — by domain

Nova fuses Java's discipline (types, effects, capabilities) with React's soul (components, declarative UI) and adds AI as a first-class citizen. Here's where that combination actually wins — and where it doesn't. No hype; the trade-offs are stated plainly.

**One honest note first:** Nova's reference runtime is Python-based today. It will not outrun the JVM on raw throughput — the native Rust NovaVM is the planned answer to that, and it doesn't exist yet. Nova competes on **correctness, clarity, and development speed**, not benchmarks.

## Web APIs & microservices

**The incumbent:** Java + Spring, or Node + Express.

**Why Nova fits:** An API is types in, types out, with fallible steps in between. Nova's `Result` + `?` makes the happy path linear without exceptions flying across layers; `effect` signatures tell you exactly what each handler may touch. You get Spring-grade explicitness without the annotation tax:

```nova
effect fn getUserName(id: Int) -> Result[String, String] {
  if id <= 0 {
    return Err("bad id")
  }
  return Ok("Ada")
}

effect fn greet(id: Int) -> Result[String, String] {
  let name = getUserName(id)?   // Err returns early — no try/catch
  return Ok("Hello, " + name)
}

print(greet(1))    // Ok("Hello, Ada")
print(greet(-1))   // Err("bad id")
```

**Vs Java:** less boilerplate (no POJOs, no checked-exception ceremony), same explicitness about effects. **Vs Node:** real types and no `undefined` — the compiler catches what your tests would. **Trade-off:** the ecosystem is tiny compared to Spring/Express; reach for Nova for new services, not for migrating a Spring monolith.

## Frontend UI

**The incumbent:** React + TypeScript.

**Why Nova fits:** Components with declared props, first-class state (no hooks, no stale closures), and a type system that rejects `undefined` at compile time. If you've lost a day to a rules-of-hooks bug or a `cannot read property of undefined`, Nova removes the entire category:

```nova
component UserCard(user: String) {
  state expanded: Bool = false
  view {
    <div>
      <h2>{user}</h2>
      <p>{if expanded { "bio here" } else { "" }}</p>
      <button onClick={expanded = not expanded}>More</button>
    </div>
  }
}

let app = render(UserCard(user: "Ada"))
app.fire("onClick")   // expanded → true, re-renders with the bio
```

**Vs React:** Nova is stricter and smaller — you trade React's vast ecosystem (and jobs) for a compiler that says no earlier. **Honest take:** for production frontend work today, React's ecosystem wins; Nova's UI story is for greenfield experiments and for teams who value the guarantees over the library shelf.

## Data pipelines & streaming

**The incumbent:** Python scripts, Kafka Streams, Flink.

**Why Nova fits:** Typed streams (`stream events: Order`), `spawn` for CPU-bound stages, `async` for IO-bound ones — with the compiler refusing to let blocking IO stall the pipeline (`E_ASYNC_BLOCKING`). Schemas are types, not documentation:

```nova-norun
stream orders: Order

effect fn pipeline() {
  let batch = orders.pollBatch(100)
  let enriched = spawn { batch.map(enrich) }   // CPU-bound, off the loop
  sink.write(enriched.await())
}
```

**Vs Python:** Nova's types and effects catch pipeline bugs before the 3am page. **Vs Kafka Streams/Flink:** Nova is far simpler to operate for small/medium workloads, but it is not a distributed engine — for massive scale, the JVM ecosystem remains the answer.

## AI-powered applications

**The incumbent:** Python + LangChain/LlamaIndex, or raw API calls.

**Why Nova fits:** This is Nova's home turf. `ai fn` makes the model call a typed function: the return type *is* the output schema, the prompt lives in the body, and evals (`nova eval`) test it like code. No prompt strings scattered through Python, no parsing JSON with prayers:

```nova
data class Summary(headline: String, score: Float)

ai fn summarize(text: String) -> Summary
  model = "nova-mock-1"
  temperature = 0.3
{
  prompt("Summarize in one sentence: " + text)
}
```

**Vs Python AI stacks:** Nova gives you schemas, evals, and effect tracking around the model call — things the Python ecosystem bolts on. **Trade-off:** model *training* stays external (PyTorch/GPU); Nova orchestrates models, it doesn't train them.

## Scripts, CLIs & automation

**The incumbent:** Python, Bash.

**Why Nova fits:** A script that grows into a tool usually collapses under untyped dictionaries and silent failures. Nova scripts start typed: `nova run deploy.nova` type-checks first, `Result` handles the sad paths, and `nova build` turns the script into a deployable artifact later. Write it once, grow it without rewriting.

## Full-stack (Nova Boot)

**The incumbent:** Next.js, or Java backend + React frontend.

**Why Nova fits:** One language, one type system, shared types between server and UI. A `data class` defined once is the API schema, the DB model shape, and the component prop type. No codegen step, no drift between frontend and backend types.

---

## Where Nova is NOT the answer (yet)

- **Raw performance-critical systems** (game engines, HFT, video encoding) — the Rust VM isn't here yet.
- **Mobile apps** — no mobile target exists.
- **Huge existing codebases** — migrating a Spring monolith or a 500-component React app is a project, not a weekend. The [migration guide](./migration-guide.md) is honest about the cost.
- **Ecosystem depth** — Java and JavaScript have decades of libraries. Nova has a standard library and a vision.

## The bottom line

Choose Nova when **correctness and clarity are worth more than ecosystem size**: new services, AI features, internal tools, and greenfield UIs. Choose the incumbent when you need battle-tested libraries, maximum hiring pool, or raw throughput today. Nova is built to earn the second list over time — starting with the compiler, not the benchmarks.
