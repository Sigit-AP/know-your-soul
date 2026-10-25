# Task 2 finding C1 — commit() ignored the final audit outcome

## Evidence

`EventStore.commit` never inspected the `AuditReceipt` returned by
`AuditLog.complete`. Repro: (1) `begin(inv)` → journal in_progress; (2) a second
process runs `recover_incomplete()` → journal closed as
`failure/INTERRUPTED_BEFORE_COMMIT`; (3) the domain write commits
(`knowledge_seq=1`); (4) the late `complete(inv_id, success)` returns the stored
receipt `status=failure`, but `commit` built a success `WriteReceipt`
unconditionally. The knowledge event is durable while the invocation's only
audit payload records failure.

## Root cause

`audit.py:70-81` returns the stored receipt when the journal is already
complete; `store.py:641-655` ignored `receipt.status`. The race window spans the
whole domain write (between `audit.begin` and `audit.complete`).

## Affected global criterion

Acceptance criterion 2 (exactly one final audit event with the correct outcome);
spec §4.4/§7.7/§7.8.

## Smallest fix applied (ruling C1)

`commit` asserts the final `AuditReceipt.status` matches the requested outcome
on both paths. On mismatch it raises `AuditOutcomeMismatch(StoreError)` carrying
both outcomes and chains the original error (`raise ... from ...`): the domain
error on the failure path, a `StoreError` wrapping the recorded outcome on the
success path. A failure-audit write that itself raises is also wrapped and
chained from the domain error. `commit` gained an optional `audit_log`
parameter (additive) so the race interleaving is exercisable in tests.

## Regression probes

- `tests/test_task2_fixes.py::test_commit_raises_when_the_final_audit_records_a_different_outcome`
- `tests/test_task2_fixes.py::test_complete_returns_the_stored_receipt_after_recovery_closed_the_journal`
- `tests/test_task2_fixes.py::test_commit_chains_the_domain_error_when_the_failure_audit_mismatches`

## Rollback note

Revert the `commit` body to ignore `receipt.status` and drop
`AuditOutcomeMismatch`; the domain/audit writes themselves are unchanged, so
stored data stays valid either way.
