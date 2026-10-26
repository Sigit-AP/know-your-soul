"""Matrix row 23 residual: whole-set durability and power-loss recovery (SUB-29).

Two RED-first fix tests pin the new guarantees in ``know_your_soul.backup``:

1. Every staged file of a protected-set backup is fsynced *before* the single
   ``os.replace`` that activates the backup directory (a directory fsync
   persists names, never file contents).
2. Crash recovery sweeps orphaned writer temp files (``*.tmp-<pid>``) left by
   a dead writer, while never touching a live writer's temp.

The remaining tests are real crash-injection probes: a child process
(``tests/_crash_child.py``) runs the genuine publish/backup path and dies
with ``os._exit(137)`` at a named critical point. The parent reopens the
store and projection exactly like a post-crash restart and asserts that no
partial artifact is ever served and that recovery converges to the last
consistent (or the bit-identically rebuilt) state.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from know_your_soul.backup import (
    ProjectionState,
    backup_protected_artifacts,
    diagnose_projection,
    recover_backup_activation,
    recover_projection,
    sweep_orphaned_write_temps,
)
from know_your_soul.domain import PrimaryDomain
from know_your_soul.importer import SeedImporter
from know_your_soul.projection import ARTIFACT_NAMES, GraphProjector
from know_your_soul.store import EventStore

ALPHA = b"# Alpha Lessons\n\n## First Lesson\n\nAlways roll the database back before retrying.\n"
BETA = b"# Beta\n\n## Counterexample\n\nA rollback that was itself rolled back.\n"

CHILD = Path(__file__).parent / "_crash_child.py"
KILLED = 137  # the child's os._exit signature: proves the crash really happened


def _run_child(workdir: Path, point: str, extra: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHILD), str(workdir), point, extra],
        capture_output=True,
        text=True,
        timeout=180,
    )


def _reopen(tmp_path: Path) -> tuple[EventStore, GraphProjector]:
    """Post-crash restart: fresh connections over the crashed process's files."""
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    return store, projector


def _populated(tmp_path: Path) -> tuple[EventStore, GraphProjector]:
    store = EventStore(tmp_path / "soul.sqlite3")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    SeedImporter(store, state_dir=tmp_path / "import-state").import_directory(corpus, domain=PrimaryDomain.P1)
    return store, GraphProjector(store, output_root=tmp_path / "out")


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid  # reaped: guaranteed dead at assert time


def _assert_generation_intact(projector: GraphProjector) -> Path:
    """Every manifest-declared artifact of the active generation exists and hash-matches."""
    generation = projector.active_generation_dir()
    assert generation is not None
    manifest = projector.active_manifest()
    assert manifest is not None
    for name, declared in sorted(manifest["artifacts"].items()):
        artifact = generation / name
        assert artifact.is_file(), f"activated artifact {name} is missing"
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        assert digest == declared, f"activated artifact {name} does not match its manifest hash"
    return generation


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ------------------------------------------------------- RED-first fix tests


def test_protected_backup_fsyncs_every_staged_file_before_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory fsync persists names, not contents: every staged file of the
    protected set must be fsynced before the one ``os.replace`` that activates
    the backup, or a power loss can leave a verified-receipt backup whose bytes
    never reached disk."""
    store, projector = _populated(tmp_path)
    projector.rebuild()
    # Resolve fsynced fds by (st_dev, st_ino) instead of /proc/self/fd — fstat
    # is portable (Linux + macOS); a rename preserves the inode, so a staged
    # file fsynced before the activating os.replace matches the dest file's
    # inode afterwards.
    events: list[tuple[str, tuple[int, int] | str]] = []  # ordered ("fsync", (dev, ino)) | ("replace", path)
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(fd: int) -> None:
        try:
            info = os.fstat(fd)
        except OSError:
            events.append(("fsync", f"fd:{fd}"))
        else:
            events.append(("fsync", (info.st_dev, info.st_ino)))
        real_fsync(fd)

    def recording_replace(src: object, dst: object) -> None:
        events.append(("replace", str(dst)))
        real_replace(str(src), str(dst))

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)
    dest = tmp_path / "backups" / "set-1"
    receipt = backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=dest)
    assert receipt.verified
    # Staged files are fsynced under the stage directory (`.<dest>.tmp-<pid>`);
    # the activating rename then moves them to `dest` unchanged.
    staged_files = [path for path in dest.rglob("*") if path.is_file()]
    relatives = [str(path.relative_to(dest)) for path in staged_files]
    assert {"soul.db", "CURRENT"} <= set(relatives) and any(rel.startswith("generation/") for rel in relatives)
    activation = events.index(("replace", str(dest)))
    for path, rel in zip(staged_files, relatives, strict=True):
        info = path.stat()
        hits = [
            index for index, (kind, key) in enumerate(events) if kind == "fsync" and key == (info.st_dev, info.st_ino)
        ]
        assert hits, f"staged file {rel} was never fsynced"
        assert max(hits) < activation, f"staged file {rel} fsynced only after the activating rename"
    store.close()


def test_recovery_sweeps_orphaned_writer_temps(tmp_path: Path) -> None:
    """A crashed writer leaves ``*.tmp-<pid>`` residue; recovery sweeps it and
    reports the swept paths on the receipt."""
    store, projector = _populated(tmp_path)
    projector.rebuild()
    output_root = tmp_path / "out"
    generation = projector.active_generation_dir()
    assert generation is not None
    dead = _dead_pid()
    orphans = [
        output_root / f"CURRENT.tmp-{dead}",
        generation / f"graph.json.tmp-{dead}",
    ]
    for orphan in orphans:
        orphan.write_bytes(b"torn-residue")
    report = recover_projection(store, projector)
    assert all(not orphan.exists() for orphan in orphans)
    assert {Path(path).name for path in report.swept_temps} == {orphan.name for orphan in orphans}
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    store.close()


def test_sweep_never_touches_a_live_writers_temp(tmp_path: Path) -> None:
    store, projector = _populated(tmp_path)
    projector.rebuild()
    output_root = tmp_path / "out"
    live = output_root / f"graph.json.tmp-{os.getpid()}"  # this very process is alive
    dead = output_root / f"graph.json.tmp-{_dead_pid()}"
    live.write_bytes(b"live writer in flight")
    dead.write_bytes(b"crash residue")
    swept = sweep_orphaned_write_temps(output_root)
    assert live.read_bytes() == b"live writer in flight"
    assert not dead.exists()
    assert [path.name for path in swept] == [dead.name]
    live.unlink()
    store.close()


# ------------------------------------------------- crash-injection probes


def test_probe_baseline_child_publishes_cleanly(tmp_path: Path) -> None:
    result = _run_child(tmp_path, "baseline")
    assert result.returncode == 0, result.stderr
    store, projector = _reopen(tmp_path)
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    _assert_generation_intact(projector)
    store.close()


def test_crash_mid_artifact_write_serves_only_the_previous_generation(tmp_path: Path) -> None:
    """Killed after two artifact renames of generation 2: the previous
    generation stays active and byte-intact; recovery converges to the new
    watermark and sweeps the crash residue."""
    result = _run_child(tmp_path, "mid-artifact-write")
    assert result.returncode == KILLED, f"child did not crash: {result.returncode}\n{result.stderr}"
    store, projector = _reopen(tmp_path)
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.STALE  # store ahead, old generation active
    old_generation = _assert_generation_intact(projector)  # old generation fully servable
    graph = json.loads((old_generation / "graph.json").read_text(encoding="utf-8"))
    assert not any("gamma" in str(node.get("source_file", "")) for node in graph.get("nodes", []))
    residue = list((tmp_path / "out" / "generations").glob("*/*.tmp-*"))
    assert residue, "the crashed writer's temp residue should be on disk before recovery"
    report = recover_projection(store, projector)
    assert report.receipt.repaired
    assert not any(path.exists() for path in residue)
    assert {path.name for path in residue} <= {Path(p).name for p in report.swept_temps}
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    new_generation = _assert_generation_intact(projector)
    assert new_generation != old_generation
    graph = json.loads((new_generation / "graph.json").read_text(encoding="utf-8"))
    assert any("gamma" in str(node.get("source_file", "")) for node in graph.get("nodes", []))
    store.close()


def test_crash_between_cas_and_pointer_swap_recovers_forward(tmp_path: Path) -> None:
    """Killed after the store CAS committed but before the CURRENT rename:
    the old generation keeps serving; recovery re-points CURRENT at the
    already-durable new generation."""
    result = _run_child(tmp_path, "before-pointer-rename")
    assert result.returncode == KILLED, f"child did not crash: {result.returncode}\n{result.stderr}"
    store, projector = _reopen(tmp_path)
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.STALE, diagnosis
    old_generation = _assert_generation_intact(projector)  # old generation fully servable
    graph = json.loads((old_generation / "graph.json").read_text(encoding="utf-8"))
    assert not any("gamma" in str(node.get("source_file", "")) for node in graph.get("nodes", []))
    report = recover_projection(store, projector)
    assert report.receipt.repaired
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    new_generation = _assert_generation_intact(projector)
    assert new_generation != old_generation
    graph = json.loads((new_generation / "graph.json").read_text(encoding="utf-8"))
    assert any("gamma" in str(node.get("source_file", "")) for node in graph.get("nodes", []))
    store.close()


def test_crash_after_pointer_rename_serves_the_complete_new_generation(tmp_path: Path) -> None:
    """Killed between the CURRENT rename and the directory fsync: the rename is
    atomic, so the pointer names the new generation, which was fully fsynced
    before activation — recovery is a no-op."""
    result = _run_child(tmp_path, "after-pointer-rename")
    assert result.returncode == KILLED, f"child did not crash: {result.returncode}\n{result.stderr}"
    store, projector = _reopen(tmp_path)
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    generation = _assert_generation_intact(projector)
    graph = json.loads((generation / "graph.json").read_text(encoding="utf-8"))
    assert any("gamma" in str(node.get("source_file", "")) for node in graph.get("nodes", []))
    report = recover_projection(store, projector)
    assert not report.receipt.repaired  # nothing to do
    store.close()


def test_crash_leaving_half_written_temp_never_serves_partial_artifact(tmp_path: Path) -> None:
    """Killed mid-write of ``graph.json``: the destination keeps the previous
    bytes (the rename never happened), the half-written temp is residue that
    recovery sweeps, and the reader never sees partial content."""
    result = _run_child(tmp_path, "partial-tmp")
    assert result.returncode == KILLED, f"child did not crash: {result.returncode}\n{result.stderr}"
    store, projector = _reopen(tmp_path)
    generation = _assert_generation_intact(projector)  # previous generation intact
    graph = json.loads((generation / "graph.json").read_text(encoding="utf-8"))
    assert not any("gamma" in str(node.get("source_file", "")) for node in graph.get("nodes", []))
    temps = list((tmp_path / "out" / "generations").glob("*/graph.json.tmp-*"))
    assert temps, "the half-written crash residue should be on disk before recovery"
    assert 0 < temps[0].stat().st_size  # torn: some bytes landed
    report = recover_projection(store, projector)
    assert not temps[0].exists()
    assert any(path.endswith(temps[0].name) for path in report.swept_temps)
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    store.close()


def test_crash_mid_backup_staging_leaves_destination_absent_or_intact(tmp_path: Path) -> None:
    """Killed while staging a protected-set backup: the activating rename never
    happens, so a pre-existing destination is byte-identical afterwards, the
    stage residue is inert, and a retry converges."""
    result = _run_child(tmp_path, "baseline")
    assert result.returncode == 0, result.stderr
    store, projector = _reopen(tmp_path)
    dest = tmp_path / "backups" / "set-1"
    first = backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=dest)
    assert first.verified
    before = _tree_digest(dest)
    store.close()
    result = _run_child(tmp_path, "mid-backup-staging", str(dest))
    assert result.returncode == KILLED, f"child did not crash: {result.returncode}\n{result.stderr}"
    assert _tree_digest(dest) == before  # previous backup byte-identical
    assert list(dest.parent.glob(f".{dest.name}.tmp-*")), "stage residue expected after the kill"
    store, projector = _reopen(tmp_path)
    retried = backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=dest)
    assert retried.verified  # retry cleans the residue and converges
    assert not list(dest.parent.glob(f".{dest.name}.tmp-*"))
    store.close()


def test_crash_mid_backup_activation_restores_the_previous_backup(tmp_path: Path) -> None:
    """Killed in the narrow window between renaming the old backup aside and
    activating the new one: the destination is absent, the trash sibling holds
    the old backup whole, and recovery rolls it back byte-identically."""
    result = _run_child(tmp_path, "baseline")
    assert result.returncode == 0, result.stderr
    store, projector = _reopen(tmp_path)
    dest = tmp_path / "backups" / "set-1"
    first = backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=dest)
    assert first.verified
    before = _tree_digest(dest)
    store.close()
    result = _run_child(tmp_path, "mid-backup-activation", str(dest))
    assert result.returncode == KILLED, f"child did not crash: {result.returncode}\n{result.stderr}"
    assert not dest.exists()  # the crash window: new backup never activated
    trashes = list(dest.parent.glob(f".{dest.name}.old-*"))
    assert len(trashes) == 1, "the old backup must survive as exactly one trash sibling"
    assert recover_backup_activation(dest) == "restored"
    assert _tree_digest(dest) == before  # rolled back byte-identically
    store, projector = _reopen(tmp_path)
    retried = backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=dest)
    assert retried.verified
    store.close()


def test_recover_backup_activation_states(tmp_path: Path) -> None:
    """The four convergent outcomes of backup-activation recovery."""
    dest = tmp_path / "backups" / "set-1"
    dest.parent.mkdir(parents=True)
    assert recover_backup_activation(dest) == "absent"
    dest.mkdir()
    (dest / "soul.db").write_bytes(b"verified-backup")
    assert recover_backup_activation(dest) == "intact"
    trash = dest.with_name(f".{dest.name}.old-{_dead_pid()}")
    trash.mkdir()
    (trash / "soul.db").write_bytes(b"older")
    assert recover_backup_activation(dest) == "cleaned"  # activation had completed
    assert not trash.exists() and dest.is_dir()
    dest.rename(trash)  # simulate the crash window: dest absent, trash whole
    assert recover_backup_activation(dest) == "restored"
    assert (dest / "soul.db").read_bytes() == b"verified-backup"


def test_lost_generation_directory_entry_rebuilds_bit_identical(tmp_path: Path) -> None:
    """Power-loss equivalent of an unfsynced parent directory entry: the
    generation directory the CURRENT pointer names is gone. Diagnosis is
    TORN, and the deterministic rebuild restores the whole set bit for bit."""
    result = _run_child(tmp_path, "baseline")
    assert result.returncode == 0, result.stderr
    store, projector = _reopen(tmp_path)
    generation = _assert_generation_intact(projector)
    before = {name: (generation / name).read_bytes() for name in ARTIFACT_NAMES}
    content_hash = generation.name
    for artifact in generation.iterdir():
        artifact.unlink()
    generation.rmdir()
    assert diagnose_projection(store, projector).state is ProjectionState.TORN
    report = recover_projection(store, projector)
    assert report.receipt.repaired
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    rebuilt = projector.active_generation_dir()
    assert rebuilt is not None and rebuilt.name == content_hash  # content-addressed: same identity
    for name in ARTIFACT_NAMES:
        assert (rebuilt / name).read_bytes() == before[name], f"{name} not bit-identical after recovery"
    store.close()
