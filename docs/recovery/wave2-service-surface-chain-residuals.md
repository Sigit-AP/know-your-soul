# Wave-2 chain-audit residuals: §7.9 service surface, read-path projection_outcome, production relearn wiring

Follow-up to `wave2-L1-L3-chain-audit.md` (c5792f9): the receipts gained the
spec 7.9 fields there, but three residuals remained at the service boundary
(reported by SUB-18 as out-of-scope leftovers).

## Evidence (RED)

`tests/test_audit_wave2_residual.py` (new), observed failing before the fix
(pytest exit=1, 3 failed):

- **R1 (spec 7.9 API surface)** — `ProposalResponse`/`ImprovementResponse`/
  `RollbackResponse` left the service with `domain_event_id=None`,
  `audit_event_seq=None`, `node_version=None`, `knowledge_seq=None` although
  the `ProposalRecord`/`ImprovementReceipt` right below carried every value.
  Probe: lifecycle walk through the service surface (log_event → propose →
  apply → rollback) asserting all four fields non-null (proposal
  `knowledge_seq` stays `None` by design — zero-knowledge-weight admin event),
  each `domain_event_id` resolving to a real domain event row and each
  `audit_event_seq` naming exactly the final audit of its operation.
- **R2 (spec 7.7 read path)** — the final audits of `knowledge.query` and
  `knowledge.get_node` (success and typed failure) had
  `projection_outcome: null`; `service._finish_success`/`_finish_failure`
  hand-rolled `InvocationOutcome` without it (service.py:265-286).
- **R3 (relearn production wiring)** — `KnowledgeService.__init__` built the
  production engine as `RelearnEngine(store)` with no `generation_dir`, so the
  verified backup covered only SQLite (`artifact kinds == {"sqlite"}`), never
  the `CURRENT` pointer or `generations/` — a relearn rollback in production
  could not have restored the projection.

## Smallest fix applied (service.py only)

- `propose_improvement`/`apply_improvement`/`rollback` (improvement branch)
  forward `domain_event_id`, `audit_event_seq`, `node_version`,
  `knowledge_seq` from the record/receipt into the response. The relearn
  rollback branch keeps them `None` (it restores bytes; no domain event —
  per the frozen contract note in contracts.py).
- `_finish_success` now delegates to `audit.success_outcome` (the shared
  c5792f9 helper) with `projection_outcome="unchanged"` — a read never moves
  the projection, so it is neither `"appended"` (mutation) nor `"skipped"`
  (failure). `_finish_failure` keeps its typed error code and gains
  `projection_outcome="skipped"`, matching the `failure_outcome` default.
- `KnowledgeService.__init__` wires `generation_dir=projector._output_root`
  into the production `RelearnEngine` (same-package private access, the
  `mcp_server.py:708` precedent; `GraphProjector` exposes no public accessor
  and projection.py is outside this repair's scope). The projection root is
  exactly the directory holding `CURRENT` and `generations/` that
  `relearn._verified_backup` expects.

## Regression probes

`tests/test_audit_wave2_residual.py`:

1. `test_service_lifecycle_responses_carry_spec_7_9_fields` — the §7.9 walk
   above, plus chain consistency (response `audit_event_id`/`audit_event_seq`
   identify the recorded final audit; `domain_event_id` ∈ domain events).
2. `test_read_path_final_audits_carry_projection_outcome` — query + get_node
   success and a typed `NodeNotFound` failure all record
   `projection_outcome`, `correlation_id` and measured `duration_ms`.
3. `test_production_relearn_backup_covers_generation` — the service-built
   engine (no test-only constructor args) applies a RESET relearn against a
   real published generation; the backup contains the `CURRENT` pointer
   (content-equal to the live pointer pre-apply) and every generation
   artifact, hash-verified against the manifest.

## Verification (GREEN)

- `pytest tests/test_audit_wave2_residual.py`: 3 passed, exit=0.
- Full gate at this tree: `pytest tests/ -q` 623 passed / 1 skipped, exit=0;
  `ruff check src tests` exit=0; `ruff format --check src tests` 61 files
  clean; `pyright` 0 errors. `tests/test_audit_wave1_residual_probes.py`
  (redaction surface, commit 4b4286c) stays green.

## Rollback note

Revert service.py; the response models keep the fields optional (they were
additive in c5792f9) and old audit payloads simply lack `projection_outcome`
on read rows — the audit payload is a JSON document, not a schema-validated
row.
