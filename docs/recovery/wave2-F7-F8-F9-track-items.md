# Wave-2 TRACK items F7 / F8 / F9 — closure record (SUB-28)

Audit: `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave2-adapters-security.md` §F7/F8/F9.
TDD evidence: `tests/test_wave2_track_security.py` (new, 9 tests) — RED exit=1
(6 failed: F7a×2, F7b, F8, F9a, F9b; 3 negative controls green) → GREEN exit=0 (9 passed).
Files: `src/soul_knowledge/projection.py`, `src/soul_knowledge/read_model_validator.py`,
`src/soul_knowledge/http_api.py`, `tests/test_wave2_track_security.py`,
`tests/test_audit_wave1_probes.py` (semantics update, see F7a). `store.py` untouched — only
its existing public `transaction()`/`insert_event()` API is used.

## F7(a) — blast radius: fail closed per node, not per graph

Design point: **projection** (`GraphProjector._extraction`), not the validator. The
validator sees whole artifacts and can only accept/reject a generation; the projection is
where nodes are assembled, so it is the only point that can remove one node while keeping
the rest. The validator stays as the whole-generation backstop (unweakened — the tampered
artifact test still fails closed).

- `_quarantine_secret_nodes` scans every publish-bound field (post-`sanitize_text`, i.e.
  exactly the bytes that would ship) against the shared `SECRET_PATTERNS`. A hit removes
  the node from the extraction; its edges drop via the `_edge` membership check.
  Invariant outcome is *stronger* than before: secret material never even reaches an
  artifact on disk.
- Typed report: one hash-chained audit event per quarantining rebuild
  (`kind='audit'`, `event_type='security.projection_quarantine'`, `knowledge_affecting=False`,
  payload = node id + field + secret *category* — never secret material) plus one
  `logger.warning` line. No invocation journal (spec 7.7 analog).
- Shrink-guard interaction: a poisoned *update* of an existing node shrinks the extraction
  by exactly the quarantined count; without accounting, `ShrinkGuardError` would re-create
  the same full-read outage. `_build_generation` now takes `quarantined_count` and only
  fails when shrinkage exceeds the quarantine. Shrink beyond quarantine still fails closed.

## F7(b) — prefix leak removed

Validator message was `…secret-shaped string AKIAIOSF…` (8-char prefix of the match).
Now: `artifact graph.json contains a secret-shaped string (category: aws access key);
node(s): <id>` — category + artifact + node location, zero secret material
(`_secret_node_location` names node ids for `graph.json`; ids are identifiers, not secrets).

## F8 — pydantic value echo removed

`dispatch` surfaced `str(ValidationError)` which embeds `input_value=...`. Now
`_validation_summary` (mirrors MCP's `_validation_summary`): `loc: msg` per error
(max 3, remainder counted), no `input`/`ctx`. The same value-free summary is journaled,
so wire and audit agree. Non-pydantic `ValueError`/`TypeError` keep `redact_text(str(...))`.

## F9(a) — boundary security denials are audited

401/403 rejections in `BoundaryMiddleware` are not invocations (spec 7.7), so they open no
journal — but each now appends one standalone `security.boundary_denial` event
(`kind='audit'`, hash-chained via the store's existing `insert_event`, `actor='boundary'`,
payload: reason/remote_addr/host/method/path; host sanitized; presented key material is
never recorded). Wired via `create_app` → `BoundaryMiddleware(denial_recorder=...)`;
a recorder failure logs one warning and never weakens or delays the denial. 413/408/415
stay non-audited (size/timeout/media rejections, not security denials).

## F9(b) — ClientDisconnect tamed

`_handle` catches `starlette.requests.ClientDisconnect` around `request.body()`:
one `logger.info` line (no traceback) and a typed 400 `CLIENT_DISCONNECTED` envelope.
No journal — the request never became an invocation.

## Non-vacuous probes (all in the test file, run green)

- AWS-key-shaped node written → rebuild succeeds, other nodes serve (`get_node`/`query` 200),
  offending node 404, secret absent from `graph.json` and from every audit payload,
  `verify_chain().ok`, warning line contains no secret prefix.
- Secret-bearing *update* of an existing node → quarantined without shrink-guard outage.
- Tampered artifact → validator still fails the whole generation closed (backstop intact),
  message has category + node id, no prefix.
- `{"purpose": 31337.1337}` → 400, wire + audit contain no `31337.1337`, keep `purpose` loc.
- 401×2 + 403 → exactly +3 `security.boundary_denial` events, +0 journal rows, key material
  absent, offending host present, chain verifies; authorized request unaffected.
- Immediate `http.disconnect` on a POST → 400 `CLIENT_DISCONNECTED`, exactly one info log,
  zero `exc_info` records.

## Regression note

`tests/test_audit_wave1_probes.py::test_f4_publish_fails_closed_on_secret_shaped_summary`
pinned the *old* blast-radius behavior (`pytest.raises(ProjectionValidationError)`). Its
stated invariant — "a node carrying a secret never reaches an activated generation" — is
preserved and strengthened; the test now pins quarantine semantics (rebuild succeeds,
node count 0, no artifact contains the secret or node id, one quarantine audit event).

## Side findings (not mine)

Full-suite failures in `test_no_ai_runtime.py`, `test_mcp_stdio_e2e.py`,
`test_acceptance_e2e.py`, `test_dependency_policy.py`, `test_power_loss_recovery.py` and
ruff/format offenders `backup.py`, `runtime_guard.py`, `test_dbg_tmp.py` are other agents'
uncommitted WIP — verified by re-running those clusters with my three src files stashed:
failures persist unchanged without my changes.
