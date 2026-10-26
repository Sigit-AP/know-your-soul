"""Versioned domain schema tests.

Task 2 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 6 and 9.

These tests pin the *schema* contract of the immutable store: seven stable
primary domains, required ``source_version_id`` provenance on every node and
evidence item, decimal-only bounded scoring fields, the frozen relation
allowlist, and event payload integrity. They fail closed: a missing field, a
binary float, or an unknown relation is an error, never a silent default.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from know_your_soul.domain import (
    DECIMAL_PRECISION,
    PRIMARY_DOMAINS,
    SCORE_QUANTUM,
    Event,
    EventKind,
    Evidence,
    LifecycleStatus,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
    ScoreVector,
    quantize_score,
)

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def build_evidence(**overrides: object) -> Evidence:
    payload: dict[str, object] = {
        "evidence_id": "ev-1",
        "node_id": "n-1",
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
        "latency_ms": 1200,
        "compute_ms": 300,
        "output_chars": 4000,
    }
    payload.update(overrides)
    return Evidence.model_validate(payload)


def build_node(**overrides: object) -> Node:
    payload: dict[str, object] = {
        "node_id": "n-1",
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": "sv-1",
        "payload": {"fact": "deterministic"},
        "summary": "a deterministic fact",
        "provenance": ("soul/01-identity-compliance.md",),
        "context_tags": ("identity", "compliance"),
        "scope": "global",
        "created_at": NOW,
        "updated_at": NOW,
        "professional_level": 3,
        "lifecycle_status": LifecycleStatus.ACTIVE,
        "version": 1,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


# ----------------------------------------------------------- primary domains


def test_primary_domains_are_exactly_seven_stable_ids() -> None:
    assert len(PrimaryDomain) == 7
    assert [member.value for member in PrimaryDomain] == ["P1", "P2", "P3", "P4", "P5", "P6", "P7"]
    assert tuple(PRIMARY_DOMAINS) == tuple(PrimaryDomain)


def test_primary_domain_titles_are_the_spec_labels() -> None:
    titles = {member.value: member.title for member in PrimaryDomain}
    assert titles == {
        "P1": "Pembelajaran",
        "P2": "Gagasan",
        "P3": "Tindakan",
        "P4": "Keputusan",
        "P5": "Pemikiran/Penalaran",
        "P6": "Disiplin",
        "P7": "Level Profesional",
    }


def test_unknown_domain_is_rejected() -> None:
    with pytest.raises(ValidationError):
        build_node(domain="P8")
    with pytest.raises(ValidationError):
        build_node(domain="pembelajaran")


# --------------------------------------------------------------- provenance


def test_node_requires_source_version_id() -> None:
    payload = build_node().model_dump()
    payload.pop("source_version_id")
    with pytest.raises(ValidationError):
        Node.model_validate(payload)


def test_evidence_requires_source_version_id() -> None:
    payload = build_evidence().model_dump()
    payload.pop("source_version_id")
    with pytest.raises(ValidationError):
        Evidence.model_validate(payload)


def test_node_and_evidence_keep_their_source_version() -> None:
    assert build_node(source_version_id="sv-abc").source_version_id == "sv-abc"
    assert build_evidence(source_version_id="sv-xyz").source_version_id == "sv-xyz"


# -------------------------------------------------------------------- bounds


def test_node_professional_level_is_bounded_1_to_5() -> None:
    for level in (1, 2, 3, 4, 5):
        assert build_node(professional_level=level).professional_level == level
    for bad in (0, 6, -1):
        with pytest.raises(ValidationError):
            build_node(professional_level=bad)


def test_score_vector_bounds_are_enforced() -> None:
    for bad in ("1.5", "-0.1", "2"):
        with pytest.raises(ValidationError):
            ScoreVector.model_validate({"p": bad})
    assert ScoreVector().p == Decimal("0")


def test_score_vector_quantizes_to_twelve_decimals_half_even() -> None:
    assert SCORE_QUANTUM == Decimal("1E-12")
    assert DECIMAL_PRECISION == 34
    # half-even: the retained digit is even so no round up.
    assert quantize_score(Decimal("0.1234567890125")) == Decimal("0.123456789012")
    # half-even: the retained digit is odd so it rounds up.
    assert quantize_score(Decimal("0.1234567890135")) == Decimal("0.123456789014")


def test_evidence_scalar_bounds_are_enforced() -> None:
    for field in (
        "correctness",
        "reliability",
        "reproducibility",
        "scope_fit",
        "successful_use",
        "task_coverage",
        "quality_at_use",
        "contradiction",
    ):
        with pytest.raises(ValidationError):
            build_evidence(**{field: "1.1"})
        with pytest.raises(ValidationError):
            build_evidence(**{field: "-0.1"})
    # outcome is signed: [-1, 1]
    assert build_evidence(outcome="-1").outcome == Decimal("-1")
    assert build_evidence(outcome="1").outcome == Decimal("1")
    with pytest.raises(ValidationError):
        build_evidence(outcome="-1.1")
    with pytest.raises(ValidationError):
        build_evidence(outcome="1.1")


def test_evidence_utilization_bin_is_one_of_the_ten_steps() -> None:
    for step in range(1, 11):
        value = f"{step / 10:.1f}"
        assert build_evidence(utilization_u=value).utilization_u == Decimal(value)
    for bad in ("0", "0.0", "1.1", "0.15", "0.55"):
        with pytest.raises(ValidationError):
            build_evidence(utilization_u=bad)


def test_evidence_resource_measurements_are_strictly_positive_where_required() -> None:
    assert build_evidence(output_chars=0).output_chars == 0
    for bad in (0, -1):
        with pytest.raises(ValidationError):
            build_evidence(latency_ms=bad)
        with pytest.raises(ValidationError):
            build_evidence(compute_ms=bad)
    with pytest.raises(ValidationError):
        build_evidence(output_chars=-1)


def test_decimal_fields_reject_binary_floats() -> None:
    with pytest.raises(ValidationError):
        build_evidence(correctness=0.9)
    with pytest.raises(ValidationError):
        ScoreVector.model_validate({"p": 0.5})
    with pytest.raises(ValidationError):
        build_evidence(utilization_u=0.5)


# ----------------------------------------------------------------- relations


def test_relation_kind_allowlist_is_the_ten_spec_relations() -> None:
    assert {member.value for member in RelationKind} == {
        "parent_of",
        "derived_from",
        "supports",
        "contradicts",
        "causes",
        "learned_from",
        "applied_in",
        "supersedes",
        "duplicates",
        "depends_on",
    }


def test_invalid_relation_kind_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Relation.model_validate({"kind": "nonsense", "source_node_id": "a", "target_node_id": "b"})
    with pytest.raises(ValidationError):
        build_node(relations=({"kind": "nonsense", "source_node_id": "a", "target_node_id": "b"},))


def test_valid_relation_round_trips() -> None:
    relation = Relation.model_validate({"kind": "supports", "source_node_id": "a", "target_node_id": "b"})
    assert relation.kind is RelationKind.SUPPORTS
    node = build_node(relations=(relation,))
    assert node.relations[0].target_node_id == "b"


# -------------------------------------------------------------------- events


def test_node_upsert_event_requires_a_node() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate({"kind": "node.upsert", "node_id": "n-1", "domain": "P1", "occurred_at": NOW})


def test_evidence_event_requires_evidence() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate({"kind": "evidence.append", "node_id": "n-1", "domain": "P1", "occurred_at": NOW})


def test_relation_event_requires_a_relation() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate({"kind": "relation.add", "node_id": "n-1", "domain": "P1", "occurred_at": NOW})


def test_event_node_id_must_match_the_embedded_node() -> None:
    node = build_node(node_id="n-1")
    with pytest.raises(ValidationError):
        Event.model_validate(
            {"kind": "node.upsert", "node_id": "other", "domain": "P1", "node": node, "occurred_at": NOW}
        )


def test_valid_node_event_round_trips() -> None:
    node = build_node()
    event = Event.model_validate(
        {"kind": "node.upsert", "node_id": node.node_id, "domain": "P1", "node": node, "occurred_at": NOW}
    )
    assert event.kind is EventKind.NODE_UPSERT
    assert event.node is not None
    assert event.node.node_id == "n-1"
    assert event.knowledge_affecting is True


def test_event_rejects_unknown_fields() -> None:
    node = build_node()
    with pytest.raises(ValidationError):
        Event.model_validate(
            {
                "kind": "node.upsert",
                "node_id": node.node_id,
                "domain": "P1",
                "node": node,
                "occurred_at": NOW,
                "model_name": "nope",
            }
        )
