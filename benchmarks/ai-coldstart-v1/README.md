# AI cold-start benchmark v1 — Nova vs TypeScript vs Python

**Question:** when an AI coding agent gets the same task and only a one-page
cheat sheet per language, in which language does it produce working code
fastest? Metrics: **first-try pass rate** and **mean iterations to green**.

## Protocol (the agent loop)

The agent is Muse acting as a competent generalist AI, with constraints:

1. Before each task+language, the agent may read ONLY `tasks/<t>/spec.md` and
   `cheatsheets/<lang>.txt`. No other docs, no memory of the language beyond
   the sheet. (For Nova the sheet is `ai-build.txt` — exactly what an external
   AI would receive.)
2. The agent writes attempt 1, then runs ONLY `tasks/<t>/test.sh <solution-dir>`.
3. On failure the agent may read ONLY the tool output (compiler errors, test
   failures) and revise. Max 5 attempts per task+language.
4. Every attempt, every error, every fix is recorded verbatim in
   `results/<task>/<lang>/transcript.md`.
5. An attempt "passes" only if `test.sh` exits 0.

The agent must not use knowledge outside the cheat sheet to pre-fix mistakes
it has not yet seen fail — first attempts are written as a careful reader of
the sheet would write them.

## Threats to validity (read before citing numbers)

- **Single model, and it knows Nova best.** The author (Muse) co-built Nova.
  Mitigated by the cold-start rule above and full transcripts — judge for
  yourself whether first attempts look naive.
- **n = 4 tasks.** Small by design for v1; tasks cover typed functions,
  fallible IO, an HTTP API, and stateful UI — Nova's headline claims.
- **Cheat sheets are author-written.** Same target length (~80 lines) and the
  same "here are the gotchas" style for all three languages.
- **Python has no compile step.** Its "compiler" is the test run itself —
  which is honest: that IS Python's feedback loop.

## Layout

- `cheatsheets/` — the only docs the agent may read per language.
- `tasks/<nn-name>/spec.md` — the identical natural-language task.
- `tasks/<nn-name>/test.sh` — acceptance tests; usage: `test.sh <solution-dir>`.
- `results/<task>/<lang>/transcript.md` — full agent log per cell.
- `REPORT.md` — numbers + analysis (written after all runs).

## Reproducing

```bash
cd benchmarks/ai-coldstart-v1
./tasks/01-typed-fns/test.sh results/01-typed-fns/nova/attempt1
```
