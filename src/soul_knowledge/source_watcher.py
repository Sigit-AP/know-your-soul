"""Deterministic doc-change watcher (matrix adapter A2, rows 11-12).

Stock Graphify's watch mode only sets a ``needs_update`` flag for document
changes. The product watcher instead detects content-hash changes against a
persisted, root-anchored snapshot and *runs the deterministic update
pipeline* (re-import + rebuild) through the ``on_change`` callback — there is
no flag-only path.

Detection is content-hashed, never stat-based: an mtime-only touch produces
no change. The snapshot is anchored to the resolved root; a snapshot written
for another root fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

__all__ = ["ChangeKind", "SourceChange", "SourceWatcher", "WatcherStateError"]


class ChangeKind(StrEnum):
    ADDED = "added"
    CHANGED = "changed"
    REMOVED = "removed"


class SourceChange(BaseModel):
    """One deterministic content-hash change under the watched root."""

    model_config = ConfigDict(frozen=True)

    path: str
    kind: ChangeKind
    content_sha256: str


class WatcherStateError(RuntimeError):
    """The watcher snapshot is missing, unreadable or anchored elsewhere."""


class SourceWatcher:
    """Polls a corpus root for content-hash changes and drives updates."""

    def __init__(
        self,
        root: str | Path,
        *,
        state_path: str | Path | None = None,
        patterns: tuple[str, ...] = (".md",),
        on_change: Callable[[list[SourceChange]], object] | None = None,
    ) -> None:
        self._root = Path(root).resolve()
        self._state_path = Path(state_path) if state_path is not None else self._root / ".soul-watch.json"
        self._patterns = patterns
        self._on_change = on_change

    def snapshot(self) -> dict[str, str]:
        """The current content-hash snapshot (relative path -> sha256)."""
        current: dict[str, str] = {}
        for path in sorted(self._root.rglob("*"), key=lambda item: item.as_posix()):
            if not file_is_candidate(path, self._patterns):
                continue
            relative = path.relative_to(self._root).as_posix()
            current[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
        return current

    def poll(self) -> list[SourceChange]:
        """Diff the filesystem against the snapshot; run the update pipeline.

        Returns the changes in path order. When anything changed, the new
        snapshot is persisted atomically *before* ``on_change`` runs, so a
        crash during the update replays from a truthful baseline.
        """
        current = self.snapshot()
        previous: dict[str, str] = {}
        if self._state_path.exists():
            try:
                stored = json.loads(self._state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise WatcherStateError(f"unreadable watcher state {self._state_path}: {error}") from error
            if stored.get("root") != str(self._root):
                raise WatcherStateError(f"watcher state is anchored to {stored.get('root')!r}, not {str(self._root)!r}")
            previous = {str(key): str(value) for key, value in dict(stored.get("files", {})).items()}

        changes: list[SourceChange] = []
        for relative in sorted(set(previous) | set(current)):
            old, new = previous.get(relative), current.get(relative)
            if old is None and new is not None:
                changes.append(SourceChange(path=relative, kind=ChangeKind.ADDED, content_sha256=new))
            elif new is None and old is not None:
                changes.append(SourceChange(path=relative, kind=ChangeKind.REMOVED, content_sha256=old))
            elif old != new and new is not None:
                changes.append(SourceChange(path=relative, kind=ChangeKind.CHANGED, content_sha256=new))

        if changes:
            self._persist(current)
            if self._on_change is not None:
                self._on_change(changes)
        return changes

    def _persist(self, files: dict[str, str]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_path.with_name(f"{self._state_path.name}.tmp-{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"root": str(self._root), "files": files}, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._state_path)


def file_is_candidate(path: Path, patterns: tuple[str, ...]) -> bool:
    """A watched file: a regular file whose suffix is in *patterns*."""
    return path.is_file() and path.suffix in patterns
