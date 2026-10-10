# Nova Roadmap — v2 and beyond

v1 is the shippable spine: complete spec, tree-walking interpreter,
stdlib providers (in-memory streams, mock AI), the `nova` toolchain,
docs. Everything below is deliberately **not** in v1 — listed here so
"later" is a plan, not a shrug.

## v2 — the runtime grows up (SHIPPED)

- **NovaVM** ✅: portable bytecode + VM (the "write once, run anywhere"
  story from §18 of SPEC.md). `nova_lang/bytecode.py` (compiler,
  assembler/disassembler, `.nvb` format) + `nova_lang/vm.py` (dispatch
  loop) cover the portable subset — arithmetic, strings, vars, fns +
  recursion, control flow, lists, Option/Result, print; effects,
  spawn/streams, classes, components, AI are compile-time rejections.
  The tree-walking interpreter remains the reference semantics; honest
  limits in `docs/v2-bytecode.md`.
- **Real event loop** ✅: the whole program runs on a cooperative event
  loop (`nova_lang/loop.py`) — `spawn_async { }` tasklets genuinely
  suspend/resume; blocking builtins offload to an IO pool from tasklets.
- **`Send` checking for `spawn`** ✅: `E_NOT_SEND` at check time —
  mutable/shared state across `spawn` boundaries is rejected, no
  data-race caveat.
- **Nurseries / task scopes** ✅: `nursery { }` joins (or cancels) all
  child tasks on every exit.

## v2 — the platform

- **Kafka provider** for streams (`NOVA_STREAMS=kafka`) ✅ (partial):
  real wire-protocol client (produce/fetch, stdlib only) + docker-compose
  for a local broker; honest limits in `docs/v2-kafka.md` — consumer-group
  rebalancing, SASL/TLS, transactions, and compression are NOT
  implemented; live round-trip against a real broker still needs running.
- **Distributed streams**: partitions, multi-region topics.
- **Cloud deploy targets** ✅: `nova deploy --target aws|gcp` generates
  real artifacts (`deploy/aws/` ECS + EventBridge Scheduler,
  `deploy/gcp/` Cloud Run + Cloud Scheduler) with idempotent deploy.sh
  scripts; honest credential/placeholder requirements in
  `docs/v2-deploy-cloud.md`.
- **Managed secret stores**: `store = "aws" | "gcp"` for `resource
  secret`; the `secret(name, io)` call is unchanged.
- **Cron scheduler** ✅: `resource cron` in app.nova is a real scheduled
  runner under `nova deploy --target local` (minute-granularity, runs
  jobs as subprocesses in a background thread).
- **Buckets**: `resource bucket` gets a runtime (S3/GCS-shaped API).
- **Browser renderer** ✅ (experimental): `nova dom` compiles Nova
  components to a self-contained HTML+JS bundle (real DOM ops, embedded
  runtime, no network); `examples/dom-counter.html` works on
  double-click; honest subset in `docs/v2-render-dom.md`.

## v2 — the language (careful additions only)

- **Typed context** ✅: `context` declarations with defaults, `provide`
  blocks, `consume` readers — type-checked, no prop drilling.
- **Async streams**: `for msg in subscription` — streams meet
  structured concurrency.
- **Effect polymorphism** (maybe): one function, many effect contexts.
  Only if it stays simple; otherwise generics-free v1 stands.
- **IDE support** ✅ (experimental): `nova lsp` speaks LSP over stdio
  (diagnostics from the real checker + heuristic hover);
  `docs/v2-lsp.md`.

## v2+ — AI-native depth (explicitly NOT v1)

- **LLM training hooks** ✅ (honest v1): `nova eval` runs eval suites
  (prompt → predicate) against the mock or configured provider and
  reports pass/fail as structured JSON — eval-driven iteration, not
  training; `docs/v2-eval.md`.
- **Real AI providers**: streaming completions, tool-use loops, budget
  caps on `ai fn` (cost as an effect).
- **Full transpilers** ✅ (experimental): `nova migrate` grew from
  scaffold to a mechanical Java/JSX migrator (classes, getters/setters,
  null-check recipes, try/catch recipes, loops, imports; components,
  props, useState, map-lists) with loud warnings for the rest;
  `docs/v2-migrate.md`.

## What v1 deliberately omits (and why)

No first-class functions, no operator overloading, no implicit
conversions, no exceptions/`try`-`catch`, no null, no eval, no macros,
no global mutable state. Each omission is load-bearing: the language
stays small enough for an AI to hold completely in context — and for a
human to learn in 30 minutes (`docs/tutorial.md`).
