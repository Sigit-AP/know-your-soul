"""Read-pool store tests (SUB-100).

The read pool holds N read-only SQLite connections against the same WAL
database the single writer owns. These tests pin the hard guarantee: a pool
connection can never write, never migrates, and never blocks the writer.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator
from pathlib import Path

import pytest
from tests._serve_helpers import seed_store, upsert_node

from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from know_your_soul.serve.superpower import ReadOnlyEventStore, ReadOnlyStoreError
from know_your_soul.store import EventStore


@pytest.fixture()
def seeded(tmp_path: Path) -> Generator[tuple[Path, Path, EventStore]]:
    graph_root, db = seed_store(tmp_path)
    writer = EventStore(db)
    yield graph_root, db, writer
    writer.close()


class TestReadOnlyEnforcement:
    def test_reads_work(self, seeded: tuple[Path, Path, EventStore]) -> None:
        _, db, writer = seeded
        reader = ReadOnlyEventStore(db, reference=writer)
        try:
            assert reader.knowledge_high_watermark() == writer.knowledge_high_watermark()
            assert reader.projection_version() == writer.projection_version()
        finally:
            reader.close()

    def test_write_attempt_fails_readonly(self, seeded: tuple[Path, Path, EventStore]) -> None:
        _, db, writer = seeded
        reader = ReadOnlyEventStore(db, reference=writer)
        try:
            with pytest.raises(sqlite3.OperationalError):
                reader.append_domain(
                    Event(
                        kind=EventKind.NODE_UPSERT,
                        node_id="n-ro",
                        domain=PrimaryDomain.P1,
                        node=Node(
                            node_id="n-ro",
                            kind=NodeKind.LEAF,
                            domain=PrimaryDomain.P1,
                            summary="must never land",
                            provenance=("soul://x",),
                            source_version_id="sv-test",
                        ),
                        source_version_id="sv-test",
                    ),
                    idempotency_key="test:ro:must-fail",
                )
        finally:
            reader.close()

    def test_execute_raw_write_fails_readonly(self, seeded: tuple[Path, Path, EventStore]) -> None:
        _, db, writer = seeded
        reader = ReadOnlyEventStore(db, reference=writer)
        try:
            with pytest.raises(sqlite3.OperationalError):
                reader.execute_raw("CREATE TABLE rogue (id INTEGER)")
        finally:
            reader.close()

    def test_memory_store_rejected(self) -> None:
        with pytest.raises(ReadOnlyStoreError):
            ReadOnlyEventStore(":memory:", reference=None)  # type: ignore[arg-type]

    def test_missing_file_rejected(self, tmp_path: Path, seeded: tuple[Path, Path, EventStore]) -> None:
        _, _, writer = seeded
        with pytest.raises(ReadOnlyStoreError):
            ReadOnlyEventStore(tmp_path / "absent.sqlite3", reference=writer)


class TestSchemaGate:
    def test_schema_mismatch_fails_closed(self, tmp_path: Path) -> None:
        """A reader bound to a different-migration database refuses to serve."""
        _, db = seed_store(tmp_path)
        writer = EventStore(db)
        other_path = tmp_path / "other.sqlite3"
        other = EventStore(other_path)
        try:
            # Simulate divergence: the reference carries an extra recorded version.
            with other.transaction() as conn:
                conn.execute(
                    "INSERT INTO schema_migration (version, name, applied_at) "
                    "VALUES (999, 'rogue', '2026-01-01T00:00:00+00:00')"
                )
            with pytest.raises(ReadOnlyStoreError):
                ReadOnlyEventStore(db, reference=other)
        finally:
            writer.close()
            other.close()

    def test_reader_not_registered_as_live_store(self, seeded: tuple[Path, Path, EventStore]) -> None:
        _, db, writer = seeded
        reader = ReadOnlyEventStore(db, reference=writer)
        try:
            assert EventStore.live_store_at(db) is writer
        finally:
            reader.close()


class TestWalConcurrency:
    def test_reader_never_blocks_writer(self, seeded: tuple[Path, Path, EventStore]) -> None:
        """A continuously reading pool connection never stalls the writer."""
        _, db, writer = seeded
        reader = ReadOnlyEventStore(db, reference=writer)
        stop = threading.Event()
        errors: list[BaseException] = []

        def read_loop() -> None:
            try:
                while not stop.is_set():
                    reader.knowledge_high_watermark()
            except BaseException as error:  # noqa: BLE001 - collected, asserted below
                errors.append(error)

        thread = threading.Thread(target=read_loop, daemon=True)
        thread.start()
        try:
            for index in range(20):
                upsert_node(
                    writer,
                    Node(
                        node_id=f"n-wal-{index}",
                        parent_id="P1",
                        kind=NodeKind.LEAF,
                        domain=PrimaryDomain.P1,
                        summary=f"wal writer node {index}",
                        provenance=("soul://x",),
                        source_version_id="sv-test",
                    ),
                )
        finally:
            stop.set()
            thread.join(timeout=5)
            reader.close()
        assert not errors, f"reader observed errors: {errors!r}"
        assert writer.knowledge_high_watermark() > 0

    def test_writer_never_blocks_reader(self, seeded: tuple[Path, Path, EventStore]) -> None:
        """Reads on the pool connection succeed while the writer holds a write txn."""
        _, db, writer = seeded
        reader = ReadOnlyEventStore(db, reference=writer)
        try:
            with writer.transaction() as conn:
                conn.execute("UPDATE system_state SET value = value WHERE key = 'chain_event_count'")
                # The write transaction is open and uncommitted; WAL readers proceed.
                assert reader.knowledge_high_watermark() >= 0
        finally:
            reader.close()
