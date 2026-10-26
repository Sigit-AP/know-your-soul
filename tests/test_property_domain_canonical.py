"""Property tests for the domain schema: canonical encoding and fail-closed validation.

Pins the domain rules (spec sections 6 and 9) that every other surface relies on:

* ``canonical_json`` is order-independent, deterministic, unicode-preserving and
  round-trips through ``json.loads``;
* ``storage_payload`` owns the serialization boundary: every Decimal becomes a
  12-decimal string, every datetime an ISO string, and the transform is
  idempotent;
* ``StrictDecimal`` refuses binary floats and booleans at validation time, so
  no IEEE-754 rounding can enter a knowledge record;
* the frozen models reject out-of-range scores, unknown utilization bins, naive
  timestamps, unknown relation kinds and smuggled extra fields.

All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from soul_knowledge.domain import (
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
    ScoreVector,
    canonical_json,
    quantize_score,
    storage_payload,
)
from soul_knowledge.scoring import serialize_score

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _jsonable(depth: int = 0) -> st.SearchStrategy[Any]:
    scalars: st.SearchStrategy[Any] = st.one_of(
        st.none(), st.booleans(), st.integers(-10_000, 10_000), st.text(max_size=20)
    )
    if depth >= 3:
        return scalars
    return st.one_of(
        scalars,
        st.lists(_jsonable(depth + 1), max_size=4),
        st.dictionaries(st.text(max_size=8), _jsonable(depth + 1), max_size=4),
    )


# ------------------------------------------------------------- canonical_json


@given(payload=_jsonable())
@settings(derandomize=True, max_examples=50, deadline=None)
def test_canonical_json_round_trips_through_json_loads(payload: Any) -> None:
    assert json.loads(canonical_json(payload)) == payload


@given(payload=_jsonable())
@settings(derandomize=True, max_examples=40, deadline=None)
def test_canonical_json_is_deterministic(payload: Any) -> None:
    assert canonical_json(payload) == canonical_json(payload)


@given(
    pairs=st.integers(min_value=1, max_value=6).flatmap(
        lambda n: st.tuples(
            st.lists(st.text(alphabet="abcdef", min_size=1, max_size=5), min_size=n, max_size=n, unique=True),
            st.lists(st.integers(0, 100), min_size=n, max_size=n),
        )
    )
)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_canonical_json_is_invariant_under_key_order(pairs: tuple[list[str], list[int]]) -> None:
    keys, values = pairs
    forward = dict(zip(keys, values, strict=True))
    backward = dict(reversed(list(forward.items())))
    assert canonical_json(forward) == canonical_json(backward)


@given(text=st.text(alphabet=st.characters(whitelist_categories=("Lo", "Ll", "Lu")), min_size=1, max_size=20))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_canonical_json_preserves_unicode_verbatim(text: str) -> None:
    encoded = canonical_json({"k": text})
    assert text in encoded
    assert "\\u" not in encoded


# ------------------------------------------------------------- storage_payload


@given(value=st.decimals(min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_storage_payload_quantizes_every_decimal_and_parses_back(value: Decimal) -> None:
    stored = storage_payload({"score": value})
    rendered = stored["score"]
    assert isinstance(rendered, str)
    assert Decimal(rendered) == quantize_score(value)


@given(value=st.decimals(min_value=0, max_value=1000, places=13, allow_nan=False))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_storage_payload_renders_nonzero_quantized_scores_in_fixed_point(value: Decimal) -> None:
    # F-SUB40-1: str() of a Decimal switches to scientific notation for zero
    # ('0E-12') and below an adjusted exponent of -6; the storage boundary pins
    # the same fixed-point 12dp shape as scoring.serialize_score for every
    # quantized value, tiny magnitudes included.
    rendered = storage_payload(value)
    assert "." in rendered
    assert len(rendered.split(".")[1]) == 12
    assert rendered == serialize_score(value)


def test_storage_payload_renders_zero_and_sub_micro_in_fixed_point() -> None:
    # F-SUB40-1 explicit pins (the shapes str() used to get wrong).
    assert storage_payload(Decimal("0")) == "0.000000000000" == serialize_score(Decimal("0"))
    assert storage_payload(Decimal("1E-12")) == "0.000000000001" == serialize_score(Decimal("1E-12"))
    assert storage_payload(Decimal("1E-20")) == "0.000000000000"  # quantized away, still fixed-point
    assert storage_payload(Decimal("-1E-12")) == "-0.000000000001" == serialize_score(Decimal("-1E-12"))


@given(moment=st.datetimes(timezones=st.just(UTC)))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_storage_payload_renders_datetimes_as_iso_strings(moment: datetime) -> None:
    stored = storage_payload({"at": moment})
    assert stored["at"] == moment.isoformat()
    assert datetime.fromisoformat(stored["at"]) == moment


@given(values=st.lists(st.decimals(min_value=0, max_value=1, allow_nan=False, allow_infinity=False), max_size=5))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_storage_payload_is_idempotent_on_nested_structures(values: list[Decimal]) -> None:
    payload = {"scores": values, "meta": {"at": NOW, "tags": ("a", "b")}}
    once = storage_payload(payload)
    assert storage_payload(once) == once


@given(value=st.decimals(min_value=0, max_value=1, places=13, allow_nan=False, allow_infinity=False))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_storage_payload_never_rejects_overprecise_decimals(value: Decimal) -> None:
    # Contract: values with more than 12 decimal places are quantized, never
    # rejected (domain.storage_payload docstring).
    stored = storage_payload(value)
    assert Decimal(stored) == quantize_score(value)


# ------------------------------------------------------------- StrictDecimal


@given(value=st.booleans())
@settings(derandomize=True, max_examples=5, deadline=None)
def test_score_vector_refuses_booleans_as_decimals(value: bool) -> None:
    with pytest.raises(ValidationError):
        ScoreVector.model_validate({"p": value})


@given(value=st.floats(allow_nan=False, allow_infinity=False, min_value=-1, max_value=1))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_score_vector_refuses_binary_floats(value: float) -> None:
    with pytest.raises(ValidationError):
        ScoreVector.model_validate({"p": value})


@given(value=st.decimals(min_value=0, max_value=1, places=6))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_score_vector_accepts_decimal_strings(value: Decimal) -> None:
    vector = ScoreVector.model_validate({"p": str(value)})
    assert vector.p == value


@given(value=st.decimals(min_value=Decimal("1.000001"), max_value=10, places=6))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_score_vector_refuses_scores_above_one(value: Decimal) -> None:
    with pytest.raises(ValidationError):
        ScoreVector.model_validate({"p": str(value)})


@given(value=st.decimals(min_value=-10, max_value=Decimal("-0.000001"), places=6))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_score_vector_refuses_scores_below_zero(value: Decimal) -> None:
    with pytest.raises(ValidationError):
        ScoreVector.model_validate({"p": str(value)})


# ------------------------------------------------------------- evidence bounds


def _evidence_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "evidence_id": "ev-1",
        "node_id": "n-1",
        "source_version_id": "sv-1",
        "source_uri": "soul://x.md",
        "observed_at": NOW,
        "independence_key": "ik-1",
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
        "latency_ms": 10,
        "compute_ms": 10,
        "output_chars": 10,
    }
    payload.update(overrides)
    return payload


@given(outcome=st.decimals(min_value=Decimal("1.000001"), max_value=100, places=6))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_evidence_refuses_outcome_above_one(outcome: Decimal) -> None:
    with pytest.raises(ValidationError):
        Evidence.model_validate(_evidence_payload(outcome=str(outcome)))


@given(outcome=st.decimals(min_value=-100, max_value=Decimal("-1.000001"), places=6))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_evidence_refuses_outcome_below_minus_one(outcome: Decimal) -> None:
    with pytest.raises(ValidationError):
        Evidence.model_validate(_evidence_payload(outcome=str(outcome)))


@given(
    scaled=st.decimals(min_value=Decimal("0.001"), max_value=2, places=3).filter(
        lambda value: not (value * 10 == (value * 10).to_integral_value() and 1 <= value * 10 <= 10)
    )
)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_evidence_refuses_utilization_outside_the_closed_bins(scaled: Decimal) -> None:
    with pytest.raises(ValidationError):
        Evidence.model_validate(_evidence_payload(utilization_u=str(scaled)))


@given(bin_number=st.integers(min_value=1, max_value=10))
@settings(derandomize=True, max_examples=12, deadline=None)
def test_evidence_accepts_every_declared_utilization_bin(bin_number: int) -> None:
    value = Decimal(bin_number) / Decimal(10)
    evidence = Evidence.model_validate(_evidence_payload(utilization_u=str(value)))
    assert evidence.utilization_u == value


def test_evidence_refuses_naive_timestamps() -> None:
    with pytest.raises(ValidationError):
        Evidence.model_validate(_evidence_payload(observed_at=datetime(2026, 10, 25)))


@given(latency=st.integers(min_value=-100, max_value=0))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_evidence_refuses_nonpositive_latency(latency: int) -> None:
    with pytest.raises(ValidationError):
        Evidence.model_validate(_evidence_payload(latency_ms=latency))


# ------------------------------------------------------------- frozen records


@given(kind=st.sampled_from(["friend_of", "related_to", "likes", "parent-of", ""]))
@settings(derandomize=True, max_examples=8, deadline=None)
def test_relation_refuses_kinds_outside_the_closed_allowlist(kind: str) -> None:
    with pytest.raises(ValidationError):
        Relation.model_validate({"kind": kind, "source_node_id": "a", "target_node_id": "b"})


@given(kind=st.sampled_from(sorted(member.value for member in RelationKind)))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_relation_accepts_every_allowlisted_kind(kind: str) -> None:
    relation = Relation(kind=RelationKind(kind), source_node_id="a", target_node_id="b")
    assert relation.kind.value == kind


@given(extra=st.text(alphabet="abc", min_size=1, max_size=8))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_node_forbids_smuggled_extra_fields(extra: str) -> None:
    base: dict[str, Any] = {
        "node_id": "n-1",
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": "sv-1",
    }
    base[extra] = "smuggled"
    with pytest.raises(ValidationError):
        Node.model_validate(base)


@given(summary=st.text(max_size=30))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_node_records_are_immutable(summary: str) -> None:
    node = Node(node_id="n-1", domain=PrimaryDomain.P1, source_version_id="sv-1", summary=summary)
    with pytest.raises(ValidationError):
        node.summary = "changed"  # type: ignore[misc]
