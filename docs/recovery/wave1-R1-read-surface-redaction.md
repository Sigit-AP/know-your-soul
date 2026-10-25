# Residual wave-1 R1 — read surfaces (get_node/get_subtree/query) emitted unredacted content text

## Evidence

`KnowledgeService._result_node`, `KnowledgeService._node_view` and the
`_query` provenance builder passed content-derived text (summary/label,
provenance entries, context tags) through `sanitize_text` only. The F4 publish
gate fails closed on *secret* shapes, but the gate scans
`SECRET_PATTERNS` only — PII (email addresses) and any future pattern gap
flowed to an activated generation and out of every read surface verbatim.
Probe: a node with summary `Rollback drill; contact admin@example.com …`
publishes cleanly and `get_node("n-leaky").node.summary` returned the raw
email.

## Root cause

Redaction (`security.redact_text`) existed and was wired into the HTTP adapter
error paths, but never into the Task 4 service read helpers — the audit's
defense-in-depth layer was missing at the exact boundary where content leaves
the process.

## Affected global criterion

"Every read surface that leaves the process is redacted" (security.py's own
`redact_value` contract) — the store may hold raw payloads only because reads
redact; the service reads did not.

## Smallest fix applied (ruling R1)

One module-private helper in `service.py`,
`_redacted_content(text, max_length=…)` = `redact_text(sanitize_text(text))`,
now wraps every content-derived string in `_result_node` (query results +
`get_subtree` nodes), `_node_view` (`get_node`: summary, provenance, context
tags), `_result_node_from_node` (review responses) and the `_query` provenance
aggregation. No new pattern list — the single source of truth stays
`security.SECRET_PATTERNS` + PII patterns. `redact_text` is byte-identical on
clean input, so the frozen token-budget contract (fixed-point
`estimated_tokens`) is untouched for clean corpora; only the string payload
changes, never the response structure (`QueryResponse`/`NodeResponse`/
`SubtreeResponse` fields unchanged).

## Regression probes

- `tests/test_audit_wave1_residual_probes.py::test_r1_get_node_redacts_secret_and_pii_shapes`
- `…::test_r1_get_subtree_redacts_secret_and_pii_shapes`
- `…::test_r1_query_redacts_results_and_provenance`
- `…::test_r1_result_and_view_helpers_redact_true_secret_shapes` (unit: AWS
  key + JWT + bearer in label/provenance/tags → none survive; clean fields
  byte-identical)
- `…::test_r1_clean_text_passes_through_unchanged` (parity: clean
  summary/provenance/tags byte-identical through all three read surfaces)

RED evidence: all four R1 probes fail with assertion errors against pristine
HEAD (94a3321) in an isolated worktree; the parity probe passes both before
and after the fix.

## Rollback note

Remove `_redacted_content` and restore the four `sanitize_text` call sites;
no stored data or artifact changes (redaction is computed at read time), so
rollback is lossless.
