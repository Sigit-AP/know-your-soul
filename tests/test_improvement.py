"""Structured improvement proposals, approval, regression gate and rollback tests.

Task 6 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 10. Proposals are explicit structured diffs with expected-version
checks; validation, regression checks and approval policy run before apply;
every revision is immutable and reversible.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

import pytest

from soul_knowledge.domain import Event, EventKind, LifecycleStatus, Node, NodeKind, PrimaryDomain, ScoreVector
from soul_knowledge.improvement import (
    ApprovalRecord,
    ApprovalRequired,
    DiffOp,
    DiffOperation,
    ImprovementProposal,
    ProposalValidationError,
    RegressionGateFailure,
    UnknownImprovement,
    UnknownNode,
    apply_improvement,
    classify_impact,
    content_fingerprint,
    default_regression_gate,
    get_proposal,
    merge_nodes,
    propose,
    retain_improvement,
    rollback_improvement,
)
from soul_knowledge.store import EventStore, VersionConflict

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)


def sv(p: str, m: str, e: str, u: str, **extra: object) -> ScoreVector:
    return ScoreVector.model_validate({"p": p, "m": m, "e": e, "uncertainty": u, **extra})


def build_node(node_id: str = "n-1", **overrides: object) -> Node:
    payload: dict[str, object] = {
        "node_id": node_id,
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": "sv-1",
        "payload": {"fact": node_id, "old": "remove-me"},
        "summary": f"node {node_id}",
        "plus": ("strength",),
        "minus": ("weakness",),
        "context_tags": ("identity",),
        "scope": "global",
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def seed_node(store: EventStore, node: Node) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key=f"seed-{node.node_id}-{node.version}",
    )


def low_diff() -> list[DiffOperation]:
    return [
        DiffOperation(op=DiffOp.SET_SUMMARY, value="improved summary"),
        DiffOperation(op=DiffOp.SET_PAYLOAD_KEY, key="fact", value="updated"),
        DiffOperation(op=DiffOp.ADD_PLUS, value="new strength"),
        DiffOperation(op=DiffOp.ADD_CONTEXT_TAG, value="reviewed"),
    ]


def approve(proposal_id: str, decision: Literal["approved", "rejected"] = "approved") -> ApprovalRecord:
    return ApprovalRecord(proposal_id=proposal_id, approved_by="operator", decision=decision, decided_at=NOW)


# ------------------------------------------------------------------- propose


def test_propose_persists_proposal_with_audit() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(store, node_id="n-1", diff=low_diff(), rationale="keep it fresh", at=NOW)
    assert record.proposal.node_id == "n-1"
    assert record.proposal.base_version == 1
    assert record.proposal.impact == "low"
    assert record.audit_event_id
    assert store.audit_event_count() == 1
    stored = get_proposal(store, record.proposal.proposal_id)
    assert stored == record.proposal
    # proposal records carry zero knowledge weight
    assert store.knowledge_high_watermark() == 1  # only the seed upsert


def test_propose_is_deterministic_per_request() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    a = propose(store, node_id="n-1", diff=low_diff(), rationale="same", at=NOW)
    assert a.proposal.proposal_id.startswith("prop_")


def test_propose_unknown_node_raises() -> None:
    store = EventStore(":memory:")
    with pytest.raises(UnknownNode):
        propose(store, node_id="ghost", diff=low_diff(), rationale="r", at=NOW)


def test_propose_stale_base_version_rejected() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    with pytest.raises(ProposalValidationError):
        propose(store, node_id="n-1", diff=low_diff(), rationale="r", base_version=7, at=NOW)


def test_propose_rejects_direct_score_writes() -> None:
    """Scores and identity fields are never writable through a proposal diff."""
    store = EventStore(":memory:")
    seed_node(store, build_node())
    with pytest.raises(ProposalValidationError):
        propose(
            store,
            node_id="n-1",
            diff=[DiffOperation(op=DiffOp.SET_PAYLOAD_KEY, key="scores", value={"p": "1.0"})],
            rationale="cheat",
            at=NOW,
        )


def test_propose_rejects_unknown_status_value() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    with pytest.raises(ProposalValidationError):
        propose(
            store,
            node_id="n-1",
            diff=[DiffOperation(op=DiffOp.SET_STATUS, value="promote-me-please")],
            rationale="invalid",
            at=NOW,
        )


# ----------------------------------------------------------------- classify


def test_classify_low_impact_for_content_edits() -> None:
    assert classify_impact(low_diff()) == "low"


def test_classify_high_impact_for_relations_and_deprecation() -> None:
    relation_op = DiffOperation(op=DiffOp.ADD_RELATION, value={"kind": "supports", "target_node_id": "n-2"})
    assert classify_impact([relation_op]) == "high"
    assert classify_impact([DiffOperation(op=DiffOp.SET_STATUS, value="deprecate")]) == "high"
    assert classify_impact([DiffOperation(op=DiffOp.REMOVE_PAYLOAD_KEY, key="old")]) == "high"


# --------------------------------------------------------------------- apply


def test_apply_low_impact_without_approval() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(store, node_id="n-1", diff=low_diff(), rationale="r", at=NOW)
    receipt = apply_improvement(store, record.proposal, at=LATER)
    assert receipt.status == "applied"
    assert receipt.from_version == 1
    assert receipt.to_version == 2
    node = store.current_node("n-1")
    assert node is not None
    assert node.version == 2
    assert node.summary == "improved summary"
    assert node.payload == {"fact": "updated", "old": "remove-me"}
    assert "new strength" in node.plus
    assert "reviewed" in node.context_tags
    assert node.updated_at == LATER  # updated_at moved to the apply time
    # previous revision retained immutably
    assert store.node_version("n-1") == 2


def test_apply_high_impact_requires_approval() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(
        store,
        node_id="n-1",
        diff=[DiffOperation(op=DiffOp.SET_STATUS, value="deprecate")],
        rationale="retire it",
        at=NOW,
    )
    assert record.proposal.impact == "high"
    with pytest.raises(ApprovalRequired):
        apply_improvement(store, record.proposal, at=LATER)
    # nothing was mutated; the refused invocation still left exactly one audit event
    assert store.node_version("n-1") == 1
    audits_after_refusal = store.audit_event_count()
    assert audits_after_refusal == 2  # propose + refused apply


def test_apply_high_impact_rejected_approval_refused() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(
        store,
        node_id="n-1",
        diff=[DiffOperation(op=DiffOp.SET_STATUS, value="deprecate")],
        rationale="retire it",
        at=NOW,
    )
    with pytest.raises(ApprovalRequired):
        apply_improvement(store, record.proposal, approval=approve(record.proposal.proposal_id, "rejected"), at=LATER)
    assert store.node_version("n-1") == 1


def test_apply_high_impact_with_approval_deprecates() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(
        store,
        node_id="n-1",
        diff=[DiffOperation(op=DiffOp.SET_STATUS, value="deprecate")],
        rationale="retire it",
        at=NOW,
    )
    receipt = apply_improvement(store, record.proposal, approval=approve(record.proposal.proposal_id), at=LATER)
    assert receipt.status == "applied"
    node = store.current_node("n-1")
    assert node is not None
    assert node.lifecycle_status == LifecycleStatus.DEPRECATE


def test_apply_expected_version_conflict() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(store, node_id="n-1", diff=low_diff(), rationale="r", at=NOW)
    # a competing writer moves the node to version 2 before the apply lands
    node = store.current_node("n-1")
    assert node is not None
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-1",
            domain=node.domain,
            node=node.model_copy(update={"summary": "concurrent edit"}),
            source_version_id=node.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key="competing-write",
    )
    assert store.node_version("n-1") == 2
    with pytest.raises(VersionConflict):
        apply_improvement(store, record.proposal, at=LATER)
    # the failed apply is still exactly one audited invocation
    assert store.current_node("n-1") is not None
    assert store.current_node("n-1").summary == "concurrent edit"  # type: ignore[union-attr]


def test_regression_gate_failure_blocks_apply() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(store, node_id="n-1", diff=low_diff(), rationale="r", at=NOW)

    def failing_gate(before: Node, after: Node) -> list[str]:
        return ["p-would-regress"]

    with pytest.raises(RegressionGateFailure):
        apply_improvement(store, record.proposal, gate=_combine(default_regression_gate, failing_gate), at=LATER)
    assert store.node_version("n-1") == 1  # no new revision


def _combine(*gates: Callable[[Node, Node], Sequence[str]]) -> Callable[[Node, Node], list[str]]:
    def run(before: Node, after: Node) -> list[str]:
        failures: list[str] = []
        for gate in gates:
            failures.extend(gate(before, after))
        return failures

    return run


def test_default_regression_gate_rejects_identity_changes() -> None:
    before = build_node()
    after = before.model_copy(update={"domain": PrimaryDomain.P2})
    assert default_regression_gate(before, after)
    assert default_regression_gate(before, before.model_copy(update={"summary": "ok"})) == ()


# ------------------------------------------------------------------ retain


def test_retain_marks_applied_improvement_without_new_revision() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(store, node_id="n-1", diff=low_diff(), rationale="r", at=NOW)
    applied = apply_improvement(store, record.proposal, at=LATER)
    retained = retain_improvement(store, record.proposal, at=LATER)
    assert retained.status == "retained"
    assert retained.from_version == applied.to_version
    assert retained.to_version == applied.to_version
    assert store.node_version("n-1") == 2  # retain adds no revision
    assert retained.audit_event_id != applied.audit_event_id


# -------------------------------------------------------------------- merge


def test_merge_supersedes_source_and_retains_history() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node("old"))
    seed_node(store, build_node("kept"))
    receipt = merge_nodes(
        store,
        source_node_id="old",
        target_node_id="kept",
        approval=approve("merge:old:kept"),
        at=LATER,
    )
    assert receipt.status == "merged"
    source = store.current_node("old")
    target = store.current_node("kept")
    assert source is not None and target is not None
    assert source.lifecycle_status == LifecycleStatus.SUPERSEDED
    assert target.lifecycle_status == LifecycleStatus.ACTIVE
    # supersession uses valid-from relation fields, never deletion
    relations = store.relations_from("old")
    assert any(relation.kind.value == "supersedes" and relation.target_node_id == "kept" for relation in relations)
    assert all(relation.valid_from is not None for relation in relations if relation.kind.value == "supersedes")
    # history retained: the pre-merge revision of the source still exists
    assert store.node_version("old") == 2


def test_merge_requires_approval() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node("old"))
    seed_node(store, build_node("kept"))
    with pytest.raises(ApprovalRequired):
        merge_nodes(store, source_node_id="old", target_node_id="kept", approval=None, at=LATER)


# ------------------------------------------------------------------ rollback


def test_rollback_restores_exact_pre_apply_content() -> None:
    store = EventStore(":memory:")
    original = build_node(scores=sv("0.5", "0.4", "0.4", "0.3"))
    seed_node(store, original)
    fingerprint_before = content_fingerprint(original)

    record = propose(store, node_id="n-1", diff=low_diff(), rationale="r", at=NOW)
    apply_improvement(store, record.proposal, at=LATER)
    mutated = store.current_node("n-1")
    assert mutated is not None
    assert content_fingerprint(mutated) != fingerprint_before

    receipt = rollback_improvement(store, node_id="n-1", at=LATER + timedelta(hours=1))
    assert receipt.status == "rolled_back"
    restored = store.current_node("n-1")
    assert restored is not None
    assert restored.version == 3  # immutable history: rollback is a new revision
    assert content_fingerprint(restored) == fingerprint_before  # byte-level content equality
    assert restored.lifecycle_status == LifecycleStatus.ROLLBACK
    assert receipt.content_sha256 == fingerprint_before


def test_rollback_to_explicit_version() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    diff_v2 = [DiffOperation(op=DiffOp.SET_SUMMARY, value="v2")]
    first = propose(store, node_id="n-1", diff=diff_v2, rationale="1", at=NOW)
    apply_improvement(store, first.proposal, at=NOW)
    node_v2 = store.current_node("n-1")
    assert node_v2 is not None
    diff_v3 = [DiffOperation(op=DiffOp.SET_SUMMARY, value="v3")]
    second = propose(store, node_id="n-1", diff=diff_v3, rationale="2", at=NOW)
    apply_improvement(store, second.proposal, at=NOW)
    receipt = rollback_improvement(store, node_id="n-1", to_version=2, at=LATER)
    restored = store.current_node("n-1")
    assert restored is not None
    assert receipt.to_version == 4
    assert content_fingerprint(restored) == content_fingerprint(node_v2)


def test_rollback_without_prior_improvement_raises() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    with pytest.raises(UnknownImprovement):
        rollback_improvement(store, node_id="n-1", at=NOW)


# -------------------------------------------------------------- idempotency


def test_apply_idempotent_replay_duplicates_nothing() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    record = propose(store, node_id="n-1", diff=low_diff(), rationale="r", at=NOW)
    first = apply_improvement(store, record.proposal, at=LATER, idempotency_key="apply-1")
    second = apply_improvement(store, record.proposal, at=LATER, idempotency_key="apply-1")
    assert second.idempotent_replay is True
    assert first.to_version == second.to_version == 2
    assert store.node_version("n-1") == 2


def test_proposal_model_is_frozen() -> None:
    proposal = ImprovementProposal(
        proposal_id="prop_test",
        node_id="n-1",
        base_version=1,
        diff=tuple(low_diff()),
        rationale="r",
        impact="low",
        created_at=NOW,
    )
    with pytest.raises(Exception):  # noqa: B017 - frozen model rejects assignment
        proposal.rationale = "changed"  # type: ignore[misc]
