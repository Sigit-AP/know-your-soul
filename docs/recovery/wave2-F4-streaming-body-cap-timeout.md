# Wave-2 finding F4 — body cap after full buffering; chunked bypass; no read timeout

## Evidence

Audit F4: the middleware trusted only declared `Content-Length`; the handler
called `await request.body()` (full in-memory buffering) *before* the cap
check. A 3 MiB chunked body with no `Content-Length` was fully consumed
(`server consumed full body before 413: True`), and a body dribbled at
1 chunk/sec was held indefinitely (`timeout_keep_alive` does not cover body
reads).

## Root cause

The size cap ran after buffering, chunked bodies skipped the early check, and
no timeout bounded `receive()`.

## Affected global criterion

Spec 13.1 payload bounds: resource limits must hold at the transport
boundary, not after the cost is paid.

## Fix applied

`BoundaryMiddleware` now streams the body itself before invoking the app:
each `http.request` message is counted, the connection is answered 413 at the
first byte past the cap (never buffered whole), and every individual read is
bounded by `body_timeout_seconds` (default 10s; `asyncio.wait_for`) with a 408
`REQUEST_TIMEOUT` envelope on expiry. The buffered-within-cap body is replayed
to the inner app, so handlers are unchanged. Client disconnect mid-body
returns silently (no traceback). `create_app`/`serve` accept
`body_timeout_seconds`.

## Regression probes

- `tests/test_wave2_security.py::test_f4_chunked_body_is_cut_at_the_cap_without_full_buffering`
  (3×600B chunks vs 1024B cap: 413 after exactly 2 reads — cut at cap+1, the
  third chunk never consumed)
- `test_f4_body_read_timeout_closes_slow_dribble` (5s-dribbled body vs 0.1s
  timeout → 408 `REQUEST_TIMEOUT` well under 5s)
- `test_f4_normal_body_within_cap_still_works` (non-vacuous legitimate flow)
- Existing `test_oversized_request_rejected` /
  `test_oversized_content_length_rejected_before_read` still pass.

## Rollback note

Restore the pass-through `await self._app(scope, receive, send)` tail of the
middleware; `body_timeout_seconds` params become inert.
