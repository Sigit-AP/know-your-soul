"""Operator-gated RESET/ACCUMULATE relearn with verified backup and rollback.

Task 6 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
frozen contract ``docs/contracts/relearn-contract.md``.

* ``offer`` delegates to the source registry: a content change emits a
  deterministic :class:`~know_your_soul.source_registry.RelearnOffer` and sets
  ``RELEARN_PENDING``. The engine never chooses a mode; without an explicit
  :class:`RelearnApproval` nothing changes.
* ``apply`` first takes a **verified atomic backup** (SQLite through the
  online backup API, the ``CURRENT`` pointer, the generation directory
  hash-verified against the projection manifest, the scoring-contract
  version — staged in a temp directory and activated with one rename via
  :func:`know_your_soul.backup.backup_protected_artifacts`), then applies the
  mode inside a single mutation transaction:

  - RESET quarantines every evidence item of the old versions; the receipt
    states the zero vector (P=M=E=0, U=1, level 1) and promotion is blocked
    until new evidence exists.
  - ACCUMULATE carries only evidence whose anchor path exists in both manifests
    with an identical SHA-256; unmapped evidence is quarantined from scoring.

  The relearn record is a zero-weight ``admin`` domain event
  (``knowledge_affecting=False``): relearn never moves P/M/E and never triggers
  promotion.
* ``rollback`` restores the byte-exact pre-relearn database and generation
  tree, verifies every recorded hash, and appends exactly one linked audit
  event.
* Mixed-source reads are refused with ``MIXED_SOURCE_VERSIONS`` unless
  explicitly approved (``mixed_source_versions=True`` plus operator approval).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from know_your_soul.audit import AuditLog, failure_outcome, success_outcome
from know_your_soul.backup import backup_protected_artifacts
from know_your_soul.domain import Event, EventKind, PrimaryDomain, canonical_json, new_id
from know_your_soul.events import Invocation
from know_your_soul.source_registry import (
    RELEARN_PENDING,
    ParserVersions,
    RelearnOffer,
    SourceKind,
    SourceRegistry,
)
from know_your_soul.store import EventStore, IdempotencyConflict, StoreError

__all__ = [
    "BackupArtifact",
    "BackupManifest",
    "BackupUnavailable",
    "BackupVerificationError",
    "MixedSourceVersions",
    "NoPendingRelearn",
    "OperatorDecisionRequired",
    "RelearnApproval",
    "RelearnEngine",
    "RelearnError",
    "RelearnPreview",
    "RelearnReceipt",
    "RelearnStatus",
    "RollbackReceipt",
    "RollbackVerificationError",
    "UnknownRelearn",
]

MIXED_SOURCE_VERSIONS = "MIXED_SOURCE_VERSIONS"
_RESET_SCORES: dict[str, Any] = {
    "p": "0.000000000000",
    "m": "0.000000000000",
    "e": "0.000000000000",
    "u": "1.000000000000",
    "professional_level": 1,
}


class RelearnError(StoreError):
    """Base class for relearn contract violations."""


class NoPendingRelearn(RelearnError):
    """A preview/apply was requested without a ``RELEARN_PENDING`` offer."""


class OperatorDecisionRequired(RelearnError):
    """RESET/ACCUMULATE are operator-gated; the approval does not match the offer."""


class MixedSourceVersions(RelearnError):
    """A read spans source versions incompatible with the active manifest."""

    def __init__(self, incompatible: list[str]) -> None:
        super().__init__(f"{MIXED_SOURCE_VERSIONS}: incompatible source versions {incompatible}")
        self.error_code = MIXED_SOURCE_VERSIONS
        self.incompatible = tuple(incompatible)


class BackupUnavailable(RelearnError):
    """A verified file backup is impossible (for example an in-memory store)."""


class BackupVerificationError(RelearnError):
    """A staged backup copy does not hash-match its source."""


class RollbackVerificationError(RelearnError):
    """A restored artifact does not hash-match the recorded pre-relearn hash."""


class UnknownRelearn(RelearnError):
    """A rollback was requested for a relearn id with no record or backup."""


class RelearnApproval(BaseModel):
    """The explicit operator decision an apply must present."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str = Field(default_factory=new_id)
    offer_id: str = Field(min_length=1)
    mode: Literal["RESET", "ACCUMULATE"]
    approved_by: str = Field(min_length=1)
    decided_at: datetime


class BackupArtifact(BaseModel):
    """One hash-recorded artifact inside a backup.

    ``generation_file`` paths are relative to the backed-up generation
    directory; ``sqlite``/``current_pointer`` paths are relative to the backup
    directory itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["sqlite", "current_pointer", "generation_file"]
    path: str
    sha256: str
    byte_length: int


class BackupManifest(BaseModel):
    """The verified content of one atomic backup."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relearn_id: str
    created_at: datetime
    artifacts: tuple[BackupArtifact, ...]
    scoring_contract_version: str
    verified: bool


class RelearnPreview(BaseModel):
    """A read-only, deterministic projection of one mode against the pending offer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    offer_id: str
    mode: Literal["RESET", "ACCUMULATE"]
    evidence_carried: int
    evidence_quarantined: int
    carried_evidence_ids: tuple[str, ...]
    quarantined_evidence_ids: tuple[str, ...]
    nodes_reused: int
    scores_after: dict[str, Any] | None
    promotion_blocked: bool = True


class RelearnStatus(BaseModel):
    """The relearn gate as adapters should surface it (final flow audit F-4).

    ``state`` is the raw registry state (``RELEARN_PENDING`` / ``CLEAR`` /
    ``None``); ``pending_offer`` carries the offer id an approval must name;
    ``active_source_version_id`` is the operator-approved baseline.
    """

    model_config = ConfigDict(frozen=True)

    state: str | None
    pending_offer: RelearnOffer | None = None
    active_source_version_id: str | None = None
    quarantined_evidence_ids: tuple[str, ...] = ()


class RelearnReceipt(BaseModel):
    """The audited result of one operator-approved relearn apply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relearn_id: str
    offer_id: str
    mode: Literal["RESET", "ACCUMULATE"]
    previous_source_version_id: str | None
    new_source_version_id: str
    carried_evidence_ids: tuple[str, ...]
    quarantined_evidence_ids: tuple[str, ...]
    old_evidence_carried: int
    scores_after: dict[str, Any] | None
    promotion_blocked: bool
    evidence_weight: int
    knowledge_seq: int | None
    backup: BackupManifest
    audit_event_id: str
    audit_event_seq: int
    idempotent_replay: bool = False


class RollbackReceipt(BaseModel):
    """The verified byte-exact restoration plus its single linked audit event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relearn_id: str
    restored_db_sha256: str
    restored_artifacts: tuple[BackupArtifact, ...]
    verified: bool
    audit_event_id: str
    audit_event_seq: int


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


class RelearnEngine:
    """Hash-scoped relearn lifecycle on top of the source registry."""

    def __init__(
        self,
        store: EventStore,
        registry: SourceRegistry | None = None,
        *,
        backup_root: str | Path | None = None,
        generation_dir: str | Path | None = None,
        scoring_contract_version: str = "1.0.0",
    ) -> None:
        self._store = store
        self._registry = registry if registry is not None else SourceRegistry(store)
        if backup_root is None and store.db_path != ":memory:":
            backup_root = Path(store.db_path).parent / "relearn_backups"
        self._backup_root = Path(backup_root) if backup_root is not None else None
        self._generation_dir = Path(generation_dir) if generation_dir is not None else None
        self._scoring_contract_version = scoring_contract_version

    # -------------------------------------------------------------- helpers

    @property
    def store(self) -> EventStore:
        return self._store

    @property
    def registry(self) -> SourceRegistry:
        return self._registry

    def checkpoint(self) -> None:
        """Flush the WAL into the main database file (backup precondition)."""
        if self._store.db_path == ":memory:":
            return
        conn = sqlite3.connect(self._store.db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()

    def _write_state(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, _utcnow().isoformat()),
        )

    def _pending_offer(self) -> RelearnOffer:
        offer = self.pending_offer()
        if offer is None:
            raise NoPendingRelearn("no RELEARN_PENDING offer: relearn is never selected automatically")
        return offer

    def pending_offer(self) -> RelearnOffer | None:
        """The outstanding offer while the gate is ``RELEARN_PENDING``, else ``None``.

        Adapter-facing (F-4): the non-raising half of the gate, so a surface
        can retrieve the offer id an approval must name.
        """
        if self._registry.relearn_state() != RELEARN_PENDING:
            return None
        with self._store.read() as conn:
            row = conn.execute("SELECT payload_json FROM relearn_offer ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is None:  # pragma: no cover - state without an offer row is inconsistent
            return None
        return RelearnOffer.model_validate(json.loads(str(row["payload_json"])))

    def status(self) -> RelearnStatus:
        """The gate state, pending offer, approved baseline and quarantine (F-4)."""
        offer = self.pending_offer()
        active = self._registry.active()
        return RelearnStatus(
            state=self._registry.relearn_state(),
            pending_offer=offer,
            active_source_version_id=active.source_version_id if active is not None else None,
            quarantined_evidence_ids=tuple(sorted(self.quarantined_evidence_ids())),
        )

    def _relearn_records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM event_log WHERE kind = 'domain' ORDER BY event_seq"
            ).fetchall()
        for row in rows:
            data = json.loads(str(row["payload_json"]))
            record = data.get("relearn") or (data.get("payload") or {}).get("relearn")
            if isinstance(record, dict):
                records.append(record)
        return records

    def _last_relearn_record(self) -> dict[str, Any] | None:
        records = self._relearn_records()
        return records[-1] if records else None

    # ---------------------------------------------------------------- offer

    def offer(self, path: str | Path, kind: SourceKind, parser_versions: ParserVersions) -> RelearnOffer | None:
        """Emit a deterministic offer on change; never selects a mode."""
        return self._registry.detect_change(path, kind, parser_versions)

    # ---------------------------------------------------------- classification

    @staticmethod
    def _anchor_path(source_uri: str, manifest_paths: set[str]) -> str | None:
        """Map an evidence ``source_uri`` to a manifest path, deterministically.

        The suffix scan iterates ``sorted(manifest_paths)`` and, among
        several suffix matches, the longest (most specific) path wins — the
        result never depends on set iteration order, so a preview computed
        in one process and an apply in another always agree (F-SUB45-1:
        plain set iteration flipped the choice across ``PYTHONHASHSEED``).
        """
        if source_uri in manifest_paths:
            return source_uri
        for path in sorted(manifest_paths, key=lambda candidate: (-len(candidate), candidate)):
            if source_uri.endswith("/" + path):
                return path
        return None

    def _evidence_rows(self) -> list[sqlite3.Row]:
        with self._store.read() as conn:
            return list(
                conn.execute(
                    "SELECT evidence_id, node_id, source_version_id, source_uri FROM evidence ORDER BY evidence_id"
                ).fetchall()
            )

    def _manifest(self, source_version_id: str) -> dict[str, str]:
        row = self._store.retained_source_version(source_version_id)
        if row is None:
            return {}
        entries = json.loads(str(row["manifest_json"]))
        return {str(entry["path"]): str(entry["sha256"]) for entry in entries}

    def _classify(self, offer: RelearnOffer, mode: Literal["RESET", "ACCUMULATE"]) -> tuple[list[str], list[str]]:
        """Return ``(carried, quarantined)`` evidence ids for *mode*."""
        new_id_ = offer.current_source_version_id
        rows = self._evidence_rows()
        if mode == "RESET":
            return [], sorted(str(row["evidence_id"]) for row in rows if str(row["source_version_id"]) != new_id_)
        previous_id = offer.previous_source_version_id
        old_manifest = self._manifest(previous_id) if previous_id else {}
        new_manifest = self._manifest(new_id_)
        mapped = {path for path, sha in old_manifest.items() if new_manifest.get(path) == sha}
        carried: list[str] = []
        quarantined: list[str] = []
        for row in rows:
            evidence_id = str(row["evidence_id"])
            source_version_id = str(row["source_version_id"])
            if source_version_id == new_id_:
                continue  # evidence of the new version is not part of the carry mapping
            if source_version_id == previous_id:
                anchor = self._anchor_path(str(row["source_uri"]), set(old_manifest))
                (carried if anchor in mapped else quarantined).append(evidence_id)
            else:
                quarantined.append(evidence_id)
        return sorted(carried), sorted(quarantined)

    # ---------------------------------------------------------------- preview

    def preview(self, mode: Literal["RESET", "ACCUMULATE"]) -> RelearnPreview:
        """Deterministic read-only projection; performs no writes."""
        offer = self._pending_offer()
        carried, quarantined = self._classify(offer, mode)
        rows = self._evidence_rows()
        carried_nodes = {str(row["node_id"]) for row in rows if str(row["evidence_id"]) in set(carried)}
        return RelearnPreview(
            offer_id=offer.offer_id,
            mode=mode,
            evidence_carried=len(carried),
            evidence_quarantined=len(quarantined),
            carried_evidence_ids=tuple(carried),
            quarantined_evidence_ids=tuple(quarantined),
            nodes_reused=len(carried_nodes),
            scores_after=dict(_RESET_SCORES) if mode == "RESET" else None,
            promotion_blocked=True,
        )

    # ----------------------------------------------------------------- backup

    @staticmethod
    def _fsync_dir(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _verified_backup(self, relearn_id: str, at: datetime) -> BackupManifest:
        """Verified atomic backup of the full protected set via the shared helper.

        Delegates the protected-set copy and its verification to
        :func:`know_your_soul.backup.backup_protected_artifacts` (wave-1
        residual): the SQLite store through the online backup API, the
        ``CURRENT`` pointer, and the generation directory verified against the
        *manifest-declared* hashes. Activation is two-phase (wave-2 re-review
        minor): the helper stages into a hidden sibling directory, the relearn
        ``manifest.json`` is written into that stage *before* activation, and
        one atomic rename then publishes the complete backup — a crash can
        never leave an activated backup without its manifest (the pre-fix
        pattern). ``generation_dir`` is the projection root (the directory
        holding ``CURRENT`` and ``generations/``).
        """
        if self._backup_root is None or self._store.db_path == ":memory:":
            raise BackupUnavailable("a verified relearn backup requires a file-backed store")
        self.checkpoint()
        final = self._backup_root / relearn_id
        projection_root = (
            self._generation_dir if self._generation_dir is not None else self._backup_root / ".projection-absent"
        )
        staging = final.with_name(f".{final.name}.tmp-{os.getpid()}")
        if staging.exists():
            shutil.rmtree(staging)
        try:
            receipt = backup_protected_artifacts(self._store, projection_root=projection_root, dest_dir=staging)
            artifacts: list[BackupArtifact] = []
            for artifact in receipt.artifacts:
                if artifact.kind == "generation_file":
                    # Manifest paths stay relative to the generation directory.
                    artifacts.append(
                        BackupArtifact(
                            kind="generation_file",
                            path=artifact.path.removeprefix("generation/"),
                            sha256=artifact.sha256,
                            byte_length=artifact.byte_length,
                        )
                    )
                elif artifact.kind == "current_pointer":
                    artifacts.append(
                        BackupArtifact(
                            kind="current_pointer",
                            path=artifact.path,
                            sha256=artifact.sha256,
                            byte_length=artifact.byte_length,
                        )
                    )
                else:
                    artifacts.append(
                        BackupArtifact(
                            kind="sqlite",
                            path=artifact.path,
                            sha256=artifact.sha256,
                            byte_length=artifact.byte_length,
                        )
                    )
            manifest = BackupManifest(
                relearn_id=relearn_id,
                created_at=at,
                artifacts=tuple(artifacts),
                scoring_contract_version=self._scoring_contract_version,
                verified=receipt.verified,
            )
            (staging / "manifest.json").write_text(
                json.dumps(manifest.model_dump(mode="json"), indent=1, sort_keys=True), encoding="utf-8"
            )
            self._fsync_dir(staging)  # the manifest is durable before activation
            if final.is_dir():
                shutil.rmtree(final)
            elif final.exists():
                final.unlink()
            os.replace(staging, final)  # one atomic rename activates the complete backup
            self._fsync_dir(final.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return manifest

    # ------------------------------------------------------------------ apply

    def apply(
        self,
        mode: Literal["RESET", "ACCUMULATE"],
        approval: RelearnApproval | None,
        *,
        at: datetime | None = None,
        actor: str = "operator",
        idempotency_key: str | None = None,
    ) -> RelearnReceipt:
        """Apply an operator-approved mode against the pending offer.

        The idempotent-replay lookup runs *before* the pending-offer gate: a
        retried apply after success finds ``relearn_state=CLEAR`` and must
        return the original receipt, not raise ``NoPendingRelearn`` (spec 7.9,
        audit wave-1 finding F2). A key reused for a different mode is an
        idempotency collision and fails loud.
        """
        at = at or _utcnow()
        started = time.monotonic()
        audit = AuditLog(self._store)
        if idempotency_key is not None:
            replayed = self._replay_receipt(idempotency_key, mode=mode, actor=actor, audit=audit)
            if replayed is not None:
                return replayed
        offer = self._pending_offer()
        if approval is None or approval.offer_id != offer.offer_id or approval.mode != mode:
            raise OperatorDecisionRequired(
                f"mode {mode} for offer {offer.offer_id!r} requires an explicit matching operator approval"
            )
        carried, quarantined = self._classify(offer, mode)
        previous_id = offer.previous_source_version_id
        new_version_id = offer.current_source_version_id

        request_hash = hashlib.sha256(
            canonical_json({"relearn_apply": offer.offer_id, "mode": mode}).encode()
        ).hexdigest()
        invocation = Invocation.create(
            "knowledge.relearn_apply", request_hash, actor=actor, idempotency_key=idempotency_key
        )
        effective_key = idempotency_key or invocation.invocation_id
        relearn_id = new_id()
        audit_begun = False
        try:
            # The verified backup precedes every write of this invocation,
            # so its hashes capture the exact pre-relearn state.
            backup = self._verified_backup(relearn_id, at)
            audit.begin(invocation)
            audit_begun = True
            record_event_id = self._apply_mutation(
                effective_key,
                relearn_id=relearn_id,
                backup=backup,
                offer=offer,
                mode=mode,
                carried=carried,
                quarantined=quarantined,
                actor=actor,
                at=at,
            )
        except Exception as error:
            if not audit_begun:
                audit.begin(invocation)
            audit.complete(
                invocation.invocation_id,
                failure_outcome(invocation, started, error),
            )
            raise
        final = audit.complete(
            invocation.invocation_id,
            success_outcome(
                invocation,
                started,
                result_references={"relearn_id": relearn_id},
                linked_domain_event_id=record_event_id,
            ),
        )
        return RelearnReceipt(
            relearn_id=relearn_id,
            offer_id=offer.offer_id,
            mode=mode,
            previous_source_version_id=previous_id,
            new_source_version_id=new_version_id,
            carried_evidence_ids=tuple(carried),
            quarantined_evidence_ids=tuple(quarantined),
            old_evidence_carried=len(carried),
            scores_after=dict(_RESET_SCORES) if mode == "RESET" else None,
            promotion_blocked=True,
            evidence_weight=0,
            knowledge_seq=None,
            backup=backup,
            audit_event_id=final.audit_event_id,
            audit_event_seq=final.event_seq,
            idempotent_replay=False,
        )

    def _replay_receipt(
        self,
        idempotency_key: str,
        *,
        mode: Literal["RESET", "ACCUMULATE"],
        actor: str,
        audit: AuditLog,
    ) -> RelearnReceipt | None:
        """Rebuild the original receipt for a retried apply, or ``None`` if never applied.

        The retry is its own audited invocation (exactly one final audit event
        each); the receipt fields come from the stored relearn record because
        the pending offer no longer exists once the first apply cleared it.
        """
        started = time.monotonic()
        with self._store.read() as conn:
            row = conn.execute(
                "SELECT event_id, payload_json FROM event_log WHERE kind = 'domain' AND idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        stored = json.loads(str(row["payload_json"]))
        record = stored.get("relearn") or (stored.get("payload") or {}).get("relearn")
        if not isinstance(record, dict) or "relearn_id" not in record:
            raise IdempotencyConflict(f"idempotency key {idempotency_key!r} is already bound to a non-relearn write")
        if str(record.get("mode")) != mode:
            raise IdempotencyConflict(
                f"idempotency key {idempotency_key!r} replayed with mode {mode} "
                f"but the stored relearn applied {record.get('mode')!r}"
            )
        relearn_id = str(record["relearn_id"])
        backup = self._load_backup_manifest(relearn_id)
        request_hash = hashlib.sha256(
            canonical_json({"relearn_apply": record["offer_id"], "mode": mode}).encode()
        ).hexdigest()
        invocation = Invocation.create(
            "knowledge.relearn_apply", request_hash, actor=actor, idempotency_key=idempotency_key
        )
        audit.begin(invocation)
        final = audit.complete(
            invocation.invocation_id,
            success_outcome(
                invocation,
                started,
                result_references={"relearn_id": relearn_id},
                linked_domain_event_id=str(row["event_id"]),
            ),
        )
        carried = [str(evidence_id) for evidence_id in record["carried_evidence_ids"]]
        return RelearnReceipt(
            relearn_id=relearn_id,
            offer_id=str(record["offer_id"]),
            mode=mode,
            previous_source_version_id=record.get("previous_source_version_id"),
            new_source_version_id=str(record["new_source_version_id"]),
            carried_evidence_ids=tuple(carried),
            quarantined_evidence_ids=tuple(str(evidence_id) for evidence_id in record["quarantined_evidence_ids"]),
            old_evidence_carried=len(carried),
            scores_after=record.get("scores_after"),
            promotion_blocked=True,
            evidence_weight=0,
            knowledge_seq=None,
            backup=backup,
            audit_event_id=final.audit_event_id,
            audit_event_seq=final.event_seq,
            idempotent_replay=True,
        )

    def _apply_mutation(
        self,
        effective_key: str,
        *,
        relearn_id: str,
        backup: BackupManifest,
        offer: RelearnOffer,
        mode: Literal["RESET", "ACCUMULATE"],
        carried: list[str],
        quarantined: list[str],
        actor: str,
        at: datetime,
    ) -> str:
        """One transaction: supersession, quarantine, state and the relearn record.

        Returns the relearn record's domain event id so the final audit links it.
        """
        new_version_id = offer.current_source_version_id
        record = {
            "relearn_id": relearn_id,
            "offer_id": offer.offer_id,
            "mode": mode,
            "previous_source_version_id": offer.previous_source_version_id,
            "new_source_version_id": new_version_id,
            "carried_evidence_ids": carried,
            "quarantined_evidence_ids": quarantined,
            "promotion_blocked": True,
            "evidence_weight": 0,
            "scores_after": dict(_RESET_SCORES) if mode == "RESET" else None,
            "backup": backup.model_dump(mode="json"),
            "at": at.isoformat(),
        }
        with self._store.transaction() as conn:
            # Supersede every other CURRENT version of this kind; retention, never deletion.
            conn.execute(
                "UPDATE source_version SET source_state = 'SUPERSEDED', superseded_by = ? "
                "WHERE source_state = 'CURRENT' AND source_version_id != ? AND source_id = "
                "(SELECT source_id FROM source_version WHERE source_version_id = ?)",
                (new_version_id, new_version_id, new_version_id),
            )
            self._write_state(conn, "relearn_quarantine", canonical_json(quarantined))
            self._write_state(conn, "relearn_state", "CLEAR")
            self._write_state(conn, "relearn_last_id", relearn_id)
            # The record is a full Event dump so replay_domain stays parseable
            # (spec 7.10 rebuild-from-events). The domain field is a schema
            # placeholder: a relearn record is system state, not node knowledge.
            event = Event(
                kind=EventKind.ADMIN,
                node_id=relearn_id,
                domain=PrimaryDomain.P1,
                occurred_at=at,
                knowledge_affecting=False,  # zero evidence weight; never moves P/M/E
                payload={"system_record": True, "relearn": record},
            )
            self._store.insert_event(
                conn,
                event_id=event.event_id,
                kind="domain",
                event_type=event.kind.value,
                knowledge_affecting=False,
                actor=actor,
                occurred_at=at.isoformat(),
                payload=event.model_dump(mode="python"),
                node_id=relearn_id,
                domain=event.domain.value,
                idempotency_key=effective_key,
            )
        return event.event_id

    def _load_backup_manifest(self, relearn_id: str) -> BackupManifest:
        if self._backup_root is None:
            raise BackupUnavailable("no backup root configured")
        manifest_path = self._backup_root / relearn_id / "manifest.json"
        if not manifest_path.exists():
            raise BackupUnavailable(f"no backup manifest for relearn {relearn_id!r}")
        return BackupManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))

    # ---------------------------------------------------------------- rollback

    def rollback(self, relearn_id: str, *, actor: str = "operator") -> RollbackReceipt:
        """Restore the byte-exact pre-relearn state plus one linked audit event."""
        started = time.monotonic()
        if self._backup_root is None:
            raise UnknownRelearn(f"no backup root; cannot roll back {relearn_id!r}")
        backup_dir = self._backup_root / relearn_id
        manifest_path = backup_dir / "manifest.json"
        if not manifest_path.exists():
            raise UnknownRelearn(f"no relearn backup for {relearn_id!r}")
        if (backup_dir / "ROLLED_BACK").exists():
            raise RelearnError(f"relearn {relearn_id!r} was already rolled back")
        manifest = BackupManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
        if not any(str(record.get("relearn_id")) == relearn_id for record in self._relearn_records()):
            raise UnknownRelearn(f"no relearn record for {relearn_id!r}")

        db_path = Path(self._store.db_path)
        store = self._store
        # Hold the store lock across close → byte-replace → reopen so a
        # concurrent holder blocks for the swap window instead of dying on a
        # closed handle (RLock: close() and the transplant re-acquire it
        # reentrantly). The store object itself is never replaced — every
        # holder of the shared instance stays valid (W3-1). The reopen recipe
        # (pragmas, busy timeout, migrations) lives in EventStore itself.
        with store._lock:  # noqa: SLF001 - same-process handle surgery
            store.close()
            try:
                for suffix in ("-wal", "-shm"):
                    side = Path(str(db_path) + suffix)
                    if side.exists():
                        side.unlink()
                staged = db_path.with_name(db_path.name + ".restore-tmp")
                shutil.copyfile(backup_dir / "soul.db", staged)
                sqlite_artifact = next(a for a in manifest.artifacts if a.kind == "sqlite")
                if _sha256_file(staged) != sqlite_artifact.sha256:
                    staged.unlink()
                    raise RollbackVerificationError("restored SQLite bytes do not hash-match the pre-relearn backup")
                os.replace(staged, db_path)
                restored_db_sha256 = _sha256_file(db_path)
                if restored_db_sha256 != sqlite_artifact.sha256:  # pragma: no cover - defensive
                    raise RollbackVerificationError("restored database hash mismatch after rename")

                if self._generation_dir is not None and (backup_dir / "generation").exists():
                    pointer = (backup_dir / "CURRENT").read_text(encoding="utf-8").strip()
                    target = self._generation_dir / "generations" / pointer
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(backup_dir / "generation", target)
                    for artifact in manifest.artifacts:
                        if artifact.kind != "generation_file":
                            continue
                        restored = target / artifact.path
                        if not restored.exists() or _sha256_file(restored) != artifact.sha256:
                            raise RollbackVerificationError(
                                f"restored generation file {artifact.path!r} does not hash-match the backup"
                            )
                    live_pointer = self._generation_dir / "CURRENT"
                    shutil.copyfile(backup_dir / "CURRENT", live_pointer)
                    pointer_artifact = next((a for a in manifest.artifacts if a.kind == "current_pointer"), None)
                    if pointer_artifact is not None and _sha256_file(live_pointer) != pointer_artifact.sha256:
                        raise RollbackVerificationError("restored CURRENT pointer does not hash-match the backup")
            except Exception:
                store.reopen_in_place()
                raise
            (backup_dir / "ROLLED_BACK").write_text(_utcnow().isoformat(), encoding="utf-8")
            store.reopen_in_place()

        audit = AuditLog(store)
        invocation = Invocation.create(
            "knowledge.relearn_rollback",
            hashlib.sha256(canonical_json({"relearn_rollback": relearn_id}).encode()).hexdigest(),
            actor=actor,
        )
        audit.begin(invocation)
        final = audit.complete(
            invocation.invocation_id,
            success_outcome(
                invocation,
                started,
                result_references={"relearn_id": relearn_id, "restored_db_sha256": restored_db_sha256},
                projection_outcome="restored",
            ),
        )
        return RollbackReceipt(
            relearn_id=relearn_id,
            restored_db_sha256=restored_db_sha256,
            restored_artifacts=manifest.artifacts,
            verified=True,
            audit_event_id=final.audit_event_id,
            audit_event_seq=final.event_seq,
        )

    # ------------------------------------------------------- source scoping

    def quarantined_evidence_ids(self) -> frozenset[str]:
        """Evidence excluded from scoring by the latest relearn apply."""
        with self._store.read() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = 'relearn_quarantine'").fetchone()
        return frozenset() if row is None else frozenset(json.loads(str(row["value"])))

    def compatible_source_version_ids(self) -> frozenset[str]:
        """The active baseline plus, after ACCUMULATE, the approved carried version."""
        active = self._registry.active()
        if active is None:
            return frozenset()
        compatible = {active.source_version_id}
        record = self._last_relearn_record()
        if (
            record is not None
            and record.get("mode") == "ACCUMULATE"
            and record.get("new_source_version_id") == active.source_version_id
            and record.get("previous_source_version_id")
        ):
            compatible.add(str(record["previous_source_version_id"]))
        return frozenset(compatible)

    def assert_source_compatible(
        self,
        source_version_ids: set[str] | frozenset[str] | list[str],
        *,
        mixed_source_versions: bool = False,
        operator_approval: bool = False,
    ) -> None:
        """Refuse stale mixing unless explicitly approved by the operator."""
        allowed = self.compatible_source_version_ids()
        incompatible = sorted(set(source_version_ids) - allowed)
        if incompatible and not (mixed_source_versions and operator_approval):
            raise MixedSourceVersions(incompatible)
