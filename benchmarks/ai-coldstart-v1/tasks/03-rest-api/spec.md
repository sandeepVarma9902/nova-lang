# Task 03 — rest-api

Serve an HTTP API on **port 8931** with this in-memory user store:

| id | name | email |
|----|------|-------|
| 1 | Ada | ada@example.com |
| 2 | Ben | ben@example.com |
| 3 | Cara | cara@example.com |

Implement:

```
GET /users/{id}
```

- `{id}` is a positive integer and the user exists → **200** with the user as
  JSON: `{"id": 1, "name": "Ada", "email": "ada@example.com"}`
- `{id}` is a positive integer but unknown → **404** with JSON `{"error": ...}`
  (message text is yours)
- `{id}` is not a positive integer (`abc`, `0`, `-5`, `1.5`) → **400** with
  JSON `{"error": ...}`

All responses must have `Content-Type: application/json`.

Entry point: Nova → `solution.nova` (run with `nova run`); TypeScript →
`solution.ts` (you may `npm install express`; compiled with `tsc --strict`,
run with `node`); Python → `solution.py` (stdlib only, run with `python3`).
The server must stay up until killed. The test harness starts it, waits for
the port, runs HTTP checks, then kills it.
