"""Atomic SQLite backup/restore and projection recovery helpers.

Task 9 core of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 13.3 (verified atomic backup) and section 7.4 (stale/torn
projection repair).

Backups use the SQLite online backup API into a same-directory temporary
file, verify the copy (``quick_check`` plus the event-count and chain-head
anchor), then ``os.replace`` it into place and fsync file and directory — a
crash can never leave a half-written backup at the destination path. Restore
is the mirror: it refuses to overwrite an existing database unless told to,
copies atomically, and reports the restored chain head.

The projection helpers classify the active generation as CURRENT / STALE /
TORN / CORRUPT / MISSING and drive :meth:`GraphProjector.repair_if_stale`,
composing with :meth:`AuditLog.recover_incomplete` so crash recovery repairs
the projection before closing stale journals.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from soul_knowledge.store import EventStore

if TYPE_CHECKING:
    from soul_knowledge.audit import AuditLog
    from soul_knowledge.events import AuditReceipt
    from soul_knowledge.projection import GraphProjector, ProjectionReceipt

__all__ = [
    "BackupError",
    "BackupReceipt",
    "ProjectionDiagnosis",
    "ProjectionState",
    "RecoveryReport",
    "backup_store",
    "diagnose_projection",
    "inspect_backup",
    "recover_projection",
    "restore_store",
]


class BackupError(RuntimeError):
    """A backup or restore contract violation."""


@dataclass(frozen=True)
class BackupReceipt:
    """The verified identity of one backup (or restored) database file."""

    source_path: str
    backup_path: str
    event_count: int
    chain_head_hash: str | None
    size_bytes: int
    sha256: str


class ProjectionState(StrEnum):
    """The classified state of the active projection generation."""

    CURRENT = "current"
    STALE = "stale"  # intact, but behind the store or disagrees with its pointer
    TORN = "torn"  # activated but artifact files are missing/unreadable
    CORRUPT = "corrupt"  # artifact bytes no longer match the manifest hashes
    MISSING = "missing"  # no CURRENT pointer at all


@dataclass(frozen=True)
class ProjectionDiagnosis:
    """The classified projection state plus the watermarks compared."""

    state: ProjectionState
    detail: str
    manifest_watermark: int | None = None
    store_watermark: int = 0
    projection_version: str | None = None


@dataclass(frozen=True)
class RecoveryReport:
    """The result of one projection recovery pass."""

    receipt: ProjectionReceipt
    audit_receipts: tuple[AuditReceipt, ...] = field(default_factory=tuple)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _anchor(conn: sqlite3.Connection) -> tuple[int, str | None]:
    count = int(conn.execute("SELECT COUNT(*) FROM event_log").fetchone()[0])
    row = conn.execute("SELECT value FROM system_state WHERE key = 'chain_head_hash'").fetchone()
    return count, (None if row is None else str(row[0]))


def _verify_copy(conn: sqlite3.Connection, *, expected_events: int, expected_head: str | None) -> None:
    quick = conn.execute("PRAGMA quick_check").fetchone()
    if quick is None or str(quick[0]) != "ok":
        raise BackupError(f"backup copy failed quick_check: {None if quick is None else quick[0]}")
    count, head = _anchor(conn)
    if count != expected_events:
        raise BackupError(f"backup copy holds {count} events, expected {expected_events}")
    if head != expected_head:
        raise BackupError("backup copy chain head does not match the source anchor")


def _copy_into(store_conn: sqlite3.Connection, dest: Path) -> BackupReceipt:
    """Backup the live connection into *dest* atomically; verify and fsync."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
    try:
        dest_conn = sqlite3.connect(str(tmp))
        try:
            store_conn.backup(dest_conn)
            expected_events, expected_head = _anchor(store_conn)
            _verify_copy(dest_conn, expected_events=expected_events, expected_head=expected_head)
        finally:
            dest_conn.close()
        os.replace(tmp, dest)
        fd = os.open(dest, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(dest.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    count, head = _anchor_readonly(dest)
    return BackupReceipt(
        source_path="",
        backup_path=str(dest),
        event_count=count,
        chain_head_hash=head,
        size_bytes=dest.stat().st_size,
        sha256=_sha256(dest),
    )


def _anchor_readonly(path: Path) -> tuple[int, str | None]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return _anchor(conn)
    finally:
        conn.close()


def backup_store(store: EventStore, dest_path: str | Path) -> BackupReceipt:
    """Verified, atomic online backup of the store to *dest_path*."""
    dest = Path(dest_path)
    source = store.db_path
    with store.read() as conn:
        receipt = _copy_into(conn, dest)
    return BackupReceipt(
        source_path=source,
        backup_path=receipt.backup_path,
        event_count=receipt.event_count,
        chain_head_hash=receipt.chain_head_hash,
        size_bytes=receipt.size_bytes,
        sha256=receipt.sha256,
    )


def inspect_backup(backup_path: str | Path) -> dict[str, Any]:
    """Integrity report for one backup file (read-only, no WAL side effects)."""
    path = Path(backup_path)
    if not path.is_file():
        raise BackupError(f"backup {str(path)!r} does not exist")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        quick = conn.execute("PRAGMA quick_check").fetchone()
        ok = quick is not None and str(quick[0]) == "ok"
        try:
            count, head = _anchor(conn)
        except sqlite3.DatabaseError:
            count, head, ok = 0, None, False
    finally:
        conn.close()
    return {
        "ok": ok,
        "path": str(path),
        "event_count": count,
        "chain_head_hash": head,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def restore_store(backup_path: str | Path, dest_path: str | Path, *, overwrite: bool = False) -> BackupReceipt:
    """Restore a verified backup to *dest_path* atomically.

    Refuses to overwrite an existing non-empty database unless *overwrite* is
    set; restores always land through a same-directory temp file.
    """
    backup = Path(backup_path)
    dest = Path(dest_path)
    info = inspect_backup(backup)
    if not info["ok"]:
        raise BackupError(f"refusing to restore an unverified backup {str(backup)!r}")
    if dest.exists() and dest.stat().st_size > 0 and not overwrite:
        raise BackupError(f"restore destination {str(dest)!r} exists; pass overwrite=True to replace it")
    source = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
    try:
        receipt = _copy_into(source, dest)
    finally:
        source.close()
    return BackupReceipt(
        source_path=str(backup),
        backup_path=receipt.backup_path,
        event_count=receipt.event_count,
        chain_head_hash=receipt.chain_head_hash,
        size_bytes=receipt.size_bytes,
        sha256=receipt.sha256,
    )


# ----------------------------------------------------- projection recovery


def diagnose_projection(store: EventStore, projector: GraphProjector) -> ProjectionDiagnosis:
    """Classify the active projection: CURRENT, STALE, TORN, CORRUPT, MISSING.

    The projector's public surface (``active_manifest``/``active_generation_dir``)
    plus the generation manifest's own artifact hashes are enough to classify;
    full semantic validation runs inside ``repair_if_stale``.
    """
    output_root = Path(projector._output_root)
    store_watermark = store.knowledge_high_watermark()
    pointer = output_root / "CURRENT"
    generation = projector.active_generation_dir()
    if generation is None:
        if pointer.is_file():
            return ProjectionDiagnosis(
                ProjectionState.TORN, "CURRENT points at a missing or invalid generation", None, store_watermark
            )
        return ProjectionDiagnosis(ProjectionState.MISSING, "no active generation", store_watermark=store_watermark)
    manifest = projector.active_manifest()
    if manifest is None:
        return ProjectionDiagnosis(
            ProjectionState.TORN, "manifest.json is missing or unreadable", None, store_watermark
        )
    manifest_watermark = int(manifest.get("source_knowledge_high_watermark", -1))
    version = str(manifest.get("projection_version"))
    if manifest_watermark != store_watermark:
        return ProjectionDiagnosis(
            ProjectionState.STALE,
            f"projection watermark {manifest_watermark} is behind the store's {store_watermark}",
            manifest_watermark,
            store_watermark,
            version,
        )
    if version != store.projection_version():
        return ProjectionDiagnosis(
            ProjectionState.STALE,
            "the store's projection pointer disagrees with the active manifest",
            manifest_watermark,
            store_watermark,
            version,
        )
    artifacts = manifest.get("artifacts", {})
    if isinstance(artifacts, dict):
        for name in sorted(artifacts):
            artifact = generation / str(name)
            if not artifact.is_file():
                return ProjectionDiagnosis(
                    ProjectionState.TORN,
                    f"activated artifact {name} is missing",
                    manifest_watermark,
                    store_watermark,
                    version,
                )
            if _sha256(artifact) != str(artifacts[name]):
                return ProjectionDiagnosis(
                    ProjectionState.CORRUPT,
                    f"activated artifact {name} no longer matches its manifest hash",
                    manifest_watermark,
                    store_watermark,
                    version,
                )
    return ProjectionDiagnosis(
        ProjectionState.CURRENT, "projection matches the store", manifest_watermark, store_watermark, version
    )


def recover_projection(
    store: EventStore,
    projector: GraphProjector,
    *,
    audit_log: AuditLog | None = None,
    stale_after_seconds: float = 300.0,
) -> RecoveryReport:
    """Repair a stale/torn/corrupt projection and (optionally) close stale journals.

    Drives :meth:`GraphProjector.repair_if_stale` — a no-op when the
    projection is already current — then, when an :class:`AuditLog` is given,
    runs crash recovery with the repair hook so stale journals are closed
    after the projection is consistent (spec 7.8). Raises :class:`BackupError`
    if the projection is not CURRENT after the repair.
    """
    receipt = projector.repair_if_stale()
    audit_receipts: tuple[AuditReceipt, ...] = ()
    if audit_log is not None:

        def repair_hook() -> None:
            projector.repair_if_stale()

        audit_receipts = tuple(
            audit_log.recover_incomplete(
                projection_repair=repair_hook,
                stale_after_seconds=stale_after_seconds,
            )
        )
    diagnosis = diagnose_projection(store, projector)
    if diagnosis.state is not ProjectionState.CURRENT:
        raise BackupError(f"projection recovery left the generation {diagnosis.state}: {diagnosis.detail}")
    return RecoveryReport(receipt=receipt, audit_receipts=audit_receipts)
