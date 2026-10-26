"""Deterministic generation retention and garbage collection (ATM F-D2).

Projection generations accumulate under ``<projection-root>/generations/`` —
one content-addressed directory per rebuild. Retention bounds that growth
without ever endangering recovery:

* the **CURRENT** generation (the live pointer) is never touched;
* **pinned** generations — named in the policy or carrying a ``PINNED``
  marker file — are never touched;
* generations **referenced by any backup** (the generation a backup
  directory's ``CURRENT`` file names) are never touched;
* everything else is kept newest-first (manifest
  ``source_knowledge_high_watermark``, tie-break by name) up to
  ``policy.keep``; the remainder is deleted, oldest first.

Every real deletion is appended as one canonical-JSON line to
``<projection-root>/retention-audit.jsonl`` with a monotonically increasing
sequence number. ``dry_run=True`` computes and reports the identical plan
without deleting anything or writing any audit line.

Fail-closed rules: a generation whose manifest is unreadable is *skipped*,
never deleted (we do not delete what we cannot classify); a symlinked
generation entry is skipped (``rmtree`` must never follow a link out of the
tree); an explicitly passed backup directory that does not exist is a typed
error, not a silent pass. Retention never opens, reads, or writes the SQLite
store — the source of truth is out of scope for GC by construction.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from know_your_soul.domain import canonical_json

__all__ = [
    "DEFAULT_AUDIT_FILENAME",
    "DEFAULT_KEEP",
    "PINNED_MARKER_FILENAME",
    "DeletionRecord",
    "RetentionError",
    "RetentionPolicy",
    "RetentionReport",
    "SkippedGeneration",
    "plan_retention",
    "run_retention",
]

DEFAULT_KEEP = 3
DEFAULT_AUDIT_FILENAME = "retention-audit.jsonl"
PINNED_MARKER_FILENAME = "PINNED"


class RetentionError(RuntimeError):
    """A retention/GC contract violation (typed, fail-closed)."""


@dataclass(frozen=True)
class RetentionPolicy:
    """Keep the *keep* newest deletable generations plus every pinned one."""

    keep: int = DEFAULT_KEEP
    pinned: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.keep < 0:
            raise RetentionError(f"retention keep must be >= 0, got {self.keep}")
        for name in self.pinned:
            if not name or "/" in name or name in {".", ".."}:
                raise RetentionError(f"pinned generation name {name!r} is not a plain directory name")


_DEFAULT_POLICY = RetentionPolicy()


@dataclass(frozen=True)
class DeletionRecord:
    """One deleted (or, in dry-run, would-be-deleted) generation."""

    generation: str
    path: str
    size_bytes: int
    file_count: int
    reason: str


@dataclass(frozen=True)
class SkippedGeneration:
    """A generation retention refused to classify as deletable."""

    generation: str
    reason: str
    # "current" | "pinned" | "pinned-marker" | "backup-referenced"
    # | "unreadable-manifest" | "unsafe-symlink"


@dataclass(frozen=True)
class RetentionReport:
    """The full, deterministic outcome of one retention pass."""

    dry_run: bool
    keep: int
    deleted: tuple[DeletionRecord, ...]
    kept: tuple[str, ...]  # deletable, but inside the keep-N window
    skipped: tuple[SkippedGeneration, ...] = field(default_factory=tuple)
    audit_path: str | None = None


def _fsync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _generation_watermark(generation_dir: Path, name: str) -> int:
    """The manifest watermark; an unreadable manifest is *not* deletable."""
    try:
        manifest = json.loads((generation_dir / "manifest.json").read_text(encoding="utf-8"))
        return int(manifest["source_knowledge_high_watermark"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RetentionError(f"generation {name!r} has an unreadable manifest: {error}") from error


def _measure(generation_dir: Path) -> tuple[int, int]:
    """Total bytes and file count of one generation tree (link-safe walk)."""
    size = 0
    count = 0
    for path in sorted(generation_dir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            size += path.stat().st_size
            count += 1
    return size, count


def _backup_references(backup_dirs: tuple[str | Path, ...]) -> set[str]:
    """Generation names referenced by backups; explicit dirs must exist."""
    referenced: set[str] = set()
    for raw in backup_dirs:
        backup = Path(raw)
        if not backup.is_dir():
            raise RetentionError(f"backup directory {str(backup)!r} does not exist; refusing to GC without it")
        pointer = backup / "CURRENT"
        if pointer.is_file():
            name = pointer.read_text(encoding="utf-8").strip()
            if name:
                referenced.add(name)
    return referenced


def plan_retention(
    projection_root: str | Path,
    policy: RetentionPolicy | None = None,
    *,
    backup_dirs: tuple[str | Path, ...] = (),
) -> tuple[tuple[DeletionRecord, ...], tuple[str, ...], tuple[SkippedGeneration, ...]]:
    """Compute the retention plan without deleting anything or writing audit.

    Returns ``(deletions, kept, skipped)`` in deterministic order: deletions
    oldest-first (watermark asc, name asc), the rest sorted by name.
    """
    if policy is None:
        policy = _DEFAULT_POLICY
    root = Path(projection_root)
    if not root.is_dir():
        raise RetentionError(f"projection root {str(root)!r} does not exist or is not a directory")
    generations_dir = root / "generations"
    referenced = _backup_references(tuple(backup_dirs))

    current: str | None = None
    pointer = root / "CURRENT"
    if pointer.is_file():
        current = pointer.read_text(encoding="utf-8").strip() or None

    deletions: list[DeletionRecord] = []
    kept: list[str] = []
    skipped: list[SkippedGeneration] = []
    if not generations_dir.is_dir():
        return (), (), ()
    candidates: list[tuple[int, str]] = []  # (watermark, name)
    for entry in sorted(generations_dir.iterdir(), key=lambda item: item.name):
        name = entry.name
        if entry.is_symlink():
            skipped.append(SkippedGeneration(name, "unsafe-symlink"))
            continue
        if not entry.is_dir():
            continue  # stray files are not generations; leave them alone
        if name == current:
            skipped.append(SkippedGeneration(name, "current"))
            continue
        if name in policy.pinned:
            skipped.append(SkippedGeneration(name, "pinned"))
            continue
        if (entry / PINNED_MARKER_FILENAME).is_file():
            skipped.append(SkippedGeneration(name, "pinned-marker"))
            continue
        if name in referenced:
            skipped.append(SkippedGeneration(name, "backup-referenced"))
            continue
        try:
            watermark = _generation_watermark(entry, name)
        except RetentionError:
            skipped.append(SkippedGeneration(name, "unreadable-manifest"))
            continue
        candidates.append((watermark, name))

    # Oldest-first (watermark asc, name asc): the ones beyond the keep window
    # are deleted in this deterministic order; the window itself is kept.
    ranked = sorted(candidates, key=lambda item: (item[0], item[1]))
    over = ranked[: max(0, len(ranked) - policy.keep)]
    over_names = {name for _, name in over}
    kept = [name for _, name in ranked if name not in over_names]
    for _watermark, name in over:
        size, count = _measure(generations_dir / name)
        deletions.append(
            DeletionRecord(
                generation=name,
                path=str(generations_dir / name),
                size_bytes=size,
                file_count=count,
                reason="over-keep",
            )
        )
    return tuple(deletions), tuple(sorted(kept)), tuple(skipped)


def _next_sequence(audit_path: Path) -> int:
    if not audit_path.is_file():
        return 1
    with open(audit_path, encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip()) + 1


def _append_audit(audit_path: Path, events: list[dict[str, object]]) -> None:
    sequence = _next_sequence(audit_path)
    with open(audit_path, "a", encoding="utf-8") as handle:
        for event in events:
            handle.write(canonical_json({**event, "sequence": sequence}) + "\n")
            sequence += 1
        handle.flush()
        os.fsync(handle.fileno())


def run_retention(
    projection_root: str | Path,
    policy: RetentionPolicy | None = None,
    *,
    backup_dirs: tuple[str | Path, ...] = (),
    dry_run: bool = False,
    audit_filename: str = DEFAULT_AUDIT_FILENAME,
) -> RetentionReport:
    """Execute the retention plan (or report it, when *dry_run* is set).

    Deletions run oldest-first; each deleted generation is ``rmtree``'d only
    after the plan classified it as deletable, and one audit event per
    deletion lands in ``<root>/<audit_filename>`` after the tree is gone and
    the directory is fsynced. A dry run performs no deletion and writes no
    audit line.
    """
    if policy is None:
        policy = _DEFAULT_POLICY
    root = Path(projection_root)
    deletions, kept, skipped = plan_retention(root, policy, backup_dirs=tuple(backup_dirs))
    if dry_run:
        return RetentionReport(dry_run=True, keep=policy.keep, deleted=deletions, kept=kept, skipped=skipped)

    events: list[dict[str, object]] = []
    for record in deletions:
        shutil.rmtree(record.path)
        events.append(
            {
                "audit_version": 1,
                "event": "retention.delete",
                "generation": record.generation,
                "path": record.path,
                "size_bytes": record.size_bytes,
                "file_count": record.file_count,
                "reason": record.reason,
                "keep": policy.keep,
            }
        )
    audit_path: str | None = None
    if events:
        _fsync_dir(root / "generations")
        target = root / audit_filename
        _append_audit(target, events)
        audit_path = str(target)
    return RetentionReport(
        dry_run=False,
        keep=policy.keep,
        deleted=deletions,
        kept=kept,
        skipped=skipped,
        audit_path=audit_path,
    )
