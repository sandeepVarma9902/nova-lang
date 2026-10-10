# Task 02 — json-cli

Write a program that reads `data.json` from the **current working directory**.
The file contains a JSON array of user objects:

```json
[{"name": "Ada", "age": 30, "email": "ada@example.com"}, ...]
```

- `"name"`: string, `"age"`: integer, `"email"`: string (optional).

Print **exactly three lines** to stdout:

```
users: <number of valid users>
avg_age: <average age with exactly one decimal>
no_email: <comma+space-separated names of valid users missing email, or "none">
```

Rules:

- A user is **valid** only if it has a string `"name"` and an integer `"age"`.
  Skip invalid entries silently (they don't count, their age isn't averaged).
- If `data.json` is missing: print `error: data.json not found` to stderr and
  exit with a nonzero status.
- If the file isn't valid JSON, or the top level isn't an array: print a stderr
  line starting with `error: malformed JSON` and exit nonzero.

Entry point: Nova → `solution.nova` (run with `nova run`); TypeScript →
`solution.ts` (compiled with `tsc --strict`, run with `node`; `@types/node` is installed, so `process`, `fs`, etc. are typed); Python →
`solution.py` (run with `python3`). The test harness copies each fixture to
`./data.json` and runs your program in that directory.
