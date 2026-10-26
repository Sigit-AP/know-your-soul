# SUB-52 — Final residual closures (SUB48-1, F-SUB51-1, F-SUB51-2)

Date: 2026-10-26 ~12:35 UTC. Base: HEAD `81e1823`. All fixes RED-first; every
number below is from a fresh command run on this host.

## SUB48-1 (minor → closed): `publish_capacity_bytes` override above the cap disarmed F-3

**Finding.** `KnowledgeService(publish_capacity_bytes=...)` accepted any
ceiling, including values above the projection's per-artifact cap
(`ARTIFACT_MAX_BYTES` = 65,536). Live probe: an override of `2**20` let the
store grow until every publish/repair failed with `OversizedArtifactError`
(observed artifact 238,600 B over a 65,536 B cap) — the F-3 write-side guard
was permanently behind an unreachable ceiling.

**Fix (fail-closed, typed).** Construction now rejects any override
`> ARTIFACT_MAX_BYTES` with `InvalidPublishCapacity`, a typed
`PublishCapacityExceeded` (StoreError) subclass defined in
`src/soul_knowledge/service.py`, so the HTTP/MCP envelopes route it through
the existing StoreError path. Overrides at or below the cap are unchanged; the
default stays `int(ARTIFACT_MAX_BYTES * PUBLISH_CAPACITY_RATIO)` (95%).

**Probes (RED → GREEN).**
`tests/test_audit_final_flow.py::test_publish_capacity_override_above_artifact_cap_fails_closed`
— `ARTIFACT_MAX_BYTES + 1` and the live-probe value `2**20` both raise
`InvalidPublishCapacity` (message carries the cap); the boundary value
`ARTIFACT_MAX_BYTES` stays constructible; the error stays inside the
`PublishCapacityExceeded` family. The pre-existing F-7 storm test that rode
the unsafe `2**20` override now uses the boundary value
(`ARTIFACT_MAX_BYTES`) — scenario unchanged, 24/24 green.

## F-SUB51-1 (important → closed): `inspect_backup` leaked untyped `sqlite3.DatabaseError`

**Finding.** `backup.inspect_backup` on a file that exists but is not a SQLite
database raised an untyped `sqlite3.DatabaseError` from the very first
`PRAGMA quick_check` (the inner guard only wrapped `_anchor`), so
`restore_store` lost its documented `BackupError` contract on garbage input.

**Fix.** The first pragma is wrapped: `sqlite3.DatabaseError` →
`BackupError("backup ... is not a SQLite database: ...")` (`raise ... from`).
Behavior for existing-but-foreign databases is unchanged (valid SQLite,
missing tables → `ok=False` report, pinned by
`test_inspect_backup_reports_a_foreign_database_as_not_ok`).

**Probes (RED → GREEN).** `tests/test_operations.py`:
`test_inspect_backup_rejects_a_non_sqlite_file` (typed error + message) and
`test_restore_store_rejects_a_non_sqlite_backup` (restore keeps the
`BackupError` contract; RED showed the raw `sqlite3.DatabaseError: file is
not a database` escaping).

## F-SUB51-2 (cosmetic → closed): dead `PayloadTooLarge` branch in `error_envelope`

**Finding.** `error_envelope` carried `elif isinstance(error,
PayloadTooLarge)` after the `KnowledgeError` branch — unreachable, because
`PayloadTooLarge` *is* a `KnowledgeError` whose `code` is the identical
`"PAYLOAD_TOO_LARGE"`.

**Fix.** Dead branch deleted. Behavior is provably unchanged and pinned harder
in `tests/test_wave3_coverage_adapters.py::test_error_envelope_maps_payload_too_large`
(now also asserts `retryable is False` and `audit_event_id is None`); the live
413 path (`Content-Length` over the cap) is covered by the adapter suite.

## Docs hygiene (SUB-48 observations)

- `deploy/Dockerfile:29`: plan-era prose "delivered by Task 7 (cli.py)" →
  "ships in the package (cli.py)".
- `README.md`: the three-access-paths section now names the operator-gated
  relearn lifecycle on both surfaces — CLI (`relearn-scan` / `relearn-status`
  / `relearn-preview` / `relearn-apply`) and HTTP (`ops/relearn_scan` /
  `ops/relearn_status` / `ops/relearn_preview` / `ops/relearn_apply`) — next
  to the frozen fourteen knowledge operations.

## Fresh gate output (2026-10-26, this host)

| Gate | Command | Exit | Result |
|------|---------|------|--------|
| Full suite | `.venv/bin/pytest tests/ -q` | 0 | **1369 passed, 1 warning, 0 skipped** (244.02s) |
| Acceptance e2e | `.venv/bin/pytest tests/test_acceptance_e2e.py -q` | 0 | 49 passed, 0 skipped (48.72s) |
| Code coverage | `.venv/bin/pytest tests/ -q --cov=soul_knowledge --cov-report=term` | 0 | **99.47%** (7,227 stmts, 38 miss) |
| Type check | `.venv/bin/pyright` | 0 | 0 errors, 0 warnings, 0 informations |
| Lint | `.venv/bin/ruff check .` | 0 | All checks passed! |
| Format | `.venv/bin/ruff format --check .` | 1 (pre-existing) | `1 file would be reformatted, 190 files already formatted`; sole offender `docs/mathematics/scoring.md` (frozen-contract drift from `724843c`, documented not reformatted) |
| Dependency audit | `/root/.local/bin/uv lock --check` | 0 | Resolved 101 packages in 3ms |
| Bench | `.venv/bin/python bench/run.py check` / `run` | 0 | `check: OK`; **24/24 accepted**, ledger `bench/results/ledger-20261026T122910Z.json`, zero drift vs `20261026T031852Z` (savings mean 94.18%, min 92.64%; system 420–968 est. tokens; quality mean 0.4451; ceiling mean 0.4042; gap mean 0.5549) |
