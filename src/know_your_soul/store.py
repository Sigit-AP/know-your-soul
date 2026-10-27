"""Immutable SQLite event store, revision history and watermarks.

Task 2 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 7 and ADR 0002.

The store is the single source of truth. It is deliberately small and
unforgiving:

* Every write runs inside one ``BEGIN IMMEDIATE`` transaction, so a single
  writer is serialized while WAL lets many readers proceed concurrently.
* ``event_log`` is append-only and carries a SHA-256 chain over every event
  (``prev_event_hash`` -> ``event_hash``); ``verify_chain`` re-derives it.
* Domain mutations deduplicate on ``idempotency_key`` and honour
  ``expected_version`` optimistic concurrency.
* ``event_seq`` is global and monotonic; ``knowledge_seq`` is assigned only to
  knowledge-affecting events, so audit-only records never move the knowledge
  watermark.

No provider or model configuration is stored anywhere in this module or its
schema.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import weakref
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from know_your_soul.domain import (
    Event,
    EventKind,
    Evidence,
    LifecycleStatus,
    Node,
    Relation,
    RelationKind,
    canonical_json,
    storage_payload,
)
from know_your_soul.events import ChainReport, DomainReceipt, Invocation, WriteReceipt

if TYPE_CHECKING:
    from know_your_soul.audit import AuditLog

__all__ = [
    "AuditOutcomeMismatch",
    "EventRow",
    "EventStore",
    "FileRow",
    "GraphEdgeRow",
    "IdempotencyConflict",
    "InvalidRelation",
    "MigrationError",
    "PublishCapacityExceeded",
    "SchemaStateError",
    "SchemaVersionTooNew",
    "StoreError",
    "SymbolRow",
    "UnresolvedRefError",
    "UnresolvedRefRow",
    "VersionConflict",
]

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Highest schema version this build supports. The file-based SQL migrations in
# ``migrations/`` cover v1/v2 (ledger: ``schema_migration``); v3 onward are
# applied transactionally from Python and recorded in ``schema_versions``
# (codegraph pattern, RESEARCH-STORAGE.md §1.4) so a failed migration rolls
# back atomically instead of leaving half-created objects behind.
SCHEMA_VERSION = 3

# Only these kinds create a new node revision and bump the node version.
_VERSION_BUMPING_KINDS: frozenset[EventKind] = frozenset({EventKind.NODE_UPSERT, EventKind.NODE_RETIRE})
_NODE_REQUIRED_KINDS: frozenset[EventKind] = frozenset({EventKind.EVIDENCE_APPEND, EventKind.RELATION_ADD})


class StoreError(RuntimeError):
    """Base class for store contract violations."""


class VersionConflict(StoreError):
    """The caller's expected node version does not match the stored version."""


class InvalidRelation(StoreError):
    """A relation references a node that does not exist."""


class AuditOutcomeMismatch(StoreError):
    """The final audit receipt does not record the outcome the caller requested."""


class IdempotencyConflict(StoreError):
    """An idempotency key was reused for a semantically different request.

    Replaying the first write's receipt here would silently drop the new
    write (audit wave-1 finding F3), so the reuse fails loud instead.
    """


class PublishCapacityExceeded(StoreError):
    """The write would push the published graph past its fail-closed byte cap.

    Final flow audit F-3: the store accepted unbounded ``log_event`` growth
    while every published artifact is capped, so enough writes wedged the
    read model permanently. The write side now fails closed *before* the
    store crosses the publish ceiling; the message carries the estimate, the
    cap and the headroom so the refusal is self-explanatory.
    """


class MigrationError(StoreError):
    """A versioned schema migration failed; the transaction was rolled back."""


class SchemaVersionTooNew(MigrationError):
    """The database records a schema version newer than this build supports.

    Fail-closed (codegraph rule): never open a store whose schema we do not
    fully understand — a downgrade attempt would silently corrupt semantics.
    """


class SchemaStateError(MigrationError):
    """The schema ledger and the actual objects disagree; refuse to guess."""


class UnresolvedRefError(StoreError):
    """An unresolved-ref lifecycle transition was requested illegally."""


# Same-process registry of open file-backed stores (final flow audit F-1):
# backup/restore must know when a destination path is a *live* store so the
# replace can be coordinated with the handle (close → swap → reopen) instead
# of leaving a WAL chimera under it. Values are weak: a garbage-collected
# store simply disappears from the registry.
_LIVE_STORES_LOCK = threading.Lock()
_LIVE_STORES: weakref.WeakValueDictionary[str, EventStore] = weakref.WeakValueDictionary()

# Publish-size estimator overheads (F-3). ``payload_json`` of the latest live
# revision is the canonical (escaped) byte length of the stored node — the
# published graph entry carries a subset of those fields plus the adapter
# additions and JSON key structure, so a fixed per-node/per-edge overhead
# keeps the estimate conservative (calibrated against the frozen corpus:
# estimate ≈ 1.6× the real ``graph.json``).
PUBLISH_ESTIMATE_NODE_OVERHEAD_BYTES = 512
PUBLISH_ESTIMATE_EDGE_OVERHEAD_BYTES = 160
PUBLISH_ESTIMATE_FIXED_BYTES = 1024
# F-3: fraction of the projection's per-artifact publish cap at which the
# write-side guard refuses further growth (headroom absorbs the estimator's
# over-count and the other artifacts sharing the cap).
PUBLISH_CAPACITY_RATIO = 0.95


@dataclass(frozen=True)
class EventRow:
    """The store-assigned identity of one appended event."""

    event_seq: int
    event_id: str
    knowledge_seq: int | None
    node_version: int | None
    prev_event_hash: str | None
    event_hash: str


# ------------------------------------------------------------------ schema v3
# Universal Knowledge Graph storage (W1, MASTER-PLAN.md; DDL patterns adopted
# from codegraph per RESEARCH-STORAGE.md §1.2/§1.3). One statement per entry:
# the runner executes them individually inside one transaction — never via
# ``executescript``, which would COMMIT early and break rollback-safety.
_SCHEMA_V3_DESCRIPTION = "universal knowledge graph: files, symbols, graph_edges, unresolved_refs"
_SCHEMA_V3_TABLES = ("files", "symbols", "graph_edges", "unresolved_refs")
_SCHEMA_V3_STATEMENTS: tuple[str, ...] = (
    # Source-file tracking for incremental ingestion. Identity is the path;
    # ``sha256`` confirms what the (size, mtime) pre-filter suspects changed.
    "CREATE TABLE files ("
    "    path TEXT PRIMARY KEY,"
    "    sha256 TEXT NOT NULL,"
    "    size INTEGER NOT NULL CHECK (size >= 0),"
    "    mtime INTEGER NOT NULL,"
    "    language TEXT NOT NULL DEFAULT 'unknown',"
    "    generated_hint INTEGER NOT NULL DEFAULT 0 CHECK (generated_hint IN (0, 1)),"
    "    extraction_version INTEGER NOT NULL"
    ")",
    "CREATE INDEX idx_files_language ON files (language)",
    "CREATE INDEX idx_files_mtime ON files (mtime)",
    # PARTIAL: generated files are the minority (codegraph idx_files_generated).
    "CREATE INDEX idx_files_generated ON files (path) WHERE generated_hint = 1",
    # Symbol lookup surface; identity is the stable node id
    # ``kind:sha256(path:kind:name:line)[:col]`` minted by the extractor.
    "CREATE TABLE symbols ("
    "    node_id TEXT PRIMARY KEY,"
    "    name TEXT NOT NULL,"
    "    kind TEXT NOT NULL,"
    "    path TEXT NOT NULL,"
    "    line INTEGER NOT NULL,"
    "    col INTEGER NOT NULL DEFAULT -1"
    ")",
    "CREATE INDEX idx_symbols_name ON symbols (name)",
    # Expression index: case-insensitive lookup without a table scan. Queries
    # must use ``lower(name) = lower(?)`` exactly or SQLite will not seek it.
    "CREATE INDEX idx_symbols_lower_name ON symbols (lower(name))",
    "CREATE INDEX idx_symbols_path ON symbols (path, line, col)",
    "CREATE INDEX idx_symbols_kind ON symbols (kind)",
    # Universal graph edges (contains/calls/imports/bridges/...). Hierarchy is
    # edges rooted at file nodes; depth is computed at traversal, never stored.
    "CREATE TABLE graph_edges ("
    "    edge_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "    source TEXT NOT NULL,"
    "    target TEXT NOT NULL,"
    "    kind TEXT NOT NULL,"
    "    line INTEGER,"
    "    col INTEGER,"
    "    metadata TEXT,"
    "    provenance TEXT"
    ")",
    # Left-prefix of the composite covers bare source/target adjacency scans;
    # separate single-column indexes are deliberately omitted (codegraph v4).
    "CREATE INDEX idx_graph_edges_source_kind ON graph_edges (source, kind)",
    "CREATE INDEX idx_graph_edges_target_kind ON graph_edges (target, kind)",
    # Unique edge identity: IFNULL folds NULL line/col to -1 because SQLite
    # treats every NULL as distinct (codegraph idx_edges_identity, #1034).
    "CREATE UNIQUE INDEX idx_graph_edges_identity"
    "    ON graph_edges (source, target, kind, IFNULL(line, -1), IFNULL(col, -1))",
    # Two-phase resolution: extractors park cross-file references here;
    # a separate resolve pass turns them into edges. ``name_tail`` (the last
    # dot segment by default) is the retry lookup key when new files appear.
    "CREATE TABLE unresolved_refs ("
    "    ref_id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "    source_node_id TEXT NOT NULL,"
    "    target_name TEXT NOT NULL,"
    "    name_tail TEXT NOT NULL DEFAULT '',"
    "    kind TEXT NOT NULL,"
    "    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'resolved', 'failed')),"
    "    first_seen TEXT NOT NULL,"
    "    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0)"
    ")",
    "CREATE INDEX idx_unresolved_source ON unresolved_refs (source_node_id)",
    "CREATE INDEX idx_unresolved_target_name ON unresolved_refs (target_name)",
    # PARTIAL: pending is the hot retry queue; resolved rows never re-query.
    "CREATE INDEX idx_unresolved_pending ON unresolved_refs (first_seen, ref_id) WHERE status = 'pending'",
    # PARTIAL: failed refs are retried by tail when a candidate file lands.
    "CREATE INDEX idx_unresolved_failed_tail ON unresolved_refs (name_tail) WHERE status = 'failed'",
)

# Adjacency statements are built per (direction, kinds) shape and cached —
# sqlite3 caches the compiled statement per SQL string, so a bounded string
# cache gives prepared-statement behaviour with a fixed memory ceiling
# (codegraph edgeKindStmt cache, capped at 64 shapes).
_ADJACENCY_SQL_CACHE_LIMIT = 64


@dataclass(frozen=True)
class FileRow:
    """One tracked source file (schema v3 ``files``)."""

    path: str
    sha256: str
    size: int
    mtime: int
    language: str
    generated_hint: bool
    extraction_version: int


@dataclass(frozen=True)
class SymbolRow:
    """One named symbol (schema v3 ``symbols``)."""

    node_id: str
    name: str
    kind: str
    path: str
    line: int
    col: int


@dataclass(frozen=True)
class GraphEdgeRow:
    """One universal-graph edge (schema v3 ``graph_edges``)."""

    edge_id: int
    source: str
    target: str
    kind: str
    line: int | None
    col: int | None
    metadata: str | None
    provenance: str | None


@dataclass(frozen=True)
class UnresolvedRefRow:
    """One parked cross-file reference (schema v3 ``unresolved_refs``)."""

    ref_id: int
    source_node_id: str
    target_name: str
    name_tail: str
    kind: str
    status: str
    first_seen: str
    attempts: int


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _hash_input(
    *,
    event_seq: int,
    event_id: str,
    kind: str,
    event_type: str,
    knowledge_affecting: bool,
    knowledge_seq: int | None,
    node_id: str | None,
    domain: str | None,
    source_version_id: str | None,
    node_version: int | None,
    actor: str,
    occurred_at: str,
    recorded_at: str,
    payload_json: str,
    idempotency_key: str | None,
    prev_event_hash: str | None,
) -> dict[str, Any]:
    return {
        "event_seq": event_seq,
        "event_id": event_id,
        "kind": kind,
        "event_type": event_type,
        "knowledge_affecting": knowledge_affecting,
        "knowledge_seq": knowledge_seq,
        "node_id": node_id,
        "domain": domain,
        "source_version_id": source_version_id,
        "node_version": node_version,
        "actor": actor,
        "occurred_at": occurred_at,
        "recorded_at": recorded_at,
        "payload_json": payload_json,
        "idempotency_key": idempotency_key,
        "prev_event_hash": prev_event_hash,
    }


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# Per-attempt identity and environmental context drift between attempts of one
# logical request: a genuine retry rebuilds the Event (fresh ``event_id`` /
# ``occurred_at``), and a re-assertion such as ``SeedImporter.ensure_roots``
# runs under a newer source version (or provenance URI) with fresh record
# timestamps. The request fingerprint below strips exactly these drift fields
# and keeps the request's substance: kind, target node, domain, actor and the
# node/evidence/relation content.
_EVENT_DRIFT_FIELDS = frozenset({"event_id", "occurred_at", "source_version_id"})
_NODE_DRIFT_FIELDS = frozenset({"created_at", "updated_at", "version", "source_version_id", "provenance"})


def _event_request_fingerprint(payload: Mapping[str, Any]) -> str:
    """Retry-stable request fingerprint for idempotency-conflict detection."""
    content = {key: value for key, value in payload.items() if key not in _EVENT_DRIFT_FIELDS}
    node = content.get("node")
    if isinstance(node, Mapping):
        content["node"] = {key: value for key, value in node.items() if key not in _NODE_DRIFT_FIELDS}
    return _digest(storage_payload(content))


class EventStore:
    """SQLite-backed immutable event store."""

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        migrations_dir: str | Path | None = None,
        busy_timeout_ms: int = 15000,
    ) -> None:
        self.db_path = str(db_path)
        self._migrations_dir = Path(migrations_dir) if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
        self._busy_timeout_ms = int(busy_timeout_ms)
        if self.db_path != ":memory:":
            Path(self.db_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._adjacency_sql_cache: dict[tuple[str, tuple[str, ...]], str] = {}
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        if self.db_path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        self.migrate()
        if self.db_path != ":memory:":
            with _LIVE_STORES_LOCK:
                _LIVE_STORES[os.path.realpath(self.db_path)] = self

    # ------------------------------------------------------------- lifecycle

    @classmethod
    def live_store_at(cls, db_path: str | Path) -> EventStore | None:
        """The open same-process store bound to *db_path*, if any (F-1).

        Restore/backup use this to coordinate with a live handle instead of
        byte-replacing a file out from under it (the WAL chimera). Only
        same-process handles are visible here; a foreign process is detected
        separately through the WAL sidecar locks.
        """
        if str(db_path) == ":memory:":
            return None
        with _LIVE_STORES_LOCK:
            return _LIVE_STORES.get(os.path.realpath(str(db_path)))

    def reopen_in_place(self) -> None:
        """Reopen this store's connection against its db file after a byte-replace.

        The store object is *shared* (service, projector, audit log, engines
        all hold the same instance), so a file swap must never rebind a new
        object — the connection is transplanted in place and every holder
        stays valid (W3-1, F-1). A fresh EventStore performs the real connect
        (pragmas + migrations), so the connection recipe stays owned here.
        The caller must hold ``self._lock``.
        """
        fresh = EventStore(
            self.db_path,
            migrations_dir=self._migrations_dir,
            busy_timeout_ms=self._busy_timeout_ms,
        )
        self._conn = fresh._conn
        if self.db_path != ":memory:":
            with _LIVE_STORES_LOCK:
                _LIVE_STORES[os.path.realpath(self.db_path)] = self

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> EventStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize one write transaction (``BEGIN IMMEDIATE`` ... ``COMMIT``)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    def execute_raw(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        """Run a statement inside a write transaction (used by guard tests)."""
        with self.transaction() as conn:
            return conn.execute(sql, parameters)

    # -------------------------------------------------------------- migration

    def migrate(self) -> None:
        with self._lock:
            conn = self._conn
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migration "
                "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
            )
            applied = {int(row["version"]) for row in conn.execute("SELECT version FROM schema_migration")}
            for path in sorted(self._migrations_dir.glob("*.sql")):
                version = int(path.name.split("_", 1)[0])
                if version in applied:
                    continue
                conn.executescript(path.read_text(encoding="utf-8"))
                conn.execute(
                    "INSERT OR IGNORE INTO schema_migration (version, name, applied_at) VALUES (?, ?, ?)",
                    (version, path.name, _now_iso()),
                )
            self._apply_versioned_migrations(conn)

    # ------------------------------------------------------ schema versioning
    # v1/v2 live in migrations/*.sql (ledger ``schema_migration``). v3 onward
    # are applied here: one BEGIN IMMEDIATE transaction per version, recorded
    # in ``schema_versions``. Rollback-safe (a failed statement aborts the
    # whole version), idempotent (a recorded version with all its objects is
    # a no-op), and fail-closed (newer-than-supported versions and
    # ledger/object disagreements raise typed errors instead of guessing).

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return (
            conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone()
            is not None
        )

    def _apply_versioned_migrations(self, conn: sqlite3.Connection) -> None:
        recorded = 0
        if self._table_exists(conn, "schema_versions"):
            row = conn.execute("SELECT MAX(version) AS v FROM schema_versions").fetchone()
            recorded = int(row["v"] or 0) if row is not None else 0
        if recorded > SCHEMA_VERSION:
            raise SchemaVersionTooNew(
                f"database schema is v{recorded}, newer than this build supports (v{SCHEMA_VERSION}); refusing to open"
            )
        if recorded >= 3:
            missing = [name for name in _SCHEMA_V3_TABLES if not self._table_exists(conn, name)]
            if missing:
                raise SchemaStateError(
                    f"schema_versions records v3 but tables are missing: {', '.join(missing)}; "
                    "refusing to open an inconsistent store"
                )
            return
        if SCHEMA_VERSION < 3:
            # Capped below v3 (tests building genuine pre-v3 fixtures): leave
            # the store exactly as v1/v2, including no schema_versions ledger.
            return
        present = [name for name in _SCHEMA_V3_TABLES if self._table_exists(conn, name)]
        if present:
            raise SchemaStateError(
                f"v3 tables exist without a schema_versions record: {', '.join(present)}; "
                "refusing to guess the schema state"
            )
        try:
            with self.transaction() as tx:
                tx.execute(
                    "CREATE TABLE schema_versions ("
                    "    version INTEGER PRIMARY KEY,"
                    "    applied_at TEXT NOT NULL,"
                    "    description TEXT"
                    ")"
                )
                # Backfill the file-based v1/v2 history into the versioned
                # ledger so schema_versions alone answers "what version am I".
                tx.execute(
                    "INSERT INTO schema_versions (version, applied_at, description) "
                    "SELECT version, applied_at, name FROM schema_migration WHERE version < 3"
                )
                for statement in _SCHEMA_V3_STATEMENTS:
                    tx.execute(statement)
                tx.execute(
                    "INSERT INTO schema_versions (version, applied_at, description) VALUES (3, ?, ?)",
                    (_now_iso(), _SCHEMA_V3_DESCRIPTION),
                )
        except StoreError:
            raise
        except sqlite3.Error as error:
            raise MigrationError(f"schema migration to v3 failed and was rolled back: {error}") from error

    def applied_migrations(self) -> list[int]:
        with self.read() as conn:
            return sorted(int(row["version"]) for row in conn.execute("SELECT version FROM schema_migration"))

    def table_names(self) -> set[str]:
        with self.read() as conn:
            rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
            return {str(row["name"]) for row in rows}

    # ---------------------------------------------------------------- events

    def insert_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_id: str,
        kind: str,
        event_type: str,
        knowledge_affecting: bool,
        actor: str,
        occurred_at: str,
        payload: Mapping[str, Any],
        node_id: str | None = None,
        domain: str | None = None,
        source_version_id: str | None = None,
        node_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> EventRow:
        """Append one event inside an open transaction and return its identity.

        The payload is quantized at this serialization boundary (every Decimal
        score to 12 dp, ``ROUND_HALF_EVEN``) before it is persisted or hashed.
        The chain anchor in ``system_state`` (expected head hash and event
        count) is updated in the same transaction.
        """
        previous = conn.execute(
            "SELECT event_seq, event_hash FROM event_log ORDER BY event_seq DESC LIMIT 1"
        ).fetchone()
        event_seq = int(previous["event_seq"]) + 1 if previous is not None else 1
        prev_event_hash = str(previous["event_hash"]) if previous is not None else None
        knowledge_seq: int | None = None
        if knowledge_affecting:
            current = conn.execute("SELECT MAX(knowledge_seq) AS maximum FROM event_log").fetchone()
            knowledge_seq = int(current["maximum"] or 0) + 1
        payload_json = canonical_json(storage_payload(payload))
        recorded_at = _now_iso()
        event_hash = _digest(
            _hash_input(
                event_seq=event_seq,
                event_id=event_id,
                kind=kind,
                event_type=event_type,
                knowledge_affecting=knowledge_affecting,
                knowledge_seq=knowledge_seq,
                node_id=node_id,
                domain=domain,
                source_version_id=source_version_id,
                node_version=node_version,
                actor=actor,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
                payload_json=payload_json,
                idempotency_key=idempotency_key,
                prev_event_hash=prev_event_hash,
            )
        )
        conn.execute(
            "INSERT INTO event_log (event_seq, event_id, kind, event_type, knowledge_affecting, knowledge_seq, "
            "node_id, domain, source_version_id, node_version, actor, occurred_at, recorded_at, payload_json, "
            "prev_event_hash, event_hash, idempotency_key) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_seq,
                event_id,
                kind,
                event_type,
                int(knowledge_affecting),
                knowledge_seq,
                node_id,
                domain,
                source_version_id,
                node_version,
                actor,
                occurred_at,
                recorded_at,
                payload_json,
                prev_event_hash,
                event_hash,
                idempotency_key,
            ),
        )
        count = conn.execute("SELECT COUNT(*) AS total FROM event_log").fetchone()
        for key, value in (("chain_head_hash", event_hash), ("chain_event_count", str(int(count["total"])))):
            conn.execute(
                "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, recorded_at),
            )
        return EventRow(
            event_seq=event_seq,
            event_id=event_id,
            knowledge_seq=knowledge_seq,
            node_version=node_version,
            prev_event_hash=prev_event_hash,
            event_hash=event_hash,
        )

    def _row_hash(self, row: sqlite3.Row) -> str:
        return _digest(
            _hash_input(
                event_seq=int(row["event_seq"]),
                event_id=str(row["event_id"]),
                kind=str(row["kind"]),
                event_type=str(row["event_type"]),
                knowledge_affecting=bool(row["knowledge_affecting"]),
                knowledge_seq=row["knowledge_seq"],
                node_id=row["node_id"],
                domain=row["domain"],
                source_version_id=row["source_version_id"],
                node_version=row["node_version"],
                actor=str(row["actor"]),
                occurred_at=str(row["occurred_at"]),
                recorded_at=str(row["recorded_at"]),
                payload_json=str(row["payload_json"]),
                idempotency_key=row["idempotency_key"],
                prev_event_hash=row["prev_event_hash"],
            )
        )

    def verify_chain(self) -> ChainReport:
        """Re-derive the whole hash chain and report the first broken link.

        Beyond the per-event links, the persisted anchor (``chain_head_hash``
        and ``chain_event_count`` in ``system_state``) is cross-checked so a
        truncated tail is detected, and every completed journal's
        ``audit_event_seq`` must resolve to that audit event in the log.
        """
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM event_log ORDER BY event_seq").fetchall()
            anchor = {
                str(row["key"]): str(row["value"])
                for row in conn.execute(
                    "SELECT key, value FROM system_state WHERE key IN ('chain_head_hash', 'chain_event_count')"
                )
            }
            journals = conn.execute(
                "SELECT audit_event_id, audit_event_seq FROM invocation_journal "
                "WHERE status = 'complete' AND audit_event_seq IS NOT NULL"
            ).fetchall()
            audit_events = {
                int(row["event_seq"]): str(row["event_id"])
                for row in conn.execute("SELECT event_seq, event_id FROM event_log WHERE kind = 'audit'")
            }
        prev_event_hash: str | None = None
        head_hash: str | None = None
        checked = 0
        for row in rows:
            if row["prev_event_hash"] != prev_event_hash or self._row_hash(row) != row["event_hash"]:
                return ChainReport(ok=False, checked=checked, first_bad_seq=int(row["event_seq"]))
            prev_event_hash = str(row["event_hash"])
            head_hash = prev_event_hash
            checked += 1
        expected_count = anchor.get("chain_event_count")
        if expected_count is not None and int(expected_count) != checked:
            return ChainReport(ok=False, checked=checked, first_bad_seq=checked + 1)
        expected_head = anchor.get("chain_head_hash")
        if expected_head is not None and expected_head != head_hash:
            last_seq = int(rows[-1]["event_seq"]) if rows else None
            return ChainReport(ok=False, checked=checked, first_bad_seq=last_seq)
        if rows and not anchor:
            # Events exist but the anchor is missing entirely: fail closed.
            return ChainReport(ok=False, checked=checked, first_bad_seq=None)
        for journal in journals:
            audit_event_seq = int(journal["audit_event_seq"])
            if audit_events.get(audit_event_seq) != str(journal["audit_event_id"]):
                return ChainReport(ok=False, checked=checked, first_bad_seq=audit_event_seq)
        return ChainReport(ok=True, checked=checked, first_bad_seq=None)

    # ------------------------------------------------------- domain mutations

    def _node_version(self, conn: sqlite3.Connection, node_id: str) -> int:
        row = conn.execute("SELECT MAX(version) AS version FROM node_revision WHERE node_id = ?", (node_id,)).fetchone()
        return int(row["version"] or 0)

    def append_domain(
        self,
        event: Event,
        *,
        idempotency_key: str,
        expected_version: int | None = None,
    ) -> DomainReceipt:
        """Append one domain event, deduplicating on *idempotency_key*."""
        with self.transaction() as conn:
            existing = conn.execute(
                "SELECT event_id, event_seq, node_version, knowledge_seq, payload_json FROM event_log "
                "WHERE idempotency_key = ? AND kind = 'domain'",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if self._conflicting_reuse(conn, idempotency_key, event, str(existing["payload_json"])):
                    raise IdempotencyConflict(
                        f"idempotency key {idempotency_key!r} is already bound to a different request "
                        f"(stored event {str(existing['event_id'])!r}); refusing to silently drop this write"
                    )
                return DomainReceipt(
                    domain_event_id=str(existing["event_id"]),
                    event_seq=int(existing["event_seq"]),
                    node_version=int(existing["node_version"] or 0),
                    knowledge_seq=existing["knowledge_seq"],
                    idempotent_replay=True,
                )

            current_version = self._node_version(conn, event.node_id)
            if event.kind in _NODE_REQUIRED_KINDS and current_version == 0:
                raise InvalidRelation(f"{event.kind.value} references unknown node {event.node_id!r}")
            if expected_version is not None and expected_version != current_version:
                raise VersionConflict(
                    f"expected node version {expected_version} but {event.node_id!r} is at {current_version}"
                )
            if event.kind is EventKind.RELATION_ADD:
                relation = event.relation
                if relation is None:  # pragma: no cover - guaranteed by Event validation
                    raise InvalidRelation("relation.add event carried no relation")
                if self._node_version(conn, relation.target_node_id) == 0:
                    raise InvalidRelation(f"relation target {relation.target_node_id!r} does not exist")

            node_version = current_version + 1 if event.kind in _VERSION_BUMPING_KINDS else current_version
            row = self.insert_event(
                conn,
                event_id=event.event_id,
                kind="domain",
                event_type=event.kind.value,
                knowledge_affecting=event.knowledge_affecting,
                actor=event.actor,
                occurred_at=event.occurred_at.isoformat(),
                payload=event.model_dump(mode="python"),
                node_id=event.node_id,
                domain=event.domain.value,
                source_version_id=event.source_version_id,
                node_version=node_version,
                idempotency_key=idempotency_key,
            )
            self._materialize(conn, event, node_version=node_version, event_seq=row.event_seq)
            return DomainReceipt(
                domain_event_id=event.event_id,
                event_seq=row.event_seq,
                node_version=node_version,
                knowledge_seq=row.knowledge_seq,
                idempotent_replay=False,
            )

    @staticmethod
    def _conflicting_reuse(
        conn: sqlite3.Connection, idempotency_key: str, event: Event, stored_payload_json: str
    ) -> bool:
        """Decide whether an idempotency-key hit is a genuine retry or a collision.

        The request fingerprint (``_event_request_fingerprint``) strips
        per-attempt identity and environmental context — source version,
        record timestamps, node version — so a genuine retry and a deliberate
        re-assertion like ``ensure_roots`` under a newer source version replay.
        Identical fingerprints replay.

        Some callers legitimately retry with drifting *substance* — an
        improvement re-apply re-derives the node from its new version. Those
        flows journal every attempt, so a retry is proven when two or more
        audited invocations under this key (the stored write's and the current
        one) carry a single identical ``request_hash``. Anything else — raw
        writes with no journals, or journals that disagree — is a collision
        and must fail loud rather than silently drop the write (finding F3).
        """
        stored = json.loads(stored_payload_json)
        if _event_request_fingerprint(stored) == _event_request_fingerprint(event.model_dump(mode="python")):
            return False
        rows = conn.execute(
            "SELECT request_hash FROM invocation_journal WHERE effective_idempotency_key = ?",
            (idempotency_key,),
        ).fetchall()
        distinct_hashes = {str(row["request_hash"]) for row in rows}
        return len(rows) < 2 or len(distinct_hashes) != 1

    def _materialize(self, conn: sqlite3.Connection, event: Event, *, node_version: int, event_seq: int) -> None:
        if event.kind is EventKind.NODE_UPSERT and event.node is not None:
            self._insert_revision(conn, event.node, node_version=node_version, event_seq=event_seq)
        elif event.kind is EventKind.NODE_RETIRE and event.node is not None:
            retired = event.node.model_copy(
                update={"version": node_version, "lifecycle_status": LifecycleStatus.SUPERSEDED}
            )
            self._insert_revision(conn, retired, node_version=node_version, event_seq=event_seq)
        elif event.kind is EventKind.EVIDENCE_APPEND and event.evidence is not None:
            evidence = event.evidence
            conn.execute(
                "INSERT INTO evidence (evidence_id, node_id, source_version_id, source_uri, observed_at, "
                "event_seq, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.evidence_id,
                    evidence.node_id,
                    evidence.source_version_id,
                    evidence.source_uri,
                    evidence.observed_at.isoformat(),
                    event_seq,
                    canonical_json(storage_payload(evidence.model_dump(mode="python"))),
                ),
            )
        elif event.kind is EventKind.RELATION_ADD and event.relation is not None:
            relation = event.relation
            conn.execute(
                "INSERT INTO node_relation (event_seq, source_node_id, target_node_id, relation, valid_from, "
                "valid_to) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event_seq,
                    relation.source_node_id,
                    relation.target_node_id,
                    relation.kind.value,
                    relation.valid_from.isoformat() if relation.valid_from else None,
                    relation.valid_to.isoformat() if relation.valid_to else None,
                ),
            )

    def _insert_revision(self, conn: sqlite3.Connection, node: Node, *, node_version: int, event_seq: int) -> None:
        revision = node.model_copy(update={"version": node_version})
        conn.execute(
            "INSERT INTO node_revision (node_id, version, domain, kind, parent_id, lifecycle_status, "
            "source_version_id, created_at, event_seq, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                revision.node_id,
                node_version,
                revision.domain.value,
                revision.kind.value,
                revision.parent_id,
                revision.lifecycle_status.value,
                revision.source_version_id,
                revision.created_at.isoformat(),
                event_seq,
                canonical_json(storage_payload(revision.model_dump(mode="python"))),
            ),
        )

    # -------------------------------------------------------------- read side

    def replay_domain(self) -> Iterator[Event]:
        """Replay every domain event in global ``event_seq`` order."""
        with self.read() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM event_log WHERE kind = 'domain' ORDER BY event_seq"
            ).fetchall()
        return iter([Event.model_validate(json.loads(str(row["payload_json"]))) for row in rows])

    def current_node(self, node_id: str) -> Node | None:
        with self.read() as conn:
            row = conn.execute(
                "SELECT payload_json FROM node_revision WHERE node_id = ? ORDER BY version DESC LIMIT 1",
                (node_id,),
            ).fetchone()
        if row is None:
            return None
        return Node.model_validate(json.loads(str(row["payload_json"])))

    def node_version(self, node_id: str) -> int:
        with self.read() as conn:
            return self._node_version(conn, node_id)

    def get_evidence(self, evidence_id: str) -> Evidence | None:
        with self.read() as conn:
            row = conn.execute("SELECT payload_json FROM evidence WHERE evidence_id = ?", (evidence_id,)).fetchone()
        if row is None:
            return None
        return Evidence.model_validate(json.loads(str(row["payload_json"])))

    def relations_from(self, node_id: str) -> list[Relation]:
        with self.read() as conn:
            rows = conn.execute(
                "SELECT source_node_id, target_node_id, relation, valid_from, valid_to FROM node_relation "
                "WHERE source_node_id = ? ORDER BY relation_seq",
                (node_id,),
            ).fetchall()
        return [
            Relation(
                kind=RelationKind(str(row["relation"])),
                source_node_id=str(row["source_node_id"]),
                target_node_id=str(row["target_node_id"]),
                valid_from=datetime.fromisoformat(str(row["valid_from"])) if row["valid_from"] else None,
                valid_to=datetime.fromisoformat(str(row["valid_to"])) if row["valid_to"] else None,
            )
            for row in rows
        ]

    def event_high_watermark(self) -> int:
        with self.read() as conn:
            row = conn.execute("SELECT MAX(event_seq) AS maximum FROM event_log").fetchone()
        return int(row["maximum"] or 0)

    def knowledge_high_watermark(self) -> int:
        with self.read() as conn:
            row = conn.execute("SELECT MAX(knowledge_seq) AS maximum FROM event_log").fetchone()
        return int(row["maximum"] or 0)

    def event_sequences(self) -> list[int]:
        with self.read() as conn:
            return [int(row["event_seq"]) for row in conn.execute("SELECT event_seq FROM event_log ORDER BY event_seq")]

    def event_count(self) -> int:
        return self._scalar("SELECT COUNT(*) AS total FROM event_log")

    def domain_event_count(self) -> int:
        return self._scalar("SELECT COUNT(*) AS total FROM event_log WHERE kind = 'domain'")

    def audit_event_count(self) -> int:
        return self._scalar("SELECT COUNT(*) AS total FROM event_log WHERE kind = 'audit'")

    def evidence_count(self) -> int:
        return self._scalar("SELECT COUNT(*) AS total FROM evidence")

    def source_version_count(self) -> int:
        return self._scalar("SELECT COUNT(*) AS total FROM source_version")

    def _scalar(self, sql: str) -> int:
        with self.read() as conn:
            row = conn.execute(sql).fetchone()
        return int(row["total"] or 0)

    # ----------------------------------------------------- publish capacity

    def estimate_publish_bytes(
        self,
        *,
        extra_payload_bytes: int = 0,
        extra_nodes: int = 0,
        extra_edges: int = 0,
    ) -> int:
        """Conservative upper estimate of the next published ``graph.json`` size.

        Final flow audit F-3: the write-side guard and the readiness capacity
        report share this estimate. It is built from the store alone — the
        canonical ``payload_json`` length of every live latest revision plus a
        fixed per-node/per-edge overhead — so it never reads the projection
        and never under-counts the real artifact (calibrated against the
        frozen corpus: estimate ≈ 1.6× the real ``graph.json``, always above
        it). ``extra_*`` price in a prospective write before it commits.
        """
        with self.read() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(nr.payload_json)), 0) AS bytes, "
                "COALESCE(MAX(LENGTH(nr.node_id)), 0) AS max_id "
                "FROM node_revision nr "
                "JOIN (SELECT node_id, MAX(version) AS v FROM node_revision GROUP BY node_id) latest "
                "ON latest.node_id = nr.node_id AND latest.v = nr.version "
                "WHERE json_extract(nr.payload_json, '$.lifecycle_status') != 'superseded'"
            ).fetchone()
            relations = conn.execute("SELECT COUNT(*) AS n FROM node_relation").fetchone()
        live_nodes = int(row["n"]) if row is not None else 0
        node_bytes = int(row["bytes"]) if row is not None else 0
        max_id = int(row["max_id"]) if row is not None else 0
        relation_count = int(relations["n"]) if relations is not None else 0
        # Every live node may also carry a parent edge, so the edge estimate
        # counts relations plus live nodes (fail-closed over-count).
        edges = relation_count + live_nodes + extra_edges
        return (
            node_bytes
            + extra_payload_bytes
            + PUBLISH_ESTIMATE_NODE_OVERHEAD_BYTES * (live_nodes + extra_nodes)
            + (2 * max_id + PUBLISH_ESTIMATE_EDGE_OVERHEAD_BYTES) * edges
            + PUBLISH_ESTIMATE_FIXED_BYTES
        )

    # -------------------------------------------------------- invocation state

    def insert_journal(self, conn: sqlite3.Connection, invocation: Invocation) -> None:
        conn.execute(
            "INSERT INTO invocation_journal (invocation_id, audit_event_id, operation, request_hash, actor, "
            "idempotency_key, effective_idempotency_key, started_at, heartbeat_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'in_progress')",
            (
                invocation.invocation_id,
                invocation.audit_event_id,
                invocation.operation,
                invocation.request_hash,
                invocation.actor,
                invocation.idempotency_key,
                invocation.idempotency_key or invocation.invocation_id,
                invocation.started_at.isoformat(),
                invocation.started_at.isoformat(),
            ),
        )

    def fetch_journal(self, conn: sqlite3.Connection, invocation_id: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM invocation_journal WHERE invocation_id = ?", (invocation_id,)).fetchone()

    def complete_journal(
        self,
        conn: sqlite3.Connection,
        invocation_id: str,
        *,
        outcome: Mapping[str, Any],
        audit_event_seq: int,
    ) -> None:
        conn.execute(
            "UPDATE invocation_journal SET status = 'complete', completed_at = ?, outcome_json = ?, "
            "audit_event_seq = ? WHERE invocation_id = ? AND status = 'in_progress'",
            (_now_iso(), canonical_json(storage_payload(outcome)), audit_event_seq, invocation_id),
        )

    def refresh_journal_heartbeat(self, invocation_id: str) -> None:
        """Refresh the liveness heartbeat of an in-progress journal (finding I1)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE invocation_journal SET heartbeat_at = ? WHERE invocation_id = ? AND status = 'in_progress'",
                (_now_iso(), invocation_id),
            )

    def journal_status(self, invocation_id: str) -> str | None:
        with self.read() as conn:
            row = self.fetch_journal(conn, invocation_id)
        return None if row is None else str(row["status"])

    def journal_outcome_json(self, invocation_id: str) -> str | None:
        with self.read() as conn:
            row = self.fetch_journal(conn, invocation_id)
        if row is None or row["outcome_json"] is None:
            return None
        return str(row["outcome_json"])

    def incomplete_journals(self, stale_after_seconds: float | None = None) -> list[sqlite3.Row]:
        """In-progress journals; with a threshold, only those stale beyond it.

        Staleness is measured from the last heartbeat (falling back to
        ``started_at``), so a live invocation whose writer refreshes its
        heartbeat is never treated as crashed.
        """
        with self.read() as conn:
            rows = list(
                conn.execute("SELECT * FROM invocation_journal WHERE status = 'in_progress' ORDER BY started_at")
            )
        if stale_after_seconds is None:
            return rows
        now = datetime.now(UTC)
        stale: list[sqlite3.Row] = []
        for row in rows:
            last_seen = datetime.fromisoformat(str(row["heartbeat_at"] or row["started_at"]))
            if (now - last_seen).total_seconds() > stale_after_seconds:
                stale.append(row)
        return stale

    def domain_event_by_idempotency(self, idempotency_key: str) -> sqlite3.Row | None:
        with self.read() as conn:
            return conn.execute(
                "SELECT event_id, event_seq, node_version, knowledge_seq FROM event_log "
                "WHERE idempotency_key = ? AND kind = 'domain'",
                (idempotency_key,),
            ).fetchone()

    # ------------------------------------------------------------- projection

    def heal_lying_projection_watermark(self) -> bool:
        """Reset the activation watermark marker when it lies ahead of the store (F-2).

        The projector's activation CAS (``active_projection_watermark`` in
        ``system_state``) rejects a generation whose knowledge watermark is
        below the active one, so out-of-order publication is impossible. A
        rebuild with a future ``upto_knowledge_seq`` poisons that marker —
        after which *no* honest rebuild can ever activate and the read model
        freezes permanently, even across restarts. The marker can never
        honestly exceed the store's real knowledge watermark, so pulling it
        back to the real watermark is always safe. Returns True when a lie
        was healed.
        """
        with self.transaction() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = 'active_projection_watermark'").fetchone()
            if row is None:
                return False
            real_row = conn.execute("SELECT MAX(knowledge_seq) AS maximum FROM event_log").fetchone()
            real = int(real_row["maximum"] or 0) if real_row is not None else 0
            claimed = int(str(row["value"]))
            if claimed <= real:
                return False
            conn.execute(
                "UPDATE system_state SET value = ?, updated_at = ? WHERE key = 'active_projection_watermark'",
                (str(real), _now_iso()),
            )
        return True

    def set_projection_version(self, version: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO system_state (key, value, updated_at) VALUES ('active_projection_version', ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (version, _now_iso()),
            )

    def projection_version(self) -> str | None:
        with self.read() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = 'active_projection_version'").fetchone()
        return None if row is None else str(row["value"])

    # ------------------------------------------------------------------ commit

    def commit(
        self,
        event: Event,
        *,
        invocation: Invocation,
        expected_version: int | None = None,
        audit_log: AuditLog | None = None,
    ) -> WriteReceipt:
        """Journal, append one domain event, then close with exactly one final audit.

        ``WriteReceipt`` returns the original domain fields plus the new
        per-invocation ``audit_event_id``/final audit ``event_seq``, the active
        projection version and the source knowledge high-water mark (spec 7.9).

        The journal heartbeat is refreshed around the domain write so a
        concurrent recovery pass never closes a live invocation (finding I1).
        The final audit receipt is asserted against the requested outcome; on a
        mismatch a typed :class:`AuditOutcomeMismatch` is raised carrying both
        outcomes, chained to the original error (ruling C1).
        """
        from know_your_soul.audit import AuditLog, failure_outcome, success_outcome

        started = time.monotonic()
        audit = audit_log if audit_log is not None else AuditLog(self)
        audit.begin(invocation)
        idempotency_key = invocation.idempotency_key or invocation.invocation_id
        try:
            self.refresh_journal_heartbeat(invocation.invocation_id)
            domain = self.append_domain(event, idempotency_key=idempotency_key, expected_version=expected_version)
            self.refresh_journal_heartbeat(invocation.invocation_id)
        except Exception as error:
            # A rejected domain write is still a valid invocation: it receives
            # exactly one final failure audit before the error propagates.
            try:
                failure_receipt = audit.complete(
                    invocation.invocation_id,
                    failure_outcome(invocation, started, error),
                )
            except Exception as audit_error:
                raise StoreError(
                    f"domain write for invocation {invocation.invocation_id!r} failed "
                    f"({type(error).__name__}: {error}) and the failure audit could not be recorded "
                    f"({type(audit_error).__name__}: {audit_error})"
                ) from error
            if failure_receipt.status != "failure":
                raise AuditOutcomeMismatch(
                    f"commit requested audit status 'failure' for invocation {invocation.invocation_id!r} "
                    f"but the final audit records {failure_receipt.status!r}"
                ) from error
            raise
        receipt = audit.complete(
            invocation.invocation_id,
            success_outcome(
                invocation,
                started,
                result_references={"operation": invocation.operation},
                linked_domain_event_id=domain.domain_event_id,
            ),
        )
        if receipt.status != "success":
            raise AuditOutcomeMismatch(
                f"commit requested audit status 'success' for invocation {invocation.invocation_id!r} "
                f"but the final audit records {receipt.status!r}"
            ) from StoreError(f"recorded audit outcome: {dict(receipt.outcome)}")
        return WriteReceipt(
            domain_event_id=domain.domain_event_id,
            event_seq=domain.event_seq,
            node_version=domain.node_version,
            knowledge_seq=domain.knowledge_seq,
            idempotent_replay=domain.idempotent_replay,
            audit_event_id=receipt.audit_event_id,
            audit_event_seq=receipt.event_seq,
            projection_version=self.projection_version(),
            source_knowledge_high_watermark=self.knowledge_high_watermark(),
        )

    # --------------------------------------------------------- source registry

    def retained_source_version(self, source_version_id: str) -> dict[str, Any] | None:
        """Return a retained source-version row (superseded/deleted rows included)."""
        with self.read() as conn:
            row = conn.execute(
                "SELECT * FROM source_version WHERE source_version_id = ?", (source_version_id,)
            ).fetchone()
        return None if row is None else dict(row)

    # ------------------------------------------------------------ v3: files

    def upsert_file(
        self,
        *,
        path: str,
        sha256: str,
        size: int,
        mtime: int,
        language: str,
        generated_hint: bool,
        extraction_version: int,
    ) -> None:
        """Record one tracked source file, replacing any earlier observation."""
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO files (path, sha256, size, mtime, language, generated_hint, extraction_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (path) DO UPDATE SET "
                "sha256 = excluded.sha256, size = excluded.size, mtime = excluded.mtime, "
                "language = excluded.language, generated_hint = excluded.generated_hint, "
                "extraction_version = excluded.extraction_version",
                (path, sha256, int(size), int(mtime), language, int(generated_hint), int(extraction_version)),
            )

    @staticmethod
    def _file_row(row: sqlite3.Row) -> FileRow:
        return FileRow(
            path=str(row["path"]),
            sha256=str(row["sha256"]),
            size=int(row["size"]),
            mtime=int(row["mtime"]),
            language=str(row["language"]),
            generated_hint=bool(row["generated_hint"]),
            extraction_version=int(row["extraction_version"]),
        )

    def get_file(self, path: str) -> FileRow | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return None if row is None else self._file_row(row)

    def list_files(self, *, language: str | None = None) -> list[FileRow]:
        with self.read() as conn:
            if language is None:
                rows = conn.execute("SELECT * FROM files ORDER BY path").fetchall()
            else:
                rows = conn.execute("SELECT * FROM files WHERE language = ? ORDER BY path", (language,)).fetchall()
        return [self._file_row(row) for row in rows]

    # ---------------------------------------------------------- v3: symbols

    def upsert_symbol(self, *, node_id: str, name: str, kind: str, path: str, line: int, col: int = -1) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO symbols (node_id, name, kind, path, line, col) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (node_id) DO UPDATE SET "
                "name = excluded.name, kind = excluded.kind, path = excluded.path, "
                "line = excluded.line, col = excluded.col",
                (node_id, name, kind, path, int(line), int(col)),
            )

    @staticmethod
    def _symbol_row(row: sqlite3.Row) -> SymbolRow:
        return SymbolRow(
            node_id=str(row["node_id"]),
            name=str(row["name"]),
            kind=str(row["kind"]),
            path=str(row["path"]),
            line=int(row["line"]),
            col=int(row["col"]),
        )

    def symbol_by_node(self, node_id: str) -> SymbolRow | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM symbols WHERE node_id = ?", (node_id,)).fetchone()
        return None if row is None else self._symbol_row(row)

    def symbols_named(self, name: str, *, case_insensitive: bool = False) -> list[SymbolRow]:
        """Exact-name lookup; the insensitive form seeks ``idx_symbols_lower_name``.

        The insensitive predicate must stay exactly ``lower(name) = lower(?)``
        — any other form (``COLLATE NOCASE``, pre-lowered parameter) silently
        degenerates to a table scan (codegraph searchNodes rule).
        """
        with self.read() as conn:
            if case_insensitive:
                rows = conn.execute(
                    "SELECT * FROM symbols WHERE lower(name) = lower(?) ORDER BY path, line, col, node_id",
                    (name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM symbols WHERE name = ? ORDER BY path, line, col, node_id", (name,)
                ).fetchall()
        return [self._symbol_row(row) for row in rows]

    def symbols_in_path(self, path: str) -> list[SymbolRow]:
        with self.read() as conn:
            rows = conn.execute("SELECT * FROM symbols WHERE path = ? ORDER BY line, col, node_id", (path,)).fetchall()
        return [self._symbol_row(row) for row in rows]

    # ------------------------------------------------------ v3: graph edges

    def add_graph_edge(
        self,
        source: str,
        target: str,
        kind: str,
        line: int | None = None,
        col: int | None = None,
        metadata: str | None = None,
        provenance: str | None = None,
    ) -> bool:
        """Insert one edge; identity duplicates (per ``idx_graph_edges_identity``) are ignored.

        Returns True when a row was actually inserted.
        """
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO graph_edges (source, target, kind, line, col, metadata, provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (source, target, kind, line, col, metadata, provenance),
            )
        return cursor.rowcount > 0

    def add_graph_edges(self, edges: Iterable[Mapping[str, Any]]) -> int:
        """Insert a batch of edges in one transaction; returns the inserted count.

        Each entry carries ``source``, ``target``, ``kind`` and optional
        ``line``/``col``/``metadata``/``provenance``. Iteration order is the
        insertion order, so a deterministic caller gets deterministic rowids.
        """
        inserted = 0
        with self.transaction() as conn:
            for edge in edges:
                cursor = conn.execute(
                    "INSERT OR IGNORE INTO graph_edges (source, target, kind, line, col, metadata, provenance) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(edge["source"]),
                        str(edge["target"]),
                        str(edge["kind"]),
                        edge.get("line"),
                        edge.get("col"),
                        edge.get("metadata"),
                        edge.get("provenance"),
                    ),
                )
                inserted += cursor.rowcount
        return inserted

    @staticmethod
    def _graph_edge_row(row: sqlite3.Row) -> GraphEdgeRow:
        return GraphEdgeRow(
            edge_id=int(row["edge_id"]),
            source=str(row["source"]),
            target=str(row["target"]),
            kind=str(row["kind"]),
            line=None if row["line"] is None else int(row["line"]),
            col=None if row["col"] is None else int(row["col"]),
            metadata=None if row["metadata"] is None else str(row["metadata"]),
            provenance=None if row["provenance"] is None else str(row["provenance"]),
        )

    def _adjacency_sql(self, direction: str, kinds: tuple[str, ...] | None) -> str:
        """The prepared-statement SQL for one adjacency shape (cached, bounded).

        Column and ordering mirror codegraph's adjacency pair: outgoing orders
        by ``kind, target``, incoming by ``kind, source``, both then by the
        IFNULL-folded position so NULL and -1 sort identically to the unique
        edge identity.
        """
        key = (direction, kinds or ())
        sql = self._adjacency_sql_cache.get(key)
        if sql is not None:
            return sql
        if direction == "outgoing":
            where_column, order_column = "source", "target"
        else:
            where_column, order_column = "target", "source"
        sql = (
            "SELECT edge_id, source, target, kind, line, col, metadata, provenance FROM graph_edges "
            f"WHERE {where_column} = ?"
        )
        if kinds:
            placeholders = ", ".join("?" for _ in kinds)
            sql += f" AND kind IN ({placeholders})"
        sql += f" ORDER BY kind, {order_column}, IFNULL(line, -1), IFNULL(col, -1), edge_id"
        if len(self._adjacency_sql_cache) >= _ADJACENCY_SQL_CACHE_LIMIT:
            self._adjacency_sql_cache.clear()
        self._adjacency_sql_cache[key] = sql
        return sql

    def _adjacency(self, endpoint: str, direction: str, kinds: tuple[str, ...] | None) -> list[GraphEdgeRow]:
        sql = self._adjacency_sql(direction, kinds)
        parameters: tuple[Any, ...] = (endpoint, *(kinds or ()))
        with self.read() as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return [self._graph_edge_row(row) for row in rows]

    def outgoing_edges(self, source: str, *, kinds: Sequence[str] | None = None) -> list[GraphEdgeRow]:
        """All edges leaving ``source``, deterministic order, optional kind filter."""
        return self._adjacency(source, "outgoing", tuple(kinds) if kinds else None)

    def incoming_edges(self, target: str, *, kinds: Sequence[str] | None = None) -> list[GraphEdgeRow]:
        """All edges arriving at ``target``, deterministic order, optional kind filter."""
        return self._adjacency(target, "incoming", tuple(kinds) if kinds else None)

    def subtree(
        self,
        root: str,
        *,
        max_depth: int,
        kinds: Sequence[str] = ("contains",),
    ) -> list[tuple[str, int]]:
        """BFS over outgoing edges from ``root``, bounded at ``max_depth``.

        Returns ``(node_id, depth)`` pairs in deterministic visit order (level
        by level, adjacency order within a level). Cycles are cut by a visited
        set; depth is computed here — never stored (hierarchy-is-edges rule).
        """
        if max_depth < 0:
            raise ValueError(f"max_depth must be >= 0, got {max_depth}")
        result: list[tuple[str, int]] = [(root, 0)]
        visited = {root}
        frontier = [root]
        depth = 0
        while frontier and depth < max_depth:
            next_frontier: list[str] = []
            for node in frontier:
                for edge in self.outgoing_edges(node, kinds=tuple(kinds)):
                    if edge.target in visited:
                        continue
                    visited.add(edge.target)
                    result.append((edge.target, depth + 1))
                    next_frontier.append(edge.target)
            frontier = next_frontier
            depth += 1
        return result

    # ------------------------------------------------- v3: unresolved refs

    def insert_unresolved_ref(
        self,
        *,
        source_node_id: str,
        target_name: str,
        kind: str,
        name_tail: str | None = None,
        first_seen: str | None = None,
    ) -> int:
        """Park one cross-file reference as ``pending``; returns its ref_id.

        ``name_tail`` defaults to the last dot segment of ``target_name`` —
        the retry lookup key when a candidate file lands later.
        """
        for label, value in (("target_name", target_name), ("name_tail", name_tail)):
            if value is None:
                continue
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise UnresolvedRefError(
                    f"{label} {value!r} is not UTF-8 encodable; refusing to park an unresolvable reference"
                ) from error
        tail = target_name.rsplit(".", 1)[-1] if name_tail is None else name_tail
        with self.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO unresolved_refs (source_node_id, target_name, name_tail, kind, status, "
                "first_seen, attempts) VALUES (?, ?, ?, ?, 'pending', ?, 0)",
                (source_node_id, target_name, tail, kind, first_seen or _now_iso()),
            )
        ref_id = cursor.lastrowid
        assert ref_id is not None  # AUTOINCREMENT table: SQLite always assigns one
        return int(ref_id)

    @staticmethod
    def _unresolved_ref_row(row: sqlite3.Row) -> UnresolvedRefRow:
        return UnresolvedRefRow(
            ref_id=int(row["ref_id"]),
            source_node_id=str(row["source_node_id"]),
            target_name=str(row["target_name"]),
            name_tail=str(row["name_tail"]),
            kind=str(row["kind"]),
            status=str(row["status"]),
            first_seen=str(row["first_seen"]),
            attempts=int(row["attempts"]),
        )

    def unresolved_ref(self, ref_id: int) -> UnresolvedRefRow | None:
        with self.read() as conn:
            row = conn.execute("SELECT * FROM unresolved_refs WHERE ref_id = ?", (int(ref_id),)).fetchone()
        return None if row is None else self._unresolved_ref_row(row)

    def pending_unresolved_refs(self, *, limit: int | None = None) -> list[UnresolvedRefRow]:
        """The pending retry queue, oldest first (seeks ``idx_unresolved_pending``)."""
        sql = "SELECT * FROM unresolved_refs WHERE status = 'pending' ORDER BY first_seen, ref_id"
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (int(limit),)
        with self.read() as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return [self._unresolved_ref_row(row) for row in rows]

    def failed_unresolved_refs(self, *, name_tail: str | None = None) -> list[UnresolvedRefRow]:
        """Failed refs, optionally narrowed to one tail (``idx_unresolved_failed_tail``)."""
        with self.read() as conn:
            if name_tail is None:
                rows = conn.execute(
                    "SELECT * FROM unresolved_refs WHERE status = 'failed' ORDER BY name_tail, ref_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM unresolved_refs WHERE status = 'failed' AND name_tail = ? ORDER BY ref_id",
                    (name_tail,),
                ).fetchall()
        return [self._unresolved_ref_row(row) for row in rows]

    def record_resolution_attempt(self, ref_id: int, *, outcome: str) -> UnresolvedRefRow:
        """Record one resolution attempt on a pending ref: ``resolved`` or ``failed``.

        Fail-closed: unknown refs, non-pending sources and unknown outcomes
        raise :class:`UnresolvedRefError` — a resolved ref is terminal, a
        failed ref must go through :meth:`retry_failed_ref` first.
        """
        if outcome not in ("resolved", "failed"):
            raise UnresolvedRefError(f"resolution outcome must be 'resolved' or 'failed', got {outcome!r}")
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM unresolved_refs WHERE ref_id = ?", (int(ref_id),)).fetchone()
            if row is None:
                raise UnresolvedRefError(f"unresolved ref {ref_id} does not exist")
            status = str(row["status"])
            if status != "pending":
                raise UnresolvedRefError(
                    f"unresolved ref {ref_id} is {status!r}, not 'pending'; refusing to record an attempt"
                )
            conn.execute(
                "UPDATE unresolved_refs SET status = ?, attempts = attempts + 1 WHERE ref_id = ?",
                (outcome, int(ref_id)),
            )
            updated = conn.execute("SELECT * FROM unresolved_refs WHERE ref_id = ?", (int(ref_id),)).fetchone()
        assert updated is not None  # same transaction, same row
        return self._unresolved_ref_row(updated)

    def retry_failed_ref(self, ref_id: int) -> UnresolvedRefRow:
        """Move a failed ref back to pending so the next resolve pass retries it.

        ``attempts`` is preserved — it counts resolution attempts, not queue
        re-entries.
        """
        with self.transaction() as conn:
            row = conn.execute("SELECT * FROM unresolved_refs WHERE ref_id = ?", (int(ref_id),)).fetchone()
            if row is None:
                raise UnresolvedRefError(f"unresolved ref {ref_id} does not exist")
            if str(row["status"]) != "failed":
                raise UnresolvedRefError(
                    f"unresolved ref {ref_id} is {str(row['status'])!r}, not 'failed'; refusing to retry"
                )
            conn.execute("UPDATE unresolved_refs SET status = 'pending' WHERE ref_id = ?", (int(ref_id),))
            updated = conn.execute("SELECT * FROM unresolved_refs WHERE ref_id = ?", (int(ref_id),)).fetchone()
        assert updated is not None  # same transaction, same row
        return self._unresolved_ref_row(updated)
