# Residual wave-1 R2 — importer file reads were uncontained and unbounded

## Evidence

`SeedImporter.import_file` read `path.read_bytes()` directly and
`import_directory` hashed every `root.rglob("*.md")` match with
`file.read_bytes()`. Two gaps:

* **Uncontained** — a symlink inside the corpus (or a standalone import path)
  pointing outside the root was followed and its bytes ingested; the
  `resolve_within_root` containment that the F5 fix wired into
  `SourceRegistry._scan` ran only *after* the importer had already read the
  escaped file (and registry containment uses the resolved file's own parent
  for single-file observes, not the corpus root).
* **Unbounded** — no size guard: a multi-gigabyte markdown file was read fully
  into memory, hashed, and parsed.

Probes: `corpus/linked.md → /tmp/outside.md` imported without error;
a 1 MiB + 1 byte file imported without error.

## Root cause

The importer grew its reads before `security.resolve_within_root` /
`OversizedPayloadError` existed (Task 9), and nobody re-wired the read paths.

## Affected global criterion

"Fail closed on out-of-policy input" — out-of-root and oversized content
entered the event store silently instead of raising a typed error.

## Smallest fix applied (ruling R2)

One module-private helper in `importer.py`,
`_read_contained_file(root, path, max_bytes=MAX_IMPORT_FILE_BYTES)`:

1. resolves the candidate through `security.resolve_within_root` (typed
   `PathEscapeError` before any byte is read),
2. rejects `stat().st_size > max_bytes` with a typed `OversizedPayloadError`,
3. re-checks the post-read byte count so a racing writer cannot grow the file
   past the guard mid-read.

Wiring: `import_file` gains keyword-only `import_root` (directory imports pass
the corpus root; the default is the file's own directory, so a standalone
import can never follow a symlink outside the directory the caller named —
mirroring `SourceRegistry._scan`'s single-file rule). `import_directory`'s
hash loop reads via the same helper against the corpus root, and the manifest
read is bounded by `MAX_IMPORT_MANIFEST_BYTES`. Constants, documented:
`MAX_IMPORT_FILE_BYTES = 1_048_576` (1 MiB, matching
`security.DEFAULT_MAX_PAYLOAD_BYTES`; seed markdown is operator-curated prose,
real files are kilobytes) and `MAX_IMPORT_MANIFEST_BYTES = 1_048_576`.
In-corpus symlinks still import; normal imports are unchanged (the whole
pre-existing `test_importer.py` suite stays green).

## Regression probes

- `tests/test_audit_wave1_residual_probes.py::test_r2_import_file_refuses_symlink_escape`
- `…::test_r2_import_file_refuses_oversized_file` (asserts the cap value is
  named in the typed error)
- `…::test_r2_import_directory_refuses_symlink_escape`
- `…::test_r2_import_directory_refuses_oversized_file`
- `…::test_r2_normal_and_in_root_symlinked_imports_still_work` (non-vacuous
  positive control: real file + in-root alias import; standalone import works)

RED evidence: all four negative R2 probes failed pre-fix (escaped symlink and
oversized file imported without any error); the positive control passes before
and after.

## Rollback note

Remove `_read_contained_file`, the two constants and the `import_root`
parameter, restoring the three direct reads; no stored data or manifest schema
changes, so rollback is lossless.
