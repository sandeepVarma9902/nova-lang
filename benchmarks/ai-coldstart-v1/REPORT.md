# REPORT — AI cold-start benchmark v1 (2026-10-10)

Agent: Muse (single model), cold-start protocol per README.md.
Toolchains: Nova `./nova` (repo), `tsc --strict` 7.0.2, Python 3.x.

## Numbers

| Task | Nova | TypeScript | Python |
|------|------|------------|--------|
| 01 typed-fns | **1** | 1 | 1 |
| 02 json-cli | **5** | 1 | 1 |
| 03 rest-api | **1**¹ | 1 | 1 |
| 04 counter-ui | **1** | 1 | 1 |
| **First-try pass rate** | **3/4** | 4/4 | 4/4 |
| **Mean iterations to green** | **2.0** | 1.0 | 1.0 |

¹ Nova's task-03 cell used ai-build.txt **plus** examples/boot-api.nova:
HTTP serving is not documented in ai-build.txt (recorded as a docs gap below).

## What the numbers actually say

**Nova does not win v1.** On straightforward typed code, an HTTP API, and a
stateful component, an AI produces working Nova on the first try — exactly
like TypeScript and Python. The json-cli task took 5 iterations, all on real
compiler feedback. That is the honest headline, and it is more useful than a
rigged victory, because every failed attempt pointed at a specific, fixable
gap:

1. **Match arms must be expressions** (2 attempts). Newcomers — human or AI —
   write `{ }` blocks as arms. The compiler error is clear (`E_SYNTAX` +
   hint), but the restriction itself is friction. Consider allowing block
   arms, or documenting the `toXList` workaround pattern.
2. **No implicit tail return** (1 attempt). `E_RETURN` teaches it in one
   round-trip; still, every mainstream language returns the tail expression.
3. **No Int→Float conversion** (1 attempt). `parseFloat(str(n))` returning
   `Option[Float]` is the only path — absurd for `avg = sum / count`. A
   `toFloat`/`float()` builtin is the obvious fix.
4. **No `exit` / stderr primitive** (discovered, worked around). Error cases
   pass only via the `assert(false, msg)` trick (JSON diagnostic on stderr +
   exit 1). A real CLI language needs `exit(code)` and stderr printing.
5. **ai-build.txt has no HTTP serving section.** The single most important
   task class for "AI builds apps" is undocumented in the AI instruction
   layer. The llms.txt `serve { }` example also looks outdated next to the
   real `router()` API.

None of the five is architectural. All five are fixable without changing the
language's design — and this harness measures whether the fixes worked. That
is the point of the benchmark: it converts "is Nova AI-friendly?" from a
vibe into a number that goes down as we fix things.

## What Nova did well

- Every Nova failure was caught **at compile time** with a stable code and a
  hint. No Nova bug ever reached runtime. (Python, by contrast, can only fail
  at runtime — it passed here, but the property doesn't generalize.)
- The `?` + `Result`/`Option` pipeline, once structured correctly, handled
  all four json-cli edge cases (missing file, malformed JSON, sparse rows)
  with no defensive `if` pyramids.
- The component task was first-try green, including event simulation.

## Threats to validity

- Single model (Muse), which co-built Nova. Cold-start protocol + full
  transcripts mitigate; judge the naivety of attempt 1s yourself.
- n = 4 tasks. v2 should add: auth middleware, DB CRUD, streaming.
- Cheat sheets are author-written at equal length, but coverage differs
  (notably serving — see gap 5).

## Reproduce

```bash
cd benchmarks/ai-coldstart-v1
./tasks/01-typed-fns/test.sh results/01-typed-fns/nova/attempt1
# ... etc; every green cell's final attempt passes its test.sh
```

Full transcripts: `results/<task>/<lang>/transcript.md`.
Final solutions: `results/<task>/<lang>/attempt<N>/solution.*`.
