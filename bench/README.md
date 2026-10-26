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

Run from the repository root with the locked environment (`uv sync --frozen`
first, or prefix with `uv run`):

```bash
uv run python bench/run.py freeze   # re-snapshot the pinned corpus + write manifest
uv run python bench/run.py verify   # frozen-corpus integrity; live-source drift report
uv run python bench/run.py check    # no-system self-checks (exit 0/1)
uv run python bench/run.py run [--repeats N]   # paired benchmark
```

## The paired measurement

Per case, over N repeats (default 5):

* **latency**: p50/p95 of `KnowledgeService.query` (nearest-rank percentile);
* **tokens**: system `estimated_tokens = ceil(utf8_byte_length / 4)` of the
  canonical response payload vs the raw-corpus baseline (13,151 tokens);
* **quality**: fraction of the case's rubric terms present in the response
  payload. The raw-corpus baseline (1.0 by construction) is still reported
  per case as `gap_vs_raw_read` — a comparison metric, not the gate;
* **quality gate (gate contract 2.1.0, "Opsi C" + Ruling Q03)**: a case is
  **accepted**
  iff `quality_system >= quality_ceiling` (its per-case *compliant ceiling*,
  ε = 1e-9) **and** `quality_system >= quality_floor_applied`
  (`min(QUALITY_FLOOR, quality_ceiling)`). Falling short of the ceiling is
  rejected **even when the token saving is large**.

### The compliant ceiling

`quality_ceiling` is the maximum rubric coverage any *contract-compliant*
response can deliver for a case: the maximum `rubric_score` over subsets of
the projected-graph nodes whose `source_file` is one of the case's
`source_files`, packed into a contract-shaped response payload
(`QueryResponse.canonical_payload` shape, fixed dummy metadata) that fits the
case's `token_budget`. It is computed from the **live projection artifact**
(newest `generations/*/graph.json`), never hardcoded — when enrichment
improves (e.g. richer `enriched_summary`), the ceiling rises automatically.
Exact subset maximization up to 12 source nodes per case (scores are monotone
in the subset, so when the full set fits the budget it *is* the optimum); a
deterministic greedy fallback (marginal rubric gain, fewer tokens, node id)
beyond that. Budget-awareness is currently never binding (largest ceiling
payload ≈ 360 est. tokens vs the 1024 budget) but is enforced so a future
richer projection cannot silently make the gate unwinnable.

**Single-root routing feasibility (gate 2.1.0, Ruling Q03).** The service
contract assembles results only from the *selected root's* traversal
subtree, so gate 2.0.0's unrestricted candidate set could credit ceilings no
compliant single-root response can achieve (q03's 0.3333 required nodes from
P1 *and* P5 in one response). Gate 2.1.0 therefore restricts ceiling
candidates to the union of the subtrees of (a) the case's
`expected_primary_domain` root and (b) every root the deterministic router
actually selected for the case — routing metadata only, never quality, feeds
the ceiling. When this routing-feasible maximum **equals** the 2.0.0 value,
the 2.0.0 record stands **byte-identical** (verified: 23/24 cases unchanged,
only q03 lowered). A lowered ceiling is annotated in the ledger with
`ceiling_unrestricted_2.0.0` and a `ceiling_routing_note`; a case whose
rubric is unreachable even under its expected routing is accepted as
`vacuous: rubric unreachable under expected routing (case-design debt)` —
the debt is named in the ledger, and `gap_vs_raw_read` keeps the honest
measured distance to raw-read visible (q03 still reports 1.0).

Why not the raw-corpus baseline as the gate (gate 1.0.0)? The response
contract forbids raw corpus text in the payload and caps the budget at 1024
tokens, while rubric terms live in section *bodies* — so `quality == 1.0`
is **provably unreachable** for any compliant response (measured ceiling:
34/83 rubric terms ≈ 0.41 coverage even with perfect retrieval). A gate
nothing can pass carries zero signal (Goodhart inverted): it rejected honest
optimal systems and misattributed a measurement-calibration flaw to the
retrieval layer. Gate 2.0.0 restored the intended semantics, and gate 2.1.0
keeps them: `rejected` now
means *the system delivered less than a compliant response could* — a real
retrieval/selection bug — while `gap_vs_raw_read` keeps the honest distance
to raw-read visible on every case. Cases whose ceiling sits below
`QUALITY_FLOOR` carry `ceiling_below_floor: true` (enrichment debt: rubric
terms absent from every publishable summary) — flagged in the ledger, never
silently accepted or rejected on floor grounds.

## Exit codes

| Code | Meaning |
|------|---------|
| 0 | all checks/cases pass |
| 1 | a check failed, or at least one case verdict was `rejected` |
| 2 | usage error |

`run` measures the whole system through the three access-path adapters
(`soul_knowledge.cli`, `soul_knowledge.http_api`, `soul_knowledge.mcp_server`)
and exits **0 with 24/24 cases accepted** on the frozen corpus (gate contract
2.1.0).

## Estimator parity

`check` verifies the local estimator byte-for-byte against the frozen fixtures
in `tests/fixtures/token_budget_cases.json` and against
`soul_knowledge.contracts.estimated_tokens_of_payload`. The same parity is
pinned by `tests/test_acceptance_e2e.py::TestTokenEstimatorParity`.
