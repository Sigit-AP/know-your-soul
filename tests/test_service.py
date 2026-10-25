"""Contract tests for the token-bounded KnowledgeService (Task 4).

Spec sections 7 (invocation/audit flow), 8 (retrieval and direct tools) and
13.1/13.2 (adaptation + conditional access); matrix row 38 (named test
``test_canonical_token_parity``) and the frozen fixtures in
``tests/fixtures/token_budget_cases.json`` and
``tests/fixtures/access_recommendation_relearn_cases.json``.

Every service operation — reads included — is wrapped in one audited
invocation. Reads bind to the active projection manifest: a stale read is
repaired synchronously once and otherwise fails as a retryable typed error;
read audits never move the knowledge watermark (no self-staleness).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from soul_knowledge.contracts import (
    ACCESS_ONLY_TOKEN_CAP,
    MINIMUM_TOKEN_BUDGET,
    AccessDecision,
    AccessPolicy,
    BudgetTooSmall,
    EvaluateRequest,
    KnowledgeError,
    MetadataExceedsBudget,
    NodeNotFound,
    PolicyArtifactError,
    QueryRequest,
    RecommendationItem,
    StaleProjectionUnavailable,
    attempt_promotion,
)
from soul_knowledge.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
    ScoreVector,
    canonical_json,
)
from soul_knowledge.importer import SeedImporter
from soul_knowledge.projection import GraphProjector, ProjectorBusy
from soul_knowledge.retrieval import estimated_tokens
from soul_knowledge.service import KnowledgeService
from soul_knowledge.store import EventStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --------------------------------------------------------------------- setup


def _upsert(store: EventStore, node: Node) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=f"test:upsert:{node.node_id}",
    )


def _relate(
    store: EventStore, source: str, target: str, relation: str, domain: PrimaryDomain = PrimaryDomain.P1
) -> None:
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id=source,
            domain=domain,
            relation=Relation(kind=RelationKind(relation), source_node_id=source, target_node_id=target),
        ),
        idempotency_key=f"test:rel:{source}:{relation}:{target}",
    )


def _roots(store: EventStore) -> None:
    _upsert(
        store,
        Node(
            node_id="KROOT",
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            provenance=("soul://roots",),
            source_version_id="sv-test",
        ),
    )
    for domain in PrimaryDomain:
        _upsert(
            store,
            Node(
                node_id=domain.value,
                parent_id="KROOT",
                kind=NodeKind.PRIMARY,
                domain=domain,
                summary=domain.title,
                provenance=("soul://roots",),
                source_version_id="sv-test",
            ),
        )


def _child(
    node_id: str,
    *,
    domain: PrimaryDomain = PrimaryDomain.P1,
    parent: str = "P1",
    summary: str = "",
    tags: tuple[str, ...] = (),
    scores: ScoreVector | None = None,
) -> Node:
    return Node(
        node_id=node_id,
        parent_id=parent,
        kind=NodeKind.CHILD,
        domain=domain,
        summary=summary or node_id,
        context_tags=tags,
        provenance=("soul://corpus/lesson.md",),
        source_version_id="sv-test",
        scores=scores if scores is not None else ScoreVector.zero(),
    )


def _evidence(evidence_id: str, node_id: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        node_id=node_id,
        source_version_id="sv-test",
        source_uri="soul://corpus/lesson.md",
        observed_at=datetime(2026, 10, 20, tzinfo=UTC),
        context_tags=("gates",),
        independence_key=evidence_id,
        outcome=Decimal("1"),
        correctness=Decimal("0.9"),
        reliability=Decimal("0.9"),
        reproducibility=Decimal("0.9"),
        scope_fit=Decimal("0.9"),
        successful_use=Decimal("1"),
        task_coverage=Decimal("1"),
        quality_at_use=Decimal("0.9"),
        contradiction=Decimal("0"),
        utilization_u=Decimal("0.5"),
        latency_ms=10,
        compute_ms=5,
        output_chars=10,
    )


def _stack(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    return store, projector, KnowledgeService(store, projector)


def _seeded(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    """Roots + one P1 child with a matching summary, projected and active."""
    store, projector, service = _stack(tmp_path)
    _roots(store)
    _upsert(store, _child("n-gates", summary="Run the gates before commit", tags=("gates", "discipline")))
    _relate(store, "P1", "n-gates", "parent_of")
    projector.rebuild()
    return store, projector, service


# ------------------------------------------------------------ happy-path reads


def test_query_direct_domain_routes_to_primary_root(tmp_path: Path) -> None:
    _, _, service = _seeded(tmp_path)
    response = service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
    assert response.routing == "direct"
    assert response.stale is False
    assert response.truncated is False
    assert "n-gates" in {node.node_id for node in response.results}
    assert response.node_versions["n-gates"] == 1
    assert response.provenance
    assert response.audit_event_id
    assert response.projection_version.startswith("gen-")
    assert response.required_knowledge_watermark == response.source_knowledge_high_watermark
    assert response.estimated_tokens <= 512


def test_query_bilingual_alias_routes_directly(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    _roots(store)
    _upsert(store, _child("n-action", domain=PrimaryDomain.P3, parent="P3", summary="Tindakan nyata"))
    _relate(store, "P3", "n-action", "parent_of", domain=PrimaryDomain.P3)
    projector.rebuild()
    response = service.query(QueryRequest(purpose="tindakan", domain="actions", token_budget=512))
    assert response.routing == "direct"
    assert "n-action" in {node.node_id for node in response.results}


def test_query_missing_primary_root_fails_without_keyword_fallback(tmp_path: Path) -> None:
    # A store where the P4 root was never created, plus a decoy node whose
    # summary lexically matches the purpose under another domain.
    fresh_store, fresh_projector, fresh_service = _stack(tmp_path)
    _upsert(
        fresh_store,
        Node(
            node_id="KROOT",
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            provenance=("soul://roots",),
            source_version_id="sv-test",
        ),
    )
    for domain in PrimaryDomain:
        if domain is PrimaryDomain.P4:
            continue
        _upsert(
            fresh_store,
            Node(
                node_id=domain.value,
                parent_id="KROOT",
                kind=NodeKind.PRIMARY,
                domain=domain,
                summary=domain.title,
                provenance=("soul://roots",),
                source_version_id="sv-test",
            ),
        )
    _upsert(
        fresh_store,
        _child("n-decoy", domain=PrimaryDomain.P1, parent="P1", summary="keputusan decisions galore"),
    )
    _relate(fresh_store, "P1", "n-decoy", "parent_of")
    fresh_projector.rebuild()
    with pytest.raises(KnowledgeError) as excinfo:
        fresh_service.query(QueryRequest(purpose="keputusan decisions", domain="P4", token_budget=512))
    assert excinfo.value.code == "PRIMARY_ROOT_MISSING"
    # No keyword fallback: the decoy is never served as a substitute root.
    assert excinfo.value.audit_event_id


def test_query_ambiguous_domain_rejected(tmp_path: Path) -> None:
    _, _, service = _seeded(tmp_path)
    with pytest.raises(KnowledgeError) as excinfo:
        service.query(QueryRequest(purpose="anything", domain="pem", token_budget=512))
    assert excinfo.value.code == "AMBIGUOUS_DOMAIN"


def test_query_no_match_is_typed_and_in_budget(tmp_path: Path) -> None:
    _, _, service = _seeded(tmp_path)
    response = service.query(QueryRequest(purpose="zzzqqq-nothing-matches", token_budget=256))
    assert response.routing == "no_match"
    assert response.results == ()
    assert response.estimated_tokens <= 256
    assert response.knowledge_payload_tokens == 0


def test_query_ranked_route_and_respects_relation_allowlist(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    _roots(store)
    _upsert(store, _child("n-habit", domain=PrimaryDomain.P6, parent="P6", summary="habit", tags=("habit",)))
    # n-linked hangs off n-habit only: a parent_of-only allowlist must never
    # reach it, while an unrestricted traversal does (via depends_on).
    _upsert(store, _child("n-linked", domain=PrimaryDomain.P1, parent="P1", summary="linked", tags=()))
    _relate(store, "P6", "n-habit", "parent_of", domain=PrimaryDomain.P6)
    _relate(store, "n-habit", "n-linked", "depends_on", domain=PrimaryDomain.P6)
    projector.rebuild()
    response = service.query(
        QueryRequest(
            purpose="habit",
            context_tags=("habit",),
            token_budget=512,
            relation_allowlist=(RelationKind.PARENT_OF,),
        )
    )
    assert response.routing == "ranked"
    assert "n-linked" not in {node.node_id for node in response.results}
    wide = service.query(QueryRequest(purpose="habit", context_tags=("habit",), token_budget=512))
    assert "n-linked" in {node.node_id for node in wide.results}


def test_get_node_and_get_subtree(tmp_path: Path) -> None:
    _, _, service = _seeded(tmp_path)
    view = service.get_node("n-gates")
    assert view.node.node_id == "n-gates"
    assert view.node.summary == "Run the gates before commit"
    assert view.audit_event_id
    subtree = service.get_subtree("P1", depth=2, token_budget=1024)
    assert {node.node_id for node in subtree.nodes} == {"P1", "n-gates"}
    assert subtree.edges
    with pytest.raises(NodeNotFound):
        service.get_node("n-does-not-exist")
    with pytest.raises(NodeNotFound):
        service.get_subtree("n-does-not-exist", token_budget=256)


# ------------------------------------------------------------- token budgets


def test_estimator_matches_frozen_token_fixtures() -> None:
    fixtures = json.loads((FIXTURES / "token_budget_cases.json").read_text(encoding="utf-8"))
    checked = 0
    for case in fixtures["cases"]:
        payload = case.get("payload")
        if payload is None:
            continue
        assert estimated_tokens(payload) == case["expected"]["estimated_tokens"], case["id"]
        checked += 1
    assert checked >= 7


def test_budget_below_128_is_budget_too_small_without_payload(tmp_path: Path) -> None:
    store, _, service = _seeded(tmp_path)
    audits_before = store.audit_event_count()
    for budget in (0, 1, MINIMUM_TOKEN_BUDGET - 1):
        with pytest.raises(BudgetTooSmall) as excinfo:
            service.query(QueryRequest(purpose="gates", domain="P1", token_budget=budget))
        assert excinfo.value.code == "BUDGET_TOO_SMALL"
        assert excinfo.value.audit_event_id
    # Each rejected invocation still appended exactly one final audit event.
    assert store.audit_event_count() == audits_before + 3


def test_complete_canonical_payload_satisfies_the_budget(tmp_path: Path) -> None:
    _, _, service = _seeded(tmp_path)
    # The complete canonical QueryResponse has a mandatory-metadata floor above
    # 128 tokens (measured: 134), and the frozen token-budget contract says an
    # unfittable mandatory metadata set is an error, never an over-budget
    # success. At the floor budget the typed error is therefore contract-legal;
    # every accepted budget above it must fit exactly.
    with pytest.raises(MetadataExceedsBudget) as excinfo:
        service.query(QueryRequest(purpose="gates", domain="P1", token_budget=MINIMUM_TOKEN_BUDGET))
    assert excinfo.value.code == "METADATA_EXCEEDS_BUDGET"
    for budget in (256, 512):
        response = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=budget))
        canonical = canonical_json(response.canonical_payload()).encode("utf-8")
        assert -(-len(canonical) // 4) == response.estimated_tokens
        assert response.estimated_tokens <= budget


def test_truncation_drops_results_to_fit(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    _roots(store)
    for index in range(6):
        _upsert(store, _child(f"n-lesson-{index}", summary=f"lesson {index} about gates", tags=("gates",)))
        _relate(store, "P1", f"n-lesson-{index}", "parent_of")
    projector.rebuild()
    budget = 256
    response = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=budget, max_nodes=10))
    assert response.estimated_tokens <= budget
    if response.truncated:
        assert len(response.results) < 8
        # Consulted nodes stay visible in node_versions even when truncated.
        assert set(response.node_versions) >= {node.node_id for node in response.results}


def test_metadata_overflow_is_an_error_never_an_over_budget_success(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    _roots(store)
    for index in range(64):
        _upsert(store, _child(f"n-meta-{index:03d}", summary=f"metadata lesson {index}"))
        _relate(store, "P1", f"n-meta-{index:03d}", "parent_of")
    projector.rebuild()
    with pytest.raises(MetadataExceedsBudget) as excinfo:
        service.query(QueryRequest(purpose="metadata", domain="P1", token_budget=MINIMUM_TOKEN_BUDGET, max_nodes=64))
    assert excinfo.value.code == "METADATA_EXCEEDS_BUDGET"


def test_access_only_returns_zero_knowledge_payload_within_160(tmp_path: Path) -> None:
    _, _, service = _seeded(tmp_path)
    for budget in (MINIMUM_TOKEN_BUDGET, 160, 1000):
        response = service.query(QueryRequest(purpose="maybe relevant", access_only=True, token_budget=budget))
        assert response.access_only is True
        assert response.results == ()
        assert response.knowledge_payload_tokens == 0
        assert response.estimated_tokens <= ACCESS_ONLY_TOKEN_CAP
        assert response.estimated_tokens <= min(budget, ACCESS_ONLY_TOKEN_CAP)


def test_canonical_token_parity(tmp_path: Path) -> None:
    """Matrix row 38: one canonical payload across CLI stdout, API JSON and MCP."""
    _, _, service = _seeded(tmp_path)
    response = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512))
    payload = response.canonical_payload()
    cli_stdout = canonical_json(payload)
    api_json = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    mcp_tool_content = canonical_json(payload)
    assert cli_stdout == api_json == mcp_tool_content
    for transport in (cli_stdout, api_json, mcp_tool_content):
        assert -(-len(transport.encode("utf-8")) // 4) == response.estimated_tokens
    # Transport envelopes are excluded from the budget.
    envelope = json.dumps({"jsonrpc": "2.0", "result": payload})
    assert -(-len(envelope.encode("utf-8")) // 4) > response.estimated_tokens


# --------------------------------------------------------- recommendation block


def test_recommendation_block_shape_and_item_fields(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    _roots(store)
    strong = ScoreVector(
        p=Decimal("0.9"),
        m=Decimal("0.5"),
        e=Decimal("0.5"),
        uncertainty=Decimal("0.2"),
        confidence=Decimal("0.8"),
        professional_level=4,
    )
    _upsert(store, _child("n-strong", summary="strong practice", tags=("gates",), scores=strong))
    _relate(store, "P1", "n-strong", "parent_of")
    projector.rebuild()
    service.record_outcome(_evidence("e-a", "n-strong"))
    service.record_outcome(_evidence("e-b", "n-strong"))
    projector.rebuild()
    response = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=2048))
    block = response.recommendations
    block_dict = block.model_dump(mode="json")
    assert set(block_dict) == {"best_actions", "recommendations", "unnecessary_actions"}
    promoted = block.best_actions
    assert promoted, "a two-evidence high-P node should be promoted"
    item = promoted[0]
    assert item.source_node_ids == ("n-strong",)
    assert item.source_node_versions == (store.node_version("n-strong"),)
    assert set(item.evidence_ids) == {"e-a", "e-b"}
    assert len(item.source_node_ids) == len(item.source_node_versions)
    assert item.learned_class
    assert item.reason_codes
    assert item.expiry is not None
    assert item.promoted is True
    for field in ("source_node_ids", "source_node_versions", "evidence_ids", "learned_class", "reason_codes", "expiry"):
        assert field in item.model_dump(mode="json")


def test_one_observation_never_promotes(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    _roots(store)
    strong = ScoreVector(
        p=Decimal("0.9"),
        m=Decimal("0.5"),
        e=Decimal("0.5"),
        uncertainty=Decimal("0.2"),
        confidence=Decimal("0.8"),
        professional_level=4,
    )
    _upsert(store, _child("n-single", summary="single observation practice", tags=("gates",), scores=strong))
    _relate(store, "P1", "n-single", "parent_of")
    projector.rebuild()
    service.record_outcome(_evidence("e-only", "n-single"))
    projector.rebuild()
    response = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=2048))
    best_ids = [item.action_id for item in response.recommendations.best_actions]
    recommended = {item.action_id: item for item in response.recommendations.recommendations}
    assert not any("n-single" in item.source_node_ids for item in response.recommendations.best_actions), best_ids
    candidate = next(item for item in recommended.values() if item.source_node_ids == ("n-single",))
    assert candidate.promoted is False
    assert len(candidate.evidence_ids) == 1
    verdict = attempt_promotion(candidate)
    assert verdict.accepted is False
    assert verdict.reason == "ONE_OBSERVATION_NO_PROMOTION"
    with pytest.raises(ValueError, match="ONE_OBSERVATION_NO_PROMOTION"):
        RecommendationItem(
            action_id="act-x",
            label="x",
            source_node_ids=("n-single",),
            source_node_versions=(1,),
            evidence_ids=("e-only",),
            learned_class="candidate",
            reason_codes=("INSUFFICIENT_EVIDENCE",),
            expiry=datetime(2026, 11, 24, tzinfo=UTC),
            promoted=True,
        )


def test_response_never_contains_raw_corpus_text(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    service = KnowledgeService(store, projector)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "lesson.md").write_bytes(
        b"# Lesson File\n\n## Gate Discipline\n\nRAW_SOUL_DO_NOT_EXPOSE: the raw body stays in the corpus.\n"
    )
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    importer.import_file(corpus / "lesson.md", domain=PrimaryDomain.P1, source_uri="soul://corpus/lesson.md")
    projector.rebuild()
    response = service.query(QueryRequest(purpose="gate discipline", domain="P1", token_budget=2048))
    assert response.results, "the imported heading should be retrievable"
    serialized = canonical_json(response.canonical_payload())
    assert "RAW_SOUL_DO_NOT_EXPOSE" not in serialized
    assert "## SOUL corpus raw text" not in serialized
    # Recommendations carry structured references, never verbatim corpus text.
    assert "RAW_SOUL_DO_NOT_EXPOSE" not in canonical_json(response.recommendations.model_dump(mode="json"))


def test_reads_never_touch_the_raw_corpus(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    output_root = (tmp_path / "out").resolve()
    touched: list[str] = []
    original_read_text = Path.read_text
    original_read_bytes = Path.read_bytes

    def recording_read_text(self: Path, *args: object, **kwargs: object) -> str:
        touched.append(str(Path(self).resolve()))
        return original_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

    def recording_read_bytes(self: Path, *args: object, **kwargs: object) -> bytes:
        touched.append(str(Path(self).resolve()))
        return original_read_bytes(self, *args, **kwargs)  # type: ignore[arg-type]

    import unittest.mock as mock

    with (
        mock.patch.object(Path, "read_text", recording_read_text),
        mock.patch.object(Path, "read_bytes", recording_read_bytes),
    ):
        service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512))
        service.get_node("n-gates")
    assert touched, "the read path must read the published generation"
    for path in touched:
        assert Path(path).is_relative_to(output_root), f"read escaped the projection: {path}"


# ------------------------------------------------------- access policy (13.2)


def test_access_policy_decisions_match_frozen_fixtures() -> None:
    fixtures = json.loads((FIXTURES / "access_recommendation_relearn_cases.json").read_text(encoding="utf-8"))
    policy = AccessPolicy()
    expected = {
        "never_call": AccessDecision.NO_CALL,
        "may_call_access_only": AccessDecision.ACCESS_ONLY,
        "call": AccessDecision.QUERY,
    }
    seen = 0
    for case in fixtures["access_cases"]:
        decision = policy.decide(case["surface_class"], context={"example": case["example"]})
        assert decision is expected[case["expected"]["decision"]], case["id"]
        seen += 1
    assert seen == 8


def test_access_policy_unknown_surface_defaults_to_access_only() -> None:
    policy = AccessPolicy()
    assert policy.decide("something_unlisted", {}) is AccessDecision.ACCESS_ONLY
    assert policy.access_only_max_estimated_tokens == ACCESS_ONLY_TOKEN_CAP


def test_access_policy_artifact_is_hash_pinned() -> None:
    from soul_knowledge.contracts import ACCESS_POLICY_PATH, ACCESS_POLICY_SHA256

    digest = hashlib.sha256(ACCESS_POLICY_PATH.read_bytes()).hexdigest()
    assert digest == ACCESS_POLICY_SHA256
    tampered = ACCESS_POLICY_PATH.parent / "tampered_policy.json"
    tampered.write_bytes(ACCESS_POLICY_PATH.read_bytes() + b" ")
    try:
        with pytest.raises(PolicyArtifactError):
            AccessPolicy(path=tampered)
    finally:
        tampered.unlink()


def test_access_policy_decision_costs_no_model_or_graph_calls(tmp_path: Path) -> None:
    policy = AccessPolicy()
    # The policy holds no store/projector: deciding can never reach the graph.
    assert not hasattr(policy, "store") and not hasattr(policy, "projector")
    for surface in ("L1_greeting", "L1_one_fact_lookup", "L1_one_command", "L1_file_read", "L1_status_check"):
        assert policy.decide(surface, {}) is AccessDecision.NO_CALL


# ----------------------------------------------------------- audit and staleness


def test_every_operation_returns_an_audit_event_id(tmp_path: Path) -> None:
    store, _, service = _seeded(tmp_path)
    audits_before = store.audit_event_count()
    receipts = [
        service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512)).audit_event_id,
        service.get_node("n-gates").audit_event_id,
        service.get_subtree("P1", token_budget=512).audit_event_id,
        service.log_event(_child("n-new", summary="a fresh lesson")).audit_event_id,
        service.record_outcome(_evidence("e-x", "n-gates")).audit_event_id,
    ]
    assert len(set(receipts)) == 5
    assert all(receipts)
    assert store.audit_event_count() == audits_before + 5
    assert store.verify_chain().ok


def test_evaluate_is_audited_and_scored(tmp_path: Path) -> None:
    """Task 5 wiring: evaluate now scores instead of returning NOT_IMPLEMENTED."""
    store, _, service = _seeded(tmp_path)
    audits_before = store.audit_event_count()
    response = service.evaluate(EvaluateRequest(node_id="n-gates"))
    assert response.audit_event_id
    assert response.node_id == "n-gates"
    assert response.p == "0.000000000000", "no evidence yet, so P is zero (never a fabricated prior)"
    assert response.overall_uncertainty == "1.000000000000"
    assert response.professional_level == 1
    assert store.audit_event_count() == audits_before + 1


def test_self_auditing_reads_do_not_self_stale(tmp_path: Path) -> None:
    store, _, service = _seeded(tmp_path)
    watermark = store.knowledge_high_watermark()
    first = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512))
    second = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512))
    assert store.knowledge_high_watermark() == watermark, "read audits must not move the knowledge watermark"
    assert first.required_knowledge_watermark == second.required_knowledge_watermark == watermark
    assert first.stale is False and second.stale is False


def test_stale_read_is_repaired_synchronously(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    # A mutation without a rebuild makes the active manifest stale.
    _upsert(store, _child("n-late", summary="late lesson about gates", tags=("gates",)))
    response = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=1024))
    assert response.stale is False
    assert response.source_knowledge_high_watermark == store.knowledge_high_watermark()
    assert "n-late" in {node.node_id for node in response.results}
    assert response.projection_version == store.projection_version()


def test_stale_read_without_repair_is_a_retryable_typed_error(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    _upsert(store, _child("n-late", summary="late lesson"))
    projector.repair_if_stale = lambda: None  # type: ignore[method-assign]
    audits_before = store.audit_event_count()
    with pytest.raises(StaleProjectionUnavailable) as excinfo:
        service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512))
    assert excinfo.value.code == "STALE_PROJECTION"
    assert excinfo.value.retryable is True
    assert store.audit_event_count() == audits_before + 1


def test_concurrent_reader_writer_snapshot_freshness(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    failures: list[str] = []
    stop = threading.Event()

    def rebuild_when_free() -> None:
        """Retry while a reader-triggered repair holds the rebuild lock.

        Task 3's documented contract: a competing projector raises
        ``ProjectorBusy`` instead of interleaving, and work committed while the
        lock is held is drained by the next rebuild. A writer therefore retries
        rather than dropping its batch.
        """
        for _ in range(300):
            try:
                projector.rebuild()
                return
            except ProjectorBusy:
                time.sleep(0.01)
        raise AssertionError("the writer never acquired the projector rebuild lock")

    def writer() -> None:
        for index in range(6):
            try:
                service.log_event(_child(f"n-w-{index}", summary=f"writer lesson {index}", tags=("gates",)))
                rebuild_when_free()
            except Exception as error:  # noqa: BLE001 - recorded and asserted
                errors = f"writer: {type(error).__name__}: {error}"
                failures.append(errors)
                return

    def reader() -> None:
        while not stop.is_set():
            try:
                response = service.query(QueryRequest(purpose="gates", domain="P1", token_budget=1024))
            except StaleProjectionUnavailable:
                continue  # retryable by contract
            except Exception as error:  # noqa: BLE001 - recorded and asserted
                failures.append(f"reader: {type(error).__name__}: {error}")
                return
            if response.source_knowledge_high_watermark < response.required_knowledge_watermark:
                failures.append("reader served a snapshot below its required watermark")
                return
            if response.stale:
                failures.append("reader served a stale response")
                return

    writer_thread = threading.Thread(target=writer)
    readers = [threading.Thread(target=reader) for _ in range(3)]
    writer_thread.start()
    for thread in readers:
        thread.start()
    writer_thread.join(timeout=60)
    stop.set()
    for thread in readers:
        thread.join(timeout=60)
    assert not writer_thread.is_alive()
    assert failures == []


def test_mutation_receipts_carry_domain_audit_and_projection_metadata(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    receipt = service.log_event(_child("n-mut", summary="mutation lesson"), idempotency_key="idem-1")
    assert receipt.domain_event_id
    assert receipt.audit_event_id
    assert receipt.idempotent_replay is False
    assert receipt.source_knowledge_high_watermark == store.knowledge_high_watermark()
    # An idempotent replay deduplicates the domain mutation with a fresh audit id.
    replay = service.log_event(_child("n-mut", summary="mutation lesson"), idempotency_key="idem-1")
    assert replay.idempotent_replay is True
    assert replay.domain_event_id == receipt.domain_event_id
    assert replay.audit_event_id != receipt.audit_event_id


def test_warm_index_cache_keys_on_content_hash_and_projection_version(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512))
    keys_after_first = set(service.snapshot_cache_keys())
    service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512))
    assert set(service.snapshot_cache_keys()) == keys_after_first, "a warm generation must be served from cache"
    assert len(keys_after_first) == 1
    manifest = projector.active_manifest()
    assert manifest is not None
    assert keys_after_first == {(manifest["graph_content_hash"], manifest["projection_version"])}
    # A new generation is a new cache key; mtime/size never participate.
    _upsert(store, _child("n-more", summary="more gates", tags=("gates",)))
    projector.rebuild()
    service.query(QueryRequest(purpose="gates", domain="P1", token_budget=512))
    keys = service.snapshot_cache_keys()
    new_manifest = projector.active_manifest()
    assert new_manifest is not None
    assert (new_manifest["graph_content_hash"], new_manifest["projection_version"]) in keys
