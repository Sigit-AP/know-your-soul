# Evidence Schema — Measurement Contract

Contract version: 1.0.0
Status: FROZEN (2026-10-24)
Binding spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`, section 9 (evidence item definition) and section 6 (provenance requirements).
Fixtures: `tests/fixtures/scoring_cases.json`. Verified by: `tests/test_measurement_contracts.py`.

Any change to the fields, units, validity rules, or serialization convention in
this document requires a new contract version and operator approval.

## Evidence item fields

Each evidence item contains exactly these fields (spec section 9):

- `evidence_id` — stable identifier; UTF-8 ordering is significant (see ordering below).
- `node_id` — the node the evidence attaches to.
- `source_uri` — provenance reference. Evidence without valid provenance is
  unscorable and reported separately.
- `observed_at` — ISO-8601 UTC timestamp of the observation.
- `context_tags` — list of context tags.
- `independence_key` — correlation group key (see the scoring contract).
- `outcome` in `[-1,1]` — decimal string.
- `correctness`, `reliability`, `reproducibility`, `scope_fit`,
  `successful_use`, `task_coverage`, `quality_at_use`, and `contradiction` in `[0,1]`
  — decimal strings.
- `utilization_u` in `{0.1,0.2,...,1.0}` — decimal string; the M-bin assignment.
- `latency_ms > 0`, `compute_ms > 0`, and `output_chars >= 0` — integers.

Units: `latency_ms` and `compute_ms` are milliseconds; `output_chars` is a UTF-8
character count; both are per completed knowledge operation (spec section 9).

`task_coverage` is `covered_required_task_items / total_required_task_items` for
the same versioned task rubric; an empty denominator makes the evidence invalid.
In fixtures this is represented as `"task_coverage": null`, which marks the item
invalid (literal reading recorded in the Task 3A report).

## Decimal and serialization convention

All decimal fields are decimal strings; binary floats are never accepted. All
calculations use Python `Decimal` with precision 34; intermediate comparisons
use full precision; serialized scores are quantized to 12 decimal places with
`ROUND_HALF_EVEN`. This matches the store boundary
(`src/soul_knowledge/domain.py::storage_payload` + `quantize_score`), which
quantizes every `Decimal` to 12 decimal places with `ROUND_HALF_EVEN` before
persistence or hashing. Exact zero scores are rendered in fixtures as the
fixed-point string `0.000000000000`; this is the same quantized value (same
quantum, same rounding) as the store's `str(quantize())` rendering.

## Validity and exclusion classes

An item is excluded from scoring, in this precedence order (literal reading,
flagged in the Task 3A report):

1. Missing provenance (`source_uri` empty) — unscorable and reported separately.
2. Invalid fields — out-of-range values, `utilization_u` outside
   `{0.1,0.2,...,1.0}`, non-positive latency/compute, negative `output_chars`,
   `task_coverage` with an empty denominator, or evidence from the future
   (spec section 9: "evidence from the future is invalid").
3. Zero reliability — contributes zero weight.
4. Disjoint/out-of-context — non-empty query context with Jaccard similarity 0.

## Deduplication and ordering

Before scoring, exact duplicates are deduplicated by canonical content hash
(canonical JSON — sorted keys, compact separators, UTF-8 — over all fields
except `evidence_id`; literal reading, flagged in the Task 3A report), retaining
the lexicographically smallest UTF-8 `evidence_id`. Retained evidence is sorted
by ascending UTF-8 `evidence_id`, and that single order is used for every
Decimal sum, weighted sum, primary aggregation, and replay.
