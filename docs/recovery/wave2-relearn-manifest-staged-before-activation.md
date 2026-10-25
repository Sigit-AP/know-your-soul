# Wave-2 re-review minor — relearn manifest.json written after backup activation

## Evidence

SUB-23 re-review (`review-fixes-wave2.md`, finding 3): after the wave-1
residual fix, `relearn._verified_backup` delegated to
`backup_protected_artifacts` (which activates the destination with one atomic
rename) and only *then* wrote `manifest.json` into the already-live backup
directory. A crash in that window leaves an activated-but-unusable backup —
rollback fails closed (availability loss, not corruption), but the pre-fix
code staged the manifest *inside* the temp dir before the rename.

## Root cause

The manifest write moved from inside the staging area (pre-fix inline
implementation) to after the helper's atomic activation (post-fix
delegation), reopening a torn-activation window the original design closed.

## Affected global criterion

Relearn contract "Backup and rollback" (spec 13.3): the protected set —
including the manifest — is captured and provably intact *before* activation;
a crash at any point leaves either the old state or a complete new backup,
never a live partial one.

## Fix applied

`_verified_backup` is two-phase again while keeping the shared helper for the
entire verified copy:

1. `backup_protected_artifacts(..., dest_dir=staging)` stages the verified
   protected set into a hidden sibling directory
   (`.<relearn_id>.tmp-<pid>`, same filesystem as the destination).
2. The relearn `manifest.json` is serialized into that stage and the stage is
   fsynced; then one `os.replace(staging, final)` activates the **complete**
   backup and the parent directory is fsynced.

Any failure before the rename removes the staging dir (`shutil.rmtree(...,
ignore_errors=True)`); a hard crash can leave only hidden staging debris,
never a live backup without a manifest. `backup.py` itself is untouched
(owned by another workstream); the helper's verification semantics
(quick_check, event-count, chain-head, manifest-declared generation hashes,
CURRENT pointer) are unchanged, and the manifest bytes/format are identical
to before.

## Regression probes

- `tests/test_relearn.py::test_backup_manifest_is_staged_before_activation` —
  spies on `os.replace`: at the rename that activates the backup directory,
  `manifest.json` must already exist inside the source dir. RED pre-fix (the
  helper's rename to `final` fired with no manifest in its stage), GREEN
  post-fix.
- `tests/test_relearn.py::test_backup_crash_leaves_no_activated_backup_without_manifest` —
  injects a crash right after the protected-set copy: no activated
  (non-hidden) backup directory may exist without its manifest. RED pre-fix
  (activated dir without `manifest.json`), GREEN post-fix (final never
  created).
- Rollback byte-exactness still green:
  `test_rollback_restores_exact_pre_relearn_state` (restored sha256 ==
  recorded manifest hashes, graph bytes, CURRENT pointer, P/M/E, exactly one
  linked audit event) and `test_apply_takes_verified_atomic_backup` — the
  ordering change does not alter the verified content.

## Rollback note

Restore `dest_dir=final` and the post-call `(final / "manifest.json")`
write; the torn-activation window returns (fail-closed but dirty).
