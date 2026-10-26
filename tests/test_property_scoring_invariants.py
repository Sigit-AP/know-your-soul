"""Property tests for the scoring engine's numeric invariants (Task 5, spec 9).

Complements ``test_scoring_properties.py`` (unit bounds and permutation
invariance) with the invariants the frozen contract implies but no test pins:

* the 12-decimal ``ROUND_HALF_EVEN`` serialization round-trips exactly and is
  idempotent;
* confidence is the exact complement of uncertainty; overall uncertainty is
  exactly the per-dimension maximum;
* the G composite gate: G > 0 implies P >= 0.70 and M,E > 0; promotability
  implies the contract's promotion preconditions;
* more eligible evidence never increases uncertainty (weights are non-negative,
  so totals grow and ``1/sqrt(1+total)`` shrinks);
* exclusion accounting is conservative: excluded + eligible = submitted, and a
  fully excluded set yields zero total weight on every dimension.

All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.domain import SCORE_QUANTUM, canonical_json, quantize_score
from know_your_soul.scoring import ScoreAssessment, score_items, serialize_score

AT = datetime(2026, 10, 24, tzinfo=UTC)

_SCORE_RE = re.compile(r"^-?\d+\.\d{12}$")

_dec01 = st.decimals(min_value=0, max_value=1, places=4).map(str)
_dec_outcome = st.decimals(min_value=-1, max_value=1, places=4).map(str)
_utilization = st.sampled_from([*(f"0.{i}" for i in range(1, 10)), "1.0"])
_tag = st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=2, max_size=8)
_past_iso = st.integers(min_value=0, max_value=3650).map(
    lambda days: (AT - timedelta(days=days, seconds=1)).isoformat()
)


def _valid_item(draw_ids: tuple[str, str, str]) -> st.SearchStrategy[dict[str, Any]]:
    evidence_id, node_id, independence_key = draw_ids
    return st.fixed_dictionaries(
        {
            "evidence_id": st.just(evidence_id),
            "node_id": st.just(node_id),
            "source_uri": st.just(f"soul://corpus/{evidence_id}.md"),
            "observed_at": _past_iso,
            "context_tags": st.lists(_tag, max_size=3),
            "independence_key": st.just(independence_key),
            "outcome": _dec_outcome,
            "correctness": _dec01,
            "reliability": st.decimals(min_value=Decimal("0.0001"), max_value=1, places=4).map(str),
            "reproducibility": _dec01,
            "scope_fit": _dec01,
            "successful_use": _dec01,
            "task_coverage": _dec01,
            "quality_at_use": _dec01,
            "contradiction": _dec01,
            "utilization_u": _utilization,
            "latency_ms": st.integers(min_value=1, max_value=10_000),
            "compute_ms": st.integers(min_value=1, max_value=10_000),
            "output_chars": st.integers(min_value=0, max_value=20_000),
        }
    )


def _valid_items(min_size: int = 0, max_size: int = 10) -> st.SearchStrategy[list[dict[str, Any]]]:
    """Eligible-by-construction items: provenance present, reliability > 0, past."""
    ids = st.integers(min_value=0, max_value=10_000)
    return st.lists(
        ids.flatmap(lambda n: _valid_item((f"ev-{n}", f"node-{n % 3}", f"ik-{n % 4}"))),
        min_size=min_size,
        max_size=max_size,
    )


def _raw_items(max_size: int = 8) -> st.SearchStrategy[list[dict[str, Any]]]:
    """Items with arbitrary field garbage: any exclusion class may fire."""
    maybe_dec = st.one_of(
        _dec01,
        st.decimals(min_value=1, max_value=5, places=2).map(str),
        st.decimals(min_value=-5, max_value=0, places=2).map(str),
        st.just("not-a-decimal"),
    )
    item = st.fixed_dictionaries(
        {
            "evidence_id": st.text(alphabet="abcdefgh", min_size=1, max_size=8),
            "node_id": st.text(alphabet="xyz", min_size=1, max_size=4),
            "source_uri": st.one_of(st.just(""), st.just("soul://x")),
            "observed_at": st.one_of(
                _past_iso,
                st.just((AT + timedelta(days=3)).isoformat()),  # future: invalid
                st.just("not-a-date"),
            ),
            "context_tags": st.lists(_tag, max_size=2),
            "independence_key": st.text(alphabet="ij", min_size=1, max_size=3),
            "outcome": maybe_dec,
            "correctness": maybe_dec,
            "reliability": maybe_dec,
            "reproducibility": maybe_dec,
            "scope_fit": maybe_dec,
            "successful_use": maybe_dec,
            "task_coverage": st.one_of(_dec01, st.none()),
            "quality_at_use": maybe_dec,
            "contradiction": maybe_dec,
            "utilization_u": st.one_of(_utilization, st.just("0.15"), st.just("2.0")),
            "latency_ms": st.integers(min_value=-3, max_value=100),
            "compute_ms": st.integers(min_value=-3, max_value=100),
            "output_chars": st.integers(min_value=-3, max_value=100),
        }
    )
    return st.lists(item, max_size=max_size)


def _score(items: list[dict[str, Any]]) -> ScoreAssessment:
    return score_items(items, at=AT)


# ----------------------------------------------- serialization (spec 9)


@given(value=st.decimals(min_value=-1_000_000, max_value=1_000_000, allow_nan=False, allow_infinity=False))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_serialize_score_round_trips_through_quantize(value: Decimal) -> None:
    serialized = serialize_score(value)
    assert _SCORE_RE.fullmatch(serialized), serialized
    assert Decimal(serialized) == quantize_score(value)


@given(value=st.decimals(min_value=-1_000_000, max_value=1_000_000, allow_nan=False, allow_infinity=False))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_quantize_is_idempotent_and_within_half_ulp(value: Decimal) -> None:
    quantized = quantize_score(value)
    assert quantize_score(quantized) == quantized
    assert abs(quantized - value) <= SCORE_QUANTUM / 2
    assert quantized == quantized.quantize(SCORE_QUANTUM)


@given(value=st.decimals(min_value=-1_000_000, max_value=1_000_000, allow_nan=False, allow_infinity=False))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_serialize_score_is_idempotent_on_its_own_output(value: Decimal) -> None:
    once = serialize_score(value)
    assert serialize_score(Decimal(once)) == once


@given(items=_valid_items(max_size=8))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_contract_dict_scores_are_12_decimal_strings_in_unit_range(items: list[dict[str, Any]]) -> None:
    contract = _score(items).to_contract_dict()
    for key in (
        "p",
        "m",
        "e",
        "u_p",
        "u_m",
        "u_e",
        "overall_uncertainty",
        "confidence_p",
        "confidence_m",
        "confidence_e",
        "g",
    ):
        serialized = contract[key]
        assert _SCORE_RE.fullmatch(serialized), (key, serialized)
        assert Decimal(0) <= Decimal(serialized) <= Decimal(1), (key, serialized)


@given(items=_valid_items(max_size=8))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_contract_dict_is_byte_identical_across_repeated_runs(items: list[dict[str, Any]]) -> None:
    first = canonical_json(_score(items).to_contract_dict())
    second = canonical_json(_score(items).to_contract_dict())
    assert first == second


# ----------------------------------------------- uncertainty algebra


@given(items=_valid_items(max_size=8))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_confidence_is_the_exact_complement_of_uncertainty(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    assert assessment.confidence_p == 1 - assessment.u_p
    assert assessment.confidence_m == 1 - assessment.u_m
    assert assessment.confidence_e == 1 - assessment.u_e


@given(items=_valid_items(max_size=8))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_overall_uncertainty_is_the_per_dimension_maximum(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    assert assessment.overall_uncertainty == max(assessment.u_p, assessment.u_m, assessment.u_e)


@given(items=_valid_items(max_size=8))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_uncertainty_stays_within_unit_interval_exclusive_zero(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    for uncertainty in (assessment.u_p, assessment.u_m, assessment.u_e):
        assert Decimal(0) < uncertainty <= Decimal(1)


@given(items=_valid_items(min_size=1, max_size=6))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_more_eligible_evidence_never_increases_uncertainty(items: list[dict[str, Any]]) -> None:
    extra = {
        "evidence_id": "ev-extra",
        "node_id": "node-extra",
        "source_uri": "soul://corpus/ev-extra.md",
        "observed_at": (AT - timedelta(days=1)).isoformat(),
        "context_tags": ["extra"],
        "independence_key": "ik-extra",
        "outcome": "0.5",
        "correctness": "0.5",
        "reliability": "0.5",
        "reproducibility": "0.5",
        "scope_fit": "0.5",
        "successful_use": "0.5",
        "task_coverage": "0.5",
        "quality_at_use": "0.5",
        "contradiction": "0.5",
        "utilization_u": "0.5",
        "latency_ms": 100,
        "compute_ms": 100,
        "output_chars": 100,
    }
    base = _score(items)
    grown = _score([*items, extra])
    assert grown.u_p <= base.u_p
    assert grown.u_m <= base.u_m
    assert grown.u_e <= base.u_e


# ----------------------------------------------- composite gate and promotion


@given(items=_valid_items(max_size=10))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_positive_g_implies_the_contract_gates(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    if assessment.g > 0:
        assert assessment.p >= Decimal("0.70")
        assert assessment.m > 0
        assert assessment.e > 0


@given(items=_valid_items(max_size=10))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_g_is_zero_whenever_any_factor_is_zero_or_p_below_gate(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    if assessment.p < Decimal("0.70") or assessment.m == 0 or assessment.e == 0:
        assert assessment.g == 0


@given(items=_valid_items(max_size=10))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_promotable_implies_the_promotion_preconditions(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    if assessment.promotable:
        assert assessment.p >= Decimal("0.70")
        assert assessment.u_p <= Decimal("0.50")
        assert not assessment.zero_total_weight["p"].zero


@given(items=_valid_items(max_size=10))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_professional_level_stays_within_1_to_5(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    assert 1 <= assessment.professional_level <= 5


@given(items=_raw_items())
@settings(derandomize=True, max_examples=40, deadline=None)
def test_zero_total_weight_forces_level_one_and_no_m_bin(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    if assessment.zero_total_weight["p"].zero:
        assert assessment.professional_level == 1
    if assessment.zero_total_weight["m"].zero:
        assert assessment.selected_m_bin is None
        assert assessment.m_bin_evidence_count is None


# ----------------------------------------------- exclusion accounting


@given(items=_raw_items())
@settings(derandomize=True, max_examples=50, deadline=None)
def test_exclusion_counts_never_exceed_submission_count(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    excluded = sum(assessment.exclusion_counts.values())
    assert excluded <= len(items)


@given(items=_raw_items())
@settings(derandomize=True, max_examples=50, deadline=None)
def test_fully_excluded_input_yields_zero_total_weight_everywhere(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    if sum(assessment.exclusion_counts.values()) == len(items):
        assert all(assessment.zero_total_weight[dimension].zero for dimension in ("p", "m", "e"))
        assert assessment.p == 0 and assessment.m == 0 and assessment.e == 0
        assert assessment.u_p == 1 and assessment.u_m == 1 and assessment.u_e == 1
        assert assessment.g == 0
        assert not assessment.promotable


@given(items=_raw_items())
@settings(derandomize=True, max_examples=50, deadline=None)
def test_exclusion_keys_are_the_closed_vocabulary(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    assert set(assessment.exclusion_counts) == {
        "missing_provenance",
        "disjoint_context",
        "zero_reliability",
        "other_exclusions",
    }
    assert all(count >= 0 for count in assessment.exclusion_counts.values())


@given(items=_raw_items())
@settings(derandomize=True, max_examples=50, deadline=None)
def test_m_bin_selection_is_internally_consistent(items: list[dict[str, Any]]) -> None:
    assessment = _score(items)
    assert (assessment.selected_m_bin is None) == (assessment.m_bin_evidence_count is None)
    if assessment.selected_m_bin is not None:
        assert assessment.m > 0
        assert assessment.m_bin_evidence_count is not None and assessment.m_bin_evidence_count >= 3
