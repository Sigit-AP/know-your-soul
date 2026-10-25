# Wave-2 finding F3 — MCP `--transport http --api-key` never authenticated requests

## Evidence

Audit F3: `mcp_server.main`'s http branch called
`assert_bind_allowed(..., api_key=...)` (bind decision only) and then
`uvicorn.run(server.streamable_http_app(), ...)` with **no auth middleware and
no Host allowlist** — the key was never checked per request; a non-loopback
bind served every remote caller.

## Root cause

The http branch wrapped the MCP app in nothing; Task 7's boundary middleware
lived only on the JSON API surface.

## Affected global criterion

Spec 8/13.1: every network-served adapter surface enforces the same bind +
auth policy.

## Fix applied

`mcp_server.main` http branch now wraps `server.streamable_http_app()` in
`http_api.BoundaryMiddleware` (the renamed, now-exported Task 7 `_BoundaryMiddleware`)
with `DEFAULT_HOST_ALLOWLIST` and the resolved API key — per-request Host
allowlist, remote-auth, and (per wave-2 F1) loopback gating when a key is
configured, plus the streaming body cap. `--api-key-env NAME` was added here
too (same argv-exposure shape as F6).

## Regression probes

- `tests/test_mcp_server.py::test_f3_http_transport_wraps_streamable_app_with_boundary_auth`
  (keyless remote → 401, keyless loopback → 401, foreign Host → 403, keyed
  caller reaches the real inner app — non-vacuous)
- `test_f3_http_transport_without_key_serves_loopback_only` (no key:
  loopback served, remote denied)

## Rollback note

Remove the `BoundaryMiddleware` wrap in the http branch; the middleware itself
is shared with the JSON API and stays.
