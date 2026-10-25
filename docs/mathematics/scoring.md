# Scoring Implementation Notes — P/M/E Engine

Implements: scoring contract 1.0.0 (`scoring-contract.md`) and evidence
schema 1.0.0 (`evidence-schema.md`), both FROZEN. This document describes the
feature code; it changes no formula. Any formula or parameter change requires
a new scoring-contract version and operator approval (spec section 9).

Feature code: `src/soul_knowledge/scoring.py`
Parameters: `src/soul_knowledge/scoring_config.toml` (mirrors the frozen
contract; the loader fails closed on an unknown contract version, unknown
keys, or inconsistent weights).
Fixtures: `tests/fixtures/scoring_cases.json` (37 cases, 13 hand-calculated;
verified independently by `tests/test_measurement_contracts.py` and
reproduced byte-identically by `tests/test_scoring.py`).

## Formula-to-code map

All arithmetic runs inside one `localcontext` at precision 34
(`DECIMAL_PRECISION` from `domain.py`); nothing passes through a binary
float. Serialized scores use `serialize_score` = `format(quantize_score(v),
"f")`, i.e. 12 decimal places with `ROUND_HALF_EVEN`.

| Contract clause | Code |
|---|---|
| Exclusion precedence: missing provenance → invalid fields → zero reliability → disjoint context | `_classify`, counted into `exclusion_counts` |
| Dedup by canonical content hash (all fields except `evidence_id`), retain smallest UTF-8 id, sort by ascending UTF-8 `evidence_id` | `_dedup_key`, `_dedup` |
| `q_j,d = reliability * exp(-ln(2)*age_days/h_d) * relevance * independence / (1+depth)` | enrichment loop in `score_items`; `h_P=90`, `h_M=30`, `h_E=7` from config |
| `age_days` from nonnegative integer microseconds | `_microseconds(at - observed) / MICROSECONDS_PER_DAY`; future evidence excluded as invalid |
| `independence_j = 1/sqrt(group_size)` per shared `independence_key` | `Counter` over deduped items, `Decimal(n).sqrt()` |
| `context_relevance`: 1 for empty query, else Jaccard | `_jaccard` |
| `p_j = 0.30*correctness + 0.25*((outcome+1)/2) + 0.20*reproducibility + 0.15*scope_fit + 0.10*(1-contradiction)` | `p_j` term in `score_items`, weights from config |
| P, E: weighted means with `w_j,d = q_j,d/sum(q_d)` | `_weighted_mean` |
| M: per-bin local normalization, `Q(u)`, `S(u)`, eligibility (≥3 distinct independence groups, `Q(u)>=0.70`), `M = max_u u*S(u)`, ties to smaller `u` then lexicographic evidence-id list | M-bin loop in `score_items`; `selected_m_bin`, `m_bin_evidence_count` (both `null` when no bin is eligible) |
| E: `benefit_j = successful_use*task_coverage`; `cost_j = 0.4*min(2,latency/1000) + 0.4*min(2,chars/4000) + 0.2*min(2,compute/1000)`; `e_j = benefit/(benefit+cost)` or 0 | `e_j` term in `score_items`, baselines from config (`default` workload class) |
| Zero total weight: score 0, `U_d=1`, reason `ZERO_TOTAL_WEIGHT`; P forces level 1; M forces `selected_m_bin=null` | zero-total-weight branches in `score_items` |
| `U_d = 1/sqrt(1+sum(q_d))`; overall `U = max(U_P,U_M,U_E)`; confidence `1-U_d` | uncertainty block; `ScoreAssessment.confidence_*` |
| Professional level: P thresholds 0.30/0.50/0.70/0.85, then group caps (5→2, 10→3, 20→4) and uncertainty caps (0.70→2, 0.50→3, 0.30→4) | level block in `score_items`, caps from config |
| Composite: `G=0` when `P<0.70` (or any of P/M/E is 0); else `G=P^0.50*M^0.30*E^0.20` | composite block; gate and exponents from config |
| Promotion: `P>=0.70`, ≥10 distinct independence groups with `q_P>0`, `U_P<=0.50` | `promotable` flag |
| Aggregation: recompute over the union of primary + descendant evidence, deduped, with depth = BFS distance along `parent_of`; child scores never averaged | `ScoreEngine.aggregate_primary` |
| Replay guarantee: permutation-invariant, byte-identical 12-decimal output | canonical sort in `_dedup` drives every sum; pinned by `test_permutation_replay_is_byte_identical` |

## Full-precision gates

Comparisons (level thresholds, the P quality gate, promotion, bin
eligibility) run at full precision 34 — never on the quantized 12-decimal
rendering. A score that renders as `0.700000000000` can still sit below the
gate at full precision when the normalized weights divide inexactly; this is
the frozen semantic and is what the fixtures pin.

## Engine surface

```python
engine = ScoreEngine(store)                    # config defaults to the bundled TOML
engine.score_node(node_id, context=tags, at=when)        # node's own evidence, depth 0
engine.aggregate_primary(primary_id, context=tags, at=when)  # primary + descendants
```

The engine reads through `EventStore.replay_domain()` only and never writes.
Unknown nodes raise `UnknownNodeError`. `score_items(items, at=...)` is the
pure, store-free core used by both the engine and the tests; it consumes the
frozen evidence-schema mapping (decimal strings), with
`evidence_item(Evidence)` adapting domain records to that shape.
`ScoreAssessment.to_contract_dict()` returns the serialized assessment in
exactly the frozen fixture shape.

`KnowledgeService.evaluate` wiring (request → engine → `EvaluateResponse`
with audit event) is applied separately; see the Task 5 report's handoff
section.
