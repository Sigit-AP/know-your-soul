"""Contract tests for operations: observability, backup, recovery (Task 9 core).

Covers structured metrics/logs, health/readiness against the event store and
projection state, audit correlation, atomic SQLite backup/restore equivalence,
migration checks, concurrent read/write, and corrupt/stale/torn projection
recovery through ``GraphProjector.repair_if_stale``.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from soul_knowledge.audit import AuditLog
from soul_knowledge.backup import (
    BackupError,
    ProjectionState,
    backup_store,
    diagnose_projection,
    inspect_backup,
    recover_projection,
    restore_store,
)
from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from soul_knowledge.events import Invocation
from soul_knowledge.importer import SeedImporter
from soul_knowledge.observability import (
    MetricsRegistry,
    StructuredLogger,
    check_health,
    check_readiness,
    collect_store_metrics,
)
from soul_knowledge.projection import GraphProjector
from soul_knowledge.store import EventStore

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

ALPHA = b"# Alpha Lessons\n\n## First Lesson\n\nAlways roll the database back before retrying.\n"
BETA = b"# Beta\n\n## Counterexample\n\nA rollback that was itself rolled back.\n"
GAMMA = b"# Gamma\n\n## Standing Order\n\nNever widen a transaction to cover reads.\n"


def build_node(node_id: str, **overrides: object) -> Node:
    payload: dict[str, object] = {
        "node_id": node_id,
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": "sv-1",
        "payload": {"fact": node_id},
        "summary": f"node {node_id}",
        "provenance": ("soul/01-identity-compliance.md",),
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def upsert(store: EventStore, node_id: str, key: str) -> None:
    node = build_node(node_id)
    event = Event.model_validate(
        {
            "kind": EventKind.NODE_UPSERT,
            "node_id": node.node_id,
            "domain": node.domain,
            "node": node,
            "source_version_id": node.source_version_id,
            "occurred_at": NOW,
        }
    )
    invocation = Invocation.create("knowledge.log_event", request_hash=f"rh:{key}", idempotency_key=key)
    store.commit(event, invocation=invocation)


def populated(tmp_path: Path) -> tuple[EventStore, GraphProjector]:
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    projector = GraphProjector(store, output_root=tmp_path / "out")
    return store, projector


# ------------------------------------------------------------------- metrics


def test_metrics_counters_gauges_and_deterministic_snapshot() -> None:
    metrics = MetricsRegistry()
    metrics.increment("requests_total", operation="query")
    metrics.increment("requests_total", 2, operation="query")
    metrics.increment("requests_total", operation="log_event")
    metrics.set_gauge("pending_journals", 3)
    snapshot = metrics.snapshot()
    counters = {
        (entry["name"], tuple(sorted(entry["labels"].items()))): entry["value"] for entry in snapshot["counters"]
    }
    assert counters[("requests_total", (("operation", "query"),))] == 3
    assert counters[("requests_total", (("operation", "log_event"),))] == 1
    assert snapshot["gauges"][0]["name"] == "pending_journals"
    assert metrics.snapshot() == snapshot  # deterministic


def test_metrics_render_text_is_stable_and_labelled() -> None:
    metrics = MetricsRegistry()
    metrics.increment("events_committed_total", operation="log_event")
    metrics.set_gauge("knowledge_watermark", 7)
    text = metrics.render_text()
    assert 'events_committed_total{operation="log_event"} 1' in text
    assert "knowledge_watermark 7" in text
    assert text == metrics.render_text()


def test_collect_store_metrics(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    upsert(store, "n-1", "m1")
    metrics = MetricsRegistry()
    collect_store_metrics(store, metrics)
    snapshot = metrics.snapshot()
    gauges = {entry["name"]: entry["value"] for entry in snapshot["gauges"]}
    assert gauges["event_log_events"] == store.event_count()
    assert gauges["knowledge_high_watermark"] == store.knowledge_high_watermark()
    assert gauges["pending_journals"] == 0
    store.close()


# ------------------------------------------------------------------ logging


def test_structured_logger_emits_redacted_correlated_records() -> None:
    logger = StructuredLogger()
    logger.info("commit.done", correlation_id="inv-1", audit_event_seq=9, detail="key sk-live0123456789abcdef0123")
    records = logger.records()
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "commit.done"
    assert record["level"] == "info"
    assert record["correlation_id"] == "inv-1"
    assert record["audit_event_seq"] == 9
    assert "sk-live0123456789abcdef0123" not in json.dumps(record)
    assert "ts" in record


def test_audit_correlation_against_real_commit(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    logger = StructuredLogger()
    invocation = Invocation.create("knowledge.log_event", request_hash="rh:corr", idempotency_key="corr")
    node = build_node("n-corr")
    event = Event.model_validate(
        {
            "kind": EventKind.NODE_UPSERT,
            "node_id": node.node_id,
            "domain": node.domain,
            "node": node,
            "source_version_id": node.source_version_id,
            "occurred_at": NOW,
        }
    )
    receipt = store.commit(event, invocation=invocation)
    logger.info(
        "commit.done",
        correlation_id=invocation.invocation_id,
        audit_event_id=receipt.audit_event_id,
        audit_event_seq=receipt.audit_event_seq,
    )
    record = logger.records()[0]
    assert record["correlation_id"] == invocation.invocation_id
    assert record["audit_event_seq"] == receipt.audit_event_seq
    audit = AuditLog(store)
    outcome = audit.journal_outcome(invocation.invocation_id)
    assert outcome is not None and outcome["status"] == "success"
    store.close()


# ------------------------------------------------------------- health checks


def test_health_ok_on_fresh_store(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    upsert(store, "n-1", "h1")
    report = check_health(store)
    assert report.ok
    assert report.checks["store_select"]
    assert report.checks["chain"]
    assert report.checks["migrations"]
    store.close()


def test_health_detects_broken_chain(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    upsert(store, "n-1", "h2")
    # event_log itself is append-only (a trigger rejects UPDATE); tamper with
    # the persisted chain anchor to simulate a truncated/corrupted tail.
    store.execute_raw("UPDATE system_state SET value = '999' WHERE key = 'chain_event_count'")
    report = check_health(store)
    assert not report.ok
    assert not report.checks["chain"]
    store.close()


def test_health_detects_unapplied_migrations(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    store = EventStore(tmp_path / "soul.sqlite3")
    # Point the health check at a migrations dir carrying one extra migration
    # the store has never applied.
    default_dir = Path(__file__).resolve().parents[1] / "src" / "soul_knowledge" / "migrations"
    for path in default_dir.glob("*.sql"):
        (migrations / path.name).write_bytes(path.read_bytes())
    (migrations / "9999_pending.sql").write_text("CREATE TABLE pending_probe (id INTEGER);\n", encoding="utf-8")
    report = check_health(store, migrations_dir=migrations)
    assert not report.ok
    assert not report.checks["migrations"]
    store.close()


def test_readiness_requires_current_projection(tmp_path: Path) -> None:
    store, projector = populated(tmp_path)
    not_ready = check_readiness(store, projector=projector)
    assert not not_ready.ok
    assert not not_ready.checks["projection_present"]
    projector.rebuild()
    ready = check_readiness(store, projector=projector)
    assert ready.ok
    assert ready.checks["projection_present"]
    assert ready.checks["projection_watermark"]
    store.close()


def test_readiness_detects_stale_journals(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    invocation = Invocation.create(
        "knowledge.log_event",
        request_hash="rh:stale",
        idempotency_key="stale",
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    AuditLog(store).begin(invocation)
    report = check_readiness(store, stale_after_seconds=300.0)
    assert not report.ok
    assert not report.checks["no_stale_journals"]
    store.close()


# -------------------------------------------------------------------- backup


def test_backup_restore_equivalence(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    for index in range(5):
        upsert(store, f"n-{index}", f"eq-{index}")
    receipt = backup_store(store, tmp_path / "backup.sqlite3")
    assert receipt.event_count == store.event_count()
    assert receipt.sha256
    restored_path = tmp_path / "restored.sqlite3"
    restore_store(tmp_path / "backup.sqlite3", restored_path)
    restored = EventStore(restored_path)
    assert restored.verify_chain().ok
    assert store.verify_chain().ok
    assert restored.event_sequences() == store.event_sequences()
    with store.read() as conn:
        original_rows = [tuple(row) for row in conn.execute("SELECT * FROM event_log ORDER BY event_seq")]
    with restored.read() as conn:
        restored_rows = [tuple(row) for row in conn.execute("SELECT * FROM event_log ORDER BY event_seq")]
    assert restored_rows == original_rows
    restored.close()
    store.close()


def test_backup_of_memory_store(tmp_path: Path) -> None:
    store = EventStore()
    upsert(store, "n-mem", "mem-1")
    receipt = backup_store(store, tmp_path / "mem-backup.sqlite3")
    assert receipt.event_count == store.event_count()
    store.close()


def test_inspect_backup_reports_integrity(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    upsert(store, "n-1", "insp-1")
    backup_store(store, tmp_path / "backup.sqlite3")
    info = inspect_backup(tmp_path / "backup.sqlite3")
    assert info["ok"]
    assert info["event_count"] == store.event_count()
    assert info["chain_head_hash"] is not None
    store.close()


def test_restore_refuses_to_overwrite_without_flag(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    upsert(store, "n-1", "rw-1")
    backup_store(store, tmp_path / "backup.sqlite3")
    occupied = tmp_path / "occupied.sqlite3"
    occupied.write_bytes(b"not empty")
    with pytest.raises(BackupError):
        restore_store(tmp_path / "backup.sqlite3", occupied)
    restore_store(tmp_path / "backup.sqlite3", occupied, overwrite=True)
    assert EventStore(occupied).event_count() == store.event_count()
    store.close()


def test_backup_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(BackupError):
        inspect_backup(tmp_path / "does-not-exist.sqlite3")


# ------------------------------------------------------------- concurrency


def test_concurrent_read_write_on_file_store(tmp_path: Path) -> None:
    path = tmp_path / "soul.sqlite3"
    writer = EventStore(path)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write_events() -> None:
        try:
            barrier.wait(timeout=10)
            for index in range(8):
                upsert(writer, f"n-c-{index}", f"cc-{index}")
        except BaseException as error:  # noqa: BLE001 - surfaced in the assertion
            errors.append(error)

    def read_events() -> None:
        reader = EventStore(path)
        try:
            barrier.wait(timeout=10)
            seen = 0
            for _ in range(200):
                count = reader.event_count()
                assert count >= seen  # append-only: counts never regress
                seen = count
        except BaseException as error:  # noqa: BLE001
            errors.append(error)
        finally:
            reader.close()

    threads = [threading.Thread(target=write_events), threading.Thread(target=read_events)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors
    assert writer.event_count() == 16  # 8 domain + 8 audit events
    reader = EventStore(path)
    assert reader.event_count() == writer.event_count()
    reader.close()
    writer.close()


# ---------------------------------------------------- projection recovery


def test_diagnose_current_and_missing_projection(tmp_path: Path) -> None:
    store, projector = populated(tmp_path)
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.MISSING
    projector.rebuild()
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.CURRENT
    assert diagnosis.manifest_watermark == store.knowledge_high_watermark()
    store.close()


def test_recover_stale_projection(tmp_path: Path) -> None:
    store, projector = populated(tmp_path)
    projector.rebuild()
    # Commit new knowledge without rebuilding: the projection is now stale.
    corpus = tmp_path / "corpus"
    (corpus / "gamma.md").write_bytes(GAMMA)
    SeedImporter(store, state_dir=tmp_path / "import-state").import_directory(corpus, domain=PrimaryDomain.P1)
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.STALE
    report = recover_projection(store, projector)
    assert report.receipt.repaired
    assert report.receipt.source_knowledge_high_watermark == store.knowledge_high_watermark()
    assert report.audit_receipts == ()
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    store.close()


def test_recover_torn_projection(tmp_path: Path) -> None:
    store, projector = populated(tmp_path)
    projector.rebuild()
    active = projector.active_generation_dir()
    assert active is not None
    (active / "labels.json").unlink()  # torn: an activated artifact is missing
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.TORN
    report = recover_projection(store, projector)
    assert report.receipt.repaired
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    assert (active / "labels.json").is_file()  # content-addressed dir restored
    store.close()


def test_recover_corrupt_projection(tmp_path: Path) -> None:
    store, projector = populated(tmp_path)
    projector.rebuild()
    active = projector.active_generation_dir()
    assert active is not None
    graph = active / "graph.json"
    graph.write_bytes(b'{"corrupt": true}\n')  # bytes no longer match the manifest
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.CORRUPT
    report = recover_projection(store, projector)
    assert report.receipt.repaired
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    manifest = json.loads((active / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["node_count"] > 0
    store.close()


def test_recover_via_audit_log_hook(tmp_path: Path) -> None:
    """The recovery helper composes with AuditLog.recover_incomplete."""
    store, projector = populated(tmp_path)
    projector.rebuild()
    invocation = Invocation.create(
        "knowledge.log_event",
        request_hash="rh:crash",
        idempotency_key="crash",
        started_at=datetime.now(UTC) - timedelta(hours=2),
    )
    audit = AuditLog(store)
    audit.begin(invocation)
    active = projector.active_generation_dir()
    assert active is not None
    (active / "graph.html").unlink()
    assert diagnose_projection(store, projector).state is ProjectionState.TORN
    report = recover_projection(store, projector, audit_log=audit, stale_after_seconds=300.0)
    assert diagnose_projection(store, projector).state is ProjectionState.CURRENT
    assert len(report.audit_receipts) == 1
    assert report.audit_receipts[0].status == "failure"  # interrupted before the domain commit
    store.close()
