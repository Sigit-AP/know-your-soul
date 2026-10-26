# Codebase push: coverage-driven test wave 3 (SUB-51)

Date: 2026-10-26 · Branch: feature/superpower-knowledge-soul · HEAD at start: 2f5e711
(SUB-50's contract-v2 retrieval verbs landed in the working tree mid-wave; see
"Exclusions" — none of their new code was tested here.)

## Goal

Close the 376-statement coverage gap reported at dispatch with meaningful
tests for *uncovered branches* — error paths, rare guards, boundary branches —
and lift test density toward/past Graphify's 76.1 functions/kLOC, honestly.

Fresh baseline at HEAD 2f5e711 measured **95.0%** (6,897 stmts, 345 miss;
`pytest tests/ --cov=soul_knowledge`), not the 94.19%/6,477 quoted at dispatch
(waves 9–10 landed in between).

## What was added (3 new files, tests/ only)

| File | Tests | Branches pinned |
|---|---|---|
| `test_wave3_coverage_units.py` | 70 | `domain` StrictDecimal int/uninterpretable-type + professional_level folding + evidence/node_id mismatch · `contracts` recommendation length guard + promotion acceptance · `scoring` config TOML guards (non-string decimal, undecimal, bool-int, key sets, non-positive half-life, P/E-weight sums, unsorted thresholds, composite exponents), empty-Jaccard, E-evidence exclusion classes, zero-quality M-bin behavior · `improvement` regression-gate source-version flag, proposal validation (unknown node, missing payload key, unknown relation kind, missing target), `_apply_diff` scope/minus/remove ops, retain/merge/rollback unknown-node paths · `observability` negative counter, empty query-audit export, warning/error records, health over a closed store · `review` hysteresis demotion (`promotion-criteria-lost`) · `runtime_guard` empty module name, forbidden console-script entry point, `install_import_guard`, `assert_clean` fail-closed on an unblocked root, double socket-guard install, AF_UNIX connect_ex/sendto passthrough, unix-path create_connection passthrough, mapping handler registry, non-mapping capabilities, outbound/import probe fail-closed sentinels, verify_runtime probe/capability refusals · `store` in-memory live-registry skip, context manager, tampered chain anchor, relation-on-unknown-node, combined domain+audit failure, incomplete-journal outcome, heal-without-marker, duplicate journal · `security` file-as-root refusal, empty-normalized host · `source_watcher` non-candidate skip, unreadable/mis-anchored state · `source_registry` directory-matching-pattern skip · `scheduler` default UTC clock |
| `test_wave3_coverage_lifecycle.py` | 77 | `projection` paragraph/list flush, headingless file, empty-cleaned lead, ghost source-version tolerance, validator-rejection typed refusal, unparseable manifest, version-pointer repair · `service` snapshot refusals (foreign projection version, graph-hash drift, validator-refused generation), estimate fixed-point cap, access-only cap refusal, generic audited failure paths (query/get_node/get_subtree), subtree minimum-budget/trim/unfittable-metadata, unknown evidence target, stale write meta, typed lifecycle mapping table, invalid-proposal guards, rollback guards (unknown node, no applied improvement, missing revision, invalid request), relearn gate errors (no pending offer, no backup root, audited status/offer failures) · `backup` copy verification (count/head), dead-sidecar probe, temp cleanup on failure, foreign-database inspection, interrupted-activation recovery (trash reuse, replace-failure rollback, multiple trashes), protected-backup guards (stage reuse, missing/drifted artifact, staged pointer/manifest hash mismatch, stage cleanup), unverified-backup refusal, restore-failure reopen-in-place, temp-pid parsing, pid-alive edges, sweep race tolerance, torn/stale diagnosis, still-broken recovery refusal · `relearn` in-memory checkpoint no-op, unknown-version manifest, classify skip/quarantine partitioning, staging/final pre-emption cleanup, non-relearn idempotency conflict, backup-root/manifest absence, rollback without root/record, tampered backup db / generation file / CURRENT pointer (all typed `RollbackVerificationError`, store reopened) · `importer` empty remap passthrough, grown-file double-check, state_dir requirement, retire-aware scope replay, ambiguous legacy wikilink fail-safe · `observability.check_readiness` lying-manifest heal + shrink-scoped rebuild · `relearn.rollback` live WAL/SHM sidecar removal |
| `test_wave3_coverage_adapters.py` | 66 | `http_api` unknown-op dispatch, non-object payload with journaling, non-ValidationError mapping, ops 404, JSON-array body 400, no-ops-root refusal, path-through-file refusal, path-moved-mid-op refusal, startup journal recovery running the projection-repair closure, handler-level body cap, denial-recorder failure never weakening a denial, garbage Content-Length → 413, disconnect mid-body silence, unknown ASGI message skip, replay-receive passthrough, unverifiable stale-socket refusal, bind failure closes the socket, exactly-one-transport policy, HTTP uvicorn-config branch, post-serve socket cleanup tolerance · `cli` max-body-bytes passthrough, KeyboardInterrupt clean exit, degraded health exit 1 · `__main__` module entry · `mcp_server` null type, nullable None, maxLength/pattern, nested missing required, relation-allowlist forwarding, ISO datetime parse/reject, malformed diff, approval record build/reject, StoreError typed tool error (idempotency conflict), no-active-projection resource, tampered graph resource, resource byte cap, missing distribution version fallback, dirty-env startup refusal (exit 2), unset api-key-env refusal, disallowed bind refusal, stdio anyio delegation · `read_model_validator` missing manifest keys/file, disk disagreement, wrong artifact set, invalid/non-object graph JSON, malformed links (non-dict, missing confidence_score, absolute endpoint), junk node id, forbidden-root leak, secret-suffix edge shapes (non-graph artifact, no matching node, non-list nodes) · `fragment_generator` dotdot stripping, non-positive cap, non-UTF-8 refusal · `graphify_adapter` schema-error refusal, fragment cache put/hit round-trip, env restore after partition · `retrieval` unique-prefix domain resolution, dangling/cyclic parent chains · `update_pipeline` empty-change receipt, ProjectorBusy retry exhaustion |

Total: **213 new test functions** (216 items minus 3 renamed/adjusted during
iteration), **+3,433 test LOC**. Everything is example-based and
deterministic; no wall-clock sleeps (the one retry-exhaustion test uses
`backoff_seconds=0`).

## Metrics (method identical to SUB-38/40/45: `cat src/soul_knowledge/*.py | wc -l`, `grep -c "def test_"`)

| Metric | Before wave 3 | After wave 3 | Target | Status |
|---|---|---|---|---|
| coverage (`--cov=soul_knowledge`) | 95.0% (345/6,897 miss) | **99.46%** (39/7,220 miss) | ≥97% | **met** |
| test LOC | 25,647 | **29,628** | — | +3,981 |
| test functions | 1,105 | **1,339** | — | +234 (213 mine + 21 SUB-50) |
| test:kode ratio | 1.53 | **1.69** | — | up |
| density (funcs/kLOC src) | 65.6 | **76.3** | ≥70 | **met** (Graphify: 76.1) |
| suite wall time | 236.9s | 270.3s | <2× | 1.14× |

(Before-column: HEAD 2f5e711 full-suite run at wave start. After-column
includes SUB-50's in-flight src/tests; density is honestly measured against
the current 17,547 src LOC.)

## Branches deliberately NOT tested (with reasons)

Dead-defensive / structurally unreachable (verified by reading the code):

- `backup.py:155` — `quick_check` returning non-"ok" *without raising* needs a
  page-level corruption that keeps the file openable; not deterministically
  craftable. (The raising variant is covered by `inspect_backup` on a foreign
  database.)
- `mcp_server.py:443`, `http_api.py:587` — a pydantic `ValidationError` with an
  empty `errors()` list; the public API never produces one.
- `http_api.py:779` — unreachable `elif`: `PayloadTooLarge` is a
  `KnowledgeError`, caught by the first branch (same wire code; see F-SUB51-2).
- `mcp_server.py:668-669` — the tool schema's `diff` item enum is exactly
  `DiffOp` and closed to unknown keys, so `DiffOperation(**item)` cannot fail
  after schema validation.
- `read_model_validator.py:245` — the pinned Graphify loader builds with
  `directed=True` unconditionally (verified against
  `graphify_adapter.build_graph`), so "not directed" cannot occur.
- `read_model_validator.py:249` — the loader emits exactly one edge per
  declared link; "more edges than declared" cannot occur.
- `security.py:363-364` — `Path.resolve(strict=False)` does not stat, so the
  `OSError` catch is unreachable on Linux (symlink loops raise `RuntimeError`
  only in strict mode).
- `source_registry.py:198-199` — `rglob` only yields root-prefixed paths, so
  `relative_to(root)` cannot raise `ValueError`.
- `service.py:1369` — `evaluate` raises `UnknownNodeError` for an unknown node
  in the scoring engine *before* `_assessment_event` re-checks; reachable only
  via a delete-between-calls race the store does not permit.
- `scoring.py:570` — M bins are pre-filtered to `q["m"] > 0`, so
  `local_total == 0` cannot occur.

Covered elsewhere, outside the coverage process:

- `mcp_server.py:948-951` (`_serve_stdio`) — exercised by
  `tests/test_mcp_stdio_e2e.py` in a real subprocess; an in-process run would
  need fake stdio streams (a wiring sham).

SUB-50's in-flight code (pool rule: not tested here):

- `service.py` 926, 976, 983-985, 1036, 1042, 1048-1050, 1112, 1119-1121,
  1168-1171, 1176-1178 and `retrieval.py:978` — the contract-v2 verbs
  (`path`/`explain`/`affected`/`god_nodes`) budget/error paths.

## Real findings (reported, not fixed — src is out of scope)

**F-SUB51-1 (typed-error hygiene, minor):** `backup.inspect_backup`
(`src/soul_knowledge/backup.py:285-297`) raises an untyped
`sqlite3.DatabaseError("file is not a database")` when handed non-SQLite
bytes, instead of returning `{"ok": False}`. Consequently
`restore_store(garbage_path)` propagates `DatabaseError`, not the documented
typed `BackupError`. Verified: `restore_store(b"this is not a database")` →
`DatabaseError`. Suggested owner: backup owner — wrap the
`PRAGMA quick_check` read in the same `sqlite3.DatabaseError` guard the anchor
read already has.

**F-SUB51-2 (dead code, cosmetic):** `http_api.error_envelope`'s
`elif isinstance(error, PayloadTooLarge)` branch is unreachable —
`PayloadTooLarge` subclasses `KnowledgeError`, which the first branch handles
with the identical code (`PAYLOAD_TOO_LARGE`). No behavior change either way.

## Gates (fresh, at submission)

- `pytest tests/ -q`: **1366 passed, 0 failed** (270s; 1.14× the pre-wave 237s)
- `pytest tests/ --cov=soul_knowledge`: **99.46%** (7,220 stmts, 39 miss)
- `ruff check tests/`: exit 0 · `ruff format --check tests/`: clean
- `pyright`: **0 errors, 0 warnings**

No commits made (pool rule). No existing test file edited. No `src/` or
frozen-contract file touched.
