"""Wave-2 chain-audit regression probes (spec 7.7/7.9; audit findings L1/L2/L3 + residual).

Findings: ``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave2-adapters-spec.md``.

* L1 (spec 7.7): every mutation-path final audit carries result references,
  error (on failure), ``projection_outcome``, a measured ``duration_ms`` and a
  ``correlation_id`` — previously null/0 on ``store.commit``,
  ``improvement._Audited`` and the relearn apply/rollback paths.
* L2 (spec 7.7): the ``apply_improvement`` final audit links the domain event
  it appended, like every other mutation.
* L3 (spec 7.9): lifecycle mutation receipts/responses expose
  ``domain_event_id``, the final audit ``event_seq``, ``node_version`` and
  ``knowledge_seq`` (WriteReceipt parity).
* Residual wave-1: the relearn backup reuses
  ``backup.backup_protected_artifacts`` (SQLite via the online backup API, the
  ``CURRENT`` pointer, and generation artifacts verified against the
  manifest-declared hashes).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from know_your_soul.backup import backup_protected_artifacts
from know_your_soul.contracts import (
    EvaluateRequest,
    ImprovementResponse,
    ProposalResponse,
    RollbackResponse,
)
from know_your_soul.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    ScoreVector,
)
from know_your_soul.improvement import (
    ApprovalRecord,
    ApprovalRequired,
    DiffOp,
    DiffOperation,
    ImprovementProposal,
    apply_improvement,
    propose,
)
from know_your_soul.projection import GraphProjector
from know_your_soul.relearn import RelearnApproval, RelearnEngine
from know_your_soul.service import KnowledgeService
from know_your_soul.source_registry import (
    RELEARN_PENDING,
    ParserVersions,
    SourceKind,
    SourceRegistry,
)
from know_your_soul.store import EventStore, VersionConflict

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
PARSER_VERSIONS = ParserVersions(
    parser_version="1.0.0",
    normalization_version="1.0.0",
    engine_version="0.9.95",
    scoring_contract_version="1.0.0",
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
            out.setdefault(str(row["event_type"]), []).append(payload)
        return out
    finally:
        conn.close()


def _domain_event_ids(db_path: Path) -> set[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT event_id FROM event_log WHERE kind='domain'").fetchall()
        return {str(row[0]) for row in rows}
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


def _make_generation_root(root: Path) -> Path:
    """A projector-layout generation root: CURRENT + generations/<name>/."""
    generation = root / "generations" / "gen-1"
    generation.mkdir(parents=True)
    (generation / "graph.json").write_text('{"graph": 1}', encoding="utf-8")
    manifest = {"artifacts": {"graph.json": _sha256(generation / "graph.json")}}
    (generation / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "CURRENT").write_text("gen-1", encoding="utf-8")
    return root


class TestLifecycleMutationAuditChain:
    """L1/L2: spec 7.7 fields on every mutation-path final audit."""

    def test_mutation_final_audits_carry_spec_7_7_fields(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        projector = GraphProjector(store, output_root=tmp_path / "out")
        service = KnowledgeService(store, projector)
        _seed_roots(store)

        service.log_event(_node("n-lc", "sv-test"), actor="probe", idempotency_key="probe:log:n-lc")
        service.record_outcome(_evidence("e-lc-1", "n-lc", "sv-test"), actor="probe", idempotency_key="probe:ev:1")
        service.evaluate(EvaluateRequest(node_id="n-lc"), actor="probe")
        service.review("n-lc", actor="probe", idempotency_key="probe:review:1")
        pre_apply_version = store.node_version("n-lc")
        proposal = service.propose_improvement(
            node_id="n-lc",
            diff=[DiffOperation(op=DiffOp.SET_SUMMARY, value="improved summary")],
            rationale="probe improvement",
            actor="probe",
            idempotency_key="probe:prop:1",
        )
        service.apply_improvement(
            proposal_id=proposal.proposal.proposal_id, actor="probe", idempotency_key="probe:apply:1"
        )
        service.rollback(node_id="n-lc", to_version=pre_apply_version, actor="probe", idempotency_key="probe:rb:1")
        store.close()

        audits = _final_audits(tmp_path / "soul.sqlite3")
        mutation_ops = (
            "knowledge.log_event",
            "knowledge.record_outcome",
            "knowledge.review",
            "knowledge.propose_improvement",
            "knowledge.apply_improvement",
            "knowledge.rollback",
        )
        domain_ids = _domain_event_ids(tmp_path / "soul.sqlite3")
        for operation in mutation_ops:
            rows = audits.get(operation, [])
            assert len(rows) == 1, f"{operation}: expected exactly one final audit, found {len(rows)}"
            payload = rows[0]
            assert payload.get("status") == "success", f"{operation}: {payload}"
            assert payload.get("correlation_id"), f"{operation}: correlation_id missing (spec 7.7)"
            assert payload.get("projection_outcome"), f"{operation}: projection_outcome missing (spec 7.7)"
            assert isinstance(payload.get("duration_ms"), int), f"{operation}: duration_ms missing (spec 7.7)"
            assert payload.get("error") is None, f"{operation}: unexpected error {payload.get('error')}"
            linked = payload.get("linked_domain_event_id")
            assert linked in domain_ids, f"{operation}: linked_domain_event_id {linked!r} not a domain event"

    def test_apply_audit_links_the_appended_node_upsert(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        _seed_roots(store)
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=_node("n-1", "sv-test"),
                source_version_id="sv-test",
            ),
            idempotency_key="seed:n-1",
        )
        record = propose(
            store,
            node_id="n-1",
            diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="v2"),),
            rationale="link probe",
        )
        receipt = apply_improvement(store, record.proposal)
        assert receipt.domain_event_id is not None
        assert receipt.node_version == record.proposal.base_version + 1
        assert receipt.knowledge_seq is not None
        store.close()

        audits = _final_audits(tmp_path / "soul.sqlite3")
        apply_audits = audits["knowledge.apply_improvement"]
        assert len(apply_audits) == 1
        assert apply_audits[0].get("linked_domain_event_id") == receipt.domain_event_id

    def test_failed_mutation_audit_carries_error_duration_and_correlation(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        projector = GraphProjector(store, output_root=tmp_path / "out")
        service = KnowledgeService(store, projector)
        _seed_roots(store)
        service.log_event(_node("n-f", "sv-test"), actor="probe", idempotency_key="probe:log:n-f")
        with pytest.raises(VersionConflict):
            service.log_event(
                _node("n-f", "sv-test", summary="conflicting"),
                actor="probe",
                idempotency_key="probe:log:n-f-2",
                expected_version=999,
            )
        store.close()

        audits = _final_audits(tmp_path / "soul.sqlite3")
        failures = [p for p in audits.get("knowledge.log_event", []) if p.get("status") == "failure"]
        assert len(failures) == 1
        payload = failures[0]
        assert payload.get("correlation_id"), "failure audit: correlation_id missing (spec 7.7)"
        assert payload.get("projection_outcome"), "failure audit: projection_outcome missing (spec 7.7)"
        assert isinstance(payload.get("duration_ms"), int)
        assert payload.get("error")


class TestRelearnAuditAndBackup:
    """L1 for relearn paths + residual: the relearn backup is the protected-set backup."""

    def _engine_stack(self, tmp_path: Path) -> tuple[EventStore, SourceRegistry, RelearnEngine, str]:
        store = EventStore(tmp_path / "soul.db")
        registry = SourceRegistry(store)
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
        v1 = registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=_node("n-1", v1.source_version_id),
                source_version_id=v1.source_version_id,
            ),
            idempotency_key="seed:n-1",
        )
        store.append_domain(
            Event(
                kind=EventKind.EVIDENCE_APPEND,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                evidence=_evidence("e1", "n-1", v1.source_version_id),
                source_version_id=v1.source_version_id,
            ),
            idempotency_key="seed:e1",
        )
        engine = RelearnEngine(
            store,
            registry,
            backup_root=tmp_path / "backups",
            generation_dir=_make_generation_root(tmp_path / "generation"),
            scoring_contract_version="1.0.0",
        )
        (corpus / "a.md").write_text("alpha v2 — changed\n", encoding="utf-8")
        offer = engine.offer(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        assert offer is not None and registry.relearn_state() == RELEARN_PENDING
        return store, registry, engine, offer.offer_id

    def test_relearn_apply_audit_carries_spec_7_7_fields(self, tmp_path: Path) -> None:
        store, _registry, engine, offer_id = self._engine_stack(tmp_path)
        receipt = engine.apply(
            "RESET",
            RelearnApproval(offer_id=offer_id, mode="RESET", approved_by="operator", decided_at=NOW),
            at=NOW,
        )
        store.close()

        audits = _final_audits(tmp_path / "soul.db")
        rows = audits.get("knowledge.relearn_apply", [])
        assert len(rows) == 1
        payload = rows[0]
        assert payload.get("correlation_id"), "relearn apply: correlation_id missing (spec 7.7)"
        assert payload.get("projection_outcome"), "relearn apply: projection_outcome missing (spec 7.7)"
        assert isinstance(payload.get("duration_ms"), int)
        references = payload.get("result_references")
        assert isinstance(references, dict) and references.get("relearn_id") == receipt.relearn_id
        assert payload.get("linked_domain_event_id") in _domain_event_ids(tmp_path / "soul.db")

    def test_relearn_backup_uses_the_protected_set(self, tmp_path: Path) -> None:
        store, _registry, engine, offer_id = self._engine_stack(tmp_path)
        receipt = engine.apply(
            "RESET",
            RelearnApproval(offer_id=offer_id, mode="RESET", approved_by="operator", decided_at=NOW),
            at=NOW,
        )
        backup = receipt.backup
        kinds = {artifact.kind for artifact in backup.artifacts}
        assert {"sqlite", "current_pointer", "generation_file"} <= kinds

        backup_dir = tmp_path / "backups" / receipt.relearn_id
        # The shared protected-set backup wrote the whole set into the backup dir.
        assert (backup_dir / "soul.db").is_file()
        assert (backup_dir / "CURRENT").read_text(encoding="utf-8") == "gen-1"
        for artifact in backup.artifacts:
            if artifact.kind == "sqlite":
                assert _sha256(backup_dir / "soul.db") == artifact.sha256
            elif artifact.kind == "current_pointer":
                assert _sha256(backup_dir / "CURRENT") == artifact.sha256
            else:
                assert _sha256(backup_dir / "generation" / artifact.path) == artifact.sha256
        store.close()

    def test_relearn_rollback_restores_pointer_and_generation(self, tmp_path: Path) -> None:
        store, _registry, engine, offer_id = self._engine_stack(tmp_path)
        receipt = engine.apply(
            "RESET",
            RelearnApproval(offer_id=offer_id, mode="RESET", approved_by="operator", decided_at=NOW),
            at=NOW,
        )
        # Post-apply the projection moved on: new pointer, clobbered generation bytes.
        generation_root = tmp_path / "generation"
        (generation_root / "generations" / "gen-1" / "graph.json").write_text('{"graph": 2}', encoding="utf-8")
        (generation_root / "CURRENT").write_text("gen-2", encoding="utf-8")

        rollback = engine.rollback(receipt.relearn_id, actor="operator")
        assert rollback.verified is True
        sqlite_artifact = next(a for a in receipt.backup.artifacts if a.kind == "sqlite")
        assert rollback.restored_db_sha256 == sqlite_artifact.sha256
        assert (generation_root / "CURRENT").read_text(encoding="utf-8") == "gen-1"
        assert (generation_root / "generations" / "gen-1" / "graph.json").read_text(encoding="utf-8") == (
            '{"graph": 1}'
        )

        reopened = EventStore(tmp_path / "soul.db")
        try:
            audits = _final_audits(tmp_path / "soul.db")
            rows = audits.get("knowledge.relearn_rollback", [])
            assert len(rows) == 1
            payload = rows[0]
            assert payload.get("correlation_id"), "relearn rollback: correlation_id missing (spec 7.7)"
            assert payload.get("projection_outcome"), "relearn rollback: projection_outcome missing (spec 7.7)"
            assert isinstance(payload.get("duration_ms"), int)
        finally:
            reopened.close()


class TestMutationReceiptFields:
    """L3 (spec 7.9): lifecycle receipts/responses carry the WriteReceipt field set."""

    def test_response_models_expose_spec_7_9_fields(self) -> None:
        meta = {
            "audit_event_id": "audit-1",
            "projection_version": "v1",
            "required_knowledge_watermark": 1,
            "source_knowledge_high_watermark": 1,
            "stale": False,
        }
        improvement = ImprovementResponse(
            **meta,
            proposal_id="prop-1",
            node_id="n-1",
            from_version=1,
            to_version=2,
            status="applied",
            content_sha256="0" * 64,
            idempotent_replay=False,
            domain_event_id="dom-1",
            audit_event_seq=7,
            node_version=2,
            knowledge_seq=3,
        )
        payload = improvement.model_dump(mode="json")
        assert payload["domain_event_id"] == "dom-1"
        assert payload["audit_event_seq"] == 7
        assert payload["node_version"] == 2
        assert payload["knowledge_seq"] == 3

        rollback = RollbackResponse(**meta, kind="improvement", node_id="n-1", status="rolled_back")
        assert rollback.domain_event_id is None  # optional until the service wires it
        proposal = ProposalResponse(
            **meta,
            proposal=ImprovementProposal(
                proposal_id="prop-1",
                node_id="n-1",
                base_version=1,
                diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="v2"),),
                rationale="probe",
                impact="low",
                created_at=NOW,
            ),
            domain_event_id="dom-0",
            audit_event_seq=6,
            node_version=1,
            knowledge_seq=None,
        )
        assert proposal.domain_event_id == "dom-0"
        assert proposal.knowledge_seq is None  # zero-knowledge-weight admin event

    def test_proposal_record_and_improvement_receipt_carry_domain_result(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        _seed_roots(store)
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=_node("n-1", "sv-test"),
                source_version_id="sv-test",
            ),
            idempotency_key="seed:n-1",
        )
        record = propose(
            store,
            node_id="n-1",
            diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="v2"),),
            rationale="receipt probe",
        )
        assert record.domain_event_id
        assert record.node_version is not None
        # A proposal is a zero-knowledge-weight admin event: no knowledge_seq.
        assert record.knowledge_seq is None

        receipt = apply_improvement(store, record.proposal)
        assert receipt.domain_event_id
        assert receipt.node_version == receipt.to_version
        assert receipt.knowledge_seq is not None

        retained = apply_improvement  # noqa: F841 - keep imports tight; retain path covered via store.commit
        store.close()


def test_approval_record_still_required_for_high_impact(tmp_path: Path) -> None:
    """Guard: the receipt/audit changes never weaken the approval gate."""
    store = EventStore(tmp_path / "soul.sqlite3")
    _seed_roots(store)
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-1",
            domain=PrimaryDomain.P1,
            node=_node("n-1", "sv-test"),
            source_version_id="sv-test",
        ),
        idempotency_key="seed:n-1",
    )
    record = propose(
        store,
        node_id="n-1",
        diff=(DiffOperation(op=DiffOp.ADD_RELATION, value={"kind": "supports", "target_node_id": "P1"}),),
        rationale="high impact probe",
    )
    assert record.proposal.impact == "high"
    with pytest.raises(ApprovalRequired):
        apply_improvement(store, record.proposal)
    approved = apply_improvement(
        store,
        record.proposal,
        approval=ApprovalRecord(
            proposal_id=record.proposal.proposal_id,
            approved_by="operator",
            decision="approved",
            decided_at=NOW,
        ),
    )
    assert approved.status == "applied"
    store.close()


def test_backup_protected_artifacts_is_the_relearn_backup_mechanism(tmp_path: Path) -> None:
    """The shared helper alone reproduces the relearn backup layout (residual probe)."""
    store = EventStore(tmp_path / "soul.sqlite3")
    _seed_roots(store)
    root = _make_generation_root(tmp_path / "projection")
    receipt = backup_protected_artifacts(store, projection_root=root, dest_dir=tmp_path / "backups" / "rl-1")
    assert receipt.verified
    assert receipt.current_pointer == "gen-1"
    assert {artifact.kind for artifact in receipt.artifacts} == {"sqlite", "current_pointer", "generation_file"}
    store.close()
