"""Hash-based source-version registry and deterministic relearn offers.

Task 2 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 13.3.

Version identity is **content hash**, never mtime. ``observe`` records a
``source_version`` row keyed by a fingerprint over the content hash, the
per-file manifest hash, and the parser/normalization/engine/scoring versions.
``detect_change`` never selects a mode: when the source changes (or a
compatibility version changes) it emits a deterministic :class:`RelearnOffer`,
sets ``RELEARN_PENDING``, and leaves the RESET/ACCUMULATE choice to the
operator. Supersession and deletion are recorded states on retained rows, never
physical deletion.

Every node and evidence item carries the ``source_version_id`` produced here
(see :mod:`soul_knowledge.domain`).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from soul_knowledge.domain import canonical_json, new_id
from soul_knowledge.store import EventStore

__all__ = [
    "RELEARN_PENDING",
    "Compatibility",
    "ManifestDiff",
    "ParserVersions",
    "RelearnOffer",
    "SourceFileEntry",
    "SourceKind",
    "SourceRegistry",
    "SourceState",
    "SourceVersion",
]

RELEARN_PENDING = "RELEARN_PENDING"
_DEFAULT_PARSER_BUFFER = timedelta(days=1)


class SourceKind(StrEnum):
    """The three source kinds the registry understands (spec 13.3)."""

    SOUL_MD = "SOUL_MD"
    SOUL_CORPUS = "SOUL_CORPUS"
    OPERATOR_IMPORT = "OPERATOR_IMPORT"


class SourceState(StrEnum):
    """Retention state of a source version; nothing is ever deleted."""

    CURRENT = "CURRENT"
    SUPERSEDED = "SUPERSEDED"
    DELETED = "DELETED"


class ParserVersions(BaseModel):
    """The version set that changes the meaning of already-imported nodes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parser_version: str
    normalization_version: str
    engine_version: str
    scoring_contract_version: str


class SourceFileEntry(BaseModel):
    """One file inside a source version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str
    byte_length: int
    line_count: int


class SourceVersion(BaseModel):
    """An immutable, hash-identified snapshot of a source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_version_id: str
    source_id: SourceKind
    source_uri: str
    content_sha256: str
    manifest_hash: str
    fingerprint: str
    byte_length: int
    char_length: int
    line_count: int
    file_count: int
    observed_at: datetime
    parser_version: str
    normalization_version: str
    engine_version: str
    scoring_contract_version: str
    source_state: SourceState
    superseded_by: str | None = None
    file_manifest: tuple[SourceFileEntry, ...] = ()


class Compatibility(BaseModel):
    """Which compatibility versions changed relative to the previous version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    parser_version_changed: bool = False
    normalization_version_changed: bool = False
    scoring_contract_version_changed: bool = False
    engine_version_changed: bool = False


class ManifestDiff(BaseModel):
    """The deterministic file-level diff between two source versions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    added: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    unchanged_count: int = 0


class RelearnOffer(BaseModel):
    """A deterministic offer to relearn; it never selects a mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    offer_id: str = Field(default_factory=new_id)
    detected_at: datetime
    previous_source_version_id: str | None
    current_source_version_id: str
    compatibility: Compatibility = Field(default_factory=Compatibility)
    diff: ManifestDiff = Field(default_factory=ManifestDiff)
    reset_preview: Mapping[str, Any] = Field(default_factory=dict)
    accumulate_preview: Mapping[str, Any] = Field(default_factory=dict)
    mode_options: tuple[Literal["RESET", "ACCUMULATE"], ...] = ("RESET", "ACCUMULATE")
    default_action: Literal["NONE"] = "NONE"
    requires_operator_decision: bool = True
    selected_mode: Literal["RESET", "ACCUMULATE"] | None = None
    offer_expires_at: datetime | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class SourceRegistry:
    """Reads the filesystem, records source versions and emits relearn offers."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    # -------------------------------------------------------------- observing

    def _scan(self, path: Path, kind: SourceKind) -> tuple[list[SourceFileEntry], bytes, int]:
        """Return per-file entries (sorted by path), concatenated bytes and char length."""
        if kind is SourceKind.SOUL_CORPUS or path.is_dir():
            targets = sorted((item for item in path.rglob("*") if item.is_file()), key=lambda item: item.as_posix())
            entries_root = path
        else:
            targets = [path]
            entries_root = path.parent
        entries: list[SourceFileEntry] = []
        concatenated = bytearray()
        char_length = 0
        for target in targets:
            raw = target.read_bytes()
            text = raw.decode("utf-8")
            entries.append(
                SourceFileEntry(
                    path=target.relative_to(entries_root).as_posix(),
                    sha256=_sha256(raw),
                    byte_length=len(raw),
                    line_count=len(text.splitlines()),
                )
            )
            concatenated.extend(raw)
            char_length += len(text)
        return entries, bytes(concatenated), char_length

    def _fingerprint(
        self,
        kind: SourceKind,
        entries: list[SourceFileEntry],
        concatenated: bytes,
        parser_versions: ParserVersions,
    ) -> tuple[str, str, str]:
        """Return ``(content_sha256, manifest_hash, fingerprint)``."""
        manifest = hashlib.sha256()
        for entry in sorted(entries, key=lambda item: item.path):
            manifest.update(f"{entry.path}\0{entry.sha256}\n".encode())
        manifest_hash = manifest.hexdigest()
        content_sha256 = _sha256(concatenated)
        fingerprint = _sha256(
            canonical_json(
                {
                    "source_id": kind.value,
                    "content_sha256": content_sha256,
                    "manifest_hash": manifest_hash,
                    "parser_version": parser_versions.parser_version,
                    "normalization_version": parser_versions.normalization_version,
                    "engine_version": parser_versions.engine_version,
                    "scoring_contract_version": parser_versions.scoring_contract_version,
                }
            ).encode()
        )
        return content_sha256, manifest_hash, fingerprint

    def _build(self, path: Path, kind: SourceKind, parser_versions: ParserVersions) -> SourceVersion:
        """Compute (but do not persist) the hash-identified version of *path*."""
        entries, concatenated, char_length = self._scan(path, kind)
        content_sha256, manifest_hash, fingerprint = self._fingerprint(kind, entries, concatenated, parser_versions)
        return SourceVersion(
            source_version_id=f"sv_{fingerprint}",
            source_id=kind,
            source_uri=str(path),
            content_sha256=content_sha256,
            manifest_hash=manifest_hash,
            fingerprint=fingerprint,
            byte_length=sum(entry.byte_length for entry in entries),
            char_length=char_length,
            line_count=sum(entry.line_count for entry in entries),
            file_count=len(entries),
            observed_at=_utcnow(),
            parser_version=parser_versions.parser_version,
            normalization_version=parser_versions.normalization_version,
            engine_version=parser_versions.engine_version,
            scoring_contract_version=parser_versions.scoring_contract_version,
            source_state=SourceState.CURRENT,
            file_manifest=tuple(entries),
        )

    def _insert_version(self, conn: sqlite3.Connection, version: SourceVersion) -> None:
        conn.execute(
            "INSERT OR IGNORE INTO source_version (source_version_id, source_id, source_uri, content_sha256, "
            "manifest_hash, fingerprint, byte_length, char_length, line_count, file_count, observed_at, "
            "parser_version, normalization_version, engine_version, scoring_contract_version, source_state, "
            "superseded_by, manifest_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                version.source_version_id,
                version.source_id.value,
                version.source_uri,
                version.content_sha256,
                version.manifest_hash,
                version.fingerprint,
                version.byte_length,
                version.char_length,
                version.line_count,
                version.file_count,
                version.observed_at.isoformat(),
                version.parser_version,
                version.normalization_version,
                version.engine_version,
                version.scoring_contract_version,
                version.source_state.value,
                version.superseded_by,
                canonical_json([entry.model_dump(mode="json") for entry in version.file_manifest]),
            ),
        )

    def _existing(self, conn: sqlite3.Connection, kind: SourceKind, fingerprint: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM source_version WHERE source_id = ? AND fingerprint = ?",
            (kind.value, fingerprint),
        ).fetchone()

    def observe(self, path: str | Path, kind: SourceKind, parser_versions: ParserVersions) -> SourceVersion:
        """Record (or return) the hash-identified version of *path*."""
        version = self._build(Path(path), kind, parser_versions)
        with self._store.transaction() as conn:
            existing = self._existing(conn, kind, version.fingerprint)
            if existing is not None:
                return _row_to_source_version(existing)
            self._insert_version(conn, version)
        return version

    # ------------------------------------------------------------- retention

    def active(self, source_id: SourceKind | None = None) -> SourceVersion | None:
        """The newest operator-approved ``CURRENT`` version.

        A version with an outstanding ``RELEARN_PENDING`` offer targeting it is
        never returned (spec 13.3: no operator decision means no change), so the
        result is always the operator-approved baseline. Returns ``None`` when
        every ``CURRENT`` version carries a pending offer.
        """
        with self._store.read() as conn:
            row = self._active_row(conn, source_id)
        return None if row is None else _row_to_source_version(row)

    def _active_row(self, conn: sqlite3.Connection, source_id: SourceKind | None) -> sqlite3.Row | None:
        pending_exclusion = (
            "NOT EXISTS (SELECT 1 FROM relearn_offer ro "
            "WHERE ro.current_source_version_id = sv.source_version_id "
            "AND (SELECT value FROM system_state WHERE key = 'relearn_state') = 'RELEARN_PENDING')"
        )
        if source_id is None:
            return conn.execute(
                f"SELECT sv.* FROM source_version sv WHERE sv.source_state = 'CURRENT' AND {pending_exclusion} "
                "ORDER BY sv.rowid DESC LIMIT 1"
            ).fetchone()
        return conn.execute(
            f"SELECT sv.* FROM source_version sv WHERE sv.source_state = 'CURRENT' AND sv.source_id = ? "
            f"AND {pending_exclusion} ORDER BY sv.rowid DESC LIMIT 1",
            (source_id.value,),
        ).fetchone()

    def _latest(self, kind: SourceKind) -> SourceVersion | None:
        """The newest observed ``CURRENT`` version, pending offers included.

        Internal change-detection baseline: ``detect_change`` compares against
        the latest observed fingerprint so re-detecting unchanged content is a
        no-op, while ``active`` stays the operator-approved baseline.
        """
        with self._store.read() as conn:
            row = conn.execute(
                "SELECT * FROM source_version WHERE source_state = 'CURRENT' AND source_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (kind.value,),
            ).fetchone()
        return None if row is None else _row_to_source_version(row)

    def supersede(self, source_version_id: str, superseded_by: str) -> SourceVersion:
        """Mark an old version superseded by a new one (retention, never deletion)."""
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE source_version SET source_state = 'SUPERSEDED', superseded_by = ? WHERE source_version_id = ?",
                (superseded_by, source_version_id),
            )
            row = conn.execute(
                "SELECT * FROM source_version WHERE source_version_id = ?", (source_version_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise KeyError(source_version_id)
        return _row_to_source_version(row)

    def mark_deleted(self, source_version_id: str) -> SourceVersion:
        """Mark a version deleted while retaining the row for audit."""
        with self._store.transaction() as conn:
            conn.execute(
                "UPDATE source_version SET source_state = 'DELETED' WHERE source_version_id = ?",
                (source_version_id,),
            )
            row = conn.execute(
                "SELECT * FROM source_version WHERE source_version_id = ?", (source_version_id,)
            ).fetchone()
        if row is None:  # pragma: no cover - defensive
            raise KeyError(source_version_id)
        return _row_to_source_version(row)

    # --------------------------------------------------------- change detect

    def relearn_state(self) -> str | None:
        """The stored relearn state (``RELEARN_PENDING``) or ``None``."""
        with self._store.read() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = 'relearn_state'").fetchone()
        return None if row is None else str(row["value"])

    def _write_state(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _utcnow().isoformat()),
        )

    def detect_change(
        self,
        path: str | Path,
        kind: SourceKind,
        parser_versions: ParserVersions,
    ) -> RelearnOffer | None:
        """Emit a deterministic relearn offer when the source fingerprint changed."""
        candidate = self._build(Path(path), kind, parser_versions)
        previous = self._latest(kind)
        if previous is None:
            with self._store.transaction() as conn:
                self._insert_version(conn, candidate)
            return None
        if previous.fingerprint == candidate.fingerprint:
            return None

        diff = _diff(previous.file_manifest, candidate.file_manifest)
        compatibility = Compatibility(
            parser_version_changed=previous.parser_version != parser_versions.parser_version,
            normalization_version_changed=previous.normalization_version != parser_versions.normalization_version,
            scoring_contract_version_changed=(
                previous.scoring_contract_version != parser_versions.scoring_contract_version
            ),
            engine_version_changed=previous.engine_version != parser_versions.engine_version,
        )
        detected_at = _utcnow()
        offer = RelearnOffer(
            detected_at=detected_at,
            previous_source_version_id=previous.source_version_id,
            current_source_version_id=candidate.source_version_id,
            compatibility=compatibility,
            diff=diff,
            reset_preview={
                "nodes_created": candidate.file_count,
                "nodes_reused": 0,
                "evidence_carried": 0,
                "evidence_quarantined": 0,
                "scores_recomputed_from_zero": True,
            },
            accumulate_preview={
                "nodes_reused": diff.unchanged_count,
                "nodes_new": len(diff.added),
                "nodes_superseded": len(diff.changed),
                "nodes_contested": 0,
                "evidence_revalidated": 0,
                "evidence_quarantined": 0,
                "unmapped_evidence": 0,
            },
            offer_expires_at=detected_at + _DEFAULT_PARSER_BUFFER,
        )
        # One transaction: the new source version, the offer and RELEARN_PENDING
        # land together, so a crash cannot record a changed source with no offer.
        with self._store.transaction() as conn:
            if self._existing(conn, kind, candidate.fingerprint) is None:
                self._insert_version(conn, candidate)
            conn.execute(
                "INSERT INTO relearn_offer (offer_id, detected_at, previous_source_version_id, "
                "current_source_version_id, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    offer.offer_id,
                    detected_at.isoformat(),
                    offer.previous_source_version_id,
                    offer.current_source_version_id,
                    canonical_json(offer.model_dump(mode="json")),
                ),
            )
            self._write_state(conn, "relearn_state", RELEARN_PENDING)
        return offer


def _diff(previous: tuple[SourceFileEntry, ...], current: tuple[SourceFileEntry, ...]) -> ManifestDiff:
    old = {entry.path: entry.sha256 for entry in previous}
    new = {entry.path: entry.sha256 for entry in current}
    shared = set(old) & set(new)
    return ManifestDiff(
        added=tuple(sorted(set(new) - set(old))),
        changed=tuple(sorted(path for path in shared if old[path] != new[path])),
        removed=tuple(sorted(set(old) - set(new))),
        unchanged_count=len([path for path in shared if old[path] == new[path]]),
    )


def _row_to_source_version(row: sqlite3.Row) -> SourceVersion:
    manifest = [SourceFileEntry.model_validate(item) for item in json.loads(str(row["manifest_json"]))]
    return SourceVersion(
        source_version_id=str(row["source_version_id"]),
        source_id=SourceKind(str(row["source_id"])),
        source_uri=str(row["source_uri"]),
        content_sha256=str(row["content_sha256"]),
        manifest_hash=str(row["manifest_hash"]),
        fingerprint=str(row["fingerprint"]),
        byte_length=int(row["byte_length"]),
        char_length=int(row["char_length"]),
        line_count=int(row["line_count"]),
        file_count=int(row["file_count"]),
        observed_at=datetime.fromisoformat(str(row["observed_at"])),
        parser_version=str(row["parser_version"]),
        normalization_version=str(row["normalization_version"]),
        engine_version=str(row["engine_version"]),
        scoring_contract_version=str(row["scoring_contract_version"]),
        source_state=SourceState(str(row["source_state"])),
        superseded_by=row["superseded_by"],
        file_manifest=tuple(manifest),
    )
