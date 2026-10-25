# Wave-2 chain-audit findings L1/L2/L3 + T8-1 + wave-1 relearn-backup residual

Source audit: `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave2-adapters-spec.md`
(SUB-16, lifecycle e2e + Task 7/8 spec compliance at e1835b8).

## Evidence

- **L1 (spec 7.7)** — every mutation-path final audit had `correlation_id: null`,
  `projection_outcome: null` and a hardcoded `duration_ms: 0`:
  `store.py` `EventStore.commit` success/failure outcomes,
  `improvement.py` `_Audited.run`, `relearn.py` apply/replay/rollback. Only
  read-path audits (`service._finish_success`) carried a correlation id.
  Probed null on all seven lifecycle operations plus relearn apply.
- **L2 (spec 7.7 chain link)** — `apply_improvement`'s final audit had
  `linked_domain_event_id: null` although it appends a `NODE_UPSERT` domain
  event; sibling mutations link theirs via `store.commit`.
- **L3 (spec 7.9)** — `ProposalResponse`/`ImprovementResponse`/`RollbackResponse`
  carried no `domain_event_id`, no final-audit `event_seq`, no
  `node_version`/`knowledge_seq` (WriteReceipt parity fields).
- **T8-1** — `docs/integration/hermes.md` shipped `sampling: {enabled: false}`
  and `elicitation: {enabled: false}` lines in the reference server entry,
  while the Task 8 brief requires no sampling configuration in the Hermes
  instructions.
- **Residual (wave 1)** — `relearn._verified_backup` reimplemented the
  protected-set backup inline (raw `copyfile` of the SQLite file, copy-equality
  checks, no `CURRENT` pointer, no manifest-declared hash verification),
  duplicating — and falling short of — `backup.backup_protected_artifacts`.

## Root cause

The mutation write paths were built before the read-path `_finish_success`
pattern existed and were never revisited; each module hand-rolled its
`InvocationOutcome` and diverged. The relearn backup predates the Task 9
protected-set helper and was never migrated.

## Affected global criteria

- Spec 7.7 / acceptance bullet 2: every valid invocation's single final audit
  must carry outcome/result references, error, projection outcome, duration
  and correlation ID.
- Spec 7.9: every mutation response carries the original `domain_event_id`,
  the invocation `audit_event_id`, the final audit `event_seq`, the original
  `node_version`/`knowledge_seq` and projection metadata.
- Relearn contract "Backup and rollback": the protected set is SQLite,
  generation directory, manifest, `CURRENT`, source registry and the
  scoring-contract version.

## Smallest fix applied

- `audit.py`: new shared `success_outcome`/`failure_outcome` helpers (the
  `_finish_success` pattern: measured `duration_ms` via `time.monotonic`,
  `correlation_id=invocation.invocation_id`, projection outcome, linked domain
  event). `service.py` untouched (out of scope).
- `store.py::commit`: both outcomes now built via the helpers; success links
  the appended domain event and records `result_references={"operation": …}`,
  `projection_outcome="appended"`; failures record `projection_outcome="skipped"`.
- `improvement.py::_Audited.run`: same helpers; new `linked_domain_event_id`
  extractor so `apply_improvement` links its `NODE_UPSERT` and `merge_nodes`
  links its `NODE_RETIRE`. `ProposalRecord`/`ImprovementReceipt`/`MergeReceipt`
  gained the spec 7.9 fields (`domain_event_id`, `node_version`,
  `knowledge_seq`) populated from the domain receipts.
- `relearn.py`: apply/replay/rollback outcomes via the helpers (replay links
  the original relearn record event; rollback uses
  `projection_outcome="restored"`); `_apply_mutation` returns the record's
  domain event id for the chain link. `_verified_backup` now delegates to
  `backup_protected_artifacts` (online backup API for SQLite, `CURRENT`
  pointer, generation artifacts verified against the projection manifest);
  `BackupArtifact.kind` gained `"current_pointer"`; rollback restores the
  pointer and the generation into `generations/<pointer>` and hash-verifies
  both.
- `contracts.py`: `ProposalResponse`/`ImprovementResponse`/`RollbackResponse`
  expose `domain_event_id`, `audit_event_seq`, `node_version`, `knowledge_seq`
  (optional, `None` until the service layer wires them — `service.py` is
  outside this repair's scope; the improvement/relearn receipts now carry the
  values at the module boundary).
- `docs/integration/hermes.md`: the two sampling/elicitation config lines and
  their justification paragraph removed.

## Regression probes

- `tests/test_audit_wave2_chain.py` (new): full lifecycle walk
  (log_event → record_outcome → evaluate → review → propose → apply →
  rollback) asserting every mutation final audit has `correlation_id`,
  `projection_outcome`, measured `duration_ms` and a `linked_domain_event_id`
  that resolves to a real domain event; failure-path audit completeness
  (VersionConflict); apply audit links the exact appended event; relearn
  apply/rollback audit completeness; protected-set backup contents including
  `CURRENT`; rollback restoring pointer + generation bytes; spec 7.9 fields on
  receipts and response models.
- `tests/test_relearn.py`: fixture moved to the projector layout
  (`CURRENT` + `generations/<name>/` with manifest-declared hashes); backup
  assertions now bind recorded hashes to the backup bytes (the online backup
  API is not byte-identical to the live file — verified empirically — while
  remaining content-verified via quick_check/event-count/chain-head inside
  `backup_store`).
- `tests/test_acceptance_e2e.py::TestRollback`: part (b) updated to the same
  recorded-hash semantics and layout.

## Rollback note

Revert the five source files; audit payloads simply lose the new fields (old
readers ignore extra payload keys — the audit payload is a JSON document, not
a schema-validated row). The relearn backup layout change is not
backward-compatible with pre-fix backups on disk; those backups were created
only in tests/pre-release runs.
