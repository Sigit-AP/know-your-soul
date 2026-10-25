# Wave-2 re-review residual F4 — per-read body timeout lets a sub-timeout dribble pin connections

## Evidence

SUB-23 re-review (`review-fixes-wave2.md`, finding 2): the wave-2 F4 timeout
was **per-read** (`asyncio.wait_for(receive(), timeout=self._body_timeout_seconds)`
per iteration). Probed live: dribbling 1 byte every 0.5 s against a 1.0 s
timeout held the connection for the full 5 s probe window and only died 1.0 s
after the last byte. Memory stays hard-bounded (cut at cap+1), so the impact
is fd/task occupancy — a slow-drip attacker pins connections indefinitely.

## Root cause

No total deadline bounded the body as a whole; each individual read restarted
the full timeout budget.

## Affected global criterion

Spec 13.1 payload bounds: resource limits must hold at the transport
boundary. A bounded-memory but unbounded-duration read still violates the
resource bound.

## Fix applied

`BoundaryMiddleware.__call__` now computes one `deadline = loop.time() +
body_timeout_seconds` before the read loop and passes the *remaining* budget
(`max(deadline - loop.time(), 0.0)`) to each `asyncio.wait_for`. The
configured timeout is therefore a **total body deadline**: a stalled read
still dies when the budget is exhausted (identical behavior for a silent
client), and a sub-timeout dribble dies at the deadline instead of living
forever. Fail-closed, no new parameters, no signature changes (the MCP http
transport reuses the same middleware). The 408 message now says "total body
deadline"; the typed code `REQUEST_TIMEOUT` is unchanged.

## Regression probes

- `tests/test_wave2_security.py::test_f4_sub_timeout_dribble_cut_by_total_body_deadline`
  — 6 chunks at 0.4 s gaps vs a 1.0 s budget: RED on pre-fix code (415 after
  the full ~2.4 s dribble; per-read never fires), GREEN post-fix (408 at
  ~1.0 s, dribble cut before completion).
- Live wire probe (uvicorn + unix socket, scratch `sub24_wire_probe.py`):
  1 byte per 0.4 s against `body_timeout_seconds=1.0` →
  `HTTP/1.1 408 Request Timeout` after 1.30 s,
  `code=REQUEST_TIMEOUT`.
- Positive controls: `test_f4_normal_body_within_cap_still_works` and the new
  `test_f4_chunked_normal_upload_within_deadline_still_works` (streamed
  multi-chunk body inside the deadline → 200) — legitimate uploads unaffected.
- Pre-existing `test_f4_body_read_timeout_closes_slow_dribble` still passes:
  a fully silent client is cut at exactly the configured budget, as before.

## Rollback note

Restore `timeout=self._body_timeout_seconds` in the `wait_for` call and drop
the `deadline` computation; the dribble regression returns (fd occupancy
only, memory stays capped).
