# Task 2 finding I2 — recovery could not reconcile keyless invocations

## Evidence

`commit` falls back to `invocation_id` as the domain idempotency key, but the
journal stored the raw `NULL` idempotency_key and `recover_incomplete` only
looked up `domain_event_by_idempotency(row['idempotency_key'])`. Repro:
`begin(inv, idempotency_key=None)` → `append_domain(...,
idempotency_key=inv.invocation_id)` → `recover_incomplete()` reported
`failure/INTERRUPTED_BEFORE_COMMIT` although the knowledge event was committed.

## Root cause

The key used for the domain write (the *effective* key) was never persisted in
the journal, so recovery reconciled against the wrong key.

## Affected global criterion

Acceptance criterion 2 (the single final audit must record the correct
outcome); spec §7.8.

## Smallest fix applied (ruling I2)

Migration `0002_journal_heartbeat_and_chain_anchor.sql` adds
`invocation_journal.effective_idempotency_key`, persisted at `begin` as
`invocation.idempotency_key or invocation.invocation_id`. Recovery reconciles
with `effective_idempotency_key`, falling back to `idempotency_key` then the
invocation id for rows written before the migration.

## Regression probes

- `tests/test_task2_fixes.py::test_recovery_reconciles_an_invocation_without_an_idempotency_key`
- `tests/test_task2_fixes.py::test_journal_persists_the_effective_idempotency_key`

## Rollback note

Revert recovery to consult `idempotency_key` only; the extra column is inert
data. Keyless invocations recovered while the fix was deployed keep their
(correct) success audit — replay protection via the domain idempotency index is
unaffected.
