# Nova v2 — Browser DOM renderer (`render_dom`)

Item 5 of the v2 roadmap. v1's `render()` printed a console tree;
`nova_lang/render_dom.py` compiles Nova components to a **single
self-contained `.html` file** with the JS runtime embedded — no CDN, no
network, works by double-clicking the file.

Public API (CLI wiring target — `cli.py` deliberately untouched):

```python
from nova_lang.render_dom import render_to_html, NovaRenderError
render_to_html("examples/dom-counter.nova", "examples/dom-counter.html")
# -> returns the out_path; raises NovaRenderError on unsupported constructs
```

## How it works

1. **Type-check first.** The source goes through the normal checker
   (`check_source`), so type errors surface before any codegen.
2. **Parse.** Components are `ComponentDecl`s; mounts are top-level
   `render(Counter(title: "Clicks"))` calls (bare or `let`-bound). Props must
   be named and literal.
3. **Codegen per component:**
   - `state n: Int = 0` → `function Counter_initState() { return { count: 0 }; }`
   - `view { ... }` → `function Counter_view(state, props, rerender)` that
     builds real DOM with `document.createElement` / `createTextNode` and
     wires handlers with `addEventListener`.
   - bare names in expressions resolve in order: **state field → param →
     loop variable**. Unknown names are a compile error with line/col.
4. **Mount.** `__novaMount(rootEl, {initState, props, view})` renders once.
   Every event handler mutates `state`, then calls `rerender()`, which
   **rebuilds the whole component subtree** (`while (rootEl.firstChild)
   rootEl.removeChild(rootEl.firstChild); rootEl.appendChild(view(...))`).
   Simple, correct; listeners are re-attached on every rebuild.
5. **Assemble.** Runtime + component functions + mounts go in one `<script>`
   inside one `<html>` with one `<div id="nova-app-N">` per mount.

## Supported subset (exact)

- `state` fields: `Int`, `Float`, `String`, `Bool`, and `List[...]` of those —
  initialized from **literal** expressions (no variable refs in initializers).
- Component params, supplied via `render(...)` named args.
- `view` blocks: lowercase element tags, `JText`, `{expr}` interpolation,
  string attrs (`class="..."`), expression attrs (`title={...}` → `String(...)`
  via `setAttribute`), bare boolean attrs, `on<Event>={expr}` (e.g. `onClick`
  → `click`) with **inline handler expressions**.
- Handler bodies: assignment to state fields (`count = count + 1`),
  `.push(...)` on state lists, and any supported expression — the compiled
  statement runs, then the subtree re-renders.
- Expressions: literals, arithmetic `+ - * / %`, comparisons
  `== != < <= > >=` (note: `==` compiles to JS `===`), `and`/`or`/`not`,
  unary `-`, string concatenation via `+`, `this.x` state access, field/index
  access, single-expression `if/else` branches (compiled to ternaries),
  `<for item in items>` over a state/param list.
- Top-level statements other than component declarations and `render(...)`
  mounts are **ignored** (console-render artifacts like `app.fire(...)`).
  This is by design, not an accident — the DOM bundle mounts components;
  it does not replay the console REPL session.

## Honest limits

- **No component methods.** The v1 parser does not accept `fn` inside a
  component body — handlers are inline expressions by design. (The roadmap
  sketch that mentioned `fn increment()` / `this.count` described syntax the
  language never shipped; this renderer compiles the language as it exists.)
- **No nested components** (`<Counter>` inside a view) — rejected at compile
  time. Nested views must be flattened for now.
- **No CSS beyond plain attributes.** There is no `<style>` block support;
  use `class="..."` + an external stylesheet by hand if you need styling.
- **Full-subtree re-render** on every state change — no diffing, no keyed
  lists. Fine for v1-scale examples.
- Only the first mount target per `render(...)` call; multiple `render(...)`
  calls each get their own container div.
- Unsupported constructs raise `NovaRenderError` with a message and the
  source line/col — never silently dropped.

## Viewing the example

```
python3 -c "from nova_lang.render_dom import render_to_html; \
            render_to_html('examples/dom-counter.nova', 'examples/dom-counter.html')"
```

then open `examples/dom-counter.html` in a browser (double-click works —
zero network). Click Increment/Decrement and watch the count update.

The checked-in `examples/dom-counter.html` was generated exactly this way.

## Verification

- `python3 tests/test_render_dom.py` — asserts the bundle is self-contained
  (no external URLs), embeds the runtime, the component state (`count: 0`),
  props (`title: "Clicks"`), and both click wirings; asserts
  `NovaRenderError` on bad inputs; runs `node --check` on the embedded JS and
  executes the bundle in node against a minimal DOM shim, firing the click
  listeners and asserting the rendered text goes `0 → 2 → 1` and the old
  subtree is fully detached after re-render.
- `python3 tests/run_tests.py` — the v1 suite stays green (the new
  `examples/dom-counter.nova` carries a `// EXPECT:` block for the console
  tree, so `nova run` and `render_dom` agree on semantics).
- `examples/todo.nova` (with `<for item in items>` + `items.push(...)`)
  also compiles and passes a headless click test — the loop path is real.
