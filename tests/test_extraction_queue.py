"""Sidecar queue store tests (SUB-106).

``ExtractionQueue`` is the durable review queue: its own SQLite database
derived from the main store path (never a table in the frozen store schema),
WAL + full-sync when file-backed, idempotent insert keyed on
``(source_sha256, actor, method_fingerprint)``, deterministic review ordering
and a volatile-masked canonical dump for the two-run determinism proof.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from _extraction_helpers import SOURCE_SHA256, SOURCE_SHA256_B, make_args, make_method
from know_your_soul.extraction import (
    ExtractionQueue,
    ExtractionQueueFullError,
    QueueRow,
    validate_submission,
)
from know_your_soul.store import EventStore


def _row(queue_path: Path | str, *, actor: str = "actor-a", agent: str = "agent-a", source: str = SOURCE_SHA256):
    submission = validate_submission(make_args(source_sha256=source, method=make_method(agent=agent)))
    return QueueRow.from_submission(
        submission,
        actor=actor,
        receipt={
            "receipt_id": submission.receipt_id(actor),
            "status": "pending_review",
            "duplicate": False,
            "source_sha256": submission.source_sha256,
            "payload_sha256": submission.payload_sha256,
            "confidence_reported": str(submission.confidence),
            "confidence_effective": str(submission.confidence),
            "corroboration": {"corroborators": 0, "contradictions": 0, "overlap_ratio": "0"},
            "nodes": len(submission.payload.nodes),
            "edges": len(submission.payload.edges),
            "audit_event_id": "a-first",
        },
    )


class TestPlacement:
    def test_sidecar_path_is_derived_from_the_store(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        queue = ExtractionQueue.for_store(store)
        try:
            assert queue.path != store.db_path
            assert "extraction" in queue.path
            assert Path(queue.path).parent == Path(store.db_path).parent
        finally:
            queue.close()
            store.close()

    def test_sidecar_file_is_created_next_to_the_store(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        queue = ExtractionQueue.for_store(store)
        row = _row(queue.path)
        queue.insert(row)
        assert Path(queue.path).is_file()
        queue.close()
        store.close()

    def test_for_store_caches_one_queue_per_store(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        first = ExtractionQueue.for_store(store)
        second = ExtractionQueue.for_store(store)
        assert first is second
        first.close()
        store.close()

    def test_frozen_store_schema_gains_no_tables(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        before = store.table_names()
        queue = ExtractionQueue.for_store(store)
        queue.insert(_row(queue.path))
        assert store.table_names() == before
        queue.close()
        store.close()

    def test_in_memory_store_gets_an_in_memory_queue(self) -> None:
        store = EventStore(":memory:")
        queue = ExtractionQueue.for_store(store)
        assert queue.path == ":memory:"
        queue.insert(_row(":memory:"))
        assert queue.count() == 1
        queue.close()
        store.close()


class TestInsert:
    def test_insert_and_get_roundtrip(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        row = _row(queue.path)
        queue.insert(row)
        loaded = queue.get(row.receipt_id)
        assert loaded is not None
        assert loaded.receipt_id == row.receipt_id
        assert loaded.source_sha256 == SOURCE_SHA256
        assert loaded.actor == "actor-a"
        assert loaded.status == "pending_review"
        assert loaded.receipt["receipt_id"] == row.receipt_id
        queue.close()

    def test_get_unknown_returns_none(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        assert queue.get("ext-" + "0" * 32) is None
        queue.close()

    def test_idem_key_lookup(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        row = _row(queue.path)
        queue.insert(row)
        loaded = queue.get_by_idem_key(row.idem_key)
        assert loaded is not None and loaded.receipt_id == row.receipt_id
        queue.close()

    def test_duplicate_idem_key_is_a_unique_violation(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        row = _row(queue.path)
        queue.insert(row)
        with pytest.raises(sqlite3.IntegrityError):
            queue.insert(row)
        queue.close()

    def test_rows_persist_across_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "q.sqlite3"
        queue = ExtractionQueue(path)
        row = _row(path)
        queue.insert(row)
        queue.close()
        reopened = ExtractionQueue(path)
        loaded = reopened.get(row.receipt_id)
        assert loaded is not None and loaded.status == "pending_review"
        reopened.close()

    def test_queue_cap_fails_closed(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3", cap=8)
        for index in range(8):
            submission = validate_submission(
                make_args(source_sha256=f"{index:064x}", method=make_method(agent="agent-a"))
            )
            queue.insert(
                QueueRow.from_submission(
                    submission, actor="actor-a", receipt={"receipt_id": submission.receipt_id("actor-a")}
                )
            )
        with pytest.raises(ExtractionQueueFullError):
            queue.insert(_row(queue.path))
        queue.close()


class TestGroupAndVerdicts:
    def test_group_selects_one_source(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        first = _row(queue.path, actor="actor-a", source=SOURCE_SHA256)
        second = _row(queue.path, actor="actor-b", agent="agent-b", source=SOURCE_SHA256)
        other = _row(queue.path, actor="actor-c", agent="agent-c", source=SOURCE_SHA256_B)
        for row in (first, second, other):
            queue.insert(row)
        group = queue.group(SOURCE_SHA256)
        assert {row.receipt_id for row in group} == {first.receipt_id, second.receipt_id}
        queue.close()

    def test_update_verdicts_writes_status_and_confidence(self, tmp_path: Path) -> None:
        from know_your_soul.extraction import GroupVerdict

        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        row = _row(queue.path)
        queue.insert(row)
        queue.update_verdicts(
            {
                row.idem_key: GroupVerdict(
                    status="corroborated",
                    corroborators=1,
                    contradictions=0,
                    overlap_ratio="1.000000000000",
                    confidence_effective="0.450000000000",
                )
            }
        )
        loaded = queue.get(row.receipt_id)
        assert loaded is not None
        assert loaded.status == "corroborated"
        assert loaded.confidence_effective == "0.450000000000"
        queue.close()

    def test_update_verdicts_never_resurrects_decided_rows(self, tmp_path: Path) -> None:
        from know_your_soul.extraction import GroupVerdict

        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        row = _row(queue.path)
        queue.insert(row)
        queue.mark_decided(row.receipt_id, "rejected", decided_by="operator", note="no")
        queue.update_verdicts(
            {
                row.idem_key: GroupVerdict(
                    status="corroborated",
                    corroborators=1,
                    contradictions=0,
                    overlap_ratio="1.000000000000",
                    confidence_effective="0.450000000000",
                )
            }
        )
        loaded = queue.get(row.receipt_id)
        assert loaded is not None
        assert loaded.status == "rejected"
        assert loaded.confidence_effective == "0.450000000000"  # the math still updates
        queue.close()


class TestReviewOrdering:
    def test_list_is_status_ranked_then_receipt_id(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        rows = [
            _row(queue.path, actor="actor-a", source=SOURCE_SHA256),
            _row(queue.path, actor="actor-b", agent="agent-b", source=SOURCE_SHA256),
            _row(queue.path, actor="actor-c", agent="agent-c", source=SOURCE_SHA256_B),
        ]
        for row in rows:
            queue.insert(row)
        queue.mark_decided(rows[2].receipt_id, "applied", decided_by="operator", note="")
        queue.mark_decided(rows[1].receipt_id, "rejected", decided_by="operator", note="")
        listed = queue.list()
        order = [row.status for row in listed]
        assert order == ["pending_review", "applied", "rejected"]
        queue.close()

    def test_list_filters_by_status(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        rows = [
            _row(queue.path, actor="actor-a", source=SOURCE_SHA256),
            _row(queue.path, actor="actor-b", agent="agent-b", source=SOURCE_SHA256_B),
        ]
        for row in rows:
            queue.insert(row)
        queue.mark_decided(rows[0].receipt_id, "rejected", decided_by="operator", note="")
        pending = queue.list(status="pending_review")
        assert [row.receipt_id for row in pending] == [rows[1].receipt_id]
        queue.close()

    def test_mark_decided_records_the_operator_and_note(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        row = _row(queue.path)
        queue.insert(row)
        queue.mark_decided(row.receipt_id, "rejected", decided_by="op-1", note="illegible scan")
        loaded = queue.get(row.receipt_id)
        assert loaded is not None
        assert loaded.status == "rejected"
        assert loaded.decided_by == "op-1"
        assert loaded.decision_note == "illegible scan"
        assert loaded.decided_at is not None
        queue.close()


class TestCanonicalDump:
    def test_dump_is_byte_identical_across_runs(self, tmp_path: Path) -> None:
        dumps = []
        for run in range(2):
            queue = ExtractionQueue(tmp_path / f"q{run}.sqlite3")
            for actor in ("actor-a", "actor-b"):
                queue.insert(_row(queue.path, actor=actor, agent=f"agent-{actor}"))
            dumps.append(queue.canonical_dump())
            queue.close()
        assert dumps[0] == dumps[1]

    def test_dump_masks_volatile_fields(self, tmp_path: Path) -> None:
        queue = ExtractionQueue(tmp_path / "q.sqlite3")
        queue.insert(_row(queue.path))
        dump = queue.canonical_dump()
        assert "submitted_at" not in dump
        queue.close()
