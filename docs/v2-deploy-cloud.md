# Cloud deploy targets — `nova deploy --target aws|gcp`

v2 generators in `nova_lang/deploy_cloud.py`: `generate_aws(man,
man_path, out_dir)` and `generate_gcp(man, man_path, out_dir)` read
`app.nova` and write real, runnable deployment artifacts. (The
`nova deploy --target aws|gcp` CLI wiring lands separately — these
functions are what it will call.)

Convention: generate into `<appdir>/deploy/aws` (or `/deploy/gcp`).
The generated `deploy.sh` derives the app dir as `<out>/../..`, so the
Docker build context is the app tree holding `nova_lang/`, the `nova`
script, and `app.nova`.

## What each target generates

### AWS (`--target aws`)

| File | What it is |
|---|---|
| `Dockerfile` | `python:3.12-slim` (or `docker { base }`), copies the Nova toolchain (pure Python, stdlib only — no pip installs) + entry, its transitive imports, and every cron `run` file into `/app`, sets `NOVA_THREADING`, exposes the docker port, `CMD ["nova", "run", "<entry>"]` |
| `task-definition.json` | ECS Fargate task definition: `awsvpc`, 256 CPU / 512 MiB, image URI rendered by `deploy.sh`, env vars from the manifest `env prod` block (falling back to `env dev`), keys uppercased. `resource secret` entries become `NOVA_SECRET_<NAME>` entries with a `FILL_IN` Secrets-Manager-ARN placeholder. awslogs driver. |
| `service.json` | Service sketch (desired count 1, Fargate, awsvpc) — the actual `create-service`/`update-service` command lives in `deploy.sh` with your cluster/subnets/security groups. |
| `deploy.sh` | Executable. Refuses to run while any `FILL_IN` placeholder is unchanged. Then: verifies `aws` credentials (`aws sts get-caller-identity`), creates the ECR repo if missing, logs docker in, builds + pushes the image, renders the image URI into the task definition, registers it, creates the `/ecs/<app>` log group, creates or updates the ECS service, and creates one **EventBridge Scheduler** schedule per `resource cron` (`aws scheduler create-schedule`, idempotent create-or-update), targeting the same task definition with the container command overridden to `nova run <cron-file>`. |

Cron schedule conversion: the manifest's 5-field cron (`0 2 * * *`)
becomes the EventBridge 6-field form `cron(0 2 * * ? *)` (one of
day-of-month/day-of-week must be `?`; generation errors out honestly if
your schedule pins both).

### GCP (`--target gcp`)

| File | What it is |
|---|---|
| `Dockerfile` | Same shape as AWS (Nova toolchain + app sources, entry as CMD). |
| `service.yaml` | Declarative Cloud Run service sketch (knative `Service`) — the equivalent of what `deploy.sh` does via flags. `%IMAGE_URI%` is filled by the script; secret refs are commented sketches you fill in by hand. |
| `deploy.sh` | Executable. Guards placeholders, checks `gcloud` + login (`gcloud auth login`), creates the Artifact Registry repo if missing, `docker build` + push, then `gcloud run deploy` with `--set-env-vars` from the `env prod` (or `env dev`) block, captures the service URL, and creates one **Cloud Scheduler** HTTP job per `resource cron` (`gcloud scheduler jobs create http`, idempotent create-or-update), POSTing to `<service-url>/cron/<name>`. |

## Exactly what YOU must supply

**Both targets:**
- Your app's `app.nova`, entry file, and the Nova toolchain next to it
  (the script needs `nova_lang/` and the `nova` launcher in the app dir).
- Non-secret config lives in `env prod { }` (falls back to `env dev`)
  and is baked in as env vars automatically.
- Secrets are never in the manifest: `resource secret` names resolve to
  `NOVA_SECRET_<NAME>` at runtime. You create the secret in Secrets
  Manager (AWS) / Secret Manager (GCP) and paste the real ARN/path into
  the placeholders before running.

**AWS — fill in `deploy/aws/deploy.sh` + `task-definition.json`:**
`AWS_REGION`, `ACCOUNT_ID`, `ECR_REPO`, `CLUSTER_NAME`, `SUBNETS`,
`SECURITY_GROUPS` (comma-separated), `SCHED_ROLE_ARN` (IAM role
EventBridge Scheduler assumes); in `task-definition.json`:
`executionRoleArn`, each secret's Secrets-Manager ARN, the awslogs
region. Prerequisites: `aws configure` (credentials), docker, an ECS
cluster, a VPC with public subnets + a security group.

**GCP — fill in `deploy/gcp/deploy.sh`:**
`PROJECT_ID`, `REGION`, `AR_REPO`. Prerequisites: `gcloud auth login`,
a project with Cloud Run + Artifact Registry + Cloud Scheduler APIs
enabled and billing on, docker.

## What isn't automated

- IAM roles and policies (task execution role, scheduler role) — you
  create them in the console/IaC and paste the ARNs.
- VPC networking beyond placeholders (AWS): subnets/security groups are
  inputs, not provisioned.
- Secrets themselves: created by you in the provider's secret store,
  then referenced.
- GCP cron webhooks: the deployed image runs `nova run <entry>`, which
  is **not an HTTP server**. Cloud Scheduler jobs POST to
  `<service-url>/cron/<name>`; the app must expose that endpoint (or you
  point the jobs elsewhere) before the schedules fire usefully. This is
  stated in the script itself.
- DNS / custom domains, TLS, scaling policy beyond max-instances=10
  (GCP), multi-region, rollbacks.

## Honest limits

- The scripts are `set -euo pipefail` and idempotent (create-or-update
  for services and schedules), but they have only been syntax-checked
  (`bash -n`) — not run against real AWS/GCP accounts.
- Env-var values with `=`, commas, or quotes may break the
  `--set-env-vars` flag on GCP and the JSON round-trip on AWS; keep
  deployment env vars simple or escape them by hand.
- Cron schedules that pin both day-of-month and day-of-week are rejected
  for AWS (EventBridge forbids it); simplify to pin one.
- Docker builds need the network (pulls `python:3.12-slim`) and must run
  on a machine with docker + the CLIs — the VM here can generate the
  artifacts but `docker`/`aws`/`gcloud` may not be installed.
- `deploy target = "aws"|"gcp"` in `app.nova` is not yet a recognized
  manifest value — the CLI wiring is a separate step; generation works
  today via the Python API.
