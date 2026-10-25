"""Property tests for the calibrated P/M/E scoring engine (Task 5).

Each test names the frozen contract clause it pins (see
``docs/mathematics/scoring-contract.md``). All runs are deterministic:
hypothesis runs derandomized with a fixed example count, and shuffles use
seeded ``random.Random`` instances.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soul_knowledge.scoring import score_items

AT = datetime(2026, 10, 24, tzinfo=UTC)


def make_item(evidence_id: str, **overrides: Any) -> dict[str, Any]:
    """A fully valid evidence item (frozen schema) with sane defaults."""
    item: dict[str, Any] = {
        "evidence_id": evidence_id,
        "node_id": "n1",
        "source_uri": f"soul://corpus/{evidence_id}",
        "observed_at": AT.isoformat(),
        "context_tags": ["python"],
        "independence_key": f"ik-{evidence_id}",
        "outcome": "1",
        "correctness": "1",
        "reliability": "1",
        "reproducibility": "1",
        "scope_fit": "1",
        "successful_use": "1",
        "task_coverage": "1",
        "quality_at_use": "1",
        "contradiction": "0",
        "utilization_u": "0.5",
        "latency_ms": 100,
        "compute_ms": 100,
        "output_chars": 100,
    }
    item.update(overrides)
    return item


def run(items: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
    return score_items(items, at=kwargs.pop("at", AT), **kwargs).to_contract_dict()


# ---------------------------------------------------------------- [0,1] bounds

_decimal_01 = st.decimals(min_value=0, max_value=1, places=4).map(str)
_decimal_outcome = st.decimals(min_value=-1, max_value=1, places=4).map(str)


@st.composite
def valid_items(draw: st.DrawFn, min_size: int = 0, max_size: int = 6) -> list[dict[str, Any]]:
    count = draw(st.integers(min_value=min_size, max_value=max_size))
    items = []
    for index in range(count):
        age_days = draw(st.integers(min_value=0, max_value=400))
        items.append(
            make_item(
                f"e{index:04d}",
                observed_at=(AT - timedelta(days=age_days)).isoformat(),
                outcome=draw(_decimal_01),
                correctness=draw(_decimal_01),
                reliability=draw(_decimal_01),
                reproducibility=draw(_decimal_01),
                scope_fit=draw(_decimal_01),
                successful_use=draw(_decimal_01),
                task_coverage=draw(_decimal_01),
                quality_at_use=draw(_decimal_01),
                contradiction=draw(_decimal_01),
                utilization_u=draw(st.sampled_from([f"0.{i}" for i in range(1, 10)] + ["1.0"])),
                latency_ms=draw(st.integers(min_value=1, max_value=10_000)),
                compute_ms=draw(st.integers(min_value=1, max_value=10_000)),
                output_chars=draw(st.integers(min_value=0, max_value=20_000)),
                independence_key=f"ik-{draw(st.integers(min_value=0, max_value=3))}",
            )
        )
    return items


@given(items=valid_items())
@settings(derandomize=True, max_examples=40, deadline=None)
def test_scores_stay_within_unit_bounds(items: list[dict[str, Any]]) -> None:
    """Contract: scores P/M/E/G and uncertainties live in [0,1]."""
    result = run(items)
    for key in ("p", "m", "e", "g", "u_p", "u_m", "u_e", "overall_uncertainty"):
        value = Decimal(result[key])
        assert Decimal(0) <= value <= Decimal(1), f"{key}={value} out of [0,1]"
    assert Decimal(result["overall_uncertainty"]) == max(
        Decimal(result["u_p"]), Decimal(result["u_m"]), Decimal(result["u_e"])
    )
    assert 1 <= result["professional_level"] <= 5


# ------------------------------------------------- permutation replay (frozen)


@given(items=valid_items(min_size=2, max_size=8))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_permutation_replay_is_byte_identical(items: list[dict[str, Any]]) -> None:
    """Contract (Replay guarantee): permuting input evidence changes nothing."""
    baseline = run(items)
    rng = random.Random(20261024)
    for _ in range(3):
        permuted = list(items)
        rng.shuffle(permuted)
        assert run(permuted) == baseline


# ------------------------------------------------------------- recency decay


def test_recency_decay_shifts_weight_toward_the_newer_item() -> None:
    """h_P=90 days: fresher evidence weighs more, so P moves toward it."""
    good = make_item("e-a", correctness="1", outcome="1")
    bad = make_item("e-b", correctness="0", outcome="-1", reproducibility="0", scope_fit="0", contradiction="1")
    old = (AT - timedelta(days=180)).isoformat()
    fresh_good = run([dict(good, observed_at=AT.isoformat()), dict(bad, observed_at=old)])
    stale_good = run([dict(good, observed_at=old), dict(bad, observed_at=AT.isoformat())])
    assert Decimal(fresh_good["p"]) > Decimal(stale_good["p"])


def test_staleness_raises_uncertainty() -> None:
    """Older evidence lowers total weight, raising U_d = 1/sqrt(1+sum(q_d))."""
    fresh = run([make_item("e-a")])
    stale = run([make_item("e-a", observed_at=(AT - timedelta(days=365)).isoformat())])
    assert Decimal(stale["u_p"]) > Decimal(fresh["u_p"])


# ------------------------------------------- reliability / context weighting


def test_higher_reliability_weighs_more() -> None:
    good = make_item("e-a", correctness="1", outcome="1")
    bad = make_item("e-b", correctness="0", outcome="-1", reproducibility="0", scope_fit="0", contradiction="1")
    high_rel = run([dict(good, reliability="1"), dict(bad, reliability="1")])
    low_rel_good = run([dict(good, reliability="0.1"), dict(bad, reliability="1")])
    assert Decimal(low_rel_good["p"]) < Decimal(high_rel["p"])


def test_zero_reliability_evidence_is_excluded() -> None:
    result = run([make_item("e-a", reliability="0"), make_item("e-b")])
    assert result["exclusion_counts"]["zero_reliability"] == 1
    only_active = run([make_item("e-b")])
    assert result["p"] == only_active["p"]


def test_context_relevance_scales_weight() -> None:
    good = make_item("e-a", correctness="1", outcome="1", context_tags=["python", "testing"])
    bad = make_item(
        "e-b", correctness="0", outcome="-1", reproducibility="0", scope_fit="0", contradiction="1",
        context_tags=["cooking"],
    )
    query = {"query_context_tags": ["python", "testing"]}
    result = run([good, bad], **query)
    # The disjoint bad item is excluded; the relevant good item keeps full weight.
    assert result["exclusion_counts"]["disjoint_context"] == 1
    assert Decimal(result["p"]) == 1  # p_j of a perfect item is 1


# ------------------------------ diminishing returns / correlated groups


def _correlated_copies(base_id: str, count: int, *, independent: bool) -> list[dict[str, Any]]:
    """Same signal copied k times; identical content would dedup, so vary time."""
    items = []
    for index in range(count):
        items.append(
            make_item(
                f"{base_id}-{index}",
                correctness="0",
                outcome="-1",
                reproducibility="0",
                scope_fit="0",
                contradiction="1",
                observed_at=(AT - timedelta(seconds=index + 1)).isoformat(),
                independence_key=f"ik-{base_id}-{index}" if independent else f"ik-{base_id}",
            )
        )
    return items


def test_correlated_group_has_diminishing_returns() -> None:
    """independence_j = 1/sqrt(group_size): 4 correlated copies weigh less
    than 4 independent copies of the same signal."""
    anchor = make_item("e-anchor", correctness="1", outcome="1")
    correlated = run([anchor, *_correlated_copies("e-b", 4, independent=False)])
    independent = run([anchor, *_correlated_copies("e-b", 4, independent=True)])
    # Bad signal drags P down; correlated copies drag less (diminishing returns).
    assert Decimal(correlated["p"]) > Decimal(independent["p"])


def test_exact_duplicates_are_represented_once() -> None:
    """Duplicate resistance: an exact copy changes nothing but the retained id."""
    single = run([make_item("e-a")])
    copy = {k: v for k, v in make_item("e-a").items() if k != "evidence_id"}
    duplicated = run([make_item("e-a"), make_item("e-b", **copy)])
    assert duplicated["p"] == single["p"]
    assert duplicated["e"] == single["e"]
    assert duplicated["u_p"] == single["u_p"]


# ------------------------------------------------------- contradiction


def test_contradiction_lowers_p() -> None:
    """p_j carries +0.10*(1-contradiction_j): contradiction directly lowers P."""
    clean = run([make_item("e-a", contradiction="0")])
    contradicted = run([make_item("e-a", contradiction="1")])
    assert Decimal(contradicted["p"]) == Decimal(clean["p"]) - Decimal("0.1")


# ------------------------------------------------------- zero total weight


def test_disjoint_context_yields_zero_total_weight() -> None:
    result = run(
        [make_item("e-a", context_tags=["cooking"])],
        query_context_tags=["python"],
    )
    for key in ("p", "m", "e"):
        assert result[key] == "0.000000000000"
        assert result["zero_total_weight"][key] == {"zero": True, "reason": "ZERO_TOTAL_WEIGHT"}
    assert result["overall_uncertainty"] == "1.000000000000"
    assert result["professional_level"] == 1
    assert result["selected_m_bin"] is None


def test_all_zero_reliability_yields_zero_total_weight() -> None:
    result = run([make_item("e-a", reliability="0"), make_item("e-b", reliability="0")])
    assert result["exclusion_counts"]["zero_reliability"] == 2
    assert result["overall_uncertainty"] == "1.000000000000"
    assert result["professional_level"] == 1


# ----------------------------------------------------------------- null M bin


def test_m_is_null_with_fewer_than_three_independence_groups() -> None:
    """A bin needs >= 3 distinct independence groups; raw count is irrelevant."""
    items = _correlated_copies("e-a", 6, independent=False)  # one group, six uses
    result = run(items)
    assert result["m"] == "0.000000000000"
    assert result["selected_m_bin"] is None
    assert result["m_bin_evidence_count"] is None


def test_m_is_null_below_the_quality_gate() -> None:
    items = [make_item(f"e-{i}", quality_at_use="0.5") for i in range(4)]
    result = run(items)
    assert result["m"] == "0.000000000000"
    assert result["selected_m_bin"] is None


def test_m_selects_an_eligible_bin() -> None:
    items = [make_item(f"e-{i}") for i in range(3)]
    result = run(items)
    assert result["selected_m_bin"] == "0.5"
    assert result["m_bin_evidence_count"] == 3
    assert Decimal(result["m"]) == Decimal("0.5")  # u * S(u) = 0.5 * 1


# ------------------------------------------------------------- depth discount


def test_depth_discount_reduces_weight() -> None:
    """q_j,d is divided by (1 + depth_j): deeper evidence weighs less."""
    good = make_item("e-a", correctness="1", outcome="1", node_id="n-root")
    bad = make_item(
        "e-b", correctness="0", outcome="-1", reproducibility="0", scope_fit="0", contradiction="1",
        node_id="n-child",
    )
    shallow_bad = run([good, bad], node_depths={"n-root": 0, "n-child": 0})
    deep_bad = run([good, bad], node_depths={"n-root": 0, "n-child": 3})
    assert Decimal(deep_bad["p"]) > Decimal(shallow_bad["p"])
    # A single item's normalized score is depth-invariant; its uncertainty is not.
    single_shallow = run([good], node_depths={"n-root": 0})
    single_deep = run([good], node_depths={"n-root": 3})
    assert single_shallow["p"] == single_deep["p"]
    assert Decimal(single_deep["u_p"]) > Decimal(single_shallow["u_p"])


# -------------------------------------------------------------------- P gate


def _m_eligible_items(count: int = 3, **overrides: Any) -> list[dict[str, Any]]:
    return [make_item(f"e-{i}", **overrides) for i in range(count)]


def test_p_gate_blocks_g_and_promotion_below_0_70() -> None:
    """G=0 when P<0.70 regardless of M and E; promotion is impossible."""
    # p_j = 0 + 0.125 + 0.20 + 0.15 + 0.10 = 0.575 < 0.70, with perfect E and M.
    items = _m_eligible_items(3, correctness="0", outcome="0", reproducibility="1", scope_fit="1")
    result = run(items)
    assert Decimal(result["p"]) < Decimal("0.70")
    assert Decimal(result["m"]) > 0
    assert Decimal(result["e"]) > 0
    assert result["g"] == "0.000000000000"
    assert result["promotable"] is False


def test_p_above_gate_allows_g_when_m_and_e_are_nonzero() -> None:
    # p_j = 0.03 + 0.25 + 0.20 + 0.15 + 0.10 = 0.73 > 0.70 at full precision.
    # (Three equal weights divide inexactly, so an exact p_j of 0.70 would
    # render as 0.700000000000 yet sit a hair below the gate at full
    # precision — the frozen contract compares at full precision.)
    items = _m_eligible_items(3, correctness="0.1", outcome="1")
    result = run(items)
    assert Decimal(result["p"]) > Decimal("0.70")
    assert Decimal(result["g"]) > 0


# ----------------------------------------------------------- M != frequency


def test_m_is_not_raw_usage_frequency() -> None:
    """More uses do not raise M: bin eligibility and S(u) decide, not counts."""
    three_uses = run(_m_eligible_items(3))
    six_uses_two_groups = run(
        _correlated_copies("e-a", 6, independent=False)
    )
    assert six_uses_two_groups["m"] == "0.000000000000"  # 6 uses, 1 group: M=0
    assert Decimal(three_uses["m"]) > 0  # 3 uses, 3 groups: M>0
    # Doubling items within the same three groups leaves M unchanged.
    doubled = run(
        [make_item(f"e-{i}") for i in range(3)]
        + [
            make_item(
                f"e-x{i}",
                independence_key=f"ik-e-{i}",
                observed_at=(AT - timedelta(seconds=1)).isoformat(),
            )
            for i in range(3)
        ]
    )
    assert doubled["m"] == three_uses["m"]


# ------------------------------------------------- E never rescues a low P


def test_e_never_rescues_low_p() -> None:
    """Perfect E with P below the gate still yields G=0 and a P-driven level."""
    items = _m_eligible_items(
        3,
        correctness="0",
        outcome="0",
        latency_ms=1,
        compute_ms=1,
        output_chars=0,  # near-zero cost -> E close to 1
    )
    result = run(items)
    assert Decimal(result["e"]) > Decimal("0.99")
    assert Decimal(result["p"]) < Decimal("0.70")
    assert result["g"] == "0.000000000000"
    assert result["promotable"] is False
    assert result["professional_level"] <= 2  # 3 groups cap at 2


# ------------------------------------------------------- uncertainty semantics


def test_uncertainty_falls_as_independent_weight_grows() -> None:
    one = run([make_item("e-0")])
    three = run(_m_eligible_items(3))
    assert Decimal(three["u_p"]) < Decimal(one["u_p"])


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
