# Wave-2 re-review observation — PATH_ESCAPE message disclosed the absolute ops_root

## Evidence

SUB-23 re-review (`review-fixes-wave2.md`, observation 6): the `PATH_ESCAPE`
wire message from `security.resolve_within_root` embedded the server's
absolute ops root (`path '../x' resolves outside the fixed root
'/abs/path/to/ops-root'`). `redact_text` masks secrets and PII, not
filesystem paths, so the absolute root reached the client verbatim. Rated an
observation (local authenticated surface), fixed here for completeness.

## Root cause

`_resolve_ops_path` returned `resolve_within_root`'s error — a message
written for server-side/operators — directly onto the wire envelope.

## Affected global criterion

Error sanitization consistency (wave-2 F5 principle): wire messages carry a
typed code plus client-useful diagnostics; server-absolute layout stays
server-side.

## Fix applied

`http_api._resolve_ops_path` now catches `PathEscapeError` from
`resolve_within_root` and re-raises a sanitized one:
`path '<candidate>' is outside the permitted operations root`. The typed
error code (`PATH_ESCAPE` via the envelope's type branch) and the 400 status
are unchanged; the caller's own candidate stays in the message (legitimate
clients keep enough diagnostics), the absolute root never appears. The
original error is chained as `__cause__`, so server-side introspection keeps
the full detail. `security.resolve_within_root` itself is untouched (shared
with importer/startup surfaces where the operator-facing detail is
appropriate; file owned outside this workstream).

## Regression probes

- `tests/test_wave2_security.py::test_f1_path_escape_message_does_not_leak_ops_root` —
  relative-traversal and absolute-path variants: 400 `PATH_ESCAPE`, the
  resolved `ops_root` string absent from the wire message, the candidate
  present. RED pre-fix (message contained the absolute root verbatim), GREEN
  post-fix.
- Non-vacuous positive controls unchanged:
  `test_f1_backup_restore_inside_ops_root_still_work` (in-root backup/restore
  → 200) and the whole F1 containment block still pass.

## Rollback note

Drop the try/except in `_resolve_ops_path`; the absolute root leaks again on
the local authenticated surface only.
