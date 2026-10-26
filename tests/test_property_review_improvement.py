"""Property tests for the review policy and improvement diff algebra.

Pins the deterministic review policy (spec section 10) and the improvement
module's pure functions on arbitrary signals:

* ``decide`` is deterministic; the target is the desired status exactly when a
  transition happens; a blocked decision never transitions;
* the override chain (retired → duplicates → contradiction tie → rollback)
  always wins over the score-driven path;
* hysteresis: leaving NEEDS_IMPROVEMENT or entering READY_TO_PROMOTE requires
  clearing the threshold *plus* the margin — the gate never weakens;
* one event can never promote, a pending relearn always blocks, and the
  cooldown blocks every non-safety transition;
* ``classify_impact``, ``content_fingerprint`` and the regression gate behave
  on arbitrary diffs and nodes.

All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from soul_knowledge.domain import LifecycleStatus, Node, NodeKind, PrimaryDomain
from soul_knowledge.improvement import (
    DiffOp,
    DiffOperation,
    _proposal_id,
    classify_impact,
    content_fingerprint,
    default_regression_gate,
)
from soul_knowledge.review import DEFAULT_REVIEW_POLICY, ReviewSignal, decide

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
POLICY = DEFAULT_REVIEW_POLICY

_dec = st.decimals(min_value=0, max_value=1, places=4)
_status = st.sampled_from(sorted(LifecycleStatus, key=lambda member: member.value))


def _signals() -> st.SearchStrategy[ReviewSignal]:
    return st.builds(
        ReviewSignal,
        node_id=st.just("n-1"),
        current_status=_status,
        p=_dec,
        m=_dec,
        e=_dec,
        uncertainty=_dec,
        evidence_count=st.integers(min_value=0, max_value=6),
        qualifying_review_streak=st.integers(min_value=0, max_value=4),
        last_transition_at=st.none()
        | st.datetimes(min_value=datetime(2026, 10, 20, tzinfo=UTC), max_value=NOW, timezones=st.just(UTC)),
        at=st.just(NOW),
        relearn_pending=st.booleans(),
        has_duplicates=st.booleans(),
        contradiction_tie=st.booleans(),
        retired=st.booleans(),
        just_rolled_back=st.booleans(),
    )


# ------------------------------------------------------------- decide shape


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_decide_is_deterministic(signal: ReviewSignal) -> None:
    assert decide(signal) == decide(signal)


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_target_equals_desired_exactly_when_transitioned(signal: ReviewSignal) -> None:
    decision = decide(signal)
    if decision.transitioned:
        assert decision.target_status is decision.desired_status
        assert decision.blocked_by == ()
        assert decision.desired_status is not decision.current_status
    else:
        assert decision.target_status is decision.current_status


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_a_blocked_decision_never_transitions(signal: ReviewSignal) -> None:
    decision = decide(signal)
    if decision.blocked_by:
        assert not decision.transitioned
        assert decision.target_status is signal.current_status


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_policy_version_is_pinned_to_the_policy(signal: ReviewSignal) -> None:
    assert decide(signal).policy_version == POLICY.policy_version


# ------------------------------------------------------------- override chain


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_retired_always_desires_deprecate(signal: ReviewSignal) -> None:
    if signal.retired:
        assert decide(signal).desired_status is LifecycleStatus.DEPRECATE


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_duplicates_desire_merge_unless_retired(signal: ReviewSignal) -> None:
    if signal.has_duplicates and not signal.retired:
        assert decide(signal).desired_status is LifecycleStatus.MERGE_OR_DEDUPLICATE


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_contradiction_tie_desires_needs_improvement(signal: ReviewSignal) -> None:
    if signal.contradiction_tie and not signal.retired and not signal.has_duplicates:
        assert decide(signal).desired_status is LifecycleStatus.NEEDS_IMPROVEMENT


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_rollback_desires_rollback_status(signal: ReviewSignal) -> None:
    if signal.just_rolled_back and not (signal.retired or signal.has_duplicates or signal.contradiction_tie):
        assert decide(signal).desired_status is LifecycleStatus.ROLLBACK


# ------------------------------------------------------------- promotion gate


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_desired_promotion_never_drops_below_the_scoring_gate(signal: ReviewSignal) -> None:
    decision = decide(signal)
    if decision.desired_status is LifecycleStatus.READY_TO_PROMOTE:
        assert signal.p >= POLICY.promotion_p_threshold
        assert signal.uncertainty <= POLICY.promotion_uncertainty_max


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_qualifying_for_promotion_requires_margin_evidence_and_low_uncertainty(signal: ReviewSignal) -> None:
    decision = decide(signal)
    if decision.qualifying_for_promotion:
        assert signal.p >= POLICY.promotion_p_threshold + POLICY.promotion_hysteresis
        assert signal.uncertainty <= POLICY.promotion_uncertainty_max
        assert signal.evidence_count >= POLICY.min_evidence_for_promotion


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_relearn_pending_blocks_promotion_transition(signal: ReviewSignal) -> None:
    decision = decide(signal)
    if decision.desired_status is LifecycleStatus.READY_TO_PROMOTE and signal.relearn_pending:
        assert "relearn-pending" in decision.blocked_by
        assert not decision.transitioned


@given(signal=_signals())
@settings(derandomize=True, max_examples=60, deadline=None)
def test_one_event_can_never_promote(signal: ReviewSignal) -> None:
    decision = decide(signal)
    if (
        decision.desired_status is LifecycleStatus.READY_TO_PROMOTE
        and signal.qualifying_review_streak + 1 < POLICY.min_qualifying_reviews
    ):
        assert "one-event-cannot-promote" in decision.blocked_by
        assert not decision.transitioned


# ------------------------------------------------------------- hysteresis/cooldown


def _score_signal(**overrides: Any) -> ReviewSignal:
    base: dict[str, Any] = {
        "node_id": "n-1",
        "current_status": LifecycleStatus.ACTIVE,
        "p": Decimal("0.5"),
        "m": Decimal("0.5"),
        "e": Decimal("0.5"),
        "uncertainty": Decimal("0.2"),
        "evidence_count": 5,
        "qualifying_review_streak": 5,
        "last_transition_at": None,
        "at": NOW,
    }
    base.update(overrides)
    return ReviewSignal(**base)


@given(margin=st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("0.0499"), places=4))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_hysteresis_holds_a_node_below_threshold_plus_margin(margin: Decimal) -> None:
    # An ACTIVE node needs p >= 0.70 + 0.05 = 0.75 to become READY_TO_PROMOTE;
    # anywhere in (0.70, 0.75) it must not get there.
    signal = _score_signal(p=POLICY.promotion_p_threshold + margin)
    assert decide(signal).desired_status is not LifecycleStatus.READY_TO_PROMOTE


@given(margin=st.decimals(min_value=Decimal("0.0001"), max_value=Decimal("0.0499"), places=4))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_hysteresis_keeps_needs_improvement_below_ceiling(margin: Decimal) -> None:
    signal = _score_signal(
        current_status=LifecycleStatus.NEEDS_IMPROVEMENT,
        p=POLICY.improvement_p_threshold + margin,
    )
    assert decide(signal).desired_status is LifecycleStatus.NEEDS_IMPROVEMENT


@given(hours=st.integers(min_value=0, max_value=23))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_cooldown_blocks_non_safety_transitions(hours: int) -> None:
    signal = _score_signal(
        p=Decimal("0.9"),
        last_transition_at=NOW - timedelta(hours=hours),
    )
    decision = decide(signal)
    if decision.desired_status not in {
        LifecycleStatus.NEEDS_IMPROVEMENT,
        LifecycleStatus.DEPRECATE,
        LifecycleStatus.MERGE_OR_DEDUPLICATE,
    }:
        assert "cooldown" in decision.blocked_by
        assert not decision.transitioned


@given(hours=st.integers(min_value=0, max_value=23))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_cooldown_never_blocks_safety_demotions(hours: int) -> None:
    signal = _score_signal(
        p=Decimal("0.05"),
        current_status=LifecycleStatus.ACTIVE,
        last_transition_at=NOW - timedelta(hours=hours),
    )
    decision = decide(signal)
    assert decision.desired_status is LifecycleStatus.NEEDS_IMPROVEMENT
    assert decision.transitioned
    assert "cooldown" not in decision.blocked_by


@given(signal=_signals())
@settings(derandomize=True, max_examples=40, deadline=None)
def test_zero_evidence_never_scores_above_observe(signal: ReviewSignal) -> None:
    decision = decide(signal)
    score_driven = not (signal.retired or signal.has_duplicates or signal.contradiction_tie or signal.just_rolled_back)
    if score_driven and signal.evidence_count == 0 and signal.p >= POLICY.improvement_p_threshold:
        assert decision.desired_status is LifecycleStatus.OBSERVE


# ------------------------------------------------------------- diff algebra


_diff_op = st.builds(
    DiffOperation,
    op=st.sampled_from(sorted(DiffOp, key=lambda member: member.value)),
    key=st.text(alphabet="abc", max_size=5),
    value=st.none() | st.text(alphabet="abc", max_size=8),
)


@given(diff=st.lists(_diff_op, max_size=6))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_classify_impact_matches_the_declared_high_impact_rules(diff: list[DiffOperation]) -> None:
    expected_high = any(
        operation.op in (DiffOp.ADD_RELATION, DiffOp.REMOVE_PAYLOAD_KEY)
        or (
            operation.op is DiffOp.SET_STATUS
            and str(operation.value)
            in {LifecycleStatus.DEPRECATE.value, LifecycleStatus.SUPERSEDED.value, LifecycleStatus.ROLLBACK.value}
        )
        for operation in diff
    )
    assert classify_impact(diff) == ("high" if expected_high else "low")


def test_classify_impact_of_an_empty_diff_is_low() -> None:
    assert classify_impact([]) == "low"


def _node(**overrides: Any) -> Node:
    base: dict[str, Any] = {
        "node_id": "n-1",
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "summary": "summary",
        "source_version_id": "sv-1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return Node(**base)


@given(summary_a=st.text(max_size=20), summary_b=st.text(max_size=20))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_content_fingerprint_ignores_version_and_status(summary_a: str, summary_b: str) -> None:
    first = _node(summary=summary_a, version=1, lifecycle_status=LifecycleStatus.ACTIVE)
    same_content = _node(summary=summary_a, version=7, lifecycle_status=LifecycleStatus.DEPRECATE)
    assert content_fingerprint(first) == content_fingerprint(same_content)
    if summary_a != summary_b:
        assert content_fingerprint(first) != content_fingerprint(_node(summary=summary_b))


@given(domain=st.sampled_from(sorted(PrimaryDomain)), kind=st.sampled_from(sorted(NodeKind)))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_regression_gate_flags_exactly_the_identity_drift(domain: PrimaryDomain, kind: NodeKind) -> None:
    before = _node()
    after = _node(domain=domain, kind=kind)
    failures = default_regression_gate(before, after)
    assert ("domain-changed" in failures) == (domain is not before.domain)
    assert ("kind-changed" in failures) == (kind is not before.kind)
    assert "source-version-changed" not in failures
    assert default_regression_gate(before, before) == ()


@given(
    rationale=st.text(max_size=24),
    version=st.integers(min_value=1, max_value=20),
)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_proposal_id_is_deterministic_and_rationale_sensitive(rationale: str, version: int) -> None:
    diff = [DiffOperation(op=DiffOp.SET_SUMMARY, value="x")]
    first = _proposal_id("n-1", version, diff, rationale)
    assert first == _proposal_id("n-1", version, diff, rationale)
    assert first.startswith("prop_")
    assert first != _proposal_id("n-1", version, diff, rationale + "!")
    assert first != _proposal_id("n-1", version + 1, diff, rationale)
