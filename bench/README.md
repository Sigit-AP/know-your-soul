# bench/ — deterministic SOUL benchmark harness (Task 10)

Reproducible, no-AI benchmark of the knowledge system against the raw SOUL
corpus. Everything here is deterministic: the corpus is pinned by sha256, the
questions are frozen, the token estimator is the frozen contract formula, and
the quality rubric is keyword coverage — no model is involved anywhere.

## Layout

| Path | What it is |
|------|------------|
| `run.py` | The harness: `freeze`, `verify`, `check`, `run` subcommands |
| `cases.json` | 24 frozen questions spanning all seven primary domains (P1-P7), each with an expected primary domain, rubric terms, and the corpus files it was derived from |
| `corpus/` | Frozen snapshot of `/root/.hermes/knowledge/soul/*.md` (28 files, 52,604 bytes = 13,151 estimated tokens — the frozen contract baseline) plus `manifest.json` with per-file sha256 |
| `results/` | Machine-readable JSON ledgers, one per `run` invocation |

The live source corpus is **read-only** for this harness: `freeze` opens it
for reading and hashing only, and `test_acceptance_e2e.py` asserts the live
source still matches the frozen manifest.

## Commands

```bash
python bench/run.py freeze   # re-snapshot the pinned corpus + write manifest
python bench/run.py verify   # frozen-corpus integrity; live-source drift report
python bench/run.py check    # no-system self-checks (exit 0/1)
python bench/run.py run [--repeats N]   # paired benchmark
```

## The paired measurement

Per case, over N repeats (default 5):

* **latency**: p50/p95 of `KnowledgeService.query` (nearest-rank percentile);
* **tokens**: system `estimated_tokens = ceil(utf8_byte_length / 4)` of the
  canonical response payload vs the raw-corpus baseline (13,151 tokens);
* **quality**: fraction of the case's rubric terms present in the response
  payload (raw-corpus baseline = 1.0 by construction);
* **quality gate**: a case whose quality loses against its paired baseline is
  **rejected even when it saves tokens** (token-budget contract 1.0.0).

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | all checks/cases pass |
| 1 | a check failed, or at least one case verdict was `rejected` |
| 2 | usage error |
| 3 | **skipped** — integration pending; a `skipped` ledger was still written |

## Integration skip-gate

`run` measures the *whole* system, so it gates on the three access-path
adapters (`soul_knowledge.cli`, `soul_knowledge.http_api`,
`soul_knowledge.mcp_server`). While any is missing, `run` exits 3 and writes a
ledger with `status: "skipped"` and per-case `pending-integration`. The gate
opens automatically when Tasks 7-8 land — nothing in this directory needs
editing at integration.

## Estimator parity

`check` verifies the local estimator byte-for-byte against the frozen fixtures
in `tests/fixtures/token_budget_cases.json` and against
`soul_knowledge.contracts.estimated_tokens_of_payload`. The same parity is
pinned by `tests/test_acceptance_e2e.py::TestTokenEstimatorParity`.
