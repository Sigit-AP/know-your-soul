# Wave-2 finding F1 — unauthenticated loopback HTTP (CSRF) + arbitrary-path ops

## Evidence

Audit `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave2-adapters-security.md`:
a cross-origin HTML form could POST `text/plain` to `http://127.0.0.1:<port>/v1/ops/*`
(no Content-Type check, loopback exempt from API-key auth), and
`ops/backup`/`ops/restore` accepted arbitrary caller-supplied paths — probe
wrote a copy of the live store to an attacker-chosen path (`POST text/plain -> 200;
file written: True`).

## Root cause

`http_api.authorize_request` returns early for loopback; `_handle` parsed the
body as JSON regardless of Content-Type; `_call_backup`/`_call_restore` passed
`dest_path`/`backup_path` straight to the filesystem with no containment.

## Affected global criterion

Spec 13.1 adapter boundary: the local API must fail closed against untrusted
local content (browser pages are attacker-controlled local callers).

## Fix applied

- `_handle` requires `Content-Type: application/json` on every POST route
  (parameters like `; charset=utf-8` allowed); anything else → 415
  `UNSUPPORTED_MEDIA_TYPE`, rejected at the boundary (not journaled, same as
  Host/auth rejections per spec 7.7).
- `BoundaryMiddleware._authorize_local`: when an API key is configured it
  gates **every** client — loopback and unix-socket peers included
  (constant-time SHA-256 digest compare, mirroring `security.py`). Fail-closed
  trade-off chosen deliberately: the CLI dispatches in-process and never talks
  to the socket, so no legitimate local flow breaks.
- `LocalStack.ops_root` (new field): `build_stack(..., ops_root=...)` pins a
  fixed containment root (default: the store's directory, must exist).
  `_call_backup`/`_call_restore` resolve every caller path through
  `resolve_within_root`; escapes → `PATH_ESCAPE` (400). CLI exposes
  `--ops-root`.

## Regression probes

- `tests/test_wave2_security.py::test_f1_non_json_content_type_gets_415_and_never_reaches_the_handler`
  (form post rejected, no file written, no audit journaled)
- `test_f1_missing_content_type_gets_415`,
  `test_f1_json_content_type_with_charset_still_works` (legit client intact)
- `test_f1_loopback_also_needs_the_configured_key`,
  `test_f1_no_configured_key_still_serves_loopback`
- `test_f1_backup_dest_outside_ops_root_rejected` (absolute + `..` traversal),
  `test_f1_restore_paths_outside_ops_root_rejected` (source and dest)
- `test_f1_backup_restore_inside_ops_root_still_works` (non-vacuous)
- `tests/test_cli.py::test_cli_backup_outside_ops_root_fails_closed`,
  `test_cli_ops_root_option_allows_a_configured_root`
- `tests/test_http_api.py::test_loopback_client_also_needs_the_configured_key`

## Rollback note

Revert the middleware `_authorize_local` call, the `_handle` Content-Type
check, and pass `ops_root=None` semantics; the `LocalStack.ops_root` field is
additive and inert without the call-site wiring.
