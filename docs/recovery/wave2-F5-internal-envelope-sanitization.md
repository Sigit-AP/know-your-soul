# Wave-2 finding F5 — INTERNAL envelopes leaked raw exception text

## Evidence

Audit F5: an unexpected server-side error was reflected verbatim —
`backup to /proc -> 500 "[Errno 2] No such file or directory: '/proc/1/no-dir'"`
— leaking absolute paths and OS internals. `redact_text` strips secrets/PII
but not filesystem paths.

## Root cause

`error_envelope`'s catch-all INTERNAL branch set
`message = redact_text(str(error))`.

## Affected global criterion

Spec 13.1 error sanitization, at parity with the MCP adapter
(`mcp_server.py` already returns a fixed string for the blanket except).

## Fix applied

In `http_api._handle`'s exception path, an envelope whose code is `INTERNAL`
gets the fixed wire message `"internal error"`; the full exception (with
traceback) is logged server-side via `logger.exception` on
`soul_knowledge.http_api`. Typed errors (KnowledgeError, StoreError, boundary
errors) keep their redacted messages — those messages are constructed from
caller-visible context. CLI envelopes are unchanged (local operator surface).

## Regression probes

- `tests/test_wave2_security.py::test_f5_internal_error_never_leaks_exception_text`
  (forced `RuntimeError("/proc/1/no-dir boom: [Errno 2]")` → 500, message
  exactly `"internal error"`, detail absent from the wire body and present in
  the server log via `exc_info` — non-vacuous on both sides)

## Rollback note

Delete the INTERNAL branch in `_handle`; `error_envelope` itself is
untouched.
