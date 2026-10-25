# Row 23 residual — whole-set durability and power-loss recovery (SUB-29, 2026-10-25)

Closes the residual named by `docs/research/graphify-feature-matrix.md` row 23
("Graphify guarantees atomicity per artifact and no durability; whole-set
durability and power-loss recovery remain product-owned", residual owner
Task 9). Evidence: `tests/test_power_loss_recovery.py` +
`tests/_crash_child.py` (12 tests, real process kills, fresh GREEN exit 0).

## Diagnosis — what the row demands vs. what existed

The row's *Adaptation* column already covers per-artifact guarantees: canonical
one-line JSON, same-directory temp + `os.replace`, fsync per artifact, a CAS
manifest per generation. Verified present at SUB-29 start:

- artifact writes: temp + flush + `os.fsync` + `os.replace`, generation dir
  fsync (`projection.py:_write_if_changed`, `_build_generation`);
- store: SQLite WAL + `PRAGMA synchronous = FULL` (`store.py:193-194`);
- activation: store CAS (rejects older watermarks) commits *before* the
  `CURRENT` pointer temp+fsync+`os.replace`+dir-fsync (`projection.py:_activate`);
- startup recovery: `AuditLog.recover_incomplete(projection_repair=...)` before
  traffic (`http_api.py:185-199`), plus `diagnose_projection` /
  `recover_projection` classifying CURRENT/STALE/TORN/CORRUPT/MISSING.

What was **missing** (the whole-set durability half of the residual):

1. **Staged-backup file durability** (`backup.py`). `backup_protected_artifacts`
   staged the CURRENT pointer and generation files with `shutil.copyfile` /
   `copytree` and fsynced only the stage *directory*. A directory fsync
   persists directory entries, never file contents — a power loss after the
   activating rename could leave a "verified" backup whose bytes never reached
   disk. **Fix:** `_fsync_tree(stage)` (every staged file, incl. the SQLite
   `-shm`/`-wal` companions) before the stage directory fsync and the
   activating rename. Pinned by
   `test_protected_backup_fsyncs_every_staged_file_before_activation`, which
   records a single ordered fsync/replace event log and asserts every staged
   file is fsynced *before* the activation. RED pre-fix (exit 1).
2. **Backup activation destroyed the old backup before the swap** (`backup.py`,
   found by the crash probe, not by reading code). The old code did
   `shutil.rmtree(dest)` *before* `os.replace(stage, dest)`; the
   mid-staging kill probe showed the pre-existing backup **gone** afterwards
   (`_tree_digest(dest) == {}`). `os.replace` cannot supersede a non-empty
   directory atomically, so activation is now two-phase: old backup renamed
   aside to `.<name>.old-<pid>` (whole and recoverable), stage renamed in,
   trash removed last. A crash in the window leaves the old backup as the
   trash sibling; `recover_backup_activation(dest)` converges it
   (`restored` / `cleaned` / `intact` / `absent`, ambiguous multi-trash fails
   closed with `BackupError`). Dead writers' stage residue
   (`.<name>.tmp-<pid>`) is reaped pid-liveness-checked at the next backup.
3. **Orphaned writer temps were never swept.** A killed writer leaves
   `*.tmp-<pid>` residue that no reader can serve (temps are never named by a
   manifest) but that accumulated forever. `sweep_orphaned_write_temps` removes
   temps whose owner pid is dead (never a live writer's) and runs at the head
   of `recover_projection`, reported on `RecoveryReport.swept_temps`.

## Crash-injection probe matrix (real kills, not mocks)

`tests/_crash_child.py` runs the genuine publish/backup path in a subprocess
and dies with `os._exit(137)` (SIGKILL signature: no atexit, no cleanup) at
each critical point. The parent reopens the store/projection exactly like a
post-crash restart. Every probe asserts `returncode == 137` — the crash is
real — and no mock stands in for the write path.

| Probe | Kill point | Post-crash state | Recovery |
|---|---|---|---|
| baseline | clean exit 0 | CURRENT | — |
| mid-artifact-write | after 1st gen-2 artifact rename, before the 2nd | old generation serves, every artifact hash-matches its manifest, tmp residue on disk, STALE | `recover_projection` sweeps residue, rebuilds gen 2 with the new content, CURRENT |
| partial-tmp | mid-write of `graph.json`: half the bytes on the real temp path | previous generation byte-intact; the half-written temp is never reachable | swept; converges CURRENT |
| before-pointer-rename | store CAS committed, CURRENT rename never ran | old generation fully servable, STALE | re-points CURRENT at the already-durable gen 2; new content served |
| after-pointer-rename | after CURRENT rename, before dir fsync | CURRENT (rename is atomic; generation was fsynced before activation) | no-op (`repaired is False`) |
| mid-backup-staging | during staging, before any activation step | pre-existing backup **byte-identical**; stage residue inert | retry reaps residue and verifies |
| mid-backup-activation | between old-backup rename-aside and stage rename | dest absent, old backup whole as trash sibling | `recover_backup_activation` → `restored`, byte-identical |
| lost generation dir entry | power-loss equivalent of an unfsynced parent dir entry (generation dir deleted) | TORN | deterministic rebuild restores the whole set **bit-identical** (same content hash, same bytes for all 7 artifacts) |

## Known boundary (reported to coordinator, not forced)

`projection.py:_build_generation` fsyncs the generation directory but not its
**parent** (`generations/`): on a power loss immediately after a *first-ever*
publish of a brand-new content hash, the generation's directory entry itself
can be lost, so restart diagnoses TORN and deterministically rebuilds
bit-identically (proven by the last probe). The recovery guarantee therefore
holds end-to-end, but durability-per-publish of the parent entry is a one-line
hardening (`os.fsync` of `generations/` after `mkdir`) inside
`projection.py`, which is outside SUB-29 scope per the work rules. Flagged for
the coordinator; it does not block the row-23 closure because the residual
names *recovery* explicitly and the system never serves a partial or corrupt
set in that window — it serves the previous generation or nothing.

## Concurrency note

`sweep_orphaned_write_temps` and the stage-residue reaper skip any temp whose
pid is alive (`os.kill(pid, 0)`; `PermissionError` counts as alive), so a
concurrent in-flight writer is never clobbered; recovery itself runs under the
single-writer rebuild lock window (startup, before traffic).
