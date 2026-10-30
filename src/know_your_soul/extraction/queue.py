"""The durable extraction review queue (contract v4, SUB-106).

A sidecar SQLite database owned by this package — deliberately **not** a
table in the frozen store schema: ``store.py`` stays untouched and the queue
is review state, never canonical graph state. The path is derived from the
main store (``soul.sqlite3`` → ``soul.extraction.sqlite3``); an in-memory
store gets an in-memory queue. Pragmas mirror the main store (WAL +
synchronous FULL when file-backed, bounded busy timeout), and every write
runs under a ``BEGIN IMMEDIATE`` transaction so concurrent writers serialize.

Idempotency is locked at the schema level: ``idem_key``
(``source_sha256|actor|method_fingerprint``) is UNIQUE.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import weakref
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from know_your_soul.domain import canonical_json
from know_your_soul.extraction.corroboration import GroupVerdict
from know_your_soul.extraction.errors import ExtractionQueueFullError
from know_your_soul.extraction.schema import Submission

__all__ = ["QUEUE_CAP", "STATUS_RANK", "ExtractionQueue", "QueueRow", "sidecar_path"]

QUEUE_CAP = 65_536

#: The contract's review order: what needs the operator first.
STATUS_RANK: dict[str, int] = {
    "contradiction_flagged": 0,
    "pending_review": 1,
    "corroborated": 2,
    "applied": 3,
    "rejected": 4,
}

PRE_DECISION_STATUSES = frozenset({"pending_review", "corroborated", "contradiction_flagged"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS extraction_submissions (
    receipt_id TEXT PRIMARY KEY,
    idem_key TEXT NOT NULL UNIQUE,
    source_sha256 TEXT NOT NULL,
    source_path TEXT,
    actor TEXT NOT NULL,
    method_json TEXT NOT NULL,
    method_fingerprint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    confidence_reported TEXT NOT NULL,
    confidence_effective TEXT NOT NULL,
    status TEXT NOT NULL,
    corroborators INTEGER NOT NULL DEFAULT 0,
    contradictions INTEGER NOT NULL DEFAULT 0,
    overlap_ratio TEXT NOT NULL DEFAULT '0.000000000000',
    receipt_json TEXT NOT NULL,
    decision_note TEXT,
    decided_by TEXT,
    submitted_at TEXT NOT NULL,
    decided_at TEXT
)
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def sidecar_path(db_path: str | Path) -> str:
    """The queue database path derived from the main store path."""
    if str(db_path) == ":memory:":
        return ":memory:"
    path = Path(db_path)
    return str(path.with_name(f"{path.stem}.extraction.sqlite3"))


@dataclass(frozen=True)
class QueueRow:
    """One durable queue entry (review state — never canonical graph state)."""

    receipt_id: str
    idem_key: str
    source_sha256: str
    source_path: str | None
    actor: str
    method: dict[str, Any]
    method_fingerprint: str
    payload: dict[str, Any]
    payload_sha256: str
    confidence_reported: str
    confidence_effective: str
    status: str
    corroborators: int
    contradictions: int
    overlap_ratio: str
    receipt: dict[str, Any]
    decision_note: str | None = None
    decided_by: str | None = None
    submitted_at: str = ""
    decided_at: str | None = None

    @classmethod
    def from_submission(
        cls,
        submission: Submission,
        *,
        actor: str,
        receipt: Mapping[str, Any],
    ) -> QueueRow:
        return cls(
            receipt_id=submission.receipt_id(actor),
            idem_key=submission.idem_key(actor),
            source_sha256=submission.source_sha256,
            source_path=submission.source_path,
            actor=actor,
            method=submission.method.canonical(),
            method_fingerprint=submission.method.fingerprint(),
            payload=submission.payload.canonical(),
            payload_sha256=submission.payload_sha256,
            confidence_reported=str(submission.confidence),
            confidence_effective=str(submission.confidence),
            status="pending_review",
            corroborators=0,
            contradictions=0,
            overlap_ratio="0.000000000000",
            receipt=dict(receipt),
        )


class ExtractionQueue:
    """The sidecar review queue: durable, locked, deterministic."""

    def __init__(self, path: str | Path, *, cap: int = QUEUE_CAP, busy_timeout_ms: int = 15000) -> None:
        self.path = str(path)
        self._cap = int(cap)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")  # noqa: S608 - int-formatted pragma
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        with self._write() as conn:
            conn.execute(_SCHEMA)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_extraction_source ON extraction_submissions(source_sha256)")

    # ------------------------------------------------------------ lifecycle

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> ExtractionQueue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        """One BEGIN IMMEDIATE transaction (serialized writers)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    # ------------------------------------------------------------ internals

    @staticmethod
    def _row_from_record(record: sqlite3.Row) -> QueueRow:
        return QueueRow(
            receipt_id=str(record["receipt_id"]),
            idem_key=str(record["idem_key"]),
            source_sha256=str(record["source_sha256"]),
            source_path=record["source_path"],
            actor=str(record["actor"]),
            method=json.loads(str(record["method_json"])),
            method_fingerprint=str(record["method_fingerprint"]),
            payload=json.loads(str(record["payload_json"])),
            payload_sha256=str(record["payload_sha256"]),
            confidence_reported=str(record["confidence_reported"]),
            confidence_effective=str(record["confidence_effective"]),
            status=str(record["status"]),
            corroborators=int(record["corroborators"]),
            contradictions=int(record["contradictions"]),
            overlap_ratio=str(record["overlap_ratio"]),
            receipt=json.loads(str(record["receipt_json"])),
            decision_note=record["decision_note"],
            decided_by=record["decided_by"],
            submitted_at=str(record["submitted_at"]),
            decided_at=record["decided_at"],
        )

    # ------------------------------------------------------------- queries

    def get(self, receipt_id: str) -> QueueRow | None:
        with self._lock:
            record = self._conn.execute(
                "SELECT * FROM extraction_submissions WHERE receipt_id = ?", (receipt_id,)
            ).fetchone()
        return self._row_from_record(record) if record is not None else None

    def get_by_idem_key(self, idem_key: str) -> QueueRow | None:
        with self._lock:
            record = self._conn.execute(
                "SELECT * FROM extraction_submissions WHERE idem_key = ?", (idem_key,)
            ).fetchone()
        return self._row_from_record(record) if record is not None else None

    def group(self, source_sha256: str) -> list[QueueRow]:
        """Every submission of one source, in submission order (rowid)."""
        with self._lock:
            records = self._conn.execute(
                "SELECT * FROM extraction_submissions WHERE source_sha256 = ? ORDER BY rowid",
                (source_sha256,),
            ).fetchall()
        return [self._row_from_record(record) for record in records]

    def list(self, *, status: str | None = None) -> list[QueueRow]:
        """The review order: status rank, then receipt_id (deterministic)."""
        with self._lock:
            if status is None:
                records = self._conn.execute("SELECT * FROM extraction_submissions").fetchall()
            else:
                records = self._conn.execute(
                    "SELECT * FROM extraction_submissions WHERE status = ?", (status,)
                ).fetchall()
        rows = [self._row_from_record(record) for record in records]
        return sorted(rows, key=lambda row: (STATUS_RANK.get(row.status, 99), row.receipt_id))

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM extraction_submissions").fetchone()
        return int(row["n"]) if row is not None else 0

    # ------------------------------------------------------------- writes

    def insert(self, row: QueueRow) -> None:
        """Insert one submission; the queue cap fails closed."""
        with self._lock:
            if self.count() >= self._cap:
                raise ExtractionQueueFullError(
                    f"the extraction review queue holds {self._cap} submissions; refusing to grow unbounded"
                )
            with self._write() as conn:
                conn.execute(
                    "INSERT INTO extraction_submissions ("
                    "receipt_id, idem_key, source_sha256, source_path, actor, method_json,"
                    " method_fingerprint, payload_json, payload_sha256, confidence_reported,"
                    " confidence_effective, status, corroborators, contradictions, overlap_ratio,"
                    " receipt_json, decision_note, decided_by, submitted_at, decided_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        row.receipt_id,
                        row.idem_key,
                        row.source_sha256,
                        row.source_path,
                        row.actor,
                        canonical_json(row.method),
                        row.method_fingerprint,
                        canonical_json(row.payload),
                        row.payload_sha256,
                        row.confidence_reported,
                        row.confidence_effective,
                        row.status,
                        row.corroborators,
                        row.contradictions,
                        row.overlap_ratio,
                        canonical_json(row.receipt),
                        row.decision_note,
                        row.decided_by,
                        _now_iso(),
                        row.decided_at,
                    ),
                )

    def update_verdicts(self, verdicts: Mapping[str, GroupVerdict]) -> None:
        """Write recomputed group verdicts; decided rows keep their status."""
        with self._lock, self._write() as conn:
            for idem_key in sorted(verdicts):
                verdict = verdicts[idem_key]
                conn.execute(
                    "UPDATE extraction_submissions SET confidence_effective = ?, corroborators = ?,"
                    " contradictions = ?, overlap_ratio = ? WHERE idem_key = ?",
                    (
                        verdict.confidence_effective,
                        verdict.corroborators,
                        verdict.contradictions,
                        verdict.overlap_ratio,
                        idem_key,
                    ),
                )
                conn.execute(
                    "UPDATE extraction_submissions SET status = ? WHERE idem_key = ? AND status IN (?, ?, ?)",
                    (
                        verdict.status,
                        idem_key,
                        "pending_review",
                        "corroborated",
                        "contradiction_flagged",
                    ),
                )

    def mark_decided(self, receipt_id: str, status: str, *, decided_by: str, note: str | None) -> None:
        """Record an operator decision (``applied`` / ``rejected``)."""
        if status not in {"applied", "rejected"}:
            raise ValueError(f"decision status must be applied or rejected, got {status!r}")
        with self._lock, self._write() as conn:
            conn.execute(
                "UPDATE extraction_submissions SET status = ?, decided_by = ?, decision_note = ?, decided_at = ?"
                " WHERE receipt_id = ?",
                (status, decided_by, note, _now_iso(), receipt_id),
            )

    def store_receipt(self, receipt_id: str, receipt: Mapping[str, Any]) -> None:
        """Persist the post-verdict receipt so replays return it verbatim."""
        with self._lock, self._write() as conn:
            conn.execute(
                "UPDATE extraction_submissions SET receipt_json = ? WHERE receipt_id = ?",
                (canonical_json(dict(receipt)), receipt_id),
            )

    # ------------------------------------------------------------- dumps

    def canonical_dump(self) -> str:
        """A byte-stable dump of the whole queue with volatile fields masked.

        ``submitted_at`` / ``decided_at`` are wall-clock columns (the v3
        ``first_seen`` precedent); everything else is content-derived, so two
        identical submission sequences dump byte-identically.
        """
        rows = sorted(self.list(), key=lambda row: row.receipt_id)
        material = [
            {
                "receipt_id": row.receipt_id,
                "idem_key": row.idem_key,
                "source_sha256": row.source_sha256,
                "source_path": row.source_path,
                "actor": row.actor,
                "method": row.method,
                "method_fingerprint": row.method_fingerprint,
                "payload": row.payload,
                "payload_sha256": row.payload_sha256,
                "confidence_reported": row.confidence_reported,
                "confidence_effective": row.confidence_effective,
                "status": row.status,
                "corroborators": row.corroborators,
                "contradictions": row.contradictions,
                "overlap_ratio": row.overlap_ratio,
                "receipt": {key: value for key, value in row.receipt.items() if key != "audit_event_id"},
                "decision_note": row.decision_note,
                "decided_by": row.decided_by,
            }
            for row in rows
        ]
        return canonical_json(material)

    # ------------------------------------------------------------- caching

    @classmethod
    def for_store(cls, store: Any) -> ExtractionQueue:
        """The one queue bound to *store* (cached; in-memory stores included)."""
        db_path = str(store.db_path)
        if db_path == ":memory:":
            with _MEMORY_QUEUES_LOCK:
                queue = _MEMORY_QUEUES.get(store)
                if queue is None:
                    queue = cls(":memory:")
                    _MEMORY_QUEUES[store] = queue
                return queue
        path = sidecar_path(db_path)
        key = str(Path(path).resolve())
        with _FILE_QUEUES_LOCK:
            queue = _FILE_QUEUES.get(key)
            if queue is None:
                queue = cls(path)
                _FILE_QUEUES[key] = queue
            return queue


_MEMORY_QUEUES: weakref.WeakKeyDictionary[Any, ExtractionQueue] = weakref.WeakKeyDictionary()
_MEMORY_QUEUES_LOCK = threading.Lock()
_FILE_QUEUES: dict[str, ExtractionQueue] = {}
_FILE_QUEUES_LOCK = threading.Lock()
