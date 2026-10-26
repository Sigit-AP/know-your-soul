# Final-audit adapter fixes: S-3, S-4, S-5, S-6, F-5/F-8 envelopes, F-4 relearn surface

- **Date**: 2026-10-26 — **Owner**: SUB-42 (adapters only)
- **Findings**: `audit-final-security.md` S-3/S-4/S-5/S-6; `audit-final-flow.md` F-5, F-8, plus the adapter halves of F-1/F-2/F-4.
- **Scope**: `src/soul_knowledge/http_api.py`, `mcp_server.py`, `cli.py`, `tests/test_final_adapter_fixes.py` (new), `docs/integration/hermes.md` (§5/§6). Core files (`service.py`, `store.py`, `relearn.py`, `backup.py`, `projection.py`, `security.py`) untouched — owned by SUB-41/SUB-43.

## S-3 — unix socket restart (Important)

`_bind_unix_socket` never unlinked the socket file, so every restart died with
raw `EADDRINUSE`. Fix, in `http_api.py`:

- `_unlink_stale_socket(path)` runs before bind: `lstat` must say **socket**
  (a regular file or symlink fails closed with `BindPolicyError`, never
  unlinked), then a `connect` probe proves it *stale* (`ECONNREFUSED`/`ENOENT`
  → unlink). A live listener answering the probe → fail closed. Any other
  probe error (e.g. `EACCES`) → fail closed.
- `serve()`'s `finally` closes the listener and removes the socket file via
  the same verified helper (cleanup never raises: a swapped/live path is left
  in place with a warning). Note (probed, uvicorn 0.54): on SIGTERM/SIGINT
  uvicorn runs its graceful shutdown ("Finished server process") but then
  terminates the process with the signal disposition (rc=-15), so `run()`
  never returns and the `finally` does not execute on signal-driven stops —
  the **bind-side stale unlink is the load-bearing restart guarantee**; the
  `finally` covers in-process graceful stops. Probe
  `/root/.hermes/cache/scratch/sub42/probe_serve_restart.py`: three
  SIGTERM-killed restarts of the real `soul-knowledge serve` CLI each served
  200 over the same socket path.

Probe: `tests/test_final_adapter_fixes.py::test_bind_unlinks_stale_socket_and_restarts_repeatedly`
binds/closes 3× on one path; `test_bind_refuses_a_live_listening_socket` and
`test_bind_refuses_a_non_socket_file` pin the fail-closed half;
`test_serve_removes_socket_file_on_shutdown` pins the shutdown unlink.

## S-4 — bind→chmod window

The old code did `bind()` then `os.chmod(path, 0o600)`: a 0o777 window under a
loose umask, and a path-based chmod that follows a swapped symlink onto a
victim file. Measured fact (probe, this host, Linux 5.15): **`os.fchmod` on a
unix-socket fd is a silent no-op** — it returns success and the on-disk mode
does not change — so the audit's fchmod suggestion is not implementable. The
fix uses the audit's other option: a temporary `os.umask(0o177)` around
`bind`, so the socket inode is **born** 0o600. There is no window and no
chmod at all; the "race-free" docstring claim is now literally true. The
umask is restored in a `finally` (the bind runs once at startup, before the
server accepts connections).

Probe: `test_bind_creates_socket_0600_with_world_open_umask` (umask 0o000) and
`test_bind_never_chmods_through_a_swapped_symlink` (monkeypatched `os.chmod`
performs the swap; the fixed code never calls it, so the victim keeps 0o644).

## S-5 — boundary-denial log flooding

300 wrong-key requests used to append 300 hash-chained audit events
(~683 B each, unbounded). `BoundaryDenialRecorder` (new, exported from
`http_api`) now keys each denial by `(reason, remote_addr, message)`:

- the **first occurrence of each distinct denial** is written immediately —
  a missing-key and a wrong-key rejection from the same peer are both
  audited (keeps the wave-2 F9(a) per-denial contract for distinct events);
- **identical repeats** inside `window_seconds` (default 60 s) are counted in
  memory and folded into the next event after the window as `denial_count`
  (payload also carries `window_seconds`). Every denial is accounted by
  exactly one event's count — the trail is complete, but a homogeneous storm
  costs one event per window;
- the key map is capped at 4096 entries; beyond the cap an unseen key is
  written immediately (fidelity over aggregation), so the map is bounded too.

Probe: `test_denial_recorder_aggregates_within_a_window` (5-denial burst → 1
event; post-window flush carries `denial_count=5`; totals preserved),
`test_denial_recorder_tracks_hosts_independently`,
`test_http_unauthorized_storm_writes_a_bounded_audit_trail` (12 × 401 → 1
event, chain verifies). The wave-2 F9(a) test (3 distinct denials → 3 events)
still passes unmodified.

## S-6 — MCP http transport denial audit

`mcp_server.main --transport http` now constructs a `BoundaryDenialRecorder`
over the service's store and passes it to `BoundaryMiddleware` — identical
wiring to the HTTP API. Probe:
`test_mcp_http_transport_records_boundary_denials` (wrong-key 401 → exactly
one `security.boundary_denial` event with reason/remote_addr).

## F-5 / F-1 / F-2 (adapter half) — typed operational envelopes

`BackupError` and the `ProjectionError` family used to fall through to
`INTERNAL` (400/500 with the wire message replaced by "internal error" — the
restore-exists `overwrite=True` guidance was sanitized away, and a rebuild
lock-loser looked like a crash). The adapter now maps them explicitly; the
wire code follows the established StoreError pattern (**the type name**) and
is identical whether or not the core declares these as `StoreError`
(SUB-41's in-flight core fix makes `BackupError` one; the adapter is robust
to either shape):

| type | code | status | retryable |
|---|---|---|---|
| `BackupError` | `BackupError` | 400 | no |
| `ProjectorBusy` | `ProjectorBusy` | 503 | **yes** |
| `StalePublicationError` | `StalePublicationError` | 409 | **yes** |
| `ShrinkGuardError` | `ShrinkGuardError` | 422 | no |
| `ProjectionValidationError` | `ProjectionValidationError` | 422 | no |
| `OversizedArtifactError` | `OversizedArtifactError` | 507 | no |
| `NoPendingRelearn` / `OperatorDecisionRequired` | type name | 409 | no |
| `UnknownRelearn` | type name | 404 | no |

The (redacted, length-capped) message is preserved for these typed codes.
MCP parity: `mcp_server.call_tool` maps `ProjectionError` to its type-name
code instead of `INTERNAL_ERROR`.

Probes: `test_error_mapping_for_backup_and_projection_family`,
`test_restore_onto_existing_without_overwrite_is_typed` (guidance survives),
`test_restore_onto_the_live_store_path_is_typed` (F-1 adapter half — the
core samefile/live-handle guard is SUB-41's),
`test_concurrent_rebuild_loser_gets_projector_busy_503` (real flock held →
503 retryable).

## F-8 — honest `ops/health`

`POST /v1/ops/health` now returns 503 when the readiness report is not ok,
matching `GET /v1/health`; healthy stays 200. Probes:
`test_ops_health_post_maps_unhealthy_to_503` (fresh stack without a
projection), `test_ops_health_post_stays_200_when_healthy`.

## F-4 (adapter half) — operator-reachable relearn lifecycle

New ops operations (CLI subcommands + `POST /v1/ops/...`), riding the
service's **public relearn gate** (`KnowledgeService.relearn_status` /
`offer_relearn` / `preview_relearn` / `apply_relearn` — each an audited
invocation; SUB-41 shipped these mid-flight, so the adapter never touches
`service._relearn_engine`):

- `relearn_scan {"path", "kind"}` → `service.offer_relearn(...)`; returns
  `{offer, relearn_state}` (`offer: null` on first baseline scan or no
  change). The path is a *source to hash* (read-only), deliberately not
  confined to `ops_root` — unlike backup/restore it writes nothing at the
  path. Note: SUB-41's watcher-gate work (F-4 core) may make scanning
  pipeline-driven; this op stays as the operator's manual trigger.
- `relearn_status` → the service's `RelearnStatus` (`state`,
  `pending_offer` — carrying the `offer_id` an approval must name —
  `active_source_version_id`, `quarantined_evidence_ids`).
- `relearn_preview {"mode"}` → the service's read-only `RelearnPreview`
  (without a pending offer: typed `LIFECYCLE_ERROR` naming
  `NoPendingRelearn`, HTTP 400).
- `relearn_apply {"mode", "approval": {offer_id, approved_by, decided_at?}}`
  → `service.apply_relearn(...)`; mismatched/absent approval → typed
  `OPERATOR_DECISION_REQUIRED` (HTTP 409); receipt carries the verified
  backup and `relearn_id`; rollback rides the existing
  `knowledge.rollback {"relearn_id"}`.

MCP gets the gate **read-only** as the resource `soul://relearn/status`
(state, pending offer, per-mode preview *counts* — evidence id lists stay on
the ops surface so the resource stays under the 64 KiB cap; the reads are
audited service invocations but never write domain events). No eleventh MCP
tool was added (spec 13.1/13.2: capabilities stay exactly tools+resources,
ten tools); approval is an operator act on CLI/HTTP, not an agent tool.

Probes: `test_relearn_lifecycle_over_http_ops` (scan → offer → status →
preview → gated apply → rollback, full loop),
`test_relearn_lifecycle_over_cli` (same via `cli.main`),
`test_relearn_preview_without_pending_offer_is_typed`,
`test_relearn_ops_validate_payloads`,
`test_mcp_relearn_status_resource` (+ no-pending variant; resource reads
never write), `test_relearn_ops_do_not_extend_the_knowledge_surface`,
`test_mcp_tools_stay_exactly_ten`.

## Notes for the coordinator

1. **Service-level relearn API**: SUB-41 delivered it mid-flight
   (`service.relearn_status/offer_relearn/preview_relearn/apply_relearn` +
   `RelearnStatus` + a public `engine.pending_offer()`); the adapter uses
   exactly those, so no private-attribute reach remains in the relearn path.
   Their service pre-validation maps gate refusals to the knowledge taxonomy
   (`OPERATOR_DECISION_REQUIRED` 409, `LIFECYCLE_ERROR` naming
   `NoPendingRelearn`); `http_api._STATUS_BY_CODE` gained the two new codes.
2. **Envelope-code convention**: codes for the BackupError/ProjectionError
   family are type names (the StoreError pattern), chosen over the audit's
   illustrative SNAKE_CASE so the wire result is identical whether or not the
   core rebases the classes under `StoreError` (SUB-41 already did so for
   `BackupError`; the adapter is robust to either shape for the
   ProjectionError family via `_OPS_ERROR_MAP` + `_STATUS_BY_TYPE`).
3. **fchmod no-op**: the S-4 fix uses a startup-scoped umask because Linux
   `fchmod` on a unix-socket fd silently does nothing (probed). If a future
   port needs per-fd semantics, re-probe on that platform first.
4. **S-5 key shape**: the aggregation key includes the denial message so the
   wave-2 F9(a) contract (distinct denials each audited) survives unchanged;
   only *identical repeats* aggregate. `tests/test_wave2_track_security.py`
   needed no edit.
