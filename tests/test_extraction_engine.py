"""Extraction engine tests (SUB-106): the full contract-v4 lifecycle.

submit (validate → bind → dedupe → corroborate → queue + one audit) → review
(deterministic queue + per-submission diff) → approve (operator-gated apply
through the existing audited write path, truth-tier stamped) / reject.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from _extraction_helpers import (
    SOURCE_SHA256,
    SOURCE_SHA256_B,
    audit_events,
    make_args,
    make_method,
    make_payload,
    seed_engine,
    seed_root,
)
from know_your_soul.domain import canonical_json
from know_your_soul.extraction import (
    INTAKE_CAP,
    TIER_CAP,
    ExtractionDuplicateError,
    ExtractionNotFoundError,
    ExtractionStateError,
    PayloadSchemaError,
    SourceHashMismatchError,
    SourceNotFoundError,
    SourcePathOutsideRootError,
    applied_node_id,
)


class TestSubmitLifecycle:
    def test_submit_returns_a_pending_receipt(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        assert receipt["receipt_id"].startswith("ext-")
        assert receipt["status"] == "pending_review"
        assert receipt["duplicate"] is False
        assert receipt["source_sha256"] == SOURCE_SHA256
        assert receipt["nodes"] == 2
        assert receipt["edges"] == 1
        assert receipt["audit_event_id"]
        store.close()

    def test_submit_never_touches_the_canonical_graph(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        engine.submit(make_args(), actor=actor)
        with store.read() as conn:
            rows = conn.execute("SELECT COUNT(*) AS n FROM event_log WHERE kind = 'domain'").fetchone()
        assert int(rows["n"]) == 0  # proposal-only: no domain events
        store.close()

    def test_exactly_one_audit_event_per_submit(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        engine.submit(make_args(), actor=actor)
        events = audit_events(store, "knowledge.submit_extraction")
        assert len(events) == 1
        assert events[0]["status"] == "success"
        store.close()

    def test_audit_event_carries_hashes_never_content(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        event = audit_events(store, "knowledge.submit_extraction")[0]
        blob = canonical_json(event)
        assert SOURCE_SHA256 in blob
        assert receipt["payload_sha256"] in blob
        assert "Graft procedure" not in blob  # content never rides the audit
        store.close()

    def test_typed_failures_are_audited_exactly_once(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        with pytest.raises(PayloadSchemaError) as caught:
            engine.submit({"source_sha256": SOURCE_SHA256}, actor=actor)
        assert caught.value.code == "PAYLOAD_SCHEMA"
        events = audit_events(store, "knowledge.submit_extraction")
        assert len(events) == 1
        assert events[0]["status"] == "failure"
        assert caught.value.audit_event_id == events[0]["audit_event_id"]
        store.close()


class TestProvenanceBinding:
    def test_source_path_binds_to_a_real_file(self, tmp_path: Path) -> None:
        root = seed_root(tmp_path)
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(source_path="scan-a.txt"), actor=actor, extraction_root=root)
        assert receipt["status"] == "pending_review"
        store.close()

    def test_tampered_hash_is_rejected(self, tmp_path: Path) -> None:
        root = seed_root(tmp_path)
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        tampered = SOURCE_SHA256_B  # the OTHER file's hash
        with pytest.raises(SourceHashMismatchError):
            engine.submit(
                make_args(source_sha256=tampered, source_path="scan-a.txt"), actor=actor, extraction_root=root
            )
        store.close()

    def test_hash_mismatch_is_audited(self, tmp_path: Path) -> None:
        root = seed_root(tmp_path)
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        with pytest.raises(SourceHashMismatchError):
            engine.submit(
                make_args(source_sha256=SOURCE_SHA256_B, source_path="scan-a.txt"), actor=actor, extraction_root=root
            )
        events = audit_events(store, "knowledge.submit_extraction")
        assert len(events) == 1 and events[0]["status"] == "failure"
        store.close()

    def test_path_escape_is_typed(self, tmp_path: Path) -> None:
        root = seed_root(tmp_path)
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        with pytest.raises(SourcePathOutsideRootError):
            engine.submit(make_args(source_path="../escape.txt"), actor=actor, extraction_root=root)
        store.close()

    def test_missing_source_is_typed(self, tmp_path: Path) -> None:
        root = seed_root(tmp_path)
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        with pytest.raises(SourceNotFoundError):
            engine.submit(make_args(source_path="ghost.txt"), actor=actor, extraction_root=root)
        store.close()

    def test_source_path_without_root_is_refused(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        with pytest.raises(SourceNotFoundError):
            engine.submit(make_args(source_path="scan-a.txt"), actor=actor)  # no extraction root
        store.close()

    def test_hash_only_submission_binds_external_source(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)  # no source_path at all
        assert receipt["source_sha256"] == SOURCE_SHA256
        store.close()


class TestIdempotency:
    def test_resubmission_returns_the_same_receipt(self, tmp_path: Path) -> None:
        engine, _service, store, queue, actor = seed_engine(tmp_path)
        first = engine.submit(make_args(), actor=actor)
        second = engine.submit(make_args(), actor=actor)
        assert second["receipt_id"] == first["receipt_id"]
        assert second["duplicate"] is True
        assert second["payload_sha256"] == first["payload_sha256"]
        assert queue.count() == 1
        store.close()

    def test_resubmission_with_a_new_timestamp_is_still_a_replay(self, tmp_path: Path) -> None:
        engine, _service, store, queue, actor = seed_engine(tmp_path)
        first = engine.submit(make_args(), actor=actor)
        later = make_args(method=make_method() | {"extracted_at": "2026-10-30T13:00:00+00:00"})
        second = engine.submit(later, actor=actor)
        assert second["receipt_id"] == first["receipt_id"]
        assert queue.count() == 1
        store.close()

    def test_same_key_different_payload_is_a_typed_conflict(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        engine.submit(make_args(), actor=actor)
        changed = make_args(payload=make_payload(text="a different reading"))
        with pytest.raises(ExtractionDuplicateError):
            engine.submit(changed, actor=actor)
        store.close()

    def test_same_payload_different_actor_is_a_new_submission(self, tmp_path: Path) -> None:
        engine, _service, store, queue, actor = seed_engine(tmp_path)
        first = engine.submit(make_args(), actor=actor)
        second = engine.submit(make_args(), actor="actor-b")
        assert second["receipt_id"] != first["receipt_id"]
        assert queue.count() == 2
        store.close()

    def test_replay_is_audited_as_its_own_invocation(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        engine.submit(make_args(), actor=actor)
        engine.submit(make_args(), actor=actor)
        events = audit_events(store, "knowledge.submit_extraction")
        assert len(events) == 2
        assert all(event["status"] == "success" for event in events)
        store.close()


class TestCorroborationLifecycle:
    def test_second_agent_corroborates(self, tmp_path: Path) -> None:
        engine, _service, store, queue, _ = seed_engine(tmp_path)
        first = engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
        second = engine.submit(make_args(method=make_method(agent="agent-b")), actor="actor-b")
        assert second["status"] == "corroborated"
        assert second["corroboration"]["corroborators"] == 1
        # the earlier row is updated deterministically too
        earlier = queue.get(first["receipt_id"])
        assert earlier is not None and earlier.status == "corroborated"
        store.close()

    def test_corroboration_raises_effective_confidence(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        engine.submit(make_args(method=make_method(agent="agent-a"), confidence="0.8"), actor="actor-a")
        second = engine.submit(make_args(method=make_method(agent="agent-b"), confidence="0.8"), actor="actor-b")
        assert Decimal(second["confidence_effective"]) == INTAKE_CAP + Decimal("0.10")

    def test_contradiction_flags_review_on_both_sides(self, tmp_path: Path) -> None:
        engine, _service, store, queue, _ = seed_engine(tmp_path)
        first = engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
        nodes = [
            {"name": "Graft procedure", "kind": "child", "domain": "P3", "summary": "needs NO witnesses"},
            {"name": "Witness", "kind": "leaf", "domain": "P3", "summary": "a procedure participant"},
        ]
        second = engine.submit(
            make_args(method=make_method(agent="agent-b"), payload=make_payload(nodes=nodes)),
            actor="actor-b",
        )
        assert second["status"] == "contradiction_flagged"
        assert second["corroboration"]["contradictions"] == 1
        earlier = queue.get(first["receipt_id"])
        assert earlier is not None and earlier.status == "contradiction_flagged"
        store.close()

    def test_contradiction_penalty_applies(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
        nodes = [
            {"name": "Graft procedure", "kind": "child", "domain": "P3", "summary": "changed"},
            {"name": "Witness", "kind": "leaf", "domain": "P3", "summary": "a procedure participant"},
        ]
        second = engine.submit(
            make_args(method=make_method(agent="agent-b"), payload=make_payload(nodes=nodes), confidence="0.8"),
            actor="actor-b",
        )
        assert Decimal(second["confidence_effective"]) == INTAKE_CAP - Decimal("0.20")

    def test_the_same_actor_never_corroborates_itself(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
        second = engine.submit(make_args(method=make_method(agent="agent-b")), actor="actor-a")
        assert second["status"] == "pending_review"
        assert second["corroboration"]["corroborators"] == 0
        store.close()

    def test_disjoint_extractions_of_one_source_stay_independent(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
        payload = make_payload(nodes=[{"name": "Unrelated", "kind": "leaf", "domain": "P1"}], edges=[])
        second = engine.submit(make_args(method=make_method(agent="agent-b"), payload=payload), actor="actor-b")
        assert second["status"] == "pending_review"
        store.close()


class TestTruthTier:
    def test_self_report_is_capped_at_the_intake_cap(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(confidence="1.0"), actor=actor)
        assert receipt["confidence_reported"] == "1.0"
        assert Decimal(receipt["confidence_effective"]) <= INTAKE_CAP
        store.close()

    def test_confidence_never_exceeds_the_tier_cap_even_corroborated(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        receipt = None
        for index in range(6):
            receipt = engine.submit(make_args(method=make_method(agent=f"agent-{index}")), actor=f"actor-{index}")
        assert receipt is not None
        assert Decimal(receipt["confidence_effective"]) <= TIER_CAP
        store.close()

    def test_applied_node_carries_the_agent_extracted_tier(self, tmp_path: Path) -> None:
        engine, service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.approve(receipt["receipt_id"], operator="operator", service=service)
        node_id = applied_node_id(SOURCE_SHA256, "child", "Graft procedure")
        node = store.current_node(node_id)
        assert node is not None
        certainty = node.payload["certainty"]
        assert certainty["tier"] == "agent-extracted"
        assert certainty["source_sha256"] == SOURCE_SHA256
        assert certainty["receipt_id"] == receipt["receipt_id"]
        assert "agent-extracted" in node.context_tags
        assert node.scores.confidence <= TIER_CAP
        assert node.scores.confidence > 0
        assert node.scores.uncertainty == 1 - node.scores.confidence
        assert node.scores.p == 0 and node.scores.m == 0 and node.scores.e == 0
        assert node.scores.professional_level == 1
        store.close()

    def test_applied_node_provenance_binds_the_source_hash(self, tmp_path: Path) -> None:
        engine, service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.approve(receipt["receipt_id"], operator="operator", service=service)
        node = store.current_node(applied_node_id(SOURCE_SHA256, "child", "Graft procedure"))
        assert node is not None
        assert f"sha256:{SOURCE_SHA256}" in node.provenance
        assert f"extraction:{receipt['receipt_id']}" in node.provenance
        assert "agent:claude-opus-4.7" in node.provenance
        store.close()

    def test_applied_node_summaries_are_sanitized(self, tmp_path: Path) -> None:
        engine, service, store, _queue, actor = seed_engine(tmp_path)
        payload = make_payload(
            nodes=[{"name": "Graft procedure", "kind": "child", "domain": "P3", "summary": "<b>two</b> witnesses"}],
            edges=[],
        )
        receipt = engine.submit(make_args(payload=payload), actor=actor)
        engine.approve(receipt["receipt_id"], operator="operator", service=service)
        node = store.current_node(applied_node_id(SOURCE_SHA256, "child", "Graft procedure"))
        assert node is not None
        assert "<b>" not in node.summary
        assert "&lt;b&gt;" in node.summary
        store.close()


class TestApprove:
    def test_approve_applies_nodes_and_edges_to_the_graph(self, tmp_path: Path) -> None:
        engine, service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        result = engine.approve(receipt["receipt_id"], operator="operator", service=service)
        assert result["status"] == "applied"
        assert len(result["applied_nodes"]) == 2
        assert result["applied_edges"] == 1
        source_id = applied_node_id(SOURCE_SHA256, "child", "Graft procedure")
        target_id = applied_node_id(SOURCE_SHA256, "leaf", "Witness")
        assert store.current_node(source_id) is not None
        assert store.current_node(target_id) is not None
        relations = store.relations_from(source_id)
        assert any(rel.target_node_id == target_id and rel.kind.value == "depends_on" for rel in relations)
        store.close()

    def test_approve_marks_the_row(self, tmp_path: Path) -> None:
        engine, service, store, queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.approve(receipt["receipt_id"], operator="operator", service=service)
        row = queue.get(receipt["receipt_id"])
        assert row is not None and row.status == "applied"
        assert row.decided_by == "operator"
        store.close()

    def test_approve_journals_one_decision_audit(self, tmp_path: Path) -> None:
        engine, service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        result = engine.approve(receipt["receipt_id"], operator="operator", service=service)
        events = audit_events(store, "knowledge.extraction_approve")
        assert len(events) == 1
        assert events[0]["status"] == "success"
        assert events[0]["audit_event_id"] == result["audit_event_id"]
        # the apply path's own writes are audited through the existing path
        assert len(audit_events(store, "knowledge.log_event")) == 2
        store.close()

    def test_approve_is_idempotent_under_retry(self, tmp_path: Path) -> None:
        engine, service, store, queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        first = engine.approve(receipt["receipt_id"], operator="operator", service=service)
        # simulate a crash before the row was marked: reset the row state
        with queue._write() as conn:  # noqa: SLF001 - test-only state surgery
            conn.execute(
                "UPDATE extraction_submissions SET status = 'pending_review' WHERE receipt_id = ?",
                (receipt["receipt_id"],),
            )
        second = engine.approve(receipt["receipt_id"], operator="operator", service=service)
        assert second["status"] == "applied"
        node = store.current_node(applied_node_id(SOURCE_SHA256, "child", "Graft procedure"))
        assert node is not None and node.version == 1  # replayed, never double-applied
        assert first["applied_nodes"] == second["applied_nodes"]
        store.close()

    def test_double_approve_is_a_typed_state_error(self, tmp_path: Path) -> None:
        engine, service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.approve(receipt["receipt_id"], operator="operator", service=service)
        with pytest.raises(ExtractionStateError):
            engine.approve(receipt["receipt_id"], operator="operator", service=service)
        store.close()

    def test_approve_of_a_rejected_row_is_a_typed_state_error(self, tmp_path: Path) -> None:
        engine, service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.reject(receipt["receipt_id"], operator="operator", note="no")
        with pytest.raises(ExtractionStateError):
            engine.approve(receipt["receipt_id"], operator="operator", service=service)
        store.close()

    def test_approve_unknown_receipt_is_typed(self, tmp_path: Path) -> None:
        engine, service, store, _queue, _ = seed_engine(tmp_path)
        with pytest.raises(ExtractionNotFoundError):
            engine.approve("ext-" + "0" * 32, operator="operator", service=service)
        events = audit_events(store, "knowledge.extraction_approve")
        assert len(events) == 1 and events[0]["status"] == "failure"
        store.close()


class TestReject:
    def test_reject_marks_the_row_with_the_note(self, tmp_path: Path) -> None:
        engine, _service, store, queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        result = engine.reject(receipt["receipt_id"], operator="operator", note="illegible")
        assert result["status"] == "rejected"
        row = queue.get(receipt["receipt_id"])
        assert row is not None and row.status == "rejected"
        assert row.decision_note == "illegible"
        store.close()

    def test_reject_writes_nothing_to_the_graph(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.reject(receipt["receipt_id"], operator="operator")
        with store.read() as conn:
            rows = conn.execute("SELECT COUNT(*) AS n FROM event_log WHERE kind = 'domain'").fetchone()
        assert int(rows["n"]) == 0
        store.close()

    def test_reject_journals_one_decision_audit(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.reject(receipt["receipt_id"], operator="operator")
        events = audit_events(store, "knowledge.extraction_reject")
        assert len(events) == 1 and events[0]["status"] == "success"
        store.close()

    def test_double_reject_is_a_typed_state_error(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.reject(receipt["receipt_id"], operator="operator")
        with pytest.raises(ExtractionStateError):
            engine.reject(receipt["receipt_id"], operator="operator")
        store.close()

    def test_reject_unknown_receipt_is_typed(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        with pytest.raises(ExtractionNotFoundError):
            engine.reject("ext-" + "0" * 32, operator="operator")
        store.close()


class TestReview:
    def test_review_lists_the_queue_in_the_contract_order(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        pending = engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
        flagged_a = engine.submit(
            make_args(source_sha256=SOURCE_SHA256_B, method=make_method(agent="agent-a")),
            actor="actor-a",
        )
        flagged_b = engine.submit(
            make_args(
                source_sha256=SOURCE_SHA256_B,
                method=make_method(agent="agent-b"),
                payload=make_payload(
                    nodes=[{"name": "Graft procedure", "kind": "child", "domain": "P3", "summary": "different"}],
                    edges=[],
                ),
            ),
            actor="actor-b",
        )
        listing = engine.review()
        assert listing["count"] == 3
        statuses = [row["status"] for row in listing["rows"]]
        assert statuses[:2] == ["contradiction_flagged", "contradiction_flagged"]
        assert statuses[2] == "pending_review"
        assert {row["receipt_id"] for row in listing["rows"]} == {
            pending["receipt_id"],
            flagged_a["receipt_id"],
            flagged_b["receipt_id"],
        }
        store.close()

    def test_review_filters_by_status(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
        listing = engine.review(status="corroborated")
        assert listing["rows"] == []
        store.close()

    def test_review_receipt_shows_the_proposed_diff(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        detail = engine.review(receipt_id=receipt["receipt_id"])
        assert detail["receipt_id"] == receipt["receipt_id"]
        assert detail["diff"]["text_excerpt"].startswith("the graft procedure")
        assert len(detail["diff"]["nodes"]) == 2
        assert len(detail["diff"]["edges"]) == 1
        assert detail["diff"]["nodes"][0]["name"] in {"Graft procedure", "Witness"}
        store.close()

    def test_review_unknown_receipt_is_typed(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, _ = seed_engine(tmp_path)
        with pytest.raises(ExtractionNotFoundError):
            engine.review(receipt_id="ext-" + "0" * 32)
        store.close()

    def test_review_is_read_only(self, tmp_path: Path) -> None:
        engine, _service, store, queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.review()
        engine.review(receipt_id=receipt["receipt_id"])
        assert queue.count() == 1
        assert audit_events(store, "knowledge.extraction_approve") == []
        store.close()


class TestRollback:
    def test_applied_extraction_rolls_back_byte_exact(self, tmp_path: Path) -> None:
        engine, service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        engine.approve(receipt["receipt_id"], operator="operator", service=service)
        node_id = applied_node_id(SOURCE_SHA256, "child", "Graft procedure")
        applied = store.current_node(node_id)
        assert applied is not None and applied.version == 1
        # an operator edit through the existing propose/apply path…
        from know_your_soul.improvement import DiffOp, DiffOperation

        proposal = service.propose_improvement(
            node_id=node_id,
            diff=[DiffOperation(op=DiffOp.SET_SUMMARY, value="operator-refined summary")],
            rationale="refine",
            actor="operator",
        )
        service.apply_improvement(proposal_id=proposal.proposal.proposal_id, actor="operator")
        edited = store.current_node(node_id)
        assert edited is not None and edited.version == 2
        # …rolls back to the exact extraction-applied revision.
        service.rollback(node_id=node_id, to_version=1, actor="operator")
        restored = store.current_node(node_id)
        assert restored is not None
        assert restored.version == 3
        # byte-exact on the content fields (version/updated_at move by design,
        # and the rollback revision itself carries the rollback status)
        from know_your_soul.domain import LifecycleStatus

        assert restored.lifecycle_status == LifecycleStatus.ROLLBACK
        for field_name in (
            "summary",
            "payload",
            "provenance",
            "context_tags",
            "scope",
            "kind",
            "domain",
            "scores",
        ):
            restored_value = getattr(restored, field_name)
            applied_value = getattr(applied, field_name)
            assert restored_value == applied_value, field_name
        assert restored.payload["certainty"]["tier"] == "agent-extracted"
        store.close()


class TestDeterminism:
    def test_two_identical_runs_produce_identical_queues(self, tmp_path: Path) -> None:
        dumps = []
        receipts = []
        for run in range(2):
            engine, _service, store, queue, _ = seed_engine(tmp_path / f"run{run}")
            first = engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
            second = engine.submit(make_args(method=make_method(agent="agent-b")), actor="actor-b")
            receipts.append((first, second))
            dumps.append(queue.canonical_dump())
            store.close()
        assert dumps[0] == dumps[1]
        assert receipts[0][0]["receipt_id"] == receipts[1][0]["receipt_id"]
        assert receipts[0][1]["receipt_id"] == receipts[1][1]["receipt_id"]
        assert receipts[0][1]["confidence_effective"] == receipts[1][1]["confidence_effective"]

    def test_decimal_rendering_is_fixed_point_never_scientific(self, tmp_path: Path) -> None:
        engine, _service, store, _queue, actor = seed_engine(tmp_path)
        receipt = engine.submit(make_args(), actor=actor)
        assert receipt["confidence_effective"] == "0.350000000000"
        assert receipt["corroboration"]["overlap_ratio"] == "0.000000000000"
        assert "E" not in receipt["confidence_effective"]
        store.close()
