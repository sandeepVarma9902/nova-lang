# AI training with Nova — the honest loop

Nova does **not** train models. There is no GPU in the language, no
backpropagation, no optimizer — and we will not pretend otherwise. What
Nova *does* is make the two halves of the real loop first-class:

1. **Data**: get training data out of your systems in the right shape
   (`nova data export`).
2. **Evaluation**: score the trained model with deterministic, versioned
   checks (`nova eval`).

Training itself happens outside Nova, in whatever framework you use. This
document describes the full loop.

## The loop

```
┌──────────────┐      ┌──────────────┐      ┌──────────────┐
│  your systems │      │  train       │      │  nova eval   │
│  (sqlite,      │─────▶│  externally  │─────▶│  score the   │
│   APIs, logs) │ jsonl│  (PyTorch,   │ model│  model       │
│               │      │   HF, APIs)  │      │              │
└──────────────┘      └──────────────┘      └──────────────┘
       ▲                                            │
       └──────────── iterate on failures ───────────┘
```

## Step 1: export — `nova data export`

```bash
./nova data export \
  --from sqlite:///app.db \
  --query "SELECT prompt, completion FROM fine_tuning_pairs" \
  --format jsonl \
  --out train.jsonl
```

Each row becomes one JSON object per line:

```jsonl
{"prompt":"Summarize: ...","completion":"..."}
{"prompt":"Summarize: ...","completion":"..."}
```

- `--from` takes the same DSNs as `nova db` (sqlite in v2.1).
- `--query` is your SQL — filter, join, and shape the dataset with the
  full power of SQL, not a limited export DSL.
- `--format jsonl` is the only format in v2.1 (the fine-tuning lingua
  franca). Output goes to `--out`, or stdout if omitted.

**Data hygiene is your job** — Nova exports what you select. Deduplicate,
decontaminate (keep eval data out of train), and check licenses *before*
you train. `nova eval` can only score what you measure.

## Step 2: train — externally

Train wherever you train: PyTorch, Hugging Face Transformers, an API
provider's fine-tuning endpoint. Nova has no opinion here and no code
here. A typical shape:

```bash
# example only — not Nova code
python train.py --train train.jsonl --eval eval.jsonl --out ./model-v3
```

Export a separate `eval.jsonl` with the same command and a different
query (or a `WHERE` split) — and **never train on it**.

## Step 3: evaluate — `nova eval`

`nova eval` (v2) scores a model against a suite of prompts and predicates:

```bash
./nova eval eval-suite.json --provider http --out report.json
```

The suite declares prompts, the expected properties, and the pass/fail
predicates; the report is JSON you can diff across model versions. The
workflow is: export → train → eval → read the failures → fix the data →
export again.

## RAG in the language (the other AI-data story)

Separately from training, Nova is RAG-native at the type level:

```nova
ai fn embed(text: String) -> Vector[1536]
  model = "text-embedding-3-small"
{
  prompt(text)
}

let docs: List[Vector[1536]] = [embed("..."), embed("...")]
let hits: List[Int] = topK(embed(query), docs, 5)  // indices, ranked
let s: Float = similarity(a, b)                    // cosine similarity
```

- `Vector[N]` is a real type — `Vector[1536]` vs `Vector[768]` is a
  **compile-time** error (`E_TYPE_MISMATCH`), in `similarity`, `topK`,
  and `ai fn` return schemas.
- `ai embed` in v2.1 is a **mock provider with the real interface**:
  deterministic pseudo-embeddings so RAG programs run and test without
  credentials. Point `NOVA_AI_PROVIDER` at a real embedding endpoint for
  real vectors — the program does not change.
- `topK(query, vecs, k)` returns the indices of the k most similar
  vectors, ranked. `similarity` is cosine similarity.

See `examples/rag.nova` for a working miniature RAG pipeline.

## What we will never claim

- ❌ "Train models in Nova." No. Export data, evaluate models, train
  elsewhere.
- ❌ "The mock embedder understands text." It is a deterministic hash —
  stable for tests, meaningless for semantics. The docs and the example
  say so.
- ❌ "Fine-tuning as a builtin." The day there is a credible local
  training story, it will arrive as a provider behind the `ai` interface,
  not as language syntax.
