# Recommendation Contract

Contract version: 1.0.0
Status: FROZEN (2026-10-24)
Binding spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`, section 13.2, quoted verbatim below.
Fixtures: `tests/fixtures/access_recommendation_relearn_cases.json` (`recommendation_cases`). Verified by: `tests/test_measurement_contracts.py`.

Any change to the response shape or item fields requires a new contract version
and operator approval.

## Response shape

When knowledge is needed, `knowledge.query` and `knowledge.review` return a
recommendation block with exactly three lists: `best_actions`, `recommendations`, and `unnecessary_actions`.
No other list keys are permitted.

## Item fields

Every item carries source node IDs and versions, evidence IDs, learned class, reason codes, and expiry.
Fixture item fields: `source_node_ids`,
`source_node_versions` (same length as `source_node_ids`), `evidence_ids`,
`learned_class`, `reason_codes`, `expiry`.

## Content boundary

The response never returns raw SOUL or corpus text. Recommendations carry
structured references (node IDs, versions, evidence IDs), never verbatim corpus
content.

## Promotion rule

One observation can never promote an action or a node. An action backed by a
single evidence item may appear in `recommendations` but must never appear
promoted in `best_actions`; a promotion attempt from one observation is rejected
with reason `ONE_OBSERVATION_NO_PROMOTION`.
