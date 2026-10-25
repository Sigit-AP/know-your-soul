# Recovery: T10-1 (Opsi C) — recalibrate the bench quality gate to the per-case compliant ceiling

- **Date**: 2026-10-25 — **Owner**: SUB-22 — **Severity**: launch blocker (T10-1), measurement-calibration half
- **Ruling**: controller Opsi C (documented in `.superpowers/collab/shared-board.md` and `review-c410fa4.md`): the bench quality gate is recalibrated to compare against the per-case **compliant ceiling** plus a quality floor; the raw-corpus baseline stays in the ledger as a comparison metric. **Reversible**: gate 1.0.0 is preserved verbatim in every ledger (`gate.legacy_rule_1.0.0`) and reverting is a one-hunk revert of `run_benchmark`'s verdict block.

## What the old gate measured, and why it was unreachable

Gate 1.0.0 accepted a case iff `quality_system >= 1.0` (the raw-corpus baseline) and `>= QUALITY_FLOOR (0.6)`. This is **provably unreachable** for any contract-compliant implementation:

- the response contract forbids raw SOUL/corpus text in the payload and caps the token budget at 1024, while the rubric terms live in section *bodies* (verified: `test_rubric_terms_are_grounded_in_frozen_corpus` — terms are grounded in raw bodies, not summaries);
- measured at HEAD `2cd51ea` (post-Opsi-A enrichment): even *perfect* retrieval of every node from a case's `source_files` yields a compliant ceiling of **34/83 rubric terms ≈ 0.41** (term-weighted; per-case ceilings 0.0–1.0, mean 0.418). Pre-recalibration ledger `ledger-20261025T190544Z.json`: **4/24 accepted** (only the four cases whose ceiling is exactly 1.0), mean quality 0.4139, exit 1.

A gate no compliant system can pass carries zero signal (Goodhart inverted): it rejected honest optimal systems and misattributed a bench-calibration flaw to the retrieval layer.

## The new gate (gate contract 2.0.0)

**Ceiling definition (exact, implemented in `bench/run.py::compliant_ceiling`):**
for case *C*, let *N* = nodes of the live projected graph (newest
`generations/*/graph.json`) whose `source_file ∈ C.source_files`. Pack a
subset *S ⊆ N* into a synthetic payload with the exact keys/shapes of
`QueryResponse.canonical_payload` (fixed dummy metadata; scored over its JSON
exactly as a live response is scored, so payload keys like
`estimated_tokens` and provenance URIs count identically on both sides).
`quality_ceiling = max over {S : estimated_tokens(payload(S)) ≤ C.token_budget}
of rubric_score(C.rubric_terms, payload(S))`. Scores are monotone in *S*, so
when the full set fits the budget it is the optimum (always the case today:
largest ceiling payload ≈ 360 est. tokens vs budget 1024); otherwise exact
enumeration up to 12 nodes, deterministic greedy (marginal gain, fewer
tokens, node id) beyond. **Everything derives from the real projection
artifact — no hardcoded numbers anywhere; if enrichment improves, the
ceiling rises automatically.**

**Acceptance:** `quality_system >= quality_ceiling − 1e-9` AND
`quality_system >= quality_floor_applied − 1e-9`, where
`quality_floor_applied = min(QUALITY_FLOOR, quality_ceiling)` — the floor can
never demand more than the achievable maximum; a case whose ceiling is below
the floor carries `ceiling_below_floor: true` (enrichment debt, flagged in
the ledger, not a system regression).

**Why this strengthens (not weakens) the quality signal:** under gate 1.0.0
`rejected` meant "not equal to reading the whole corpus" — true of every
compliant response, hence no information. Under 2.0.0 `rejected` means "the
system delivered strictly less than a contract-compliant response could" — a
real retrieval/selection defect, and it immediately caught three (below).
The raw-read gap is not hidden: every case still reports
`quality_baseline: 1.0` and `gap_vs_raw_read` explicitly.

## Measured before → after (same system, same HEAD `2cd51ea`)

| Metric | Gate 1.0.0 (`ledger-20261025T190544Z`, repeats=3) | Gate 2.0.0 (`ledger-20261025T192059Z`, repeats=5) |
|---|---|---|
| Accepted | 4/24 | **21/24** |
| Quality mean (system) | 0.4139 | 0.4139 (unchanged — measurement, not the system, was recalibrated) |
| Ceiling mean (per-case) | — (gate was flat 1.0) | 0.4181 |
| p50 per case | ≤ 12.3 ms | median 8.46 ms (3.29–16.11) |
| p95 per case | ≤ 28.7 ms | median 11.02 ms (max 35.67) |
| Token saving vs raw corpus | 0.92–0.99 (mean 0.943) | 0.9245–0.9910 (mean 0.9428; system payloads 123–993 est. tokens vs 13,151) |
| Mean `gap_vs_raw_read` | 0.586 (implicit) | 0.5861 (explicit per case) |
| Exit code | 1 | 1 (3 genuine retrieval bugs, below) |

## The 3 rejected cases — genuine retrieval bugs (owned by the retrieval line, NOT this task)

1. **q05-chestertons-fence** (sys 0.000 vs ceiling 0.500): routing `ambiguous` → zero results; an exact P2/P7 tie on the shared term "heuristic" (also recorded as a residual in `task-10-T10-1-summary-enrichment-opsi-a.md`).
2. **q16-premortem-steelman** (sys 0.500 vs ceiling 0.750): misrouted to P6 (`21-completion-criteria` digest outscores P4's `09-premortem-steelman` 17.68 vs 17.27 per SUB-20's probe); the pre-mortem node never enters the payload, so "not finished" is missing.
3. **q03-overconfidence-countermeasure** (sys 0.000 vs ceiling 0.333): routed P1-correct, but the `08-anti-bias` node (the case's second `source_files` entry, carrying the countermeasure vocabulary) is not selected into the payload within depth 2.

Bench exit stays **1** until the retrieval owner fixes these; this task's scope (bench/, docs/, the e2e gate assertion) forbids touching `src/`.

## Also flagged, not gated

- 18/24 cases carry `ceiling_below_floor: true` — their rubric terms are partly absent from every publishable summary (enrichment debt, owner: projection/enrichment line). Ceiling term coverage 34/83 ≈ 0.41 is the current compliant maximum; the raw-corpus gap (mean `gap_vs_raw_read` 0.586) is reported per case.
- `tests/test_acceptance_e2e.py::TestBenchmarkQualityGate` updated to the 2.0.0 semantics (asserts the ceiling rule, `floor_applied = min(floor, ceiling)`, baseline quality still 1.0, `gap_vs_raw_read` consistency, token saving > 0). Justification: the old assertion encoded the unreachable gate; the new one pins the calibrated rule including its anti-Goodhart invariants.

## Rollback

Revert the `run_benchmark` verdict block to `quality_system < quality_baseline or quality_system < QUALITY_FLOOR` and drop the ceiling computation; gate 1.0.0's exact rule text is embedded in every 2.0.0 ledger under `gate.legacy_rule_1.0.0`.
