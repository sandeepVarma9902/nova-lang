# Todo Full-Stack — one language, both ends

A todo app with a Nova Boot backend (REST API) and a Nova component
frontend (compiled by `nova dom`), in one project. It really runs: the
backend serves the API *and* the frontend bundle.

## What it shows

| Nova feature | Where |
|---|---|
| Boot REST API (`/api/todos` CRUD) | `api.nova` |
| Typed path params | `api.nova:57` (`/api/todos/{id: Int}`) |
| `nova dom` component → self-contained HTML | `ui.nova` → `public/app.html` |
| Static file serving | `api.nova:100` (`staticDir(r, "/", "showcase/todo/public")`) |
| Shared `Todo` data class, both ends | `api.nova:9` (backend); component state in `ui.nova` |
| JSON request/response binding | `api.nova:29-44` |

## How to run

From the `nova-lang` directory:

```bash
# 1. compile the frontend (Nova component -> static HTML bundle):
./nova dom showcase/todo/ui.nova -o showcase/todo/public/app.html
cp showcase/todo/public/app.html showcase/todo/public/index.html

# 2. serve backend + frontend:
NOVA_PORT=8082 ./nova run showcase/todo/api.nova
```

- Open http://localhost:8082/ — the todo UI renders (add buttons work;
  state is local in v1).
- `curl localhost:8082/api/todos` — the REST API.
- `curl -X POST localhost:8082/api/todos -d '{"text":"Try Nova"}'`

## Key design points (for the React dev)

**One language, two runtimes.** `api.nova` runs on the Nova interpreter
(server); `ui.nova` compiles to a self-contained HTML+JS bundle (browser).
The `Todo` shape is defined once in Nova (`api.nova:9`) — compare: a
TypeScript type shared between Next.js server and client, except here the
*language* is shared, not just the types.

**The component is real.** `ui.nova` uses `component`, `state`, `view`,
`<for>` loops, and `onClick` handlers — the same component model as
`examples/todo.nova`. `nova dom` compiles it to HTML with an embedded
runtime (no network, no dependencies). Open `public/app.html` directly —
it works from `file://` too.

**Honest v1 limits.** The dom renderer (v1) supports local state, `push`,
and `<for>` — there is no fetch bridge yet, so the UI manages local state
instead of calling `/api/todos` over HTTP. The API is complete and tested
via curl; wiring the component to live HTTP is the next step, and this
README will say when it lands. We would rather show a real local-state UI
than a fake "integrated" one.

## Files

- `api.nova` — Boot backend: `/api/todos` CRUD + static serving.
- `ui.nova` — Nova component (the frontend source).
- `public/app.html`, `public/index.html` — compiled bundles (build step
  above; committed so the app runs without rebuilding).
