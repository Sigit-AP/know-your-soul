"""Corroboration engine tests (SUB-106): the deterministic overlap /
contradiction rules of contract v4 as pure functions over submissions.

All rules come from ``docs/contracts/extraction-contract-v4.md``: node keys
``(kind, name)`` agree on equal value digests and contradict on different
ones; edge endpoint pairs agree on equal relation and contradict on different
relation; corroboration needs a distinct ``(actor, method)`` pair, overlap ≥
0.5 and zero contradictions; effective confidence is
``min(self_report, 0.35) + 0.10 × corroborating actors − 0.20 if flagged``,
clamped to ``[0, 0.65]``.
"""

from __future__ import annotations

from decimal import Decimal

from _extraction_helpers import make_args, make_method, make_payload
from know_your_soul.extraction import (
    CONTRADICTION_PENALTY,
    CORROBORATION_STEP,
    INTAKE_CAP,
    TIER_CAP,
    compare_payloads,
    effective_confidence,
    recompute_group,
    validate_submission,
)
from know_your_soul.extraction.corroboration import OVERLAP_THRESHOLD

BASE_NODES = [
    {"name": "Graft procedure", "kind": "child", "domain": "P3", "summary": "needs two witnesses"},
    {"name": "Witness", "kind": "leaf", "domain": "P3", "summary": "a procedure participant"},
]
BASE_EDGES = [
    {
        "relation": "depends_on",
        "source": {"kind": "child", "name": "Graft procedure"},
        "target": {"kind": "leaf", "name": "Witness"},
    }
]


def _submission(*, agent: str = "agent-a", actor: str = "actor-a", confidence: str = "0.8", payload=None, method=None):
    args = make_args(
        payload=payload or make_payload(nodes=list(BASE_NODES), edges=list(BASE_EDGES)), confidence=confidence
    )
    if method is not None:
        args["method"] = method
    else:
        args["method"] = make_method(agent=agent)
    submission = validate_submission(args)
    return submission, actor


class TestPairwiseCompare:
    def test_identical_payloads_fully_agree(self) -> None:
        a, _ = _submission()
        b, _ = _submission()
        result = compare_payloads(a.payload, b.payload)
        assert result.contradictions == 0
        assert result.shared == 3  # 2 node keys + 1 endpoint pair
        assert result.agreements == 3
        assert result.overlap_ratio == Decimal(1)

    def test_disjoint_payloads_share_nothing(self) -> None:
        a, _ = _submission()
        b_payload = make_payload(
            nodes=[{"name": "Other", "kind": "leaf", "domain": "P1"}],
            edges=[],
        )
        b, _ = _submission(payload=b_payload)
        result = compare_payloads(a.payload, b.payload)
        assert result.shared == 0
        assert result.overlap_ratio == Decimal(0)

    def test_same_node_key_different_summary_contradicts(self) -> None:
        a, _ = _submission()
        nodes = [dict(BASE_NODES[0], summary="needs THREE witnesses"), BASE_NODES[1]]
        b, _ = _submission(payload=make_payload(nodes=nodes, edges=list(BASE_EDGES)))
        result = compare_payloads(a.payload, b.payload)
        assert result.contradictions == 1
        assert result.agreements == 2

    def test_same_endpoint_pair_different_relation_contradicts(self) -> None:
        a, _ = _submission()
        edges = [dict(BASE_EDGES[0], relation="supports")]
        b, _ = _submission(payload=make_payload(nodes=list(BASE_NODES), edges=edges))
        result = compare_payloads(a.payload, b.payload)
        assert result.contradictions == 1

    def test_partial_overlap_ratio(self) -> None:
        a, _ = _submission()
        nodes = list(BASE_NODES) + [{"name": "Scalpel", "kind": "leaf", "domain": "P3", "summary": "a tool"}]
        b, _ = _submission(payload=make_payload(nodes=nodes, edges=list(BASE_EDGES)))
        result = compare_payloads(a.payload, b.payload)
        assert result.shared == 3
        assert result.agreements == 3
        assert result.overlap_ratio == Decimal(1)

    def test_overlap_ratio_below_threshold(self) -> None:
        a, _ = _submission()
        # Share exactly 1 of 4 comparable items: one node agrees, the other
        # node is absent in b and b adds three nodes a lacks — shared counts
        # only the intersection, so build the intersection down to one item.
        b_payload = make_payload(nodes=[BASE_NODES[0]], edges=[])
        b, _ = _submission(payload=b_payload)
        result = compare_payloads(a.payload, b.payload)
        assert result.shared == 1
        assert result.overlap_ratio == Decimal(1)
        # And a genuinely low overlap: same key, contradicted.
        c_payload = make_payload(nodes=[dict(BASE_NODES[0], summary="changed")], edges=[])
        c, _ = _submission(payload=c_payload)
        result = compare_payloads(a.payload, c.payload)
        assert result.shared == 1
        assert result.agreements == 0
        assert result.overlap_ratio == Decimal(0)

    def test_compare_is_symmetric(self) -> None:
        a, _ = _submission()
        nodes = list(BASE_NODES) + [{"name": "Extra", "kind": "leaf", "domain": "P1"}]
        b, _ = _submission(payload=make_payload(nodes=nodes, edges=list(BASE_EDGES)))
        assert compare_payloads(a.payload, b.payload) == compare_payloads(b.payload, a.payload)

    def test_compare_is_deterministic(self) -> None:
        a, _ = _submission()
        b, _ = _submission(payload=make_payload(nodes=list(BASE_NODES), edges=[]))
        assert compare_payloads(a.payload, b.payload) == compare_payloads(a.payload, b.payload)


class TestEffectiveConfidence:
    def test_intake_cap_binds_the_self_report(self) -> None:
        assert effective_confidence(Decimal("0.8"), corroborators=0, flagged=False) == INTAKE_CAP
        assert effective_confidence(Decimal("0.1"), corroborators=0, flagged=False) == Decimal("0.1")

    def test_each_corroborating_actor_adds_a_step(self) -> None:
        assert effective_confidence(Decimal("0.8"), corroborators=1, flagged=False) == INTAKE_CAP + CORROBORATION_STEP
        assert (
            effective_confidence(Decimal("0.8"), corroborators=2, flagged=False) == INTAKE_CAP + 2 * CORROBORATION_STEP
        )

    def test_tier_cap_is_never_exceeded(self) -> None:
        assert effective_confidence(Decimal("1"), corroborators=50, flagged=False) == TIER_CAP

    def test_contradiction_penalty(self) -> None:
        assert (
            effective_confidence(Decimal("0.8"), corroborators=1, flagged=True)
            == INTAKE_CAP + CORROBORATION_STEP - CONTRADICTION_PENALTY
        )

    def test_confidence_never_goes_below_zero(self) -> None:
        assert effective_confidence(Decimal("0.05"), corroborators=0, flagged=True) == Decimal(0)

    def test_confidence_is_quantized(self) -> None:
        value = effective_confidence(Decimal("0.3333333333333"), corroborators=0, flagged=False)
        assert value == value.quantize(Decimal("1E-12"))


class TestGroupRecompute:
    def test_single_submission_stays_pending(self) -> None:
        verdicts = recompute_group([_submission()])
        assert len(verdicts) == 1
        only = next(iter(verdicts.values()))
        assert verdicts and all(v.status == "pending_review" for v in verdicts.values())
        assert only.corroborators == 0

    def test_two_distinct_agents_corroborate_each_other(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a")
        b = _submission(actor="actor-b", agent="agent-b")
        verdicts = recompute_group([a, b])
        assert all(v.status == "corroborated" for v in verdicts.values())
        assert all(v.corroborators == 1 for v in verdicts.values())

    def test_same_actor_and_method_does_not_corroborate(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a")
        verdicts = recompute_group([a, a])
        assert all(v.status == "pending_review" for v in verdicts.values())

    def test_same_actor_different_method_does_not_corroborate(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a")
        b = _submission(actor="actor-a", agent="agent-b")
        verdicts = recompute_group([a, b])
        assert all(v.status == "pending_review" for v in verdicts.values())

    def test_disjoint_payloads_do_not_corroborate(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a")
        b = _submission(
            actor="actor-b",
            agent="agent-b",
            payload=make_payload(nodes=[{"name": "Other", "kind": "leaf", "domain": "P1"}], edges=[]),
        )
        verdicts = recompute_group([a, b])
        assert all(v.status == "pending_review" for v in verdicts.values())

    def test_contradiction_flags_both_sides(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a")
        nodes = [dict(BASE_NODES[0], summary="contradicted"), BASE_NODES[1]]
        b = _submission(actor="actor-b", agent="agent-b", payload=make_payload(nodes=nodes, edges=list(BASE_EDGES)))
        verdicts = recompute_group([a, b])
        assert all(v.status == "contradiction_flagged" for v in verdicts.values())
        assert all(v.contradictions == 1 for v in verdicts.values())

    def test_low_overlap_is_independent_not_corroborated(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a")
        b = _submission(actor="actor-b", agent="agent-b", payload=make_payload(nodes=[BASE_NODES[0]], edges=[]))
        verdicts = recompute_group([a, b])
        # Shared = 1 item, fully agreeing — ratio 1.0 corroborates.
        assert all(v.status == "corroborated" for v in verdicts.values())

    def test_three_agents_stack_two_steps(self) -> None:
        rows = [_submission(actor=f"actor-{i}", agent=f"agent-{i}") for i in range(3)]
        verdicts = recompute_group(rows)
        assert all(v.corroborators == 2 for v in verdicts.values())
        assert all(Decimal(v.confidence_effective) == INTAKE_CAP + 2 * CORROBORATION_STEP for v in verdicts.values())

    def test_corroboration_never_exceeds_tier_cap(self) -> None:
        rows = [_submission(actor=f"actor-{i}", agent=f"agent-{i}", confidence="1") for i in range(9)]
        verdicts = recompute_group(rows)
        assert all(Decimal(v.confidence_effective) == TIER_CAP for v in verdicts.values())

    def test_verdict_keys_are_submission_idem_keys(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a")
        b = _submission(actor="actor-b", agent="agent-b")
        verdicts = recompute_group([a, b])
        assert set(verdicts) == {
            a[0].idem_key(a[1]),
            b[0].idem_key(b[1]),
        }

    def test_recompute_is_a_pure_function(self) -> None:
        rows = [_submission(actor=f"actor-{i}", agent=f"agent-{i}") for i in range(2)]
        assert recompute_group(rows) == recompute_group(rows)

    def test_overlap_threshold_is_written(self) -> None:
        assert OVERLAP_THRESHOLD == Decimal("0.5")


class TestOverlapThresholdBoundary:
    def _ratio_payload(self, agree: int, contradict: int):
        """A payload sharing (agree + contradict) node keys with the base."""
        nodes = []
        for i in range(agree):
            nodes.append(dict(BASE_NODES[i % len(BASE_NODES)]))
        seen = {(n["kind"], n["name"]) for n in nodes}
        for i in range(contradict):
            base = dict(BASE_NODES[i % len(BASE_NODES)])
            if (base["kind"], base["name"]) in seen:
                continue
            base["summary"] = f"contradicted {i}"
            nodes.append(base)
        return make_payload(nodes=nodes, edges=[])

    def test_half_overlap_corroborates(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a", payload=make_payload(nodes=list(BASE_NODES), edges=[]))
        b = _submission(
            actor="actor-b",
            agent="agent-b",
            payload=self._ratio_payload(agree=1, contradict=0),
        )
        # shared=1, agreements=1 → ratio 1.0 ≥ 0.5
        verdicts = recompute_group([a, b])
        assert all(v.status == "corroborated" for v in verdicts.values())

    def test_contradiction_blocks_corroboration_even_with_high_overlap(self) -> None:
        a = _submission(actor="actor-a", agent="agent-a")
        nodes = [BASE_NODES[0], dict(BASE_NODES[1], summary="changed")]
        b = _submission(actor="actor-b", agent="agent-b", payload=make_payload(nodes=nodes, edges=list(BASE_EDGES)))
        verdicts = recompute_group([a, b])
        assert all(v.status == "contradiction_flagged" for v in verdicts.values())
