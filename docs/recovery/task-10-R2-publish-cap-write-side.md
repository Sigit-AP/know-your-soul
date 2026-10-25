# Recovery: R2 — publish cap 64 KiB could wedge rebuilds behind an untyped error

- **Date**: 2026-10-25 — **Owner**: SUB-20 — **Severity**: residual R2 (availability-impacting follow-up of wave-1 F6)
- **Evidence**: review-fixes-wave1.md R2 — the store accepts a 70 KB node summary (probed: write succeeds), then every subsequent `rebuild()` failed with a bare `OversizedPayloadError` raised by `security.sanitize_text` mid-extraction, after the build was already under way; no typed error, no write-side guard.
- **Root cause**: the publish-side 64 KiB text cap existed only implicitly inside `security.sanitize_text` (a length *exception*, not a projection decision), and artifacts were size-checked nowhere at all.
- **Fix (projection.py only)**:
  - New typed error `OversizedArtifactError(ProjectionError)`; constants `PUBLISH_TEXT_MAX_CHARS` (64 KiB, aliases the security default) and `ARTIFACT_MAX_BYTES` (64 KiB per artifact).
  - `_check_publish_text` runs **before** sanitization for every published free-text field (summary, source_file, provenance entries, context tags) and raises the typed error naming the node and field — eager, before the generation is assembled.
  - `_check_artifact_size` runs **as each artifact is produced** in `_build_generation` (graph.json → … → manifest.json), before any byte reaches disk, and again inside `_write_if_changed` at the actual write point (defense in depth).
  - `GraphProjector(max_artifact_bytes=...)` exposes the cap for probes; default 64 KiB covers the real corpus (largest current artifact: graph.html ≈ 29 KB).
- **Regression probes** (tests/test_projection.py): `test_oversized_node_summary_fails_typed_before_building` (70 KB summary → typed error naming the node; the previously active generation keeps serving, `CURRENT` untouched), `test_artifact_size_cap_is_fail_closed_and_typed` (cap=64 → typed error, no `CURRENT`, nothing written; default cap → rebuild publishes), `test_write_if_changed_rechecks_the_cap_at_the_write_point` (direct write-path probe).
- **Gate**: full suite 620 passed / 1 skipped (design skip-gate); ruff check + format clean; pyright 0 errors.
- **Rollback**: remove the three check call sites and the typed error; the pre-fix fail-closed-but-untyped behavior returns.
