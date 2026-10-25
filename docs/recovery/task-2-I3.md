# Task 2 finding I3 — hash chain had no anchor (tail truncation invisible)

## Evidence

On a consistent clone with the append-only DELETE trigger dropped, deleting the
last event (an audit event whose journal still claimed `audit_event_seq=4`)
left `verify_chain()` reporting `ok=True`. `system_state` held no chain state,
and `recorded_at`/`event_seq` were not hash inputs, so rewriting `recorded_at`
also verified clean.

## Root cause

The chain linked events to each other but to nothing outside `event_log`: no
persisted expected head hash or event count, no cross-check of
`invocation_journal.audit_event_seq`, and a hash input that omitted
`event_seq`/`recorded_at`.

## Affected global criterion

Tamper-evidence of the immutable history (spec §7; Task 2 report §4 "Hash-chain
integrity").

## Smallest fix applied (ruling I3)

- `insert_event` upserts `system_state.chain_head_hash` and
  `system_state.chain_event_count` in the same transaction as every event
  append.
- `event_seq` (precomputed as `MAX(event_seq)+1` inside the serialized write
  transaction) and `recorded_at` (generated before hashing) are now hash
  inputs; `event_seq` is inserted explicitly.
- `verify_chain()` additionally cross-checks the persisted head hash and event
  count, and every completed journal's `audit_event_seq` against `event_log`
  (the journal's `audit_event_id` must equal the log row's `event_id`).

Pre-release database: no backward-compatibility constraint on existing hashes
(ruling). A log with events but no anchor fails closed.

## Regression probes

- `tests/test_task2_fixes.py::test_verify_chain_detects_tail_truncation`
- `tests/test_task2_fixes.py::test_verify_chain_cross_checks_journal_audit_event_seq`
  (anchor rewritten to match the truncated log; the journal cross-check still
  fails)
- `tests/test_task2_fixes.py::test_verify_chain_detects_recorded_at_rewrite`

## Rollback note

Revert `verify_chain` and `_hash_input` to the old field set and stop writing
the anchor rows; existing databases would need their event hashes recomputed
(the pre-release schema has no migration path for hash-input changes).
