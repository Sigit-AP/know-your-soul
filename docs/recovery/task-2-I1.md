# Task 2 finding I1 — recover_incomplete() had no liveness guard

## Evidence

`recover_incomplete` selected every `status='in_progress'` journal regardless of
age and closed each one. A recovery pass in one process could finalise an
invocation still executing in another (the interleaving proven in C1), auditing
a live mutation as `INTERRUPTED_BEFORE_COMMIT`.

## Root cause

No staleness threshold, lease or heartbeat existed on `invocation_journal`;
recovery treated "in progress" as "crashed".

## Affected global criterion

Acceptance criterion 2 / spec §7.8 (reconciliation of *crashed* invocations).

## Smallest fix applied (ruling I1)

Migration `0002_journal_heartbeat_and_chain_anchor.sql` adds
`invocation_journal.heartbeat_at`. `insert_journal` seeds it from
`started_at`; `EventStore.commit` refreshes it around the domain write via
`refresh_journal_heartbeat`; `recover_incomplete(stale_after_seconds=...)`
(default 300 s) only closes journals whose last heartbeat (falling back to
`started_at`) is older than the threshold. Fresh journals are left untouched.
The liveness contract — "run recovery before accepting traffic" — is documented
in `audit.py`'s module docstring.

## Regression probes

- `tests/test_task2_fixes.py::test_recover_incomplete_ignores_fresh_journals`
- `tests/test_task2_fixes.py::test_recover_incomplete_closes_stale_journals`
- `tests/test_task2_fixes.py::test_recover_incomplete_honours_the_staleness_threshold`
- `tests/test_task2_fixes.py::test_commit_refreshes_the_journal_heartbeat`

## Rollback note

Drop the column in a new migration and revert `recover_incomplete` to select all
in-progress journals; heartbeat data is advisory and lossless to discard.
