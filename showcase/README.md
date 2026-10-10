# Nova Showcase — docs as code

Three complete, working sample applications built on Nova Boot. Each one
runs, each one serves, each README walks through the code file by file.
Written for a React developer learning Nova: the READMEs explain the
Nova feature, then point at the exact file and line.

| App | What it is | Run |
|---|---|---|
| [blog/](blog/) | REST API (posts CRUD) over sqlite, via `nova db pull` models — the "PetClinic" | `NOVA_PORT=8081 ./nova run showcase/blog/api.nova` |
| [todo/](todo/) | Full-stack: Boot backend + Nova component frontend (`nova dom`) | `NOVA_PORT=8082 ./nova run showcase/todo/api.nova` |
| [shortener/](shortener/) | URL shortener; visits stream to an analytics subscriber | `NOVA_PORT=8083 ./nova run showcase/shortener/server.nova` |

All commands run from the `nova-lang` directory.

## What each app demonstrates

- **blog/** — database nativity end to end: `nova db pull` generated
  `models.nova`, check-time column validation, the query builder, and
  `Result[Response, Response]` handlers against a real sqlite file.
- **todo/** — one language, two runtimes: the API runs on the Nova
  interpreter, the UI is a Nova component compiled by `nova dom` to a
  self-contained HTML bundle, served by the same Boot server.
- **shortener/** — the platform story: HTTP + typed streams in one
  program. Redirects publish visit events; a consumer-group subscriber
  counts clicks. Swap `NOVA_STREAMS=kafka` for the real provider.

## Verifying they boot

Each app was verified to boot and serve before this shipped (see the
test log in the parent report). Quick smoke:

```bash
NOVA_PORT=8081 ./nova run showcase/blog/api.nova &
curl localhost:8081/posts
kill %1
```

## For the React dev: the 5-minute tour

1. Read `blog/api.nova` — it is the whole backend. Handlers are functions,
   routes are values, errors are `Result`s.
2. Compare `todo/api.nova` (JSON API) with `todo/ui.nova` (component) —
   same `Todo` shape, same language, different runtimes.
3. Read `shortener/server.nova` — the stream is 3 lines to declare, the
   subscriber is 15 lines. That is the entire "Kafka" story for v1.
