"""Residual wave-2 chain-audit probes (SUB-21): the service surface closes the chain.

Findings carried over from ``docs/recovery/wave2-L1-L3-chain-audit.md``
(the receipts gained the spec 7.9 fields in c5792f9; the service layer did
not forward them yet):

* §7.9 API surface — ``propose_improvement``/``apply_improvement``/``rollback``
  responses must carry ``domain_event_id``, ``audit_event_seq``,
  ``node_version`` and ``knowledge_seq`` from the receipt, not ``None``.
* §7.7 read path — ``KnowledgeService._finish_success``/``_finish_failure``
  final audits must carry a non-null ``projection_outcome`` like every
  mutation-path audit.
* Relearn production wiring — ``KnowledgeService`` constructs the production
  ``RelearnEngine``; it must pass the projector's generation root so the
  verified relearn backup covers the ``CURRENT`` pointer and
  ``generations/``, not just the SQLite file.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from soul_knowledge.contracts import NodeNotFound, QueryRequest
from soul_knowledge.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    ScoreVector,
)
from soul_knowledge.improvement import DiffOp, DiffOperation
from soul_knowledge.projection import GraphProjector
from soul_knowledge.relearn import RelearnApproval
from soul_knowledge.service import KnowledgeService
from soul_knowledge.source_registry import ParserVersions, SourceKind
from soul_knowledge.store import EventStore

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
PARSER_VERSIONS = ParserVersions(
    parser_version="1.0.0",
    normalization_version="1.0.0",
    engine_version="0.9.95",
    scoring_contract_version="1.0.0",
)


def _final_audits(db_path: Path) -> dict[str, list[dict[str, object]]]:
    """event_type -> the final audit payloads (kind='audit' rows), in order."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT event_seq, event_id, event_type, payload_json FROM event_log WHERE kind='audit' ORDER BY event_seq"
        ).fetchall()
        out: dict[str, list[dict[str, object]]] = {}
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            payload["__event_id__"] = str(row["event_id"])
            payload["__event_seq__"] = int(str(row["event_seq"]))
            out.setdefault(str(row["event_type"]), []).append(payload)
        return out
    finally:
        conn.close()


def _node(node_id: str, source_version_id: str, *, summary: str = "probe node") -> Node:
    return Node(
        node_id=node_id,
        parent_id="P1",
        kind=NodeKind.CHILD,
        domain=PrimaryDomain.P1,
        summary=summary,
        context_tags=("probe",),
        provenance=("soul://corpus/lesson.md",),
        source_version_id=source_version_id,
        scores=ScoreVector.zero(),
    )


def _evidence(evidence_id: str, node_id: str, source_version_id: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        node_id=node_id,
        source_version_id=source_version_id,
        source_uri="soul://corpus/lesson.md",
        observed_at=NOW,
        context_tags=("probe",),
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


def _seed_roots(store: EventStore, source_version_id: str = "sv-test") -> None:
    def up(node: Node, key: str) -> None:
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id=node.node_id,
                domain=node.domain,
                node=node,
                source_version_id=node.source_version_id,
            ),
            idempotency_key=key,
        )

    up(
        Node(
            node_id="KROOT",
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            provenance=("soul://roots",),
            source_version_id=source_version_id,
        ),
        "seed:kroot",
    )
    for domain in PrimaryDomain:
        up(
            Node(
                node_id=domain.value,
                parent_id="KROOT",
                kind=NodeKind.PRIMARY,
                domain=domain,
                summary=domain.title,
                provenance=("soul://roots",),
                source_version_id=source_version_id,
            ),
            f"seed:{domain.value}",
        )


def _service(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    return store, projector, KnowledgeService(store, projector)


def test_service_lifecycle_responses_carry_spec_7_9_fields(tmp_path: Path) -> None:
    """§7.9: propose/apply/rollback responses forward the receipt chain fields."""
    store, _projector, service = _service(tmp_path)
    _seed_roots(store)
    service.log_event(_node("n-79", "sv-test"), actor="probe", idempotency_key="probe:log:n-79")

    proposal = service.propose_improvement(
        node_id="n-79",
        diff=[DiffOperation(op=DiffOp.SET_SUMMARY, value="improved summary")],
        rationale="spec 7.9 probe",
        actor="probe",
        idempotency_key="probe:prop:79",
    )
    assert proposal.domain_event_id, "ProposalResponse.domain_event_id missing (spec 7.9)"
    assert proposal.audit_event_seq is not None, "ProposalResponse.audit_event_seq missing (spec 7.9)"
    assert proposal.node_version is not None, "ProposalResponse.node_version missing (spec 7.9)"
    # A proposal is a zero-knowledge-weight admin event: knowledge_seq stays None by design.
    assert proposal.knowledge_seq is None

    pre_apply_version = store.node_version("n-79")
    applied = service.apply_improvement(
        proposal_id=proposal.proposal.proposal_id, actor="probe", idempotency_key="probe:apply:79"
    )
    assert applied.domain_event_id, "ImprovementResponse.domain_event_id missing (spec 7.9)"
    assert applied.audit_event_seq is not None, "ImprovementResponse.audit_event_seq missing (spec 7.9)"
    assert applied.node_version == applied.to_version
    assert applied.knowledge_seq is not None, "ImprovementResponse.knowledge_seq missing (spec 7.9)"

    rolled_back = service.rollback(
        node_id="n-79", to_version=pre_apply_version, actor="probe", idempotency_key="probe:rb:79"
    )
    assert rolled_back.kind == "improvement"
    assert rolled_back.domain_event_id, "RollbackResponse.domain_event_id missing (spec 7.9)"
    assert rolled_back.audit_event_seq is not None, "RollbackResponse.audit_event_seq missing (spec 7.9)"
    assert rolled_back.node_version is not None, "RollbackResponse.node_version missing (spec 7.9)"
    assert rolled_back.knowledge_seq is not None, "RollbackResponse.knowledge_seq missing (spec 7.9)"
    store.close()

    # Chain consistency: every response's audit_event_seq resolves to the final
    # audit of the matching operation, and domain_event_id is a real domain event.
    db_path = tmp_path / "soul.sqlite3"
    audits = _final_audits(db_path)
    assert audits, "non-vacuous: no final audits were recorded"
    conn = sqlite3.connect(db_path)
    try:
        domain_ids = {str(r[0]) for r in conn.execute("SELECT event_id FROM event_log WHERE kind='domain'")}
        for response, operation in (
            (proposal, "knowledge.propose_improvement"),
            (applied, "knowledge.apply_improvement"),
            (rolled_back, "knowledge.rollback"),
        ):
            assert response.domain_event_id in domain_ids, f"{operation}: domain_event_id not a domain event"
            final = audits[operation][0]
            assert final["__event_seq__"] == response.audit_event_seq, (
                f"{operation}: audit_event_seq does not name the final audit"
            )
            assert final["__event_id__"] == response.audit_event_id
    finally:
        conn.close()


def test_read_path_final_audits_carry_projection_outcome(tmp_path: Path) -> None:
    """§7.7: read-path final audits (success and failure) carry projection_outcome."""
    store, projector, service = _service(tmp_path)
    _seed_roots(store)
    service.log_event(_node("n-rd", "sv-test"), actor="probe", idempotency_key="probe:log:n-rd")
    projector.rebuild()

    response = service.query(QueryRequest(purpose="probe node", domain="P1", token_budget=1024), actor="probe")
    assert response.audit_event_id
    view = service.get_node("n-rd", actor="probe")
    assert view.node.node_id == "n-rd"
    with pytest.raises(NodeNotFound):
        service.get_node("n-missing", actor="probe")
    store.close()

    audits = _final_audits(tmp_path / "soul.sqlite3")
    for operation in ("knowledge.query", "knowledge.get_node"):
        rows = audits.get(operation, [])
        assert rows, f"{operation}: no final audit recorded"
        for payload in rows:
            assert payload.get("projection_outcome"), (
                f"{operation} ({payload.get('status')}): projection_outcome missing (spec 7.7)"
            )
            assert payload.get("correlation_id"), f"{operation}: correlation_id missing (spec 7.7)"
            assert isinstance(payload.get("duration_ms"), int), f"{operation}: duration_ms missing (spec 7.7)"
    statuses = {str(p.get("status")) for p in audits["knowledge.get_node"]}
    assert statuses == {"success", "failure"}, f"expected one success and one failure read audit, got {statuses}"


def test_production_relearn_backup_covers_generation(tmp_path: Path) -> None:
    """The service-wired production RelearnEngine backs up CURRENT + generations/."""
    store, projector, service = _service(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
    engine = service._relearn_engine  # noqa: SLF001 - same-package wiring probe (mcp_server precedent)
    registry = engine.registry
    v1 = registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-rl",
            domain=PrimaryDomain.P1,
            node=_node("n-rl", v1.source_version_id),
            source_version_id=v1.source_version_id,
        ),
        idempotency_key="seed:n-rl",
    )
    # A real published generation: CURRENT + generations/<hash>/ with a manifest.
    projector.rebuild()
    assert (tmp_path / "out" / "CURRENT").is_file()
    live_pointer = (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8").strip()

    (corpus / "a.md").write_text("alpha v2 — changed\n", encoding="utf-8")
    offer = engine.offer(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    assert offer is not None
    receipt = engine.apply(
        "RESET",
        RelearnApproval(offer_id=offer.offer_id, mode="RESET", approved_by="operator", decided_at=NOW),
        at=NOW,
    )
    store.close()

    kinds = {artifact.kind for artifact in receipt.backup.artifacts}
    assert "sqlite" in kinds, "backup lost the store itself"
    assert "current_pointer" in kinds, (
        "production relearn backup misses the CURRENT pointer — generation_dir was not wired"
    )
    assert "generation_file" in kinds, (
        "production relearn backup misses the generation artifacts — generation_dir was not wired"
    )
    backup_dir = tmp_path / "relearn_backups" / receipt.relearn_id
    pointer_artifact = next(a for a in receipt.backup.artifacts if a.kind == "current_pointer")
    assert (backup_dir / pointer_artifact.path).read_text(encoding="utf-8").strip() == live_pointer
    generation_files = [a for a in receipt.backup.artifacts if a.kind == "generation_file"]
    assert generation_files, "no generation bytes in the production backup"
    for artifact in generation_files:
        backed_up = backup_dir / "generation" / artifact.path
        assert backed_up.is_file(), f"generation artifact {artifact.path} absent from the backup"
        assert hashlib.sha256(backed_up.read_bytes()).hexdigest() == artifact.sha256
