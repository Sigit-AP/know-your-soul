"""Feature-code tests for the calibrated P/M/E scoring engine (Task 5).

These tests exercise ``know_your_soul.scoring`` against the frozen fixtures in
``tests/fixtures/scoring_cases.json`` (read-only; owned and validated by the
Task 3A contract tests) and against an in-memory event store. The frozen
reference implementation in ``tests/test_measurement_contracts.py`` is never
imported or reused here: the expectations come from the fixture file itself.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from know_your_soul.domain import Event, EventKind, Evidence, Node, PrimaryDomain
from know_your_soul.scoring import (
    SCORING_CONFIG_PATH,
    ScoreAssessment,
    ScoreEngine,
    ScoringConfig,
    score_items,
)
from know_your_soul.store import EventStore

ROOT = Path(__file__).resolve().parent.parent
SCORING_FIXTURES = ROOT / "tests" / "fixtures" / "scoring_cases.json"

AT = datetime(2026, 10, 24, tzinfo=UTC)


@pytest.fixture(scope="module")
def scoring_cases() -> dict[str, Any]:
    return json.loads(SCORING_FIXTURES.read_text(encoding="utf-8"))


# ------------------------------------------------------------ config loading


def test_default_config_loads_from_bundled_toml() -> None:
    config = ScoringConfig.default()
    assert config.contract_version == "1.0.0"
    assert config.half_life_days == {"p": Decimal("90"), "m": Decimal("30"), "e": Decimal("7")}


def test_config_path_is_bundled_toml() -> None:
    assert SCORING_CONFIG_PATH.name == "scoring_config.toml"
    assert SCORING_CONFIG_PATH.is_file()


def test_config_rejects_unknown_contract_version(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(SCORING_CONFIG_PATH.read_text(encoding="utf-8").replace('"1.0.0"', '"9.9.9"'), encoding="utf-8")
    with pytest.raises(ValueError, match="contract_version"):
        ScoringConfig.load(bad)


def test_config_rejects_unknown_keys(tmp_path: Path) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(
        SCORING_CONFIG_PATH.read_text(encoding="utf-8") + '\n[half_life_days_extra]\nx = "1"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        ScoringConfig.load(bad)


# --------------------------------------------- frozen fixture reproduction


def test_all_frozen_scoring_cases_reproduce_exactly(scoring_cases: dict[str, Any]) -> None:
    """Every one of the 37 frozen fixtures must reproduce byte-identically."""
    assert len(scoring_cases["cases"]) == 37
    for case in scoring_cases["cases"]:
        assessment = score_items(
            case["evidence"],
            at=datetime.fromisoformat(case["at"]),
            query_context_tags=case.get("query_context_tags", []),
            node_depths=case.get("node_depths", {}),
        )
        assert assessment.to_contract_dict() == case["expected"], (
            f"{case['id']}: scoring engine output diverges from the frozen fixture"
        )


def test_zero_evidence_case_is_the_zero_assessment() -> None:
    assessment = score_items([], at=AT)
    contract = assessment.to_contract_dict()
    assert contract["p"] == "0.000000000000"
    assert contract["m"] == "0.000000000000"
    assert contract["e"] == "0.000000000000"
    assert contract["u_p"] == "1.000000000000"
    assert contract["overall_uncertainty"] == "1.000000000000"
    assert contract["professional_level"] == 1
    assert contract["promotable"] is False
    assert contract["selected_m_bin"] is None
    assert contract["m_bin_evidence_count"] is None
    assert contract["zero_total_weight"] == {
        dimension: {"zero": True, "reason": "ZERO_TOTAL_WEIGHT"} for dimension in ("p", "m", "e")
    }


# ---------------------------------------------------- engine against a store


def _fixture_case(scoring_cases: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in scoring_cases["cases"]:
        if case["id"] == case_id:
            return case
    raise AssertionError(f"fixture {case_id} missing")


def _item_to_domain_evidence(item: dict[str, Any]) -> Evidence:
    return Evidence(
        evidence_id=item["evidence_id"],
        node_id=item["node_id"],
        source_version_id="sv-test",
        source_uri=item["source_uri"] or "soul://corpus/placeholder",
        observed_at=datetime.fromisoformat(item["observed_at"]),
        context_tags=tuple(item["context_tags"]),
        independence_key=item["independence_key"],
        outcome=item["outcome"],
        correctness=item["correctness"],
        reliability=item["reliability"],
        reproducibility=item["reproducibility"],
        scope_fit=item["scope_fit"],
        successful_use=item["successful_use"],
        task_coverage=item["task_coverage"] if item["task_coverage"] is not None else Decimal("0"),
        quality_at_use=item["quality_at_use"],
        contradiction=item["contradiction"],
        utilization_u=item["utilization_u"],
        latency_ms=item["latency_ms"],
        compute_ms=item["compute_ms"],
        output_chars=item["output_chars"],
    )


def _build_store(case: dict[str, Any]) -> EventStore:
    store = EventStore()
    node_ids = sorted({item["node_id"] for item in case["evidence"]}) or ["n1"]
    seq = 0
    for node_id in node_ids:
        seq += 1
        node = Node(node_id=node_id, domain=PrimaryDomain.P1, source_version_id="sv-test")
        store.append_domain(
            Event(kind=EventKind.NODE_UPSERT, node_id=node_id, domain=PrimaryDomain.P1, node=node),
            idempotency_key=f"node-{node_id}-{seq}",
        )
    for item in case["evidence"]:
        seq += 1
        try:
            evidence = _item_to_domain_evidence(item)
        except Exception:
            continue  # fixture items invalid under the domain schema are excluded upstream
        store.append_domain(
            Event(
                kind=EventKind.EVIDENCE_APPEND,
                node_id=evidence.node_id,
                domain=PrimaryDomain.P1,
                evidence=evidence,
            ),
            idempotency_key=f"ev-{evidence.evidence_id}-{seq}",
        )
    return store


def test_score_node_matches_frozen_fixture(scoring_cases: dict[str, Any]) -> None:
    case = _fixture_case(scoring_cases, "score-001-single-item-hand")
    store = _build_store(case)
    try:
        engine = ScoreEngine(store)
        assessment = engine.score_node("n1", context=(), at=datetime.fromisoformat(case["at"]))
    finally:
        store.close()
    assert isinstance(assessment, ScoreAssessment)
    assert assessment.to_contract_dict() == case["expected"]


def test_score_node_unknown_node_raises() -> None:
    store = EventStore()
    try:
        engine = ScoreEngine(store)
        with pytest.raises(Exception, match="(?i)(unknown|not found|missing)"):
            engine.score_node("nope", context=(), at=AT)
    finally:
        store.close()


def test_score_node_is_deterministic_under_replay(scoring_cases: dict[str, Any]) -> None:
    case = _fixture_case(scoring_cases, "score-025-permutation-replay-canonical")
    store = _build_store(case)
    try:
        engine = ScoreEngine(store)
        node_ids = sorted({item["node_id"] for item in case["evidence"]})
        first = engine.score_node(node_ids[0], context=(), at=datetime.fromisoformat(case["at"]))
        second = engine.score_node(node_ids[0], context=(), at=datetime.fromisoformat(case["at"]))
    finally:
        store.close()
    assert first.to_contract_dict() == second.to_contract_dict()


def test_aggregate_primary_unions_descendant_evidence_with_depth(scoring_cases: dict[str, Any]) -> None:
    """aggregate_primary over a primary + one child must equal score_items over
    the union with depths 0/1 (contract: Aggregation)."""
    case = _fixture_case(scoring_cases, "score-001-single-item-hand")
    item = dict(case["evidence"][0])
    child_item = dict(item, evidence_id="e0002", node_id="n-child")
    store = EventStore()
    try:
        from know_your_soul.domain import Relation, RelationKind

        for node_id in ("n1", "n-child"):
            node = Node(node_id=node_id, domain=PrimaryDomain.P1, source_version_id="sv-test")
            store.append_domain(
                Event(kind=EventKind.NODE_UPSERT, node_id=node_id, domain=PrimaryDomain.P1, node=node),
                idempotency_key=f"node-{node_id}",
            )
        store.append_domain(
            Event(
                kind=EventKind.RELATION_ADD,
                node_id="n1",
                domain=PrimaryDomain.P1,
                relation=Relation(kind=RelationKind.PARENT_OF, source_node_id="n1", target_node_id="n-child"),
            ),
            idempotency_key="rel-1",
        )
        for evidence_item in (item, child_item):
            evidence = _item_to_domain_evidence(evidence_item)
            store.append_domain(
                Event(
                    kind=EventKind.EVIDENCE_APPEND,
                    node_id=evidence.node_id,
                    domain=PrimaryDomain.P1,
                    evidence=evidence,
                ),
                idempotency_key=f"ev-{evidence.evidence_id}",
            )
        engine = ScoreEngine(store)
        aggregated = engine.aggregate_primary("n1", context=(), at=datetime.fromisoformat(case["at"]))
    finally:
        store.close()
    expected = score_items(
        [item, child_item],
        at=datetime.fromisoformat(case["at"]),
        query_context_tags=[],
        node_depths={"n1": 0, "n-child": 1},
    )
    assert aggregated.to_contract_dict() == expected.to_contract_dict()


def test_aggregate_primary_unknown_node_raises() -> None:
    store = EventStore()
    try:
        engine = ScoreEngine(store)
        with pytest.raises(Exception, match="(?i)(unknown|not found|missing)"):
            engine.aggregate_primary("nope", context=(), at=AT)
    finally:
        store.close()


# ------------------------------------------------------- assessment contract


def test_assessment_exposes_explanations(scoring_cases: dict[str, Any]) -> None:
    case = _fixture_case(scoring_cases, "score-015-m-eligible-single-bin-hand")
    assessment = score_items(
        case["evidence"],
        at=datetime.fromisoformat(case["at"]),
        query_context_tags=case.get("query_context_tags", []),
        node_depths=case.get("node_depths", {}),
    )
    explanations = assessment.explanations
    assert "p" in explanations and "m" in explanations and "e" in explanations
    assert explanations["m"]["selected_m_bin"] == case["expected"]["selected_m_bin"]
    contributing_ids = [entry["evidence_id"] for entry in explanations["p"]["evidence"]]
    assert contributing_ids == sorted(contributing_ids)
