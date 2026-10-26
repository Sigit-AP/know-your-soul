# Recovery: SUB-41 — final flow audit core fixes (F-1..F-4, C1, minors F-5/F-6/F-7/F-9)

- **Date**: 2026-10-26 — **Owner**: SUB-41 — **Basis**: `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-final-flow.md`
- **RED evidence** (pre-fix, exit 0, non-vacuous): `/root/.hermes/cache/scratch/sub41/red_probe.py` — F-1 chimera reproduced (fresh connection sees `n-2` after "restore"), F-2 read model frozen (`NodeNotFound` for post-lie writes), F-3 no write-side guard, F-4 baseline flip with `relearn_state=None`, F-6 rollback no-op flipped status v2→v3, F-9 readiness red until first read. Plus pytest collection errors for the new typed symbols (exit 2).
- **GREEN evidence**: `green_probe.py` exit 0 (all six scenarios), `http_probe.py` exit 0 (live uvicorn: restore-over-live → 200 → coherent reads/writes after), pytest suites below.

## F-1 — restore/backup vs the live store (chimera)

Root cause: `restore_store` byte-replaced the main DB file under the live handle and left the old `-wal`/`-shm` in place, so fresh connections replayed the old WAL over the restored file (the restore regressed *nothing*) while the service handle went incoherent with untyped `sqlite3.OperationalError`.

Fix (backup.py + store.py):

- `EventStore` keeps a weak same-process registry keyed by `realpath(db_path)`; `EventStore.live_store_at(path)` exposes it.
- `EventStore.reopen_in_place()` — the W3-1 transplant, now owned by the store itself (relearn.py's private helper deleted; rollback uses the method).
- `restore_store` onto a *same-process live* store: coordinated swap under the store lock — close → purge `-wal`/`-shm` → verified copy → `os.replace` → fsync → `reopen_in_place()`; on failure the handle is reopened before the error propagates. Requires `overwrite=True` (typed `BackupError` otherwise).
- `restore_store`/`backup_store` onto a path held by a *foreign* process: detected via an exclusive probe lock on the shm lock region (bytes 120+, empirically where SQLite holds WAL/DMS locks) — typed `BackupError` telling the operator to stop that process. Crash-residue sidecars (no locks) are purged by the restore.
- `_anchor_readonly` opens with `immutable=1` so verification no longer recreates empty sidecars at a restore destination.
- Recovery note: after a live restore the manifest is *ahead* of the store — the F-2 heal (below) repairs the projection on the next read.

## F-2 — `upto_knowledge_seq` in the future (lying watermark)

Root cause: `projection.rebuild(upto_knowledge_seq=N)` with N beyond the store's real watermark writes a manifest that claims coverage it does not have; `_try_snapshot` accepted it (`manifest >= required`), and the activation CAS marker `active_projection_watermark` was poisoned too, so no honest rebuild could ever activate — permanent freeze, surviving restarts.

Fix (in scope: store/service/backup/observability; projection.py belongs to SUB-43):

- `EventStore.heal_lying_projection_watermark()` — pulls a marker that exceeds the real `MAX(knowledge_seq)` back to the real watermark (impossible for an honest build; fail-closed safe). Returns whether a lie was healed.
- `service._try_snapshot` treats `manifest_watermark > store_watermark` as stale → the read path repairs instead of freezing.
- `service._repair_projection_once()` / `backup.recover_projection` / `check_readiness`: the ahead-of-store case heals the marker and rebuilds with `allow_shrink=True` scoped to exactly this condition (a restored/regressed store legitimately shrinks the graph).
- Probes: `test_lying_manifest_watermark_is_healed_on_read` (read heals, post-lie node served), `test_recover_projection_repairs_a_lying_manifest` (repair path from the corrupted state), `test_service_reads_are_consistent_after_live_restore` (F-1 composition).
- **Residual for SUB-43** (projection.py, out of SUB-41 scope): prevention — reject `upto_knowledge_seq > store.knowledge_high_watermark()` typed at `rebuild()` entry (projection.py:1009). The heal above makes the lie self-repairing; the typed rejection should still land at the source.

## F-3 — publish cap vs unbounded `log_event`

Policy decision: keep the 64 KiB per-artifact publish cap (projection, frozen token-budget contract untouched — different domain, per ruling) and make the *write side* fail closed before the store crosses it:

- `EventStore.estimate_publish_bytes()` — conservative upper estimate of the next `graph.json` from store tables alone (latest live revision payload lengths + per-node/per-edge overhead). Calibrated against the frozen corpus: estimate 53,635 B vs real graph.json 33,186 B (1.62×, never under), and the frozen corpus stays inside the guard (`test_publish_estimate_bounds_the_real_graph` pins both directions).
- `KnowledgeService(publish_capacity_bytes=…)` defaults to 95% of `ARTIFACT_MAX_BYTES`; `log_event` refuses over-capacity writes with typed `PublishCapacityExceeded(StoreError)` naming estimate, guard and cap, *before* commit, with exactly one audited failure event. Writes that fit are unaffected.
- Readiness now always reports `details["publish_capacity"]` (estimated/guard/cap) and the `publish_capacity` check goes red at the guard threshold — monitoring sees the ceiling approaching before writes are refused.
- **Recovery runbook** when the guard refuses writes: (1) `check_readiness` capacity details confirm; (2) retire nodes that no longer serve (watcher/ops flows or a RESET relearn against a pruned corpus); or (3) `restore_store` a pre-growth backup onto the live store (F-1 path, now safe) — the guard prevents re-crossing. The pre-fix failure mode (permanent read outage) can no longer be reached through `log_event`.

## F-4 — watcher/update pipeline bypassed the relearn gate

- `update_from_changes` now only *detects*: every batch routes through `SourceRegistry.detect_change` → deterministic `RelearnOffer` + `RELEARN_PENDING`; baseline, store knowledge and projection stay untouched ("no operator decision means no change", spec 13.3). Replay is a no-op; first observation records the baseline (`baseline_recorded=True`, initial import stays operator-explicit).
- `apply_approved_update(...)` — the post-decision executor: refuses typed (`UpdatePipelineError(StoreError)`) while `RELEARN_PENDING`; after the operator's decision it retires the offer-diff removals (idempotency-keyed), re-imports and rebuilds with pipeline-scoped `allow_shrink` (busy-lock retry kept).
- Service API for adapters (SUB-42 binds these — verified compatible with their in-flight wiring): `relearn_status()`, `offer_relearn(path, kind, parser_versions=None)`, `preview_relearn(mode)`, `apply_relearn(mode, approval, idempotency_key=None)` — all audited (`knowledge.relearn_status/offer/preview/apply`), typed failures. `RelearnEngine.pending_offer()`/`status()` + `RelearnStatus` model + `SourceRegistry.latest_offer()`/`latest_observed()` support them.

## C1 — two-process "duplicate" writes

Investigation: exactly-once already holds. The store dedupes on `idempotency_key` inside `BEGIN IMMEDIATE` (serialized cross-process), backed by the partial UNIQUE index `event_log_idempotency` and the `event_id` UNIQUE constraint; the final audit's own recount by `kind='domain'` found the same. The "id recorded 11×" signal was a probe artifact (counting audit events/journals and distinct writes to one node as duplicates). Pinned by `tests/test_concurrency_twoprocess.py`: two real OS processes, one store — one domain event per key (unique and shared), conflicting key reuse fails typed `IdempotencyConflict`, chain verifies. No code change required beyond the regression probe.

## Minors

- **F-5**: `BackupError` now subclasses `StoreError` → HTTP envelope code `BackupError` (400) with the actionable message, no more `INTERNAL`/"internal error" for the backup family. The `ProjectionError` family (`ProjectorBusy`→500) is projection.py — SUB-43's file; flagged on the board.
- **F-6**: `service.rollback` refuses typed (`LifecycleError`) when the resolved target revision's content fingerprint equals the current node's (covers both rollback-to-current and repeated target-less rollback); version/status no longer flip on a no-op.
- **F-7**: reader-triggered repair cooldown (`repair_cooldown_seconds`, default 1.0s) engages only after a repair attempt that still did not cover the read — a covering repair clears it, so the normal write→repair flow is unchanged; under a write stream the storm test asserts rebuilds track writes, not reads.
- **F-9**: `check_readiness` proactively repairs a lagging projection during the check (including the F-2 ahead case) instead of reporting red until the first read.
- F-8 (POST ops/health always 200) is http_api.py — SUB-42's scope (already fixed in their in-flight tree per route comment).

## Files

- `src/soul_knowledge/store.py` — live registry, `reopen_in_place`, `heal_lying_projection_watermark`, `estimate_publish_bytes` + estimator constants + `PUBLISH_CAPACITY_RATIO`, `PublishCapacityExceeded`.
- `src/soul_knowledge/backup.py` — `BackupError(StoreError)`, live-swap restore, WAL-lock liveness probe, sidecar purge, `immutable` verification, F-2 heal in `recover_projection`.
- `src/soul_knowledge/service.py` — F-2 snapshot strictness + `_repair_projection_once`, F-3 guard in `log_event`, F-7 cooldown, F-6 rollback no-op guard, relearn API (4 methods).
- `src/soul_knowledge/relearn.py` — `RelearnStatus`, `pending_offer()`, `status()`; rollback uses `store.reopen_in_place()`.
- `src/soul_knowledge/source_registry.py` — `latest_offer()`, `latest_observed()`.
- `src/soul_knowledge/update_pipeline.py` — offer-gated `update_from_changes` + `apply_approved_update` + `UpdatePipelineError`.
- `src/soul_knowledge/observability.py` — proactive readiness repair + publish-capacity reporting.
- Tests: `tests/test_audit_final_flow.py` (new), `tests/test_service_relearn_api.py` (new), `tests/test_concurrency_twoprocess.py` (new), `tests/test_update_pipeline.py` (rewritten to the gated semantics), `tests/test_operations.py` (readiness proactive semantics).

## Gates

- `pytest tests/ -q` — 950 passed, 0 failed (includes SUB-39/40 WIP property tests and SUB-42 adapter tests).
- `ruff check src tests` / `ruff format --check src tests` — clean.
- `pyright` on all SUB-41 files — 0 errors (repo-wide 2 errors are SUB-42's in-flight `tests/test_final_adapter_fixes.py`, not touched here).

## Rollback

Revert the listed source files; the pre-fix behaviors return (chimera restore, frozen read model on watermark lies, unbounded writes, silent baseline flip, no-op rollbacks, lazy health).
