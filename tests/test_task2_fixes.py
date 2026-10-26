"""Regression probes for Task 2 review fix round 1 (findings C1, I1-I5).

Findings: ``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/task-2-findings.md``.
Every probe drives real SQLite state through the public store API; the two
crash-race probes use a second real ``EventStore`` on the same file so the
interleaving is executed, not mocked.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from know_your_soul.audit import AuditLog, InvocationOutcome
from know_your_soul.domain import Event, EventKind, Evidence, Node, NodeKind, PrimaryDomain
from know_your_soul.events import Invocation
from know_your_soul.source_registry import ParserVersions, SourceKind, SourceRegistry
from know_your_soul.store import AuditOutcomeMismatch, EventStore, StoreError, VersionConflict

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ONE_HOUR_AGO = datetime.now(UTC) - timedelta(hours=1)

PARSER_VERSIONS = ParserVersions(
    parser_version="1.0.0",
    normalization_version="1.0.0",
    engine_version="0.9.95",
    scoring_contract_version="1.0.0",
)


# ------------------------------------------------------------------ builders


def build_node(node_id: str = "n-1", **overrides: object) -> Node:
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


def build_evidence(evidence_id: str = "ev-1", node_id: str = "n-1", **overrides: object) -> Evidence:
    payload: dict[str, object] = {
        "evidence_id": evidence_id,
        "node_id": node_id,
        "source_version_id": "sv-1",
        "source_uri": "soul/01-identity-compliance.md",
        "observed_at": NOW,
        "independence_key": "grp-1",
        "outcome": "0.5",
        "correctness": "0.9",
        "reliability": "0.8",
        "reproducibility": "0.7",
        "scope_fit": "0.6",
        "successful_use": "0.5",
        "task_coverage": "0.4",
        "quality_at_use": "0.75",
        "contradiction": "0.1",
        "utilization_u": "0.5",
        "latency_ms": 1000,
        "compute_ms": 100,
        "output_chars": 500,
    }
    payload.update(overrides)
    return Evidence.model_validate(payload)


def node_event(node: Node, **overrides: object) -> Event:
    payload: dict[str, object] = {
        "kind": EventKind.NODE_UPSERT,
        "node_id": node.node_id,
        "domain": node.domain,
        "node": node,
        "source_version_id": node.source_version_id,
        "occurred_at": NOW,
    }
    payload.update(overrides)
    return Event.model_validate(payload)


def evidence_event(evidence: Evidence) -> Event:
    return Event.model_validate(
        {
            "kind": EventKind.EVIDENCE_APPEND,
            "node_id": evidence.node_id,
            "domain": PrimaryDomain.P1,
            "evidence": evidence,
            "source_version_id": evidence.source_version_id,
            "occurred_at": NOW,
        }
    )


def make_invocation(
    key: str | None,
    operation: str = "knowledge.log_event",
    *,
    started_at: datetime | None = None,
) -> Invocation:
    return Invocation.create(
        operation,
        request_hash=f"rh:{key}",
        actor="operator",
        idempotency_key=key,
        started_at=started_at,
    )


def journal_row(store: EventStore, invocation_id: str) -> dict[str, Any]:
    with store.read() as conn:
        row = conn.execute("SELECT * FROM invocation_journal WHERE invocation_id = ?", (invocation_id,)).fetchone()
    assert row is not None
    return dict(row)


def clone_without_triggers(store: EventStore, tmp_path: Path, *drop: str) -> Path:
    """Consistent clone of the live WAL database with the named triggers dropped."""
    clone = tmp_path / "clone.sqlite3"
    source = sqlite3.connect(store.db_path)
    target = sqlite3.connect(clone)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    connection = sqlite3.connect(clone)
    try:
        for trigger in drop:
            connection.execute(f"DROP TRIGGER {trigger}")
        connection.commit()
    finally:
        connection.close()
    return clone


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------------- C1


class RacingAuditLog(AuditLog):
    """Simulates a service process running recovery while a commit is in flight.

    The recovery pass runs right after ``begin`` (before the domain write), which
    is the interleaving from finding C1: the journal is closed as
    ``INTERRUPTED_BEFORE_COMMIT`` while the domain write later commits.
    """

    def __init__(self, store: EventStore, db_path: str) -> None:
        super().__init__(store)
        self._db_path = db_path

    def begin(self, invocation: Invocation) -> Any:
        journal = super().begin(invocation)
        other = EventStore(self._db_path)
        try:
            AuditLog(other).recover_incomplete(stale_after_seconds=0)
        finally:
            other.close()
        return journal


def test_commit_raises_when_the_final_audit_records_a_different_outcome(store: EventStore) -> None:
    invocation = make_invocation("k1")
    with pytest.raises(AuditOutcomeMismatch) as excinfo:
        store.commit(
            node_event(build_node("n-1")),
            invocation=invocation,
            expected_version=None,
            audit_log=RacingAuditLog(store, store.db_path),
        )

    message = str(excinfo.value)
    assert "success" in message and "failure" in message
    assert isinstance(excinfo.value, StoreError)
    assert excinfo.value.__cause__ is not None  # the recorded outcome is chained

    # The domain write is durable, but the invocation's only audit event records
    # the recovery failure — commit must never report success against it.
    assert store.domain_event_count() == 1
    assert store.audit_event_count() == 1
    assert store.knowledge_high_watermark() == 1
    outcome = json.loads(journal_row(store, invocation.invocation_id)["outcome_json"])
    assert outcome["status"] == "failure"
    assert outcome["error"] == "INTERRUPTED_BEFORE_COMMIT"


def test_complete_returns_the_stored_receipt_after_recovery_closed_the_journal(
    store: EventStore,
) -> None:
    """The audit seam of the C1 repro: recovery closes a live journal, the domain
    write commits anyway, and the late ``complete`` returns the stored receipt."""
    audit = AuditLog(store)
    invocation = make_invocation("k1")
    audit.begin(invocation)

    assert audit.recover_incomplete(stale_after_seconds=0)[0].status == "failure"
    domain = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)

    receipt = audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=1))
    assert receipt.status == "failure"
    assert store.domain_event_count() == 1
    assert domain.domain_event_id  # the knowledge event is durable


def test_commit_chains_the_domain_error_when_the_failure_audit_mismatches(store: EventStore) -> None:
    """If the failure-audit write returns a mismatched stored receipt, the raised
    StoreError chains the original domain error (``raise ... from ...``)."""

    class PreClosedSuccess(AuditLog):
        def complete(self, invocation_id: str, outcome: InvocationOutcome) -> Any:
            if outcome.status == "failure":
                super().complete(invocation_id, InvocationOutcome(status="success", duration_ms=0))
            return super().complete(invocation_id, outcome)

    store.append_domain(node_event(build_node("n-1")), idempotency_key="setup", expected_version=None)
    with pytest.raises(AuditOutcomeMismatch) as excinfo:
        store.commit(
            node_event(build_node("n-1")),
            invocation=make_invocation("k1"),
            expected_version=0,  # forces VersionConflict inside commit
            audit_log=PreClosedSuccess(store),
        )
    assert isinstance(excinfo.value.__cause__, VersionConflict)


# --------------------------------------------------------------------- I1


def test_recover_incomplete_ignores_fresh_journals(store: EventStore) -> None:
    audit = AuditLog(store)
    fresh = make_invocation("k1")
    audit.begin(fresh)

    assert audit.recover_incomplete() == []
    assert store.journal_status(fresh.invocation_id) == "in_progress"
    assert store.audit_event_count() == 0


def test_recover_incomplete_closes_stale_journals(store: EventStore) -> None:
    audit = AuditLog(store)
    stale = make_invocation("k1", started_at=ONE_HOUR_AGO)
    audit.begin(stale)

    receipts = audit.recover_incomplete()
    assert len(receipts) == 1
    assert receipts[0].status == "failure"
    assert store.journal_status(stale.invocation_id) == "complete"


def test_recover_incomplete_honours_the_staleness_threshold(store: EventStore) -> None:
    audit = AuditLog(store)
    fresh = make_invocation("k1")
    audit.begin(fresh)

    assert audit.recover_incomplete(stale_after_seconds=3600) == []
    assert store.journal_status(fresh.invocation_id) == "in_progress"

    receipts = audit.recover_incomplete(stale_after_seconds=0)
    assert len(receipts) == 1
    assert store.journal_status(fresh.invocation_id) == "complete"


def test_commit_refreshes_the_journal_heartbeat(store: EventStore) -> None:
    invocation = make_invocation("k1", started_at=ONE_HOUR_AGO)
    store.commit(node_event(build_node("n-1")), invocation=invocation, expected_version=None)

    row = journal_row(store, invocation.invocation_id)
    heartbeat = datetime.fromisoformat(str(row["heartbeat_at"]))
    assert heartbeat > ONE_HOUR_AGO + timedelta(minutes=59)
    assert row["effective_idempotency_key"] == "k1"


# --------------------------------------------------------------------- I2


def test_recovery_reconciles_an_invocation_without_an_idempotency_key(store: EventStore) -> None:
    audit = AuditLog(store)
    invocation = make_invocation(None)
    audit.begin(invocation)
    # commit() falls back to invocation_id as the domain idempotency key.
    domain = store.append_domain(
        node_event(build_node("n-1")), idempotency_key=invocation.invocation_id, expected_version=None
    )

    receipts = audit.recover_incomplete(stale_after_seconds=0)
    assert len(receipts) == 1
    assert receipts[0].status == "success"
    outcome = json.loads(journal_row(store, invocation.invocation_id)["outcome_json"])
    assert outcome["linked_domain_event_id"] == domain.domain_event_id


def test_journal_persists_the_effective_idempotency_key(store: EventStore) -> None:
    audit = AuditLog(store)
    keyed = make_invocation("k1")
    keyless = make_invocation(None)
    audit.begin(keyed)
    audit.begin(keyless)

    assert journal_row(store, keyed.invocation_id)["effective_idempotency_key"] == "k1"
    assert journal_row(store, keyless.invocation_id)["effective_idempotency_key"] == keyless.invocation_id


# --------------------------------------------------------------------- I3


def test_verify_chain_detects_tail_truncation(store: EventStore, tmp_path: Path) -> None:
    for index in range(3):
        store.append_domain(node_event(build_node(f"n-{index}")), idempotency_key=f"k{index}", expected_version=None)
    audit = AuditLog(store)
    invocation = make_invocation("k3")
    audit.begin(invocation)
    audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=1))
    assert store.event_count() == 4
    assert store.verify_chain().ok is True

    # The store persists the expected chain head and event count.
    with store.read() as conn:
        anchor = {
            str(row["key"]): str(row["value"])
            for row in conn.execute(
                "SELECT key, value FROM system_state WHERE key IN ('chain_head_hash', 'chain_event_count')"
            )
        }
        last_hash = str(conn.execute("SELECT event_hash FROM event_log ORDER BY event_seq DESC LIMIT 1").fetchone()[0])
    assert anchor["chain_event_count"] == "4"
    assert anchor["chain_head_hash"] == last_hash

    clone = clone_without_triggers(store, tmp_path, "event_log_no_delete")
    connection = sqlite3.connect(clone)
    try:
        connection.execute("DELETE FROM event_log WHERE event_seq = 4")
        connection.commit()
    finally:
        connection.close()

    truncated = EventStore(clone)
    try:
        report = truncated.verify_chain()
        assert report.ok is False
        assert report.checked == 3
        assert report.first_bad_seq == 4
    finally:
        truncated.close()


def test_verify_chain_cross_checks_journal_audit_event_seq(store: EventStore, tmp_path: Path) -> None:
    """Even when the attacker rewrites the persisted anchor to match the truncated
    log, the completed journal still points at the missing audit event."""
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    audit = AuditLog(store)
    invocation = make_invocation("k2")
    audit.begin(invocation)
    audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=1))

    clone = clone_without_triggers(store, tmp_path, "event_log_no_delete")
    connection = sqlite3.connect(clone)
    try:
        head = connection.execute(
            "SELECT event_hash FROM event_log ORDER BY event_seq DESC LIMIT 1 OFFSET 1"
        ).fetchone()[0]
        connection.execute("DELETE FROM event_log WHERE event_seq = 2")
        connection.execute("UPDATE system_state SET value = '1' WHERE key = 'chain_event_count'")
        connection.execute("UPDATE system_state SET value = ? WHERE key = 'chain_head_hash'", (head,))
        connection.commit()
    finally:
        connection.close()

    truncated = EventStore(clone)
    try:
        report = truncated.verify_chain()
        assert report.ok is False
        assert report.first_bad_seq == 2
    finally:
        truncated.close()


def test_verify_chain_detects_recorded_at_rewrite(store: EventStore, tmp_path: Path) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    assert store.verify_chain().ok is True

    clone = clone_without_triggers(store, tmp_path, "event_log_no_update")
    connection = sqlite3.connect(clone)
    try:
        connection.execute("UPDATE event_log SET recorded_at = '1900-01-01T00:00:00+00:00' WHERE event_seq = 1")
        connection.commit()
    finally:
        connection.close()

    rewritten = EventStore(clone)
    try:
        report = rewritten.verify_chain()
        assert report.ok is False
        assert report.first_bad_seq == 1
    finally:
        rewritten.close()


# --------------------------------------------------------------------- I4


def test_evidence_scores_are_stored_quantized_to_12_decimal_places(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    evidence = build_evidence("ev-1", "n-1", correctness="0.1234567890123456789")
    store.append_domain(evidence_event(evidence), idempotency_key="k2", expected_version=1)

    with store.read() as conn:
        raw = str(conn.execute("SELECT payload_json FROM evidence WHERE evidence_id = 'ev-1'").fetchone()[0])
    assert "0.1234567890123456789" not in raw
    assert "0.123456789012" in raw

    recorded = store.get_evidence("ev-1")
    assert recorded is not None
    assert recorded.correctness == Decimal("0.123456789012")


def test_score_quantization_rounds_half_even_at_the_store_boundary(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    store.append_domain(
        evidence_event(build_evidence("ev-even", "n-1", correctness="0.1234567890125")),
        idempotency_key="k2",
        expected_version=1,
    )
    store.append_domain(
        evidence_event(build_evidence("ev-odd", "n-1", correctness="0.1234567890115")),
        idempotency_key="k3",
        expected_version=1,
    )

    even = store.get_evidence("ev-even")
    odd = store.get_evidence("ev-odd")
    assert even is not None and odd is not None
    assert even.correctness == Decimal("0.123456789012")  # 2 is even: stays
    assert odd.correctness == Decimal("0.123456789012")  # 1 is odd: rounds up to even


def test_event_hashes_are_computed_over_the_quantized_payload(store: EventStore) -> None:
    node = build_node("n-1", scores={"p": "0.1234567890123456789", "m": "0.5", "e": "0.25"})
    store.append_domain(node_event(node), idempotency_key="k1", expected_version=None)

    with store.read() as conn:
        row = conn.execute("SELECT payload_json FROM event_log WHERE event_seq = 1").fetchone()
        revision = conn.execute("SELECT payload_json FROM node_revision WHERE node_id = 'n-1'").fetchone()
    raw_event = str(row[0])
    assert "0.1234567890123456789" not in raw_event
    assert "0.123456789012" in raw_event
    assert "0.500000000000" in raw_event
    assert "0.500000000000" in str(revision[0])

    # The stored hash matches the quantized payload: the chain verifies.
    assert store.verify_chain().ok is True
    current = store.current_node("n-1")
    assert current is not None
    assert current.scores.p == Decimal("0.123456789012")


# --------------------------------------------------------------------- I5


def test_active_excludes_a_version_with_a_pending_relearn_offer(store: EventStore, tmp_path: Path) -> None:
    source = tmp_path / "SOUL.md"
    source.write_text("v1\n", encoding="utf-8")
    registry = SourceRegistry(store)
    first = registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)
    assert registry.active(SourceKind.SOUL_MD) is not None
    assert registry.active(SourceKind.SOUL_MD).source_version_id == first.source_version_id  # type: ignore[union-attr]

    source.write_text("v2 changed\n", encoding="utf-8")
    offer = registry.detect_change(source, SourceKind.SOUL_MD, PARSER_VERSIONS)
    assert offer is not None
    assert offer.current_source_version_id != first.source_version_id

    # The unapproved version is never active while its offer is outstanding.
    active = registry.active(SourceKind.SOUL_MD)
    assert active is not None
    assert active.source_version_id == first.source_version_id
    assert registry.active().source_version_id == first.source_version_id  # type: ignore[union-attr]


def test_active_never_returns_an_unapproved_version_after_revert(store: EventStore, tmp_path: Path) -> None:
    source = tmp_path / "SOUL.md"
    source.write_text("v1\n", encoding="utf-8")
    registry = SourceRegistry(store)
    first = registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)

    source.write_text("v2 changed\n", encoding="utf-8")
    registry.detect_change(source, SourceKind.SOUL_MD, PARSER_VERSIONS)

    source.write_text("v1\n", encoding="utf-8")
    offer = registry.detect_change(source, SourceKind.SOUL_MD, PARSER_VERSIONS)
    assert offer is not None
    assert offer.current_source_version_id == first.source_version_id

    # Both CURRENT versions now carry outstanding offers: there is no
    # operator-approved baseline, and the stale v2 is never returned.
    active = registry.active(SourceKind.SOUL_MD)
    assert active is None
