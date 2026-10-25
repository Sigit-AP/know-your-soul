# Scoring Contract — P, M, E

Contract version: 1.0.0
Status: FROZEN (2026-10-24)
Binding spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`, section 9, quoted verbatim below.
Fixtures: `tests/fixtures/scoring_cases.json` (37 cases, 13 independently hand-calculated). Verified by: `tests/test_measurement_contracts.py`.

Any change to a formula, parameter, or the M definition requires a new
scoring-contract version and operator approval (spec section 9: "Formula and
parameter changes require a new scoring-contract version").

## Numeric semantics

Scores P, M, and E use decimal strings in `[0,1]` and remain separately visible.
All calculations use Python `Decimal` with precision 34 and no binary-float
conversion. Intermediate comparisons use full precision; serialized scores are
quantized to 12 decimal places with `ROUND_HALF_EVEN`.

## Evidence weight

For dimension `d`, evidence weight before normalization is:

`q_j,d = reliability_j * exp(-ln(2)*age_days_j/h_d) * context_relevance_j * independence_j / (1 + depth_j)`

- `age_days_j` is the nonnegative integer microsecond difference between UTC
  `at` and `observed_at`, converted with `Decimal(microseconds)/(86400*1000000)`;
  evidence from the future is invalid.
- Defaults: `h_P=90`, `h_M=30`, `h_E=7` days.
- `depth_j=0` for the scored node and increases toward descendants. (Fixtures
  carry depth as scoring context per node, since depth is relative to the scored
  node; literal reading flagged in the Task 3A report.)
- `independence_j=1/sqrt(group_size)` for evidence sharing an `independence_key`;
  exact duplicates are represented once.
- `context_relevance=1` for an empty query context, otherwise the Jaccard
  similarity between evidence and query context tags.

Before scoring, deduplicate exact duplicates by canonical content hash, retain
the lexicographically smallest UTF-8 `evidence_id`, sort retained evidence by
ascending UTF-8 `evidence_id`, and use that single order for every Decimal sum,
weighted sum, primary aggregation, and replay. For P and E, normalize with
`w_j,d=q_j,d/sum(q_d)` over all eligible items for that dimension. Missing
evidence is never replaced with a fabricated prior score.

## P — quality

P is the weighted mean of:

`p_j = 0.30*correctness_j + 0.25*((outcome_j+1)/2) + 0.20*reproducibility_j + 0.15*scope_fit_j + 0.10*(1-contradiction_j)`

Usage frequency alone cannot increase P. Contradictory, correlated, stale, and
out-of-context evidence affects P through the defined terms rather than
undocumented penalties.

## M — maximum effective utilization under the quality gate

M means maximum effective utilization under the quality gate, not raw usage
count. For each utilization bin `u`, select only evidence in that bin with
`q_j,M>0` and normalize locally: `w_j,u=q_j,M/sum(q_M in bin u)`. Then `Q(u)=sum(w_j,u*quality_at_use_j)` and `S(u)=sum(w_j,u*successful_use_j*task_coverage_j)`.
The independent-item count is the number of distinct `independence_key` groups
remaining after exact deduplication in that bin. A bin is eligible only with at least three distinct independence groups, nonzero local weight, and `Q(u)>=0.70`.

`M = max over eligible u of (u * S(u))`; if no bin is eligible, `M=0` and `selected_m_bin=null`. Ties are resolved toward the smaller `u`, then the lexicographically smallest contributing evidence-ID list. The selected bin and
its evidence count must be returned (fixtures: `selected_m_bin`,
`m_bin_evidence_count`; both `null` when no bin is eligible — literal reading
flagged in the Task 3A report).

`task_coverage` is `covered_required_task_items / total_required_task_items`
for the same versioned task rubric; an empty denominator makes the evidence
invalid.

## E — quality-adjusted efficiency

For each evidence item:

`benefit_j = successful_use_j * task_coverage_j`
`cost_j = 0.4*min(2, latency_ms_j/1000) + 0.4*min(2, output_chars_j/4000) + 0.2*min(2, compute_ms_j/1000)`
`e_j = benefit_j/(benefit_j+cost_j)`, or zero if both are zero

E is the weighted mean of `e_j`. Baselines are milliseconds and UTF-8 characters
per completed knowledge operation, versioned by workload class; the defaults
above are the initial `default` class. E never overrides the P gate.

## Zero total weight and uncertainty

For a dimension with no valid provenance-bearing evidence or with `sum(q_d)=0`,
that dimension is treated as having zero eligible weighted evidence: its score
is 0, `U_d=1`, and no weighted mean is evaluated. For M this also forces
`selected_m_bin=null`; for P it forces professional level 1. The response
reports separate counts for missing provenance, disjoint/out-of-context
evidence, zero reliability, and other exclusions with reason
`ZERO_TOTAL_WEIGHT` when applicable. If all three dimensions have zero total
weight, `P=M=E=0` and overall uncertainty is 1. Otherwise scores and
uncertainty remain independent per dimension. For nonzero total weight,
`U_d=1/sqrt(1+sum(q_d))`. Overall uncertainty is `max(U_P,U_M,U_E)` and
confidence per dimension is `1-U_d`, not an AI probability.

## Aggregation

Primary aggregation recomputes the same formulas over the union of the primary
node's own evidence and descendant evidence, deduplicated by `evidence_id`, with
the depth factor above. Child scores are never averaged directly and low
children remain inspectable.

## Professional level

Professional level starts from P: level 1 below 0.30; 2 at 0.30; 3 at 0.50; 4 at 0.70; 5 at 0.85.
Evidence counts below mean distinct `independence_key` groups with `q_P>0`
after exact deduplication. Caps then apply: fewer than 5 groups caps at 2; fewer than 10 caps at 3; fewer than 20 caps at 4.
`U_P>0.70` caps at 2, `U_P>0.50` caps at 3, and `U_P>0.30` caps at 4.

## Composite and promotion

The optional composite is `G=0` when `P<0.70`; otherwise `G=P^0.50 * M^0.30 * E^0.20`.
Promotion additionally requires at least 10 distinct independence groups with `q_P>0` and `U_P<=0.50`. G never replaces the
three primary scores. (The promotion quality gate is read as `P>=0.70` — the
same 0.70 used by the G gate; flagged in the Task 3A report.)

## Replay guarantee

Permuting input evidence must produce byte-identical 12-decimal output after
canonical sorting. Exact expected output uses the 12-decimal serialization rule.
