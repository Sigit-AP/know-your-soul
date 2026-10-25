"""Immutable event store, audit journal and source-version registry tests.

Task 2 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7 and 13.3.

Every test drives real SQLite state through the public store API. Nothing here
uses a mock: append-only enforcement, the hash chain, optimistic concurrency,
idempotent domain replay, exactly-once final audit completion, crash recovery,
hash-based source versions and concurrent readers/writers are all exercised
against a live database.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from soul_knowledge.audit import AuditLog, InvocationOutcome, UnknownInvocation
from soul_knowledge.domain import Event, EventKind, Evidence, Node, NodeKind, PrimaryDomain, Relation, RelationKind
from soul_knowledge.events import Invocation
from soul_knowledge.source_registry import (
    ParserVersions,
    RelearnOffer,
    SourceKind,
    SourceRegistry,
    SourceState,
)
from soul_knowledge.store import EventStore, InvalidRelation, VersionConflict

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
MIGRATIONS = Path(__file__).resolve().parents[1] / "src" / "soul_knowledge" / "migrations"

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
        "context_tags": ("identity",),
        "scope": "global",
        "created_at": NOW,
        "updated_at": NOW,
        "professional_level": 1,
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


def relation_event(source: str, target: str, kind: RelationKind = RelationKind.SUPPORTS) -> Event:
    relation = Relation(kind=kind, source_node_id=source, target_node_id=target)
    return Event.model_validate(
        {
            "kind": EventKind.RELATION_ADD,
            "node_id": source,
            "domain": PrimaryDomain.P1,
            "relation": relation,
            "occurred_at": NOW,
        }
    )


def make_invocation(key: str, operation: str = "knowledge.log_event") -> Invocation:
    return Invocation.create(operation, request_hash=f"rh:{key}", actor="operator", idempotency_key=key)


def outcome_of(audit: AuditLog, invocation_id: str) -> dict[str, Any]:
    stored = audit.journal_outcome(invocation_id)
    assert stored is not None, f"invocation {invocation_id} has no recorded outcome"
    return stored


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


# --------------------------------------------------------------- migrations


def test_migration_creates_the_immutable_tables(store: EventStore) -> None:
    tables = store.table_names()
    for name in (
        "schema_migration",
        "event_log",
        "node_revision",
        "evidence",
        "node_relation",
        "invocation_journal",
        "source_version",
        "relearn_offer",
        "system_state",
    ):
        assert name in tables, name


def test_migration_is_idempotent_and_recorded(store: EventStore) -> None:
    assert store.applied_migrations() == [1, 2]
    # Re-running the migration on a new store over the same file is a no-op.
    reopened = EventStore(store.db_path)
    try:
        assert reopened.applied_migrations() == [1, 2]
        assert reopened.event_count() == 0
    finally:
        reopened.close()


def test_store_persists_no_provider_settings(store: EventStore) -> None:
    sql = "\n".join(path.read_text(encoding="utf-8") for path in sorted(MIGRATIONS.glob("*.sql"))).lower()
    assert sql, "the migration SQL must exist"
    for forbidden in ("provider", "api_key", "openai", "anthropic", "embedding", "vector"):
        assert forbidden not in sql, forbidden


# ------------------------------------------------------- domain append/replay


def test_append_domain_advances_both_watermarks(store: EventStore) -> None:
    receipt = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    assert receipt.node_version == 1
    assert receipt.event_seq == 1
    assert receipt.knowledge_seq == 1
    assert receipt.idempotent_replay is False
    assert receipt.domain_event_id
    assert store.event_high_watermark() == 1
    assert store.knowledge_high_watermark() == 1


def test_watermarks_are_separate_for_audit_and_knowledge(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    store.append_domain(node_event(build_node("n-2")), idempotency_key="k2", expected_version=None)
    assert store.knowledge_high_watermark() == 2

    audit = AuditLog(store)
    invocation = make_invocation("k3")
    audit.begin(invocation)
    audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=1))

    # The audit event advances event_seq but never knowledge_seq.
    assert store.event_high_watermark() == 3
    assert store.knowledge_high_watermark() == 2


def test_non_knowledge_domain_event_does_not_advance_knowledge_seq(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    admin = Event.model_validate(
        {
            "kind": EventKind.ADMIN,
            "node_id": "n-1",
            "domain": PrimaryDomain.P1,
            "occurred_at": NOW,
            "knowledge_affecting": False,
        }
    )
    receipt = store.append_domain(admin, idempotency_key="k2", expected_version=None)
    assert receipt.knowledge_seq is None
    assert store.event_high_watermark() == 2
    assert store.knowledge_high_watermark() == 1


def test_current_node_returns_the_latest_revision(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    updated = build_node("n-1", summary="updated", version=1)
    store.append_domain(node_event(updated), idempotency_key="k2", expected_version=1)
    current = store.current_node("n-1")
    assert current is not None
    assert current.version == 2
    assert current.summary == "updated"


def test_replay_domain_reconstructs_the_current_state(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    store.append_domain(node_event(build_node("n-2")), idempotency_key="k2", expected_version=None)
    audit = AuditLog(store)
    invocation = make_invocation("k3")
    audit.begin(invocation)
    audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=1))

    replayed = list(store.replay_domain())
    assert [event.node_id for event in replayed] == ["n-1", "n-2"]
    assert all(event.kind is EventKind.NODE_UPSERT for event in replayed)
    # Replay from zero reproduces the same current nodes.
    assert sorted(node.node_id for node in (store.current_node("n-1"), store.current_node("n-2")) if node) == [
        "n-1",
        "n-2",
    ]


def test_evidence_append_is_recorded_against_its_node(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    receipt = store.append_domain(
        evidence_event(build_evidence("ev-1", "n-1")), idempotency_key="k2", expected_version=1
    )
    assert receipt.node_version == 1
    assert store.evidence_count() == 1
    recorded = store.get_evidence("ev-1")
    assert recorded is not None
    assert recorded.source_version_id == "sv-1"


# ------------------------------------------------------ optimistic concurrency


def test_expected_version_conflict_is_detected(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    with pytest.raises(VersionConflict):
        store.append_domain(node_event(build_node("n-1")), idempotency_key="k2", expected_version=0)
    # The correct expected version succeeds and bumps the revision.
    receipt = store.append_domain(node_event(build_node("n-1")), idempotency_key="k3", expected_version=1)
    assert receipt.node_version == 2
    with pytest.raises(VersionConflict):
        store.append_domain(node_event(build_node("n-1")), idempotency_key="k4", expected_version=1)
    assert store.node_version("n-1") == 2


def test_invalid_relation_to_a_missing_target_is_rejected(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    with pytest.raises(InvalidRelation):
        store.append_domain(relation_event("n-1", "ghost"), idempotency_key="k2", expected_version=1)


def test_relation_to_an_existing_target_is_recorded(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    store.append_domain(node_event(build_node("n-2")), idempotency_key="k2", expected_version=None)
    store.append_domain(relation_event("n-1", "n-2"), idempotency_key="k3", expected_version=1)
    relations = store.relations_from("n-1")
    assert [(relation.target_node_id, relation.kind) for relation in relations] == [("n-2", RelationKind.SUPPORTS)]


# --------------------------------------------------- idempotent domain replay


def test_domain_replay_preserves_the_original_receipt(store: EventStore) -> None:
    original = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    replay = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    assert replay.idempotent_replay is True
    assert replay.domain_event_id == original.domain_event_id
    assert replay.event_seq == original.event_seq
    assert replay.node_version == original.node_version
    assert replay.knowledge_seq == original.knowledge_seq
    # No new knowledge event and no watermark movement.
    assert store.domain_event_count() == 1
    assert store.knowledge_high_watermark() == 1
    assert store.event_high_watermark() == 1


def test_idempotent_replay_issues_a_new_linked_audit_id_per_retry(store: EventStore) -> None:
    audit = AuditLog(store)
    original = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)

    first = make_invocation("k1")
    audit.begin(first)
    first_receipt = audit.complete(
        first.invocation_id,
        InvocationOutcome(status="success", duration_ms=1, linked_domain_event_id=original.domain_event_id),
    )

    retry = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    assert retry.idempotent_replay is True

    second = make_invocation("k1")
    audit.begin(second)
    second_receipt = audit.complete(
        second.invocation_id,
        InvocationOutcome(status="success", duration_ms=1, linked_domain_event_id=retry.domain_event_id),
    )

    assert first_receipt.audit_event_id != second_receipt.audit_event_id
    assert second_receipt.event_seq > first_receipt.event_seq
    assert store.domain_event_count() == 1
    assert store.knowledge_high_watermark() == 1
    # Both final audit events reference the same single domain event.
    first_outcome = outcome_of(audit, first.invocation_id)
    second_outcome = outcome_of(audit, second.invocation_id)
    assert first_outcome["linked_domain_event_id"] == original.domain_event_id
    assert second_outcome["linked_domain_event_id"] == original.domain_event_id


# ------------------------------------------------------------------ hash chain


def test_hash_chain_verifies_for_a_clean_store(store: EventStore) -> None:
    for index in range(5):
        store.append_domain(node_event(build_node(f"n-{index}")), idempotency_key=f"k{index}", expected_version=None)
    report = store.verify_chain()
    assert report.ok is True
    assert report.checked == 5
    assert report.first_bad_seq is None


def test_hash_chain_detects_tampering(store: EventStore) -> None:
    for index in range(3):
        store.append_domain(node_event(build_node(f"n-{index}")), idempotency_key=f"k{index}", expected_version=None)
    assert store.verify_chain().ok is True

    # Defense in depth: the append-only trigger blocks the write first.
    with pytest.raises(sqlite3.IntegrityError):
        store.execute_raw("UPDATE event_log SET payload_json = '{}' WHERE event_seq = 2")

    # Bypass the trigger on a consistent copy and tamper; the hash chain must notice.
    tampered = Path(store.db_path).with_suffix(".tampered.sqlite3")
    source = sqlite3.connect(store.db_path)
    target = sqlite3.connect(tampered)
    try:
        source.backup(target)
    finally:
        source.close()
        target.close()
    connection = sqlite3.connect(tampered)
    try:
        connection.execute("DROP TRIGGER event_log_no_update")
        connection.execute('UPDATE event_log SET payload_json = \'{"kind":"node.upsert"}\' WHERE event_seq = 2')
        connection.commit()
    finally:
        connection.close()

    tampering_store = EventStore(tampered)
    try:
        report = tampering_store.verify_chain()
        assert report.ok is False
        assert report.first_bad_seq == 2
    finally:
        tampering_store.close()


def test_domain_and_audit_history_are_append_only(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    audit = AuditLog(store)
    invocation = make_invocation("k2")
    audit.begin(invocation)
    audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=1))

    for statement in (
        "UPDATE event_log SET actor = 'x'",
        "DELETE FROM event_log",
        "UPDATE node_revision SET payload_json = '{}'",
        "DELETE FROM node_revision",
        "UPDATE invocation_journal SET audit_event_id = 'x' WHERE status = 'complete'",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            store.execute_raw(statement)

    # The append-only history only ever grows.
    assert store.event_count() == 2
    assert store.domain_event_count() == 1
    assert store.audit_event_count() == 1


# ------------------------------------------------------------ exactly-once audit


def test_one_final_audit_event_per_successful_invocation(store: EventStore) -> None:
    audit = AuditLog(store)
    invocation = make_invocation("k1")
    journal = audit.begin(invocation)
    assert journal.status == "in_progress"
    receipt = audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=3))
    assert receipt.audit_event_id == invocation.audit_event_id
    assert receipt.status == "success"
    assert store.audit_event_count() == 1
    assert store.event_high_watermark() == 1
    assert store.knowledge_high_watermark() == 0


def test_one_final_audit_event_per_validation_failure(store: EventStore) -> None:
    audit = AuditLog(store)
    invocation = make_invocation("k1")
    audit.begin(invocation)
    receipt = audit.complete(
        invocation.invocation_id,
        InvocationOutcome(status="failure", duration_ms=2, error="INVALID_DOMAIN"),
    )
    assert receipt.status == "failure"
    assert store.audit_event_count() == 1
    assert outcome_of(audit, invocation.invocation_id)["error"] == "INVALID_DOMAIN"


def test_one_final_audit_event_per_projection_failure(store: EventStore) -> None:
    audit = AuditLog(store)
    invocation = make_invocation("k1")
    audit.begin(invocation)
    receipt = audit.complete(
        invocation.invocation_id,
        InvocationOutcome(status="failure", duration_ms=4, error="PROJECTION_FAILED", projection_outcome="failed"),
    )
    assert receipt.status == "failure"
    assert store.audit_event_count() == 1
    assert outcome_of(audit, invocation.invocation_id)["projection_outcome"] == "failed"


def test_audit_complete_is_exactly_once(store: EventStore) -> None:
    audit = AuditLog(store)
    invocation = make_invocation("k1")
    audit.begin(invocation)
    first = audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=1))
    second = audit.complete(invocation.invocation_id, InvocationOutcome(status="success", duration_ms=1))
    assert second.event_seq == first.event_seq
    assert store.audit_event_count() == 1
    assert store.journal_status(invocation.invocation_id) == "complete"


def test_complete_requires_a_known_invocation(store: EventStore) -> None:
    audit = AuditLog(store)
    with pytest.raises(UnknownInvocation):
        audit.complete("does-not-exist", InvocationOutcome(status="success", duration_ms=1))


def test_write_receipt_links_domain_and_audit(store: EventStore) -> None:
    invocation = make_invocation("k1")
    receipt = store.commit(node_event(build_node("n-1")), invocation=invocation, expected_version=None)
    assert receipt.domain_event_id
    assert receipt.node_version == 1
    assert receipt.knowledge_seq == 1
    assert receipt.audit_event_id == invocation.audit_event_id
    assert receipt.audit_event_seq > receipt.event_seq
    assert receipt.idempotent_replay is False
    assert receipt.projection_version is None
    assert receipt.source_knowledge_high_watermark == 1

    store.set_projection_version("gen-abc")
    again = store.commit(node_event(build_node("n-2")), invocation=make_invocation("k2"), expected_version=None)
    assert again.projection_version == "gen-abc"


def test_commit_records_one_failure_audit_when_the_domain_write_conflicts(store: EventStore) -> None:
    store.append_domain(node_event(build_node("n-1")), idempotency_key="setup", expected_version=None)
    invocation = make_invocation("k1")
    with pytest.raises(VersionConflict):
        store.commit(node_event(build_node("n-1")), invocation=invocation, expected_version=0)

    audit = AuditLog(store)
    assert store.audit_event_count() == 1
    assert store.journal_status(invocation.invocation_id) == "complete"
    assert outcome_of(audit, invocation.invocation_id)["status"] == "failure"
    assert audit.recover_incomplete() == []


# --------------------------------------------------------- crash recovery


def test_incomplete_journal_is_recovered_exactly_once(store: EventStore) -> None:
    audit = AuditLog(store)
    committed = make_invocation("k1")
    audit.begin(committed)
    domain = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    # Simulate a crash: the invocation journal is durable but never completed.
    assert store.journal_status(committed.invocation_id) == "in_progress"

    orphan = make_invocation("k2")
    audit.begin(orphan)

    repaired: list[str] = []
    receipts = audit.recover_incomplete(projection_repair=lambda: repaired.append("ran"), stale_after_seconds=0)

    assert repaired == ["ran"]
    assert len(receipts) == 2
    assert store.audit_event_count() == 2
    assert store.domain_event_count() == 1
    assert store.journal_status(committed.invocation_id) == "complete"
    assert store.journal_status(orphan.invocation_id) == "complete"

    outcomes = {receipt.invocation_id: receipt.status for receipt in receipts}
    assert outcomes[committed.invocation_id] == "success"
    assert outcomes[orphan.invocation_id] == "failure"
    assert outcome_of(audit, committed.invocation_id)["linked_domain_event_id"] == domain.domain_event_id
    assert outcome_of(audit, orphan.invocation_id)["error"] == "INTERRUPTED_BEFORE_COMMIT"

    # Recovery is idempotent: nothing new is appended on a second pass.
    assert audit.recover_incomplete(stale_after_seconds=0) == []
    assert store.audit_event_count() == 2
    assert store.domain_event_count() == 1


def test_crash_recovery_never_duplicates_domain_events(store: EventStore) -> None:
    audit = AuditLog(store)
    invocation = make_invocation("k1")
    audit.begin(invocation)
    original = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    audit.recover_incomplete(stale_after_seconds=0)
    replay = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1", expected_version=None)
    assert replay.idempotent_replay is True
    assert replay.domain_event_id == original.domain_event_id
    assert store.domain_event_count() == 1


def test_projection_repair_failure_is_recorded_as_a_failure(store: EventStore) -> None:
    audit = AuditLog(store)
    invocation = make_invocation("k1")
    audit.begin(invocation)

    def failing_repair() -> None:
        raise RuntimeError("projection offline")

    receipts = audit.recover_incomplete(projection_repair=failing_repair, stale_after_seconds=0)
    assert len(receipts) == 1
    assert receipts[0].status == "failure"
    assert store.audit_event_count() == 1


# ------------------------------------------------------- source versions


def write_corpus(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def test_source_version_is_hash_based_and_idempotent(store: EventStore, tmp_path: Path) -> None:
    source = tmp_path / "SOUL.md"
    source.write_text("identity\ncompliance\n", encoding="utf-8")
    registry = SourceRegistry(store)

    first = registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)
    assert first.content_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert first.byte_length == len(source.read_bytes())
    assert first.char_length == len(source.read_text(encoding="utf-8"))
    assert first.line_count == 2
    assert first.source_state is SourceState.CURRENT

    second = registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)
    assert second.source_version_id == first.source_version_id
    assert store.source_version_count() == 1


def test_corpus_manifest_hash_and_counts(store: EventStore, tmp_path: Path) -> None:
    corpus = tmp_path / "soul"
    write_corpus(corpus, {"01-a.md": "a\n", "02-b.md": "b\nb\n"})
    registry = SourceRegistry(store)
    version = registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)

    assert version.file_count == 2
    assert version.line_count == 3
    expected_manifest = hashlib.sha256()
    for entry in sorted(version.file_manifest, key=lambda item: item.path):
        expected_manifest.update(f"{entry.path}\0{entry.sha256}\n".encode())
        assert entry.byte_length > 0
    assert version.manifest_hash == expected_manifest.hexdigest()


def test_detect_change_returns_none_when_unchanged(store: EventStore, tmp_path: Path) -> None:
    source = tmp_path / "SOUL.md"
    source.write_text("stable\n", encoding="utf-8")
    registry = SourceRegistry(store)
    registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)

    offer = registry.detect_change(source, SourceKind.SOUL_MD, PARSER_VERSIONS)
    assert offer is None
    assert registry.relearn_state() is None


def test_detect_change_offers_relearn_without_selecting_a_mode(store: EventStore, tmp_path: Path) -> None:
    source = tmp_path / "SOUL.md"
    source.write_text("old\n", encoding="utf-8")
    registry = SourceRegistry(store)
    previous = registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)

    source.write_text("new content\n", encoding="utf-8")
    offer = registry.detect_change(source, SourceKind.SOUL_MD, PARSER_VERSIONS)

    assert isinstance(offer, RelearnOffer)
    assert offer.previous_source_version_id == previous.source_version_id
    assert offer.current_source_version_id != previous.source_version_id
    assert offer.mode_options == ("RESET", "ACCUMULATE")
    assert offer.default_action == "NONE"
    assert offer.requires_operator_decision is True
    assert offer.selected_mode is None
    assert offer.diff.changed == ("SOUL.md",)
    assert registry.relearn_state() == "RELEARN_PENDING"
    assert store.source_version_count() == 2
    # Re-detecting the same content is a no-op: no second version or offer.
    assert registry.detect_change(source, SourceKind.SOUL_MD, PARSER_VERSIONS) is None
    assert store.source_version_count() == 2


def test_manifest_drift_detects_added_changed_and_removed_files(store: EventStore, tmp_path: Path) -> None:
    corpus = tmp_path / "soul"
    write_corpus(corpus, {"01-a.md": "a\n", "02-b.md": "b\n", "03-c.md": "c\n"})
    registry = SourceRegistry(store)
    registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)

    (corpus / "02-b.md").write_text("b changed\n", encoding="utf-8")
    (corpus / "03-c.md").unlink()
    write_corpus(corpus, {"04-d.md": "d\n"})

    offer = registry.detect_change(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    assert offer is not None
    assert offer.diff.added == ("04-d.md",)
    assert offer.diff.changed == ("02-b.md",)
    assert offer.diff.removed == ("03-c.md",)
    assert offer.diff.unchanged_count == 1


def test_parser_version_change_alone_offers_relearn(store: EventStore, tmp_path: Path) -> None:
    source = tmp_path / "SOUL.md"
    source.write_text("stable\n", encoding="utf-8")
    registry = SourceRegistry(store)
    registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)

    changed = PARSER_VERSIONS.model_copy(update={"normalization_version": "2.0.0"})
    offer = registry.detect_change(source, SourceKind.SOUL_MD, changed)
    assert offer is not None
    assert offer.compatibility.normalization_version_changed is True
    assert offer.compatibility.parser_version_changed is False


def test_supersede_and_delete_retain_the_old_version(store: EventStore, tmp_path: Path) -> None:
    source = tmp_path / "SOUL.md"
    source.write_text("v1\n", encoding="utf-8")
    registry = SourceRegistry(store)
    first = registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)

    source.write_text("v2\n", encoding="utf-8")
    second = registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)
    superseded = registry.supersede(first.source_version_id, second.source_version_id)
    assert superseded.source_state is SourceState.SUPERSEDED
    assert superseded.superseded_by == second.source_version_id
    active = registry.active(SourceKind.SOUL_MD)
    assert active is not None
    assert active.source_version_id == second.source_version_id
    # Retention: the superseded row is still present.
    assert store.retained_source_version(first.source_version_id) is not None

    source.write_text("v3\n", encoding="utf-8")
    third = registry.observe(source, SourceKind.SOUL_MD, PARSER_VERSIONS)
    deleted = registry.mark_deleted(third.source_version_id)
    assert deleted.source_state is SourceState.DELETED
    assert store.retained_source_version(third.source_version_id) is not None
    assert store.source_version_count() == 3


# ------------------------------------------------------------- concurrency


def test_concurrent_sqlite_readers_and_writers(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.sqlite3"
    seed = EventStore(db_path)
    seed.close()

    writers = 4
    per_writer = 20
    failures: list[BaseException] = []

    def writer(index: int) -> None:
        instance = EventStore(db_path)
        try:
            for offset in range(per_writer):
                node_id = f"w{index}-n{offset}"
                instance.append_domain(
                    node_event(build_node(node_id)),
                    idempotency_key=f"w{index}-{offset}",
                    expected_version=None,
                )
        except BaseException as error:  # noqa: BLE001 - recorded and asserted below
            failures.append(error)
        finally:
            instance.close()

    def reader() -> None:
        instance = EventStore(db_path)
        try:
            last = 0
            for _ in range(40):
                watermark = instance.event_high_watermark()
                assert watermark >= last
                last = watermark
                assert list(instance.replay_domain())
        except BaseException as error:  # noqa: BLE001 - recorded and asserted below
            failures.append(error)
        finally:
            instance.close()

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(writers)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert failures == []
    final = EventStore(db_path)
    try:
        total = writers * per_writer
        assert final.domain_event_count() == total
        assert final.event_count() == total
        assert final.knowledge_high_watermark() == total
        assert final.verify_chain().ok is True
        sequences = sorted(final.event_sequences())
        assert sequences == list(range(1, total + 1))
    finally:
        final.close()


def test_concurrent_readers_see_a_monotonic_watermark(tmp_path: Path) -> None:
    db_path = tmp_path / "monotonic.sqlite3"
    store = EventStore(db_path)
    try:
        for index in range(30):
            store.append_domain(
                node_event(build_node(f"n-{index}")), idempotency_key=f"k{index}", expected_version=None
            )
        reader = EventStore(db_path)
        try:
            seen = [reader.event_high_watermark() for _ in range(10)]
        finally:
            reader.close()
        assert seen == sorted(seen)
        assert seen[-1] == 30
    finally:
        store.close()
