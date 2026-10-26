# Operations — soul-knowledge

This document covers day-2 operation of the store, projection and
API surfaces: observability, health/readiness, backup/restore, projection
recovery, and the security posture. The HTTP/Unix-socket server
(`http_api.py`) builds on the pure policies documented here.

## Security posture

Least privilege by default (`src/soul_knowledge/security.py`):

- **Bind policy** — `assert_bind_allowed(bind, api_key=...)`: Unix sockets
  (`unix:/path.sock`) and loopback binds (`127.0.0.1`, `::1`, `localhost`)
  need no key. Any wider bind (`0.0.0.0`, LAN addresses) without a configured
  API key raises `BindPolicyError` at startup. Non-loopback deployments belong
  behind TLS / a reverse proxy.
- **Request authorization** — `authorize_request(RequestView, api_key=..., host_allowlist=...)`:
  the Host allowlist is enforced on *every* request, loopback included, and an
  empty allowlist fails closed. Beyond loopback a configured API key is
  mandatory (compared via `hmac.compare_digest` over SHA-256 digests). The
  `make_host_allowlist` / `make_request_authorizer` factories are the server
  middleware seam.
- **Payload sanitization** — `sanitize_text` strips every Unicode format
  character (bidi controls, zero-width characters, BOM) and every control
  character except tab/LF/CR, and raises `OversizedPayloadError` beyond the
  length cap. `ensure_payload_size` / `ensure_text_size` bound request sizes
  (default 1 MiB).
- **Fixed-root path containment** — `resolve_within_root(root, candidate)`
  resolves symlinks of every existing component and fails closed
  (`PathEscapeError`) on `..` traversal, absolute paths outside the root,
  cross-tree reads, and symlink escapes. This closes the deferred Task 3
  symlink finding; the MCP/API file surfaces must resolve every caller path
  through it.
- **Redaction** — `redact_text` / `redact_value` remove known secret shapes
  (API keys, AWS keys, GitHub/Slack tokens, private-key blocks, bearer/JWT
  tokens, credential assignments) and email PII. Apply on the write surface
  before persistence and on every read surface that leaves the process; the
  structured logger redacts all fields automatically.

## Observability

`src/soul_knowledge/observability.py`:

- `MetricsRegistry` — thread-safe counters/gauges, deterministic
  `snapshot()` and Prometheus-style `render_text()`.
  `collect_store_metrics(store, registry)` publishes store gauges
  (`event_log_events`, `knowledge_high_watermark`, `pending_journals`, ...).
- `StructuredLogger` — JSON-lines records with `ts`, `level`, `event`,
  `component` and caller fields; every field is redacted before it reaches
  the sink. Carry `correlation_id=<invocation_id>` (plus `audit_event_id` /
  `audit_event_seq` from the `WriteReceipt`) so any log line joins to exactly
  one final audit event.

## Health and readiness

- `check_health(store)` — liveness: `SELECT 1`, full hash-chain verification
  (`EventStore.verify_chain`), and applied migrations equal to the migrations
  on disk. Any failed check makes the report not-ok; probes never raise.
- `check_readiness(store, projector=..., stale_after_seconds=300)` — traffic
  gate: health **plus** no stale incomplete journals (heartbeat older than
  the threshold) **plus**, when a projector is attached, an active generation
  whose knowledge watermark equals the store's.

Run readiness before accepting traffic; run
`AuditLog.recover_incomplete(projection_repair=projector.repair_if_stale)` (or
`recover_projection(..., audit_log=...)`) at startup to close crashed
invocations exactly once.

## Backup and restore

`src/soul_knowledge/backup.py`:

- `backup_store(store, dest)` — SQLite online backup into a same-directory
  temp file, verified (`PRAGMA quick_check`, event count, chain-head anchor),
  then `os.replace` + fsync of file and directory. A crash never leaves a
  half-written backup at the destination. Returns a `BackupReceipt` with the
  event count, chain head, size and SHA-256.
- `inspect_backup(path)` — read-only integrity report; raises `BackupError`
  for a missing file.
- `restore_store(backup, dest, overwrite=False)` — verified, atomic restore;
  refuses to overwrite an existing non-empty database unless `overwrite=True`.
  Equivalence is proven by `test_backup_restore_equivalence` (row-for-row
  `event_log` equality and `verify_chain` on both copies).

Recommended cadence: backup before every relearn/apply (spec 13.3) and on a
schedule; keep receipts with the backup so rollback can assert the chain head.

## Projection recovery

- `diagnose_projection(store, projector)` classifies the active generation:
  `CURRENT`, `STALE` (watermark/pointer disagreement), `TORN` (missing
  artifacts or dangling `CURRENT`), `CORRUPT` (artifact bytes disagree with
  the manifest hashes), `MISSING` (no active generation).
- `recover_projection(store, projector, audit_log=None)` drives
  `GraphProjector.repair_if_stale` (a no-op when current), optionally runs
  `AuditLog.recover_incomplete` with the repair hook, and raises
  `BackupError` unless the projection is `CURRENT` afterwards. Because
  generations are content-addressed and deterministic, repair re-derives the
  same directory and restores torn/corrupted artifacts byte-identically.

## Source watch (spec 13.5)

`soul-knowledge serve --watch PATH` runs the doc-change watcher for the
server's lifespan (arch review F-1; `update_pipeline.SourceWatchLoop` +
`source_watcher.SourceWatcher`):

- **Detect-only, always gated.** The watcher polls the corpus root
  (`--watch-interval`, default 30s) for content-hash changes. A detected
  change rides the same gated path as `ops/relearn_scan`: the deterministic
  `RelearnOffer` is recorded and the gate becomes `RELEARN_PENDING` —
  audited as `knowledge.relearn_offer` with actor `source-watcher`. The
  watcher **never** applies, never selects a mode, never flips the
  operator-approved baseline; `relearn_preview` / `relearn_apply` stay
  mandatory. The first poll after startup records the `SOUL_CORPUS`
  detection baseline and emits no offer (the initial import stays
  operator-explicit).
- **Cooldown.** After an offer is emitted the watcher pauses
  (`--watch-cooldown`, default 300s). Edits during the pause accumulate and
  arrive as one batched offer — a save storm cannot mint a stream of offers,
  and no change is ever absorbed silently.
- **State file.** The watcher's snapshot defaults to `source-watch.json`
  alongside the store file (override with `--watch-state`). It must live
  *outside* the watched root — startup fails closed otherwise, because a
  state file inside would perturb the corpus fingerprint and mint spurious
  offers. The snapshot is anchored to the resolved root; a snapshot written
  for another root fails closed (`WatcherStateError`).
- **Failure isolation.** Watcher errors (corrupt/foreign state file,
  unreadable corpus) and gate-callback failures are logged
  (`soul_knowledge.update_pipeline`) and backed off exponentially — they
  never take the API down. Operator repair: remove or fix the state file;
  detection resumes on the next polls. The watch thread
  (`soul-source-watch`) starts and stops with the server.
- **Restart.** An unchanged corpus after a restart re-emits nothing (the
  persisted snapshot and the registry baseline agree); changes made while
  the server was down surface as an offer on the first poll.

## Container

`deploy/Dockerfile`: two-stage build on `python:3.14-slim`, non-root `soul`
user, `/data` volume for the store and projections, no runtime network
requirement. Dependency installs differ by surface, deliberately:

- **Local / CI reproduction** uses `uv sync --frozen`, which installs the
  bit-exact locked environment from `uv.lock`.
- **The container build** runs `pip install .`, which resolves dependency
  ranges from `pyproject.toml` — pip does not read `uv.lock` (see the
  Dockerfile header note). For a bit-exact locked container build, generate a
  constraints file from the lock (`uv export`) or install with uv; the
  current image accepts the pyproject ranges by design.

The `soul-knowledge` entrypoint (console script, `cli.py`) is installed by
the build stage.
