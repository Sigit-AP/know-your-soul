# Task 2 finding I5 — active() returned an unapproved source version

## Evidence

After `detect_change` on changed content, two rows were `CURRENT` for one
source and `active()` returned the newest — the unapproved version with an
outstanding `RELEARN_PENDING` offer. After the source content was reverted to
v1, `active()` still returned the stale v2 row. Spec §13.3: no operator
decision means no change.

## Root cause

`active()` selected the highest-rowid `CURRENT` row with no regard for pending
relearn offers, so it bound consumers to a version no operator approved.

## Affected global criterion

Spec §13.3 (operator-gated relearn; default scoring must bind to the approved
source manifest).

## Smallest fix applied (ruling I5)

`active()` excludes any `CURRENT` version targeted by a `relearn_offer` while
`relearn_state = 'RELEARN_PENDING'` and returns the newest remaining `CURRENT`
row (the operator-approved baseline), or `None` when every `CURRENT` version
carries a pending offer. `detect_change` now compares against `_latest()` (the
newest observed `CURRENT` fingerprint, pending offers included), so
re-detecting unchanged content stays a no-op while `active()` never returns the
unapproved version. Promotion/demotion remains Task 6: clearing
`relearn_state` after the operator decision makes the decided version eligible
again.

## Regression probes

- `tests/test_task2_fixes.py::test_active_excludes_a_version_with_a_pending_relearn_offer`
- `tests/test_task2_fixes.py::test_active_never_returns_an_unapproved_version_after_revert`
  (revert offers v1 again; with both CURRENT versions pending, `active()` is
  `None` and never the stale v2)

## Rollback note

Revert `active()` to the highest-rowid `CURRENT` query; the offers and state
rows are unchanged, so no data migration is needed.
