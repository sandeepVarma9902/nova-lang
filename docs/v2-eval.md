# `nova eval` — eval-driven LLM iteration

`nova eval` runs a suite of prompts through Nova `ai fn`s, checks each
completion against a declared predicate, and reports pass/fail + latency as
JSON. It is the honest v1 of "training things":

> **This is eval-driven iteration, not training.** Nothing here trains,
> fine-tunes, or modifies any model. It measures prompt/model/config
> changes so you can iterate: tweak the prompt, re-run the suite, diff the
> report. Any improvement comes from your iteration loop, not from this
> harness doing anything to the model.

## Running it

```sh
nova eval path/to/suite.json
```

- Prints the JSON report to **stdout** (pure JSON — safe to pipe into
  `jq` or save).
- Prints a one-line human summary to **stderr**, e.g.
  `eval: smoke — 4/5 passed (provider=mock); failed: will-fail`.
- Exit code: `0` = all evals passed, `1` = at least one failed,
  `2` = the suite itself could not run (missing file, bad JSON, …).

Provider selection follows the existing rule in
`nova_lang/providers.py`: the mock provider by default,
`NOVA_AI_PROVIDER=http` (or an explicit override) for the experimental
HTTP provider. The mock provider is deterministic, so mock runs are
reproducible.

## Suite format

A suite is a JSON object:

```json
{
  "suite": "summarizer-smoke",
  "description": "optional free text",
  "model": "nova-eval",
  "temperature": 0.7,
  "max_tokens": 512,
  "evals": [
    {
      "name": "mentions-topic",
      "prompt": "Summarize in one sentence: Nova is a programming language.",
      "predicate": { "contains": "Nova" }
    },
    {
      "name": "valid-json",
      "prompt": "Reply with JSON only: {\"answer\": 42}.",
      "model": "other-model",
      "temperature": 0.0,
      "predicate": { "json_has_key": "answer" }
    }
  ]
}
```

- `suite` (string): report label.
- `model` / `temperature` / `max_tokens` (optional, suite- or per-eval):
  defaults for the generated `ai fn` config. Per-eval values override
  suite-level ones.
- `evals`: non-empty list. Each eval needs a unique `name`, a string
  `prompt`, and a `predicate`.
- The prompt is embedded in a generated Nova `ai fn` and goes through the
  real pipeline — lex, parse, type-check, interpreter executes the body and
  collects `prompt(...)` parts, then the selected provider completes it.
  Quotes, newlines and tabs in prompts are escaped safely.

## Predicates

A predicate is a single-key object `{ "<kind>": <argument> }`. The
completion text (the `ai fn`'s `String` output) is checked against it:

| Kind | Argument | Passes when… |
|---|---|---|
| `contains` | substring | output contains it |
| `not_contains` | substring | output does not contain it |
| `regex` | pattern | `re.search(pattern, output)` matches |
| `equals` | exact text | output equals it exactly |
| `json_valid` | `true` | output parses as JSON (bare `"json_valid"` also works) |
| `json_has_key` | key path | output parses as a JSON object containing the key; dotted paths (`"a.b"`) descend into nested objects |

Bad predicates (unknown kind, invalid regex, non-string argument) are
rejected when the suite loads — before anything runs. An eval whose `ai fn`
errors (bad prompt rendering, provider failure) is recorded as failed with
an `error` field; it never aborts the rest of the suite.

## The report

```json
{
  "suite": "summarizer-smoke",
  "provider": "mock",
  "total": 2,
  "passed": 1,
  "failed": 1,
  "evals": [
    {
      "name": "mentions-topic",
      "pass": true,
      "latency_ms": 0.42,
      "output_excerpt": "[mock] Summarize in one sentence: Nova is a ...",
      "predicate": { "contains": "Nova" },
      "detail": "output contains 'Nova'",
      "error": null
    }
  ]
}
```

`output_excerpt` is the first 200 characters of the completion (enough to
debug a failure without bloating the report); `detail` is a human-readable
why-it-passed/failed; `error` is non-null only when the eval errored before
the predicate ran.

## The iteration loop

This is what the harness is for:

1. Write a suite that captures what "good" looks like for your prompt —
   start with `contains` / `not_contains` / `regex` checks against the mock
   provider so the loop is fast and free.
2. Run it: `nova eval suite.json > report-1.json`.
3. Tweak the prompt (or model, temperature) in the suite.
4. Re-run: `nova eval suite.json > report-2.json`, then diff the two
   reports. The `detail` and `output_excerpt` fields tell you exactly what
   moved.
5. Promote the winning prompt into your real `.nova` source.

Tips:

- Keep evals small and focused — one behavior per eval, so a failure names
  the regression.
- Use the mock provider while iterating on prompt wording; switch to the
  real provider (`NOVA_AI_PROVIDER=http`) for a final validation pass.
  Mock runs cost nothing and are deterministic; real runs cost money and
  are not.
- Track `latency_ms` across runs if responsiveness matters to you — the
  mock reports interpreter overhead only; real providers report true
  round-trip time.

## What this is not

- Not training or fine-tuning — no weights change, no gradients, no data
  collection. The word "training" in the roadmap means "training *things*":
  tooling around models, starting here.
- Not a benchmark of Nova itself — it grades prompt/provider behavior,
  not the language.
- Not a judge of quality beyond what your predicates assert — a passing
  suite means "the outputs matched my checks", nothing more. Write
  predicates that actually capture what you care about.
