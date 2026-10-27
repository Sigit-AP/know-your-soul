# Memory benchmark

Measures the product's retrieval recall on the real
[LOCOMO](https://github.com/snap-research/locomo) conversational-memory
dataset — the same dataset behind the recall@10 numbers several memory systems
publish. Everything here is deterministic and model-free: no LLM anywhere in
the pipeline, results reproduce byte-for-byte.

## Run it

```bash
uv run python bench/memory/run.py download   # fetch locomo10.json (sha256-pinned, verified every run)
uv run python bench/memory/run.py build      # write turns through the product's real write path
uv run python bench/memory/run.py run        # evaluate recall@10, write bench/memory/results/
uv run python bench/memory/run.py keyfact    # key-fact coverage on our own frozen corpus
```

The dataset is downloaded once and pinned by sha256. Every run re-verifies the
pin before evaluating.

## What it measures

| Subcommand | Metric | Unit |
|---|---|---|
| `run` | recall@10 (any-hit), recall@10 (all-hit) | 1,982 questions with evidence (plus a seeded n=300 sample) |
| `keyfact` | rubric key-fact coverage | 24 frozen benchmark cases in `bench/cases.json` |

Systems measured, all deterministic:

- **kys_federated** — the product as-is. Turns are written through
  `KnowledgeService.log_event` and published by `GraphProjector.rebuild`. The
  product's 64 KiB artifact cap bounds one store to ~40 turn nodes, so the
  corpus is sharded 32 turns per store (a real, supported topology: N
  independent product stores). Rankings come from the product's own relevance
  engine, merged by the product's own ordering.
- **kys_global_diagnostic** — the same published nodes merged into one index
  to isolate index quality from the artifact cap. Diagnostic only; not a
  supported product topology.
- **bm25_anchor** — stdlib Okapi BM25 (k1=1.5, b=0.75) over the same turn
  texts with the same hit criteria, as a comparability anchor.

## Latest results

| System | recall@10 (full, n=1,982) | recall@10 (n=300, seed 83) |
|---|---|---|
| kys_federated (product) | 0.5419 | 0.5433 |
| kys_global_diagnostic | 0.5969 | 0.5900 |
| bm25_anchor | 0.5121 | 0.5100 |
| Key-fact coverage (frozen corpus) | 0.4312 | — |

Full ledgers with per-category breakdowns: `bench/memory/results/`.

## What this benchmark does not measure

QA accuracy (LOCOMO, LongMemEval) requires an LLM reader/judge. The product
has a hard no-AI runtime boundary, so those numbers are reported as N/A in the
report — never fabricated.

See the full methodology, dataset pin, and caveats in the module docstring of
`run.py` and in `docs/` reports under `.superpowers/sdd/`.
