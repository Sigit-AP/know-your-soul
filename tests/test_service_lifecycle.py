"""Wiring probes for the lifecycle tools (findings K2/C-series, Task 4 → 5/6).

``KnowledgeService.evaluate`` must delegate to the frozen score engine and
materialize P/M/E; ``review``/``propose_improvement``/``apply_improvement``/
``rollback`` must delegate to the Task 6 engines while keeping the same
audit + projection envelope as every other tool. Each probe asserts the audit
count moves by exactly one, because the spec allows exactly one final audit
event per invocation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from soul_knowledge.contracts import (
    ApplyImprovementRequest,
    ApprovalRequiredError,
    EvaluateRequest,
    ImprovementNotFound,
    NodeNotFound,
    ProposeImprovementRequest,
    ReviewRequest,
    RollbackRequest,
)
from soul_knowledge.improvement import DiffOp, DiffOperation
from soul_knowledge.scoring import ScoreEngine
from test_service import _evidence, _seeded

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)


def _q(value: Decimal) -> str:
    """The 12-decimal serialization the contracts use."""
    return str(value.quantize(Decimal("0.000000000001")))


# --------------------------------------------------------------------- evaluate


def test_evaluate_matches_the_score_engine_and_materializes_scores(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    service.record_outcome(_evidence("e-1", "n-gates"))
    projector.rebuild()
    audits_before = store.audit_event_count()

    response = service.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))

    expected = ScoreEngine(store).score_node("n-gates", at=AT).to_contract_dict()
    assert response.p == expected["p"]
    assert response.m == expected["m"]
    assert response.e == expected["e"]
    assert response.u_p == expected["u_p"]
    assert response.overall_uncertainty == expected["overall_uncertainty"]
    assert response.professional_level == expected["professional_level"]
    assert response.node_id == "n-gates"
    assert response.audit_event_id
    assert response.stale is False
    assert response.source_knowledge_high_watermark == store.knowledge_high_watermark()
    assert response.explanations

    # evaluate is a score-moving tool: the frozen vector lands on the node.
    node = store.current_node("n-gates")
    assert node is not None
    assert _q(node.scores.p) == expected["p"]
    assert node.scores.professional_level == expected["professional_level"]

    # Exactly one final audit event per invocation.
    assert store.audit_event_count() == audits_before + 1


def test_repeated_evaluate_does_not_inflate_revisions(tmp_path: Path) -> None:
    store, _, service = _seeded(tmp_path)
    service.record_outcome(_evidence("e-1", "n-gates"))
    first = service.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))
    version = store.node_version("n-gates")
    audits = store.audit_event_count()

    second = service.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))

    assert store.node_version("n-gates") == version, "an unchanged vector must not write a revision"
    assert second.p == first.p
    assert second.audit_event_id != first.audit_event_id
    assert store.audit_event_count() == audits + 1


def test_evaluate_include_descendants_uses_primary_aggregation(tmp_path: Path) -> None:
    store, _, service = _seeded(tmp_path)
    service.record_outcome(_evidence("e-child", "n-gates"))
    response = service.evaluate(EvaluateRequest(node_id="P1", at=AT, include_descendants=True))
    expected = ScoreEngine(store).aggregate_primary("P1", at=AT).to_contract_dict()
    assert response.p == expected["p"]
    assert response.overall_uncertainty == expected["overall_uncertainty"]


def test_evaluate_unknown_node_is_typed_and_audited(tmp_path: Path) -> None:
    store, _, service = _seeded(tmp_path)
    audits_before = store.audit_event_count()
    with pytest.raises(NodeNotFound) as excinfo:
        service.evaluate(EvaluateRequest(node_id="n-missing"))
    assert excinfo.value.code == "NODE_NOT_FOUND"
    assert excinfo.value.audit_event_id
    assert store.audit_event_count() == audits_before + 1


def test_evaluate_honours_the_relearn_evidence_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _, service = _seeded(tmp_path)
    service.record_outcome(_evidence("e-1", "n-gates"))
    scored = service.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))
    assert Decimal(scored.p) > 0

    monkeypatch.setattr(service._relearn_engine, "quarantined_evidence_ids", lambda: frozenset({"e-1"}))
    quarantined = service.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))
    assert quarantined.p == "0.000000000000"
    assert quarantined.overall_uncertainty == "1.000000000000"


# ----------------------------------------------------------------------- review


def test_review_returns_decision_and_recommendation_block(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    service.record_outcome(_evidence("e-a", "n-gates"))
    service.record_outcome(_evidence("e-b", "n-gates"))
    projector.rebuild()
    audits_before = store.audit_event_count()

    response = service.review(ReviewRequest(node_id="n-gates"))

    assert response.node_id == "n-gates"
    assert response.decision.policy_version.startswith("review-policy")
    assert response.decision.current_status == "active"
    assert response.decision.target_status in {
        "active",
        "maintain",
        "observe",
        "needs_improvement",
        "ready_to_promote",
    }
    assert response.persisted is True
    assert response.relearn_pending is False
    assert response.audit_event_id
    assert response.stale is False

    block = response.recommendations
    lists = (block.best_actions, block.recommendations, block.unnecessary_actions)
    assert sum(len(items) for items in lists) == 1, "exactly one item for the reviewed node"
    item = next(items[0] for items in lists if items)
    assert item.source_node_ids == ("n-gates",)
    assert item.evidence_ids == ("e-a", "e-b")
    assert item.reason_codes and item.expiry > AT

    assert store.audit_event_count() == audits_before + 1


def test_review_due_scan_returns_typed_candidates(tmp_path: Path) -> None:
    _, _, service = _seeded(tmp_path)
    response = service.review(ReviewRequest(node_id="n-gates", include_due=True))
    assert isinstance(response.due, tuple)
    for candidate in response.due:
        assert candidate.node_id and candidate.version >= 1
        assert candidate.domain


def test_review_unknown_node_is_typed_and_audited(tmp_path: Path) -> None:
    store, _, service = _seeded(tmp_path)
    audits_before = store.audit_event_count()
    with pytest.raises(NodeNotFound) as excinfo:
        service.review(ReviewRequest(node_id="n-missing"))
    assert excinfo.value.code == "NODE_NOT_FOUND"
    assert excinfo.value.audit_event_id
    assert store.audit_event_count() == audits_before + 1


# ------------------------------------------------ propose / apply / rollback


def test_propose_apply_rollback_roundtrip(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    original = store.current_node("n-gates")
    assert original is not None

    proposal = service.propose_improvement(
        ProposeImprovementRequest(
            node_id="n-gates",
            diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="Updated gate rule"),),
            rationale="clarify the commit gate",
        )
    )
    assert proposal.proposal.proposal_id.startswith("prop_")
    assert proposal.proposal.impact == "low"
    assert proposal.audit_event_id
    assert proposal.stale is False

    applied = service.apply_improvement(ApplyImprovementRequest(proposal_id=proposal.proposal.proposal_id, at=AT))
    assert applied.status == "applied"
    assert applied.to_version > applied.from_version
    assert applied.content_sha256
    assert store.current_node("n-gates").summary == "Updated gate rule"  # type: ignore[union-attr]

    rolled = service.rollback(RollbackRequest(node_id="n-gates"))
    assert rolled.kind == "improvement"
    assert rolled.status == "rolled_back"
    assert store.current_node("n-gates").summary == original.summary  # type: ignore[union-attr]
    for response in (proposal, applied, rolled):
        assert response.audit_event_id
        assert response.source_knowledge_high_watermark <= store.knowledge_high_watermark()


def test_apply_high_impact_requires_an_approval_record(tmp_path: Path) -> None:
    _, projector, service = _seeded(tmp_path)
    projection = service.propose_improvement(
        ProposeImprovementRequest(
            node_id="n-gates",
            diff=(DiffOperation(op=DiffOp.ADD_RELATION, value={"kind": "supports", "target_node_id": "P1"}),),
            rationale="link to the primary domain",
        )
    )
    assert projection.proposal.impact == "high"
    with pytest.raises(ApprovalRequiredError) as excinfo:
        service.apply_improvement(ApplyImprovementRequest(proposal_id=projection.proposal.proposal_id))
    assert excinfo.value.code == "APPROVAL_REQUIRED"
    assert excinfo.value.audit_event_id


def test_apply_unknown_proposal_is_typed_and_audited(tmp_path: Path) -> None:
    store, _, service = _seeded(tmp_path)
    audits_before = store.audit_event_count()
    with pytest.raises(ImprovementNotFound) as excinfo:
        service.apply_improvement(ApplyImprovementRequest(proposal_id="prop_missing"))
    assert excinfo.value.code == "IMPROVEMENT_NOT_FOUND"
    assert excinfo.value.audit_event_id
    assert store.audit_event_count() == audits_before + 1


def test_rollback_request_requires_exactly_one_target() -> None:
    with pytest.raises(ValidationError):
        RollbackRequest()
    with pytest.raises(ValidationError):
        RollbackRequest(node_id="n-gates", relearn_id="relearn-1")
    with pytest.raises(ValidationError):
        RollbackRequest(relearn_id="relearn-1", to_version=2)
    request = RollbackRequest(node_id="n-gates")
    assert request.node_id == "n-gates" and request.relearn_id is None


def test_rollback_unknown_relearn_is_typed(tmp_path: Path) -> None:
    _, _, service = _seeded(tmp_path)
    with pytest.raises(Exception) as excinfo:
        service.rollback(RollbackRequest(relearn_id="relearn-missing"))
    assert getattr(excinfo.value, "code", "") == "UNKNOWN_RELEARN"
