# Nova

**An AI-native programming language — a hybrid of Java and React, authored
by AI, for AI.** Static typing with inference, classes + interfaces,
components with type-checked views, no null, explicit effects, structured
concurrency, first-class AI calls — and a platform layer (streams,
manifest, deploy) around a small, coherent core.

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

effect fn main() {
  render(Counter(title: "Clicks"))
}

main()
```

## 5-minute quickstart

```bash
./nova run examples/counter.nova     # component + state + re-render
./nova run examples/concurrency.nova # spawn / async / await
./nova run examples/streams.nova     # pub/sub streams
./nova check examples/api.nova       # diagnostics are JSON, for AI consumers
./nova fmt examples/hello.nova       # the ONE canonical style
python3 tests/run_tests.py           # 17 checks — all green is the v1 bar
```

The manifest platform story:

```bash
cd examples/deploy-demo
../../nova build                      # type-check + stage dist/ + Dockerfile
../../nova deploy --target local      # run as a process (env=dev)
../../nova deploy --target docker     # docker build (or prints the command)
```

## Design rationale

**Inherited from Java:** static typing (with inference to kill
verbosity), classes + interfaces + single inheritance, modules with
explicit imports, encapsulation — and "write once, run anywhere" via the
NovaVM bytecode vision (§18 of SPEC.md; v1 runs on the tree-walking
interpreter).

**Inherited from React:** everything-is-a-component, declarative
type-checked views with JSX-like syntax, unidirectional data flow, props
immutable by default, local state as a first-class `state` declaration.

**Java's cons, fixed:** no null (`Option[T]`, compiler-enforced) · no
checked-exception ceremony (`Result[T,E]` + `?` + `match`) · no
primitive/object duality · no boilerplate (inference, `data class`) · no
classpath hell (explicit imports, hash-pinned by `nova lock`).

**React's cons, fixed:** no hook-rules footguns (`state` is
compiler-managed, order-independent) · no prop drilling by accident
(typed context is v2, explicit) · no dynamic-typing footguns (everything
is typed, views included) · no build-toolchain maze (one `nova`
command).

**AI-native principles:** exactly one obvious way to write anything ·
no operator overloading · no implicit conversions · canonical
auto-formatting built in · compiler diagnostics as structured JSON with
stable error codes and suggested fixes · explicit effects (pure by
default) · optional `requires`/`ensures` contracts · token-efficient but
readable syntax.

**Security as a pillar:** no `eval` or dynamic code execution at all ·
IO/network/secrets only through explicit capabilities (no ambient
authority) · checked integer arithmetic (overflow is an error, never
wraparound) · hash-pinned modules (`nova.lock`) · async discipline
enforced by the compiler (`E_ASYNC_BLOCKING`).

## The A–Z story, layered

The **language stays small**; the A–Z lives in the platform around it:

| Layer | v1 |
|---|---|
| Language | types, effects, components, classes, modules, `spawn`/`async`/`await`, `stream` decls, `ai` blocks |
| Stdlib/runtime | in-memory stream provider (swappable), mock AI provider, capability-gated secrets |
| Toolchain | `nova run/check/fmt/lock/migrate/build/deploy`, `app.nova` manifest, Dockerfile generation |
| Docs | SPEC, tutorial, FAQ, migration guide (Java→Nova, React→Nova), deployment guide |

v2 (ROADMAP.md): NovaVM bytecode, real event loop, Kafka provider, cloud
deploy targets, managed secret stores, typed context, LSP.

## Repo layout

```
nova_lang/        interpreter: lexer, parser, ast, checker, interpreter,
                  formatter, providers (AI), streams, manifest, migrate, cli
examples/         12 runnable programs, each with // EXPECT: blocks
                  (+ deploy-demo/: app.nova manifest walkthrough)
tests/            run_tests.py + errors/ (diagnostic-code assertions)
docs/             tutorial.md, faq.md, migration-guide.md, deployment.md
SPEC.md           the complete v1 language specification
ROADMAP.md        what v2+ holds (and what v1 deliberately omits)
```

## Requirements

Python 3.8+, stdlib only — no pip installs, no dependencies. Correct
over clever: a smaller language that fully works beats a sprawling one
that half-works.

## Status

v1 prototype: complete spec, working interpreter, 12 examples, 17 green
tests. The `migrate` command and the HTTP AI provider are
**experimental** scaffolds; the migration *guide* (mapping tables) is
the real v1 deliverable there. See ROADMAP.md for what's next.
