"""Review state machine and due-review scheduler tests.

Task 6 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 10 and the frozen ``docs/contracts/relearn-contract.md``.

The transition policy is a pure deterministic function over a
:class:`~know_your_soul.review.ReviewSignal`; the engine persists transitions
as immutable node revisions and non-transition reviews as zero-weight admin
records. The scheduler scans due metadata only — no model calls, no writes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from know_your_soul.domain import (
    Event,
    EventKind,
    Evidence,
    LifecycleStatus,
    Node,
    NodeKind,
    PrimaryDomain,
    ScoreVector,
)
from know_your_soul.review import (
    DEFAULT_REVIEW_POLICY,
    ReviewDecision,
    ReviewEngine,
    ReviewSignal,
    UnknownNode,
    decide,
)
from know_your_soul.scheduler import DueReviewScheduler, SchedulePolicy
from know_your_soul.source_registry import RELEARN_PENDING, ParserVersions, SourceKind, SourceRegistry
from know_your_soul.store import EventStore

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=30)

PARSER_VERSIONS = ParserVersions(
    parser_version="1.0.0",
    normalization_version="1.0.0",
    engine_version="0.9.95",
    scoring_contract_version="1.0.0",
)


# ------------------------------------------------------------------ builders


def sv(p: str, m: str, e: str, u: str, **extra: object) -> ScoreVector:
    return ScoreVector.model_validate({"p": p, "m": m, "e": e, "uncertainty": u, **extra})


def build_node(node_id: str = "n-1", **overrides: object) -> Node:
    payload: dict[str, object] = {
        "node_id": node_id,
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": "sv-1",
        "payload": {"fact": node_id},
        "summary": f"node {node_id}",
        "context_tags": ("identity",),
        "scope": "global",
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def seed_node(store: EventStore, node: Node, key: str | None = None) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key=key or f"seed-{node.node_id}",
    )


def make_signal(node_id: str = "n-1", **overrides: object) -> ReviewSignal:
    payload: dict[str, object] = {
        "node_id": node_id,
        "current_status": LifecycleStatus.MAINTAIN,
        "p": "0.50",
        "m": "0.40",
        "e": "0.40",
        "uncertainty": "0.30",
        "evidence_count": 5,
        "qualifying_review_streak": 0,
        "last_transition_at": None,
        "at": NOW,
    }
    payload.update(overrides)
    return ReviewSignal.model_validate(payload)


# ------------------------------------------------------- pure policy: statuses


def test_decide_maintain_for_healthy_node() -> None:
    decision = decide(make_signal())
    assert decision.target_status == LifecycleStatus.MAINTAIN
    assert decision.transitioned is False


def test_decide_observe_on_high_uncertainty() -> None:
    decision = decide(make_signal(uncertainty="0.90"))
    assert decision.target_status == LifecycleStatus.OBSERVE
    assert decision.transitioned is True


def test_decide_observe_on_zero_evidence() -> None:
    decision = decide(make_signal(evidence_count=0))
    assert decision.target_status == LifecycleStatus.OBSERVE


def test_decide_needs_improvement_below_p_threshold() -> None:
    decision = decide(make_signal(p="0.20"))
    assert decision.target_status == LifecycleStatus.NEEDS_IMPROVEMENT


def test_decide_ready_to_promote_with_full_qualification() -> None:
    decision = decide(make_signal(p="0.90", uncertainty="0.30", evidence_count=5, qualifying_review_streak=1))
    assert decision.target_status == LifecycleStatus.READY_TO_PROMOTE
    assert decision.transitioned is True


def test_decide_merge_or_deduplicate_on_duplicate_relations() -> None:
    decision = decide(make_signal(has_duplicates=True))
    assert decision.target_status == LifecycleStatus.MERGE_OR_DEDUPLICATE


def test_decide_deprecate_on_retired_node() -> None:
    decision = decide(make_signal(retired=True))
    assert decision.target_status == LifecycleStatus.DEPRECATE


def test_decide_rollback_after_improvement_rollback() -> None:
    decision = decide(make_signal(just_rolled_back=True))
    assert decision.target_status == LifecycleStatus.ROLLBACK


def test_every_spec_status_is_reachable() -> None:
    reachable = {
        decide(make_signal()).target_status,
        decide(make_signal(uncertainty="0.90")).target_status,
        decide(make_signal(p="0.20")).target_status,
        decide(make_signal(p="0.90", uncertainty="0.30", qualifying_review_streak=1)).target_status,
        decide(make_signal(has_duplicates=True)).target_status,
        decide(make_signal(retired=True)).target_status,
        decide(make_signal(just_rolled_back=True)).target_status,
    }
    assert reachable == {
        LifecycleStatus.MAINTAIN,
        LifecycleStatus.OBSERVE,
        LifecycleStatus.NEEDS_IMPROVEMENT,
        LifecycleStatus.READY_TO_PROMOTE,
        LifecycleStatus.MERGE_OR_DEDUPLICATE,
        LifecycleStatus.DEPRECATE,
        LifecycleStatus.ROLLBACK,
    }


# ------------------------------------------- insufficient evidence / promotion


def test_insufficient_evidence_blocks_promotion() -> None:
    decision = decide(make_signal(p="0.90", uncertainty="0.30", evidence_count=1, qualifying_review_streak=5))
    assert decision.target_status == LifecycleStatus.MAINTAIN
    assert "insufficient-evidence" in decision.blocked_by
    assert decision.transitioned is False


def test_one_event_cannot_promote() -> None:
    """A first qualifying review (streak 0) can never reach ready_to_promote."""
    decision = decide(make_signal(p="0.95", uncertainty="0.20", evidence_count=9, qualifying_review_streak=0))
    assert decision.target_status == LifecycleStatus.MAINTAIN
    assert "one-event-cannot-promote" in decision.blocked_by
    assert decision.qualifying_for_promotion is True


def test_relearn_pending_blocks_promotion() -> None:
    decision = decide(
        make_signal(p="0.95", uncertainty="0.20", evidence_count=9, qualifying_review_streak=3, relearn_pending=True)
    )
    assert decision.target_status == LifecycleStatus.MAINTAIN
    assert "relearn-pending" in decision.blocked_by


def test_contradiction_tie_goes_to_needs_improvement() -> None:
    """Frozen signed recency rule: a tie is left needs_improvement (spec 13.3)."""
    decision = decide(make_signal(p="0.90", uncertainty="0.30", qualifying_review_streak=3, contradiction_tie=True))
    assert decision.target_status == LifecycleStatus.NEEDS_IMPROVEMENT
    assert "contradiction-tie" in decision.reasons[0]


# ------------------------------------------------------ oscillation prevention


def test_cooldown_blocks_rapid_promotion() -> None:
    recent = NOW - timedelta(hours=1)
    decision = decide(
        make_signal(
            p="0.90",
            uncertainty="0.30",
            qualifying_review_streak=3,
            last_transition_at=recent,
        )
    )
    assert decision.target_status == LifecycleStatus.MAINTAIN
    assert "cooldown" in decision.blocked_by


def test_cooldown_elapsed_allows_transition() -> None:
    old = NOW - (DEFAULT_REVIEW_POLICY.cooldown + timedelta(seconds=1))
    decision = decide(make_signal(p="0.90", uncertainty="0.30", qualifying_review_streak=3, last_transition_at=old))
    assert decision.target_status == LifecycleStatus.READY_TO_PROMOTE
    assert decision.transitioned is True


def test_cooldown_never_blocks_safety_demotion() -> None:
    recent = NOW - timedelta(seconds=5)
    decision = decide(make_signal(p="0.10", last_transition_at=recent))
    assert decision.target_status == LifecycleStatus.NEEDS_IMPROVEMENT
    assert "cooldown" not in decision.blocked_by


def test_hysteresis_promotion_entry_above_exit_threshold() -> None:
    """Between 0.70 and 0.75: a maintain node stays maintain..."""
    decision = decide(make_signal(p="0.72", uncertainty="0.30", qualifying_review_streak=9))
    assert decision.target_status == LifecycleStatus.MAINTAIN


def test_hysteresis_promotion_exit_threshold_lower_than_entry() -> None:
    """...but a node already ready_to_promote keeps its status at the same 0.72."""
    decision = decide(
        make_signal(
            current_status=LifecycleStatus.READY_TO_PROMOTE,
            p="0.72",
            uncertainty="0.30",
            qualifying_review_streak=9,
        )
    )
    assert decision.target_status == LifecycleStatus.READY_TO_PROMOTE
    assert decision.transitioned is False


def test_hysteresis_needs_improvement_exit_margin() -> None:
    """A needs_improvement node at p=0.32 stays (exit needs >= 0.35)..."""
    decision = decide(make_signal(current_status=LifecycleStatus.NEEDS_IMPROVEMENT, p="0.32"))
    assert decision.target_status == LifecycleStatus.NEEDS_IMPROVEMENT


def test_hysteresis_needs_improvement_entry_uses_raw_threshold() -> None:
    """...while a maintain node at the same 0.32 does not drop (entry needs < 0.30)."""
    decision = decide(make_signal(p="0.32"))
    assert decision.target_status == LifecycleStatus.MAINTAIN


def test_policy_is_frozen_and_versioned() -> None:
    assert DEFAULT_REVIEW_POLICY.policy_version
    with pytest.raises(Exception):  # noqa: B017 - frozen model rejects assignment
        DEFAULT_REVIEW_POLICY.cooldown = timedelta(0)  # type: ignore[misc]


# -------------------------------------------------------------- engine (store)


def test_engine_unknown_node_raises() -> None:
    engine = ReviewEngine(EventStore(":memory:"))
    with pytest.raises(UnknownNode):
        engine.assess("missing", at=NOW)


def test_engine_review_transition_persists_immutable_revision() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node(scores=sv("0.20", "0.1", "0.1", "0.4")))
    engine = ReviewEngine(store)
    report = engine.review("n-1", at=NOW)
    assert report.decision.target_status == LifecycleStatus.NEEDS_IMPROVEMENT
    assert report.persisted is True
    node = store.current_node("n-1")
    assert node is not None
    assert node.lifecycle_status == LifecycleStatus.NEEDS_IMPROVEMENT
    assert node.version == 2
    # exactly one final audit event for the review invocation
    assert store.audit_event_count() == 1
    assert report.audit_event_id


def test_engine_review_without_transition_is_zero_weight_admin_record() -> None:
    store = EventStore(":memory:")
    seed_node(
        store,
        build_node(
            lifecycle_status=LifecycleStatus.MAINTAIN,
            scores=sv("0.5", "0.4", "0.4", "0.3"),
        ),
    )
    for index in range(3):
        store.append_domain(
            Event(
                kind=EventKind.EVIDENCE_APPEND,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                evidence=_evidence(f"ev-nt-{index}"),
                source_version_id="sv-1",
                occurred_at=NOW,
            ),
            idempotency_key=f"ev-nt-key-{index}",
        )
    engine = ReviewEngine(store)
    before_watermark = store.knowledge_high_watermark()
    report = engine.review("n-1", at=NOW)
    assert report.decision.target_status == LifecycleStatus.MAINTAIN
    assert report.persisted is True  # the review record itself is persisted
    assert store.current_node("n-1") is not None
    assert store.node_version("n-1") == 1  # no new revision
    assert store.knowledge_high_watermark() == before_watermark  # zero knowledge weight
    assert store.audit_event_count() == 1


def test_engine_streak_accumulates_until_promotion_allowed() -> None:
    """Two spaced qualifying reviews: the first cannot promote, the second can."""
    store = EventStore(":memory:")
    seed_node(
        store,
        build_node(
            lifecycle_status=LifecycleStatus.MAINTAIN,
            scores=sv("0.90", "0.5", "0.5", "0.30"),
        ),
    )
    engine = ReviewEngine(store)
    for index in range(5):
        store.append_domain(
            Event(
                kind=EventKind.EVIDENCE_APPEND,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                evidence=_evidence(f"ev-{index}"),
                source_version_id="sv-1",
                occurred_at=NOW,
            ),
            idempotency_key=f"ev-key-{index}",
        )
    first = engine.review("n-1", at=NOW)
    assert first.decision.target_status == LifecycleStatus.MAINTAIN
    assert "one-event-cannot-promote" in first.decision.blocked_by
    second = engine.review("n-1", at=NOW + timedelta(days=2))
    assert second.decision.target_status == LifecycleStatus.READY_TO_PROMOTE
    assert store.current_node("n-1") is not None
    assert store.current_node("n-1").lifecycle_status == LifecycleStatus.READY_TO_PROMOTE  # type: ignore[union-attr]


def _evidence(evidence_id: str) -> Evidence:
    return Evidence.model_validate(
        {
            "evidence_id": evidence_id,
            "node_id": "n-1",
            "source_version_id": "sv-1",
            "source_uri": "soul/a.md",
            "observed_at": NOW,
            "independence_key": f"grp-{evidence_id}",
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
    )


def test_engine_review_blocks_promotion_while_relearn_pending(tmp_path: Path) -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node(scores=sv("0.95", "0.5", "0.5", "0.20")))
    for index in range(3):
        store.append_domain(
            Event(
                kind=EventKind.EVIDENCE_APPEND,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                evidence=_evidence(f"ev-p-{index}"),
                source_version_id="sv-1",
                occurred_at=NOW,
            ),
            idempotency_key=f"ev-p-key-{index}",
        )
    registry = SourceRegistry(store)
    soul = tmp_path / "SOUL.md"
    soul.write_text("v1", encoding="utf-8")
    registry.observe(soul, SourceKind.SOUL_MD, PARSER_VERSIONS)
    soul.write_text("v1 changed", encoding="utf-8")
    offer = registry.detect_change(soul, SourceKind.SOUL_MD, PARSER_VERSIONS)
    assert offer is not None
    assert registry.relearn_state() == RELEARN_PENDING

    engine = ReviewEngine(store, registry=registry)
    engine.review("n-1", at=NOW)  # first qualifying review recorded
    report = engine.review("n-1", at=NOW + timedelta(days=2))
    assert report.decision.target_status != LifecycleStatus.READY_TO_PROMOTE
    assert "relearn-pending" in report.decision.blocked_by
    assert report.relearn_pending is True


# ------------------------------------------------------------------ scheduler


def _seed_reviewed(store: EventStore, node_id: str, at: datetime) -> None:
    engine = ReviewEngine(store)
    engine.review(node_id, at=at)


def test_scheduler_flags_never_reviewed_node() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    scheduler = DueReviewScheduler(store, ReviewEngine(store))
    candidates = scheduler.scan_due(at=NOW)
    assert [candidate.node_id for candidate in candidates] == ["n-1"]
    assert "never-reviewed" in candidates[0].reasons


def test_scheduler_skips_recently_reviewed_node() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node(scores=sv("0.5", "0.4", "0.4", "0.3")))
    _seed_reviewed(store, "n-1", NOW)
    scheduler = DueReviewScheduler(store, ReviewEngine(store))
    candidates = scheduler.scan_due(at=NOW + timedelta(hours=1))
    assert candidates == []


def test_scheduler_flags_overdue_and_dormant_and_uncertain() -> None:
    store = EventStore(":memory:")
    seed_node(
        store,
        build_node("old", scores=sv("0.5", "0.4", "0.4", "0.3"), updated_at=NOW),
    )
    _seed_reviewed(store, "old", NOW)
    seed_node(
        store,
        build_node(
            "dormant",
            updated_at=NOW,
            last_used_at=NOW,
            scores=sv("0.5", "0.4", "0.4", "0.3"),
        ),
    )
    engine = ReviewEngine(store)
    engine.review("dormant", at=LATER)
    seed_node(store, build_node("fuzzy", scores=sv("0.5", "0.4", "0.4", "0.95")))
    engine.review("fuzzy", at=LATER)

    scheduler = DueReviewScheduler(store, engine)
    far_future = LATER + timedelta(days=60)
    candidates = {candidate.node_id: candidate for candidate in scheduler.scan_due(at=far_future)}
    assert "review-overdue" in candidates["old"].reasons
    assert "dormant" in candidates["old"].reasons
    assert "dormant" in candidates["dormant"].reasons
    assert "high-uncertainty" in candidates["fuzzy"].reasons


def test_scheduler_makes_no_writes_and_no_model_calls() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node())
    scheduler = DueReviewScheduler(store, ReviewEngine(store))
    events_before = store.event_count()
    candidates = scheduler.scan_due(at=NOW, attach_decisions=True)
    assert store.event_count() == events_before  # read-only scan
    assert candidates[0].decision is not None
    assert isinstance(candidates[0].decision, ReviewDecision)


def test_scheduler_skips_terminal_statuses() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node("gone", lifecycle_status=LifecycleStatus.SUPERSEDED))
    seed_node(store, build_node("dead", lifecycle_status=LifecycleStatus.DEPRECATE))
    scheduler = DueReviewScheduler(store, ReviewEngine(store))
    assert scheduler.scan_due(at=NOW) == []


def test_scheduler_policy_interval_is_configurable() -> None:
    store = EventStore(":memory:")
    seed_node(store, build_node(scores=sv("0.5", "0.4", "0.4", "0.3")))
    _seed_reviewed(store, "n-1", NOW)
    policy = SchedulePolicy(review_interval=timedelta(hours=1))
    scheduler = DueReviewScheduler(store, ReviewEngine(store), policy=policy)
    candidates = scheduler.scan_due(at=NOW + timedelta(hours=2))
    assert [candidate.node_id for candidate in candidates] == ["n-1"]
    assert "review-overdue" in candidates[0].reasons
