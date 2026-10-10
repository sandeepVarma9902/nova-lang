# Deployment — app.nova manifest reference + worked example

## Manifest schema reference

`app.nova` is optional but built-in: place it next to your entry file and the toolchain auto-discovers it. Every key:

| Key | Type | Default | Validation |
|---|---|---|---|
| `app "<name>"` | String | *required* | Non-empty; names the application (used in `dist/` and image tags) |
| `version` | String | `"0.1.0"` | Semver-shaped `X.Y.Z` recommended |
| `entry` | String | `"main.nova"` | Must be a readable `.nova` file; type-checked by `nova build` |
| `threading` | `"single" \| "multi" \| "auto"` | `"auto"` | Unknown values rejected; overridable via `NOVA_THREADING` env |
| `env <name> { ... }` | String→String map | — | Non-secret config; applied as env vars on `nova deploy`; selected with `--env NAME` |
| `resource stream "<topic>"` | `{ partitions: Int }` | `partitions = 1` | Declares a typed topic the app uses; must match a `stream` declaration |
| `resource secret "<name>"` | `{ store: "env" }` | `store = "env"` | v1 only supports `store = "env"` → resolves `NOVA_SECRET_<NAME>` at runtime (uppercased, `-`→`_`) |
| `resource cron "<name>"` | `{ schedule, run }` | — | **Declared only in v1** — the scheduler itself is v2; `schedule` is cron syntax, `run` is the `.nova` file |
| `docker { base, port }` | String, Int | `base = "python:3.12-slim"`, `port = 8080` | `port` must be 1–65535; `base` is the image tag |
| `deploy target` | `"local" \| "docker"` | `"local"` | Cloud targets are v2 |

Validation failures surface as structured diagnostics before anything runs — `nova build` never produces a half-built `dist/`.

## Worked example: deploy-demo

The demo lives at `examples/deploy-demo/`. Read the two source files first:

**app.nova**
```nova-norun
app "deploy-demo" {
  version = "0.1.0"
  entry = "main.nova"
  threading = "single"   // deterministic: spawn runs inline

  env dev { log_level = "debug" }
  env prod { log_level = "info" }

  resource stream "events" { partitions = 4 }
  resource secret "db-password" { store = "env" }
  resource cron "nightly-report" { schedule = "0 2 * * *", run = "report.nova" }

  docker { base = "python:3.12-slim", port = 8080 }

  deploy target = "local"
}
```

**main.nova**
```nova
stream events: String

effect io fn readPassword(io: IO) -> String {
  return match secret("db-password", io) {
    Ok(pw) => "secret loaded",
    Err(e) => "no secret: " + e
  }
}

effect io fn main(io: IO) {
  print(readPassword(io))
  let sub = events.subscribe(group: "demo")
  let t = spawn { "processed" }
  events.publish("hello from " + t.await())
  match sub.poll() {
    None => print("(empty)"),
    Some(m) => print(m.value)
  }
}

main(io)
```

Notice the capability story: `io` arrives as a parameter at the entry (`main(io)`), and `secret("db-password", io)` resolves through the capability-gated store — the name is in the manifest, the value is never in source.

### Step 1 — `nova build`

```bash
cd examples/deploy-demo
nova build
```

Build validates the manifest, type-checks `main.nova`, and stages `dist/`:

```
dist/
  main.nova      # entry + transitive imports, flattened
  Dockerfile     # generated from the docker {} block
```

### Step 2 — the generated Dockerfile, walked through

```dockerfile
FROM python:3.12-slim          # from docker.base
WORKDIR /app
COPY nova_lang /opt/nova-lang/nova_lang   # the interpreter (stdlib-only, no pip)
COPY nova /opt/nova-lang/nova
ENV PATH="/opt/nova-lang:${PATH}"
COPY main.nova /app/           # staged app sources
ENV NOVA_THREADING=single      # threading model baked in from app.nova
EXPOSE 8080                    # from docker.port
CMD ["nova", "run", "main.nova"]
```

Nothing but the interpreter and your sources — no dependency install step because there are no dependencies.

### Step 3 — `nova deploy --target local`

```bash
nova deploy --target local --env prod
```

This runs the entry as a process with the `env prod` section applied (`log_level=info` becomes an env var). Secrets resolve **at runtime** from the store:

```bash
NOVA_SECRET_DB_PASSWORD=s3cr3t nova deploy --target local
# => secret loaded
# => hello from processed
```

Without the env var you get the honest fallback: `no secret: secret not found: db-password` — the `match` on `secret(...)` makes the missing-secret path explicit, not a crash.

### Step 4 — `nova deploy --target docker`

```bash
nova deploy --target docker
```

This `docker build`s the staged image (or prints the exact `docker build` command if docker is absent, so CI can run it). Tag and port derive from `app` name, `version`, and `docker.port`.

## What v2 adds

- **Cloud targets** (`deploy target = "aws" | "gcp"`): the same manifest, managed runtimes
- **Managed secret stores**: `resource secret ... { store = "aws" }` / `"gcp"` instead of env vars
- **Cron scheduler**: `resource cron` stops being declarative-only — the platform actually runs `report.nova` on the schedule
- **Kafka provider** for streams (`NOVA_STREAMS=kafka`): same `publish`/`poll`/`commit` code, real broker
- **NovaVM**: the interpreter is replaced by bytecode execution inside the same images — smaller, faster, same semantics (SPEC §18)
