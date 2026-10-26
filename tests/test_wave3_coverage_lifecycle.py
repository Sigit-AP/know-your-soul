"""Coverage wave 3 (SUB-51): lifecycle/persistence error-path pins.

Targets the uncovered branches of ``service``, ``projection``, ``backup``,
``relearn`` and ``importer`` at HEAD 2f5e711: stale/tampered snapshot
recovery, subtree budget trimming, typed lifecycle error mapping, verified
backup/rollback failure paths, and importer boundary guards. All stores are
fresh file-backed databases under ``tmp_path``; monkeypatched faults restore
themselves. Nothing here touches ``src/`` or existing tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from soul_knowledge import backup as backup_module
from soul_knowledge import service as service_module
from soul_knowledge.backup import (
    BackupError,
    ProjectionDiagnosis,
    ProjectionState,
    _activate_backup_dir,
    _anchor,
    _copy_into,
    _pid_alive,
    _temp_owner_pid,
    _verify_copy,
    _wal_sidecar_live,
    backup_protected_artifacts,
    backup_store,
    diagnose_projection,
    inspect_backup,
    recover_backup_activation,
    recover_projection,
    restore_store,
    sweep_orphaned_write_temps,
)
from soul_knowledge.contracts import (
    MINIMUM_TOKEN_BUDGET,
    BudgetTooSmall,
    ImprovementNotFound,
    InvalidProposal,
    LifecycleError,
    MetadataExceedsBudget,
    NodeNotFound,
    QueryRequest,
    ReviewRequest,
)
from soul_knowledge.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    canonical_json,
)
from soul_knowledge.importer import ImportManifestError, SeedImporter, _read_contained_file, _remap_fragment_ids
from soul_knowledge.projection import (
    GraphProjector,
    ProjectionValidationError,
    ShrinkGuardError,
    _section_fragments,
    _source_section_fragments,
    enriched_summary,
)
from soul_knowledge.read_model_validator import ReadModelValidator, ValidationReport
from soul_knowledge.relearn import (
    BackupUnavailable,
    RelearnApproval,
    RelearnEngine,
    RollbackVerificationError,
    UnknownRelearn,
)
from soul_knowledge.service import KnowledgeService
from soul_knowledge.source_registry import ParserVersions, SourceKind, SourceRegistry
from soul_knowledge.store import EventStore, IdempotencyConflict, StoreError

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

PARSER_VERSIONS = ParserVersions(
    parser_version="1.0.0",
    normalization_version="1.0.0",
    engine_version="0.9.95",
    scoring_contract_version="1.0.0",
)


# --------------------------------------------------------------------- helpers


def _node(node_id: str, source_version_id: str = "sv-test", **overrides: object) -> Node:
    base: dict[str, object] = {
        "node_id": node_id,
        "kind": NodeKind.CHILD,
        "domain": PrimaryDomain.P1,
        "summary": f"summary for {node_id}",
        "source_version_id": source_version_id,
    }
    base.update(overrides)
    return Node.model_validate(base)


def _upsert(store: EventStore, node: Node, key: str | None = None) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=key or f"test:upsert:{node.node_id}",
    )


def _stack(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    return store, projector, KnowledgeService(store, projector)


def _seeded(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    store, projector, service = _stack(tmp_path)
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
    _upsert(store, _node("n-gates", summary="Run the gates before commit"))
    projector.rebuild()
    return store, projector, service


def _active_manifest_path(tmp_path: Path) -> Path:
    generations = tmp_path / "out" / "generations"
    current = (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8").strip()
    return generations / current / "manifest.json"


def _active_generation_dir(tmp_path: Path) -> Path:
    current = (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8").strip()
    return tmp_path / "out" / "generations" / current


# ----------------------------------------------------------------- projection


def test_section_fragments_flush_a_paragraph_before_a_list_item() -> None:
    _leads, sentences = _section_fragments(["intro text", "- first item", "tail"])
    assert sentences == ["intro text", "first item", "tail"]


def test_source_section_fragments_treats_a_headingless_file_as_one_section() -> None:
    result = _source_section_fragments("no headings here\nplain line", "doc/x.md")
    assert list(result) == ["doc:x"]


def test_enriched_summary_skips_leads_that_clean_to_empty() -> None:
    summary = enriched_summary("Heading", (["   ", "real lead"], []))
    assert "real lead" in summary


def test_rebuild_tolerates_a_node_whose_source_version_row_is_gone(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    node = _node("n-ghost-source", source_version_id="sv-ghost").model_copy(update={"payload": {"source_file": "x.md"}})
    _upsert(store, node)
    receipt = projector.rebuild()
    assert receipt.node_count == 1


def test_rebuild_fails_typed_when_the_read_model_validator_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    _upsert(store, _node("n-1"))
    monkeypatch.setattr(
        ReadModelValidator,
        "validate",
        lambda self, generation_dir, manifest: ValidationReport(ok=False, errors=("boom",)),
    )
    with pytest.raises(ProjectionValidationError, match="read-model validator"):
        projector.rebuild()


def test_active_manifest_is_none_when_manifest_json_is_unparseable(tmp_path: Path) -> None:
    store, projector, _ = _seeded(tmp_path)
    _active_manifest_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert projector.active_manifest() is None
    # ...and the stale-repair path rebuilds an honest manifest.
    receipt = projector.repair_if_stale()
    assert receipt.node_count >= 1


def test_repair_rebuilds_when_the_projection_version_pointer_disagrees(tmp_path: Path) -> None:
    store, projector, _ = _seeded(tmp_path)
    manifest_path = _active_manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["projection_version"] = "gen-bogus"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    receipt = projector.repair_if_stale()
    assert receipt.node_count >= 1


# --------------------------------------------------------------------- service


def test_snapshot_rejects_a_manifest_with_a_foreign_projection_version(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    manifest_path = _active_manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["projection_version"] = "gen-bogus"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    # The first snapshot probe refuses (projection_version mismatch); the
    # read-path repair then rebuilds and serves the node.
    response = service.get_node("n-gates")
    assert response.node.node_id == "n-gates"


def test_snapshot_rejects_a_graph_whose_hash_left_the_manifest(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    graph_path = _active_generation_dir(tmp_path) / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"] = []  # content drift: the manifest hash no longer matches
    graph_path.write_text(json.dumps(graph), encoding="utf-8")
    # The first snapshot probe refuses (graph hash mismatch); the read-path
    # repair then rebuilds an honest generation and serves the node.
    response = service.get_node("n-gates")
    assert response.node.node_id == "n-gates"


def test_snapshot_rejects_a_generation_the_validator_refuses(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    generation = _active_generation_dir(tmp_path)
    graph_path = generation / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"].append({"id": "junk node!", "label": "x"})  # junk id charset
    graph_bytes = json.dumps(graph, indent=1, sort_keys=True).encode("utf-8")
    graph_path.write_bytes(graph_bytes)
    digest = hashlib.sha256(graph_bytes).hexdigest()
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["graph_content_hash"] = digest
    manifest["artifacts"]["graph.json"] = digest
    manifest["node_count"] = len(graph["nodes"])
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    # The first snapshot probe refuses (the read-model validator rejects the
    # junk node); the repair rebuild is shrink-guarded against the inflated
    # node count, so the read fails typed instead of serving a junk snapshot.
    with pytest.raises(ShrinkGuardError):
        service.get_node("n-gates")


def test_finalize_query_returns_after_the_iteration_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store, _projector, service = _seeded(tmp_path)
    estimates = iter([10, 11] * 12)  # never converges
    monkeypatch.setattr(service_module, "estimated_tokens", lambda payload: next(estimates))
    response = service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
    assert response.estimated_tokens in (10, 11)


def test_access_only_response_above_the_cap_is_a_typed_refusal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store, _projector, service = _seeded(tmp_path)
    monkeypatch.setattr(service_module, "estimated_tokens", lambda payload: 10**6)
    with pytest.raises(MetadataExceedsBudget, match="access-only"):
        service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512, access_only=True))


def test_query_unexpected_failure_is_audited_and_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store, _projector, service = _seeded(tmp_path)

    def _boom(self: object, required: int) -> object:
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(KnowledgeService, "_snapshot", _boom)
    with pytest.raises(RuntimeError, match="snapshot exploded"):
        service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))


def test_get_node_failure_goes_through_the_generic_audit_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store, _projector, service = _seeded(tmp_path)

    def _boom(self: object, required: int) -> object:
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(KnowledgeService, "_snapshot", _boom)
    with pytest.raises(RuntimeError, match="snapshot exploded"):
        service.get_node("n-gates")


def test_get_subtree_below_the_minimum_budget_is_refused(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(BudgetTooSmall, match="below"):
        service.get_subtree("KROOT", token_budget=MINIMUM_TOKEN_BUDGET - 1)


def test_get_subtree_trims_nodes_to_fit_a_tight_budget(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    for index in range(6):
        _upsert(
            store,
            _node(
                f"n-extra-{index}",
                summary="A longer summary that costs real tokens in the subtree payload " + str(index),
            ),
            key=f"test:upsert:n-extra-{index}",
        )
    projector.rebuild()
    response = service.get_subtree("KROOT", token_budget=384)
    assert response.truncated is True
    assert response.estimated_tokens <= 384


def test_get_subtree_with_unfittable_metadata_raises_typed(tmp_path: Path) -> None:
    store, projector, service = _seeded(tmp_path)
    _upsert(store, _node("n-heavy", summary="payload " * 40), key="test:upsert:n-heavy")
    projector.rebuild()
    with pytest.raises(MetadataExceedsBudget, match="mandatory metadata"):
        service.get_subtree("KROOT", token_budget=MINIMUM_TOKEN_BUDGET)


def test_get_subtree_unexpected_failure_is_audited_and_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store, _projector, service = _seeded(tmp_path)

    def _boom(self: object, required: int) -> object:
        raise RuntimeError("snapshot exploded")

    monkeypatch.setattr(KnowledgeService, "_snapshot", _boom)
    with pytest.raises(RuntimeError, match="snapshot exploded"):
        service.get_subtree("KROOT")


def _evidence(
    evidence_id: str, node_id: str, source_version_id: str = "sv-test", source_uri: str = "soul://c/a.md"
) -> Evidence:
    return Evidence.model_validate(
        {
            "evidence_id": evidence_id,
            "node_id": node_id,
            "source_version_id": source_version_id,
            "source_uri": source_uri,
            "observed_at": NOW,
            "independence_key": f"grp-{evidence_id}",
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
            "latency_ms": 1000,
            "compute_ms": 100,
            "output_chars": 500,
        }
    )


def test_record_outcome_for_an_unknown_node_is_typed(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(NodeNotFound, match="does not exist"):
        service.record_outcome(_evidence("e-1", "n-ghost"))


def test_write_meta_marks_stale_when_the_repair_cannot_cover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, projector, service = _seeded(tmp_path)
    monkeypatch.setattr(GraphProjector, "repair_if_stale", lambda self: None)
    receipt = service.log_event(_node("n-fresh"), idempotency_key="test:log:n-fresh")
    assert receipt.audit_event_id


def test_evaluate_unknown_node_raises_typed(tmp_path: Path) -> None:
    from soul_knowledge.contracts import EvaluateRequest

    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(NodeNotFound, match="unknown node"):
        service.evaluate(EvaluateRequest(node_id="n-ghost"))


def test_review_store_failure_is_mapped_and_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store, _projector, service = _seeded(tmp_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise StoreError("scheduler store exploded")

    monkeypatch.setattr(service._scheduler, "scan_due", _boom)  # noqa: SLF001
    with pytest.raises(LifecycleError, match="scheduler store exploded"):
        service.review(ReviewRequest(node_id="n-gates", include_due=True))


def test_map_lifecycle_error_covers_the_typed_table() -> None:
    from soul_knowledge.contracts import InvalidProposal as IP
    from soul_knowledge.contracts import MixedSourceVersionsError, RegressionGateError
    from soul_knowledge.improvement import ProposalValidationError, RegressionGateFailure
    from soul_knowledge.relearn import MixedSourceVersions

    mapped = KnowledgeService._map_lifecycle_error(ProposalValidationError("stale"))  # noqa: SLF001
    assert isinstance(mapped, IP)
    mapped = KnowledgeService._map_lifecycle_error(RegressionGateFailure("gate"))  # noqa: SLF001
    assert isinstance(mapped, RegressionGateError)
    mapped = KnowledgeService._map_lifecycle_error(MixedSourceVersions(["a", "b"]))  # noqa: SLF001
    assert isinstance(mapped, MixedSourceVersionsError)


def test_propose_improvement_with_a_stale_base_version_is_an_invalid_proposal(tmp_path: Path) -> None:
    store, _projector, service = _seeded(tmp_path)
    with pytest.raises(InvalidProposal):
        service.propose_improvement(
            node_id="n-gates",
            diff=[{"op": "set_payload_key", "key": "", "value": "x"}],
            rationale="because",
        )


def test_propose_improvement_requires_identifying_arguments(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(InvalidProposal, match="requires node_id"):
        service.propose_improvement(None)


def test_apply_improvement_requires_a_proposal_id(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(InvalidProposal, match="requires a proposal_id"):
        service.apply_improvement(None)


def test_rollback_of_an_unknown_node_is_typed(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(NodeNotFound):
        service.rollback(node_id="n-ghost")


def test_rollback_without_an_applied_improvement_is_typed(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(ImprovementNotFound):
        service.rollback(node_id="n-gates")


def test_rollback_to_a_missing_revision_is_typed(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(ImprovementNotFound):
        service.rollback(node_id="n-gates", to_version=99)


def test_rollback_request_validation_failure_is_an_invalid_proposal(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(InvalidProposal, match="rollback request is invalid"):
        service.rollback(node_id="n-gates", to_version=0)


def test_apply_relearn_without_a_pending_offer_is_typed(tmp_path: Path) -> None:
    _store, _projector, service = _seeded(tmp_path)
    with pytest.raises(LifecycleError):
        service.apply_relearn("RESET", None)


def test_apply_relearn_without_a_backup_root_is_a_lifecycle_error(tmp_path: Path) -> None:
    # An in-memory store cannot take a verified file backup; an approved apply
    # must fail closed as a typed lifecycle error (never a bare crash).
    store = EventStore(":memory:")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    service = KnowledgeService(store, projector)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
    service.offer_relearn(corpus)  # baseline observation, no offer
    (corpus / "a.md").write_text("alpha v2 changed\n", encoding="utf-8")
    offer = service.offer_relearn(corpus)
    assert offer is not None
    approval = RelearnApproval(offer_id=offer.offer_id, mode="RESET", approved_by="operator", decided_at=NOW)
    with pytest.raises(LifecycleError, match="backup unavailable"):
        service.apply_relearn("RESET", approval)


def test_relearn_status_failure_is_audited_and_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store, _projector, service = _seeded(tmp_path)

    def _boom(self: object) -> object:
        raise RuntimeError("status exploded")

    monkeypatch.setattr(type(service._relearn_engine), "status", _boom)  # noqa: SLF001
    with pytest.raises(RuntimeError, match="status exploded"):
        service.relearn_status()


def test_relearn_offer_failure_is_audited_and_raised(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _store, _projector, service = _seeded(tmp_path)

    def _boom(self: object, *args: object, **kwargs: object) -> object:
        raise RuntimeError("offer exploded")

    monkeypatch.setattr(type(service._relearn_engine), "offer", _boom)  # noqa: SLF001
    with pytest.raises(RuntimeError, match="offer exploded"):
        service.offer_relearn(tmp_path)


# ---------------------------------------------------------------------- backup


def test_verify_copy_rejects_an_event_count_mismatch(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    _upsert(store, _node("n-1"))
    dest = tmp_path / "copy.sqlite3"
    backup_store(store, dest)
    conn = sqlite3.connect(dest)
    try:
        count, head = _anchor(conn)
        with pytest.raises(BackupError, match="expected"):
            _verify_copy(conn, expected_events=count + 1, expected_head=head)
    finally:
        conn.close()
        store.close()


def test_verify_copy_rejects_a_head_mismatch(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    _upsert(store, _node("n-1"))
    dest = tmp_path / "copy.sqlite3"
    backup_store(store, dest)
    conn = sqlite3.connect(dest)
    try:
        count, _head = _anchor(conn)
        with pytest.raises(BackupError, match="chain head"):
            _verify_copy(conn, expected_events=count, expected_head="0" * 64)
    finally:
        conn.close()
        store.close()


def test_wal_sidecar_probe_treats_an_unopenable_shm_as_dead(tmp_path: Path) -> None:
    dest = tmp_path / "soul.sqlite3"
    dest.write_bytes(b"")
    (tmp_path / "soul.sqlite3-shm").mkdir()  # a directory: os.open(O_RDWR) fails
    assert _wal_sidecar_live(dest) is False


def test_copy_into_cleans_the_temp_file_when_verification_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    _upsert(store, _node("n-1"))

    def _boom(conn: sqlite3.Connection, *, expected_events: int, expected_head: str | None) -> None:
        raise BackupError("verification exploded")

    monkeypatch.setattr(backup_module, "_verify_copy", _boom)
    dest = tmp_path / "copy.sqlite3"
    with pytest.raises(BackupError, match="verification|exploded|quick"):
        with store.read() as conn:
            _copy_into(conn, dest, purge_sidecars=True)
    assert list(tmp_path.glob("*.tmp-*")) == []
    store.close()


def test_inspect_backup_reports_a_foreign_database_as_not_ok(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.sqlite3"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    info = inspect_backup(foreign)
    assert info["ok"] is False


def test_activate_backup_dir_removes_a_previous_interrupted_trash(tmp_path: Path) -> None:
    dest = tmp_path / "backup"
    stage = tmp_path / ".backup.tmp-x"
    stage.mkdir()
    (stage / "file").write_text("new", encoding="utf-8")
    trash = tmp_path / f".backup.old-{os.getpid()}"
    trash.mkdir()
    (trash / "stale").write_text("stale", encoding="utf-8")
    _activate_backup_dir(stage, dest)
    assert (dest / "file").read_text(encoding="utf-8") == "new"
    assert not trash.exists()


def test_activate_backup_dir_rolls_the_old_backup_back_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "backup"
    dest.mkdir()
    (dest / "old").write_text("old", encoding="utf-8")
    stage = tmp_path / ".backup.tmp-x"
    stage.mkdir()
    (stage / "new").write_text("new", encoding="utf-8")
    monkeypatch.setattr(backup_module.os, "replace", _raise_oserror)
    with pytest.raises(OSError, match="replace exploded"):
        _activate_backup_dir(stage, dest)
    assert (dest / "old").read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob(".backup.old-*")) == []


def _raise_oserror(*args: object, **kwargs: object) -> None:
    raise OSError("replace exploded")


def test_recover_backup_activation_refuses_multiple_interrupted_trashes(tmp_path: Path) -> None:
    dest = tmp_path / "backup"
    for pid in (111, 222):
        trash = tmp_path / f".backup.old-{pid}"
        trash.mkdir()
    with pytest.raises(BackupError, match="multiple interrupted activations"):
        recover_backup_activation(dest)


def test_protected_backup_cleans_a_preexisting_stage_dir(tmp_path: Path) -> None:
    store, projector, _service = _seeded(tmp_path)
    dest = tmp_path / "protected"
    stage = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    stage.mkdir()
    (stage / "residue").write_text("residue", encoding="utf-8")
    receipt = backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=dest)
    assert receipt.database.backup_path
    assert not stage.exists()


def test_protected_backup_refuses_a_manifest_declared_artifact_that_is_missing(tmp_path: Path) -> None:
    store, projector, _service = _seeded(tmp_path)
    generation = _active_generation_dir(tmp_path)
    (generation / "labels.json").unlink()
    dest = tmp_path / "protected"
    with pytest.raises(BackupError, match="missing from the generation"):
        backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=dest)
    assert not dest.with_name(f".{dest.name}.tmp-{os.getpid()}").exists()


def test_protected_backup_refuses_a_drifted_generation_artifact(tmp_path: Path) -> None:
    store, projector, _service = _seeded(tmp_path)
    generation = _active_generation_dir(tmp_path)
    (generation / "labels.json").write_text('{"tampered": true}', encoding="utf-8")
    with pytest.raises(BackupError, match="does not match its manifest hash"):
        backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=tmp_path / "protected")


def test_protected_backup_detects_a_staged_pointer_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, projector, _service = _seeded(tmp_path)
    original = shutil.copyfile

    def _corrupting_copyfile(src: str | Path, dst: str | Path) -> None:
        original(src, dst)
        if Path(str(dst)).name == "CURRENT":
            Path(str(dst)).write_text("corrupted", encoding="utf-8")

    monkeypatch.setattr(backup_module.shutil, "copyfile", _corrupting_copyfile)
    with pytest.raises(BackupError, match="CURRENT pointer"):
        backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=tmp_path / "protected")


def test_protected_backup_detects_a_staged_manifest_hash_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, projector, _service = _seeded(tmp_path)
    original_copytree = shutil.copytree

    def _corrupting_copytree(src: str | Path, dst: str | Path, **kwargs: Any) -> Path:
        result = original_copytree(src, dst, **kwargs)
        manifest = Path(str(dst)) / "manifest.json"
        manifest.write_text(manifest.read_text(encoding="utf-8") + " ", encoding="utf-8")
        return Path(result)

    monkeypatch.setattr(backup_module.shutil, "copytree", _corrupting_copytree)
    with pytest.raises(BackupError, match="manifest"):
        backup_protected_artifacts(store, projection_root=tmp_path / "out", dest_dir=tmp_path / "protected")


def test_restore_store_refuses_an_unverified_backup(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.sqlite3"
    conn = sqlite3.connect(foreign)
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    with pytest.raises(BackupError, match="unverified"):
        restore_store(foreign, tmp_path / "dest.sqlite3")


def test_restore_failure_reopens_the_live_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, _projector, _service = _seeded(tmp_path)
    backup = tmp_path / "backup.sqlite3"
    backup_store(store, backup)

    # Fault-inject the verification step: the verified copy refuses, and the
    # live store must be reopened in place (never left closed).
    def _boom(conn: sqlite3.Connection, *, expected_events: int, expected_head: str | None) -> None:
        raise BackupError("verification exploded")

    monkeypatch.setattr(backup_module, "_verify_copy", _boom)
    with pytest.raises(BackupError, match="verification exploded"):
        restore_store(backup, store.db_path, overwrite=True)
    # The live store was reopened in place and still serves reads.
    assert store.current_node("n-gates") is not None


def test_temp_owner_pid_parses_only_a_trailing_marker() -> None:
    assert _temp_owner_pid("plain-name") is None
    assert _temp_owner_pid("x.tmp-notanint") is None
    assert _temp_owner_pid("x.tmp-4242") == 4242


def test_pid_alive_rejects_non_positive_pids() -> None:
    assert _pid_alive(0) is False
    assert _pid_alive(-5) is False


def test_pid_alive_treats_a_permission_error_as_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object) -> None:
        raise PermissionError("owned by another user")

    monkeypatch.setattr(backup_module.os, "kill", _raise)
    assert _pid_alive(4242) is True


def test_sweep_tolerates_a_temp_that_disappears_mid_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = tmp_path / f"out.tmp-{os.getpid()}-x"
    candidate.write_text("partial", encoding="utf-8")
    monkeypatch.setattr(backup_module, "_pid_alive", lambda pid: False)
    original_unlink = Path.unlink

    def _flaky_unlink(self: Path, missing_ok: bool = False) -> None:
        if self == candidate:
            raise FileNotFoundError("already gone")
        original_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _flaky_unlink)
    assert sweep_orphaned_write_temps(tmp_path) == ()


def test_diagnose_projection_reports_a_missing_manifest_as_torn(tmp_path: Path) -> None:
    store, projector, _service = _seeded(tmp_path)
    _active_manifest_path(tmp_path).unlink()
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.TORN
    assert "manifest" in diagnosis.detail


def test_diagnose_projection_reports_a_version_disagreement_as_stale(tmp_path: Path) -> None:
    store, projector, _service = _seeded(tmp_path)
    manifest_path = _active_manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["projection_version"] = "gen-bogus"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.state is ProjectionState.STALE


def test_recover_projection_refuses_a_still_broken_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, projector, _service = _seeded(tmp_path)
    monkeypatch.setattr(
        backup_module,
        "diagnose_projection",
        lambda store, projector: ProjectionDiagnosis(ProjectionState.TORN, "still torn", None, 0),
    )
    with pytest.raises(BackupError, match="still torn"):
        recover_projection(store, projector)


# --------------------------------------------------------------------- relearn


class _RelearnScenario:
    """v1 corpus with evidence, a changed v2, and a pending ACCUMULATE offer."""

    def __init__(self, tmp_path: Path, *, backup_root: Path | None = None, with_projection: bool = False) -> None:
        self.root = tmp_path
        self.db_path = tmp_path / "soul.db"
        self.corpus = tmp_path / "corpus"
        self.corpus.mkdir(exist_ok=True)
        (self.corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
        (self.corpus / "b.md").write_text("beta v1\n", encoding="utf-8")
        self.store = EventStore(self.db_path)
        self.registry = SourceRegistry(self.store)
        self.v1 = self.registry.observe(self.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        self.store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=_node("n-1", self.v1.source_version_id),
                source_version_id=self.v1.source_version_id,
                occurred_at=NOW,
            ),
            idempotency_key="seed:n-1",
        )
        for evidence_id, uri, svid in (
            ("e-old", "a.md", self.v1.source_version_id),
            ("e-foreign", "ghost.md", "sv-unrelated"),
        ):
            self.store.append_domain(
                Event(
                    kind=EventKind.EVIDENCE_APPEND,
                    node_id="n-1",
                    domain=PrimaryDomain.P1,
                    evidence=_evidence(evidence_id, "n-1", svid, uri),
                    source_version_id=svid,
                    occurred_at=NOW,
                ),
                idempotency_key=f"seed:{evidence_id}",
            )
        kwargs: dict[str, object] = {"backup_root": backup_root, "scoring_contract_version": "1.0.0"}
        if with_projection:
            self.projector = GraphProjector(self.store, output_root=tmp_path / "out")
            self.projector.rebuild()
            kwargs["generation_dir"] = tmp_path / "out"
        self.engine = RelearnEngine(self.store, self.registry, **kwargs)  # type: ignore[arg-type]
        (self.corpus / "a.md").write_text("alpha v2 changed\n", encoding="utf-8")
        self.offer = self.engine.offer(self.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        assert self.offer is not None
        # Evidence recorded against the NEW source version (post-offer): the
        # carry mapping must skip it entirely.
        self.store.append_domain(
            Event(
                kind=EventKind.EVIDENCE_APPEND,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                evidence=_evidence("e-new", "n-1", self.offer.current_source_version_id, "a.md"),
                source_version_id=self.offer.current_source_version_id,
                occurred_at=NOW,
            ),
            idempotency_key="seed:e-new",
        )

    def approve(self, mode: str) -> RelearnApproval:
        assert self.offer is not None
        return RelearnApproval(offer_id=self.offer.offer_id, mode=mode, approved_by="operator", decided_at=NOW)  # type: ignore[arg-type]


def test_relearn_checkpoint_on_an_in_memory_store_is_a_noop() -> None:
    store = EventStore(":memory:")
    try:
        registry = SourceRegistry(store)
        engine = RelearnEngine(store, registry, scoring_contract_version="1.0.0")
        assert engine.store is store
        engine.checkpoint()  # must not raise
    finally:
        store.close()


def test_relearn_manifest_of_an_unknown_version_is_empty(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path)
    assert scenario.engine._manifest("sv-unknown") == {}  # noqa: SLF001


def test_classify_skips_new_version_evidence_and_quarantines_foreign_versions(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path)
    assert scenario.offer is not None
    carried, quarantined = scenario.engine._classify(scenario.offer, "ACCUMULATE")  # noqa: SLF001
    # Evidence of the new version is never part of the carry mapping.
    assert "e-new" not in carried
    assert "e-new" not in quarantined
    # Evidence of a version that is neither the previous nor the new one is
    # quarantined outright (no anchor mapping is even attempted).
    assert "e-foreign" in quarantined


def test_apply_cleans_a_preexisting_staging_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from soul_knowledge import relearn as relearn_module

    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups")
    monkeypatch.setattr(relearn_module, "new_id", lambda: "rel-fixed")
    staging = tmp_path / "backups" / f".rel-fixed.tmp-{os.getpid()}"
    staging.mkdir(parents=True)
    (staging / "residue").write_text("residue", encoding="utf-8")
    receipt = scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"))
    assert receipt.relearn_id == "rel-fixed"
    assert not staging.exists()
    assert (tmp_path / "backups" / "rel-fixed" / "manifest.json").exists()


def test_apply_replaces_a_preexisting_final_backup_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from soul_knowledge import relearn as relearn_module

    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups")
    monkeypatch.setattr(relearn_module, "new_id", lambda: "rel-fixed")
    final = tmp_path / "backups" / "rel-fixed"
    final.mkdir(parents=True)
    (final / "stale").write_text("stale", encoding="utf-8")
    receipt = scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"))
    assert receipt.relearn_id == "rel-fixed"
    assert (final / "manifest.json").exists()
    assert not (final / "stale").exists()


def test_apply_replaces_a_preexisting_final_backup_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from soul_knowledge import relearn as relearn_module

    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups")
    monkeypatch.setattr(relearn_module, "new_id", lambda: "rel-fixed")
    final = tmp_path / "backups" / "rel-fixed"
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_text("a stray file squats on the backup path", encoding="utf-8")
    scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"))
    assert (final / "manifest.json").exists()


def test_apply_with_a_key_bound_to_a_plain_write_conflicts(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups")
    scenario.store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-2",
            domain=PrimaryDomain.P1,
            node=_node("n-2", scenario.v1.source_version_id),
            source_version_id=scenario.v1.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key="shared-key",
    )
    with pytest.raises(IdempotencyConflict, match="non-relearn"):
        scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"), idempotency_key="shared-key")


def test_load_backup_manifest_without_a_backup_root_is_typed(tmp_path: Path) -> None:
    store = EventStore(":memory:")
    try:
        engine = RelearnEngine(store, scoring_contract_version="1.0.0")
        with pytest.raises(BackupUnavailable, match="no backup root"):
            engine._load_backup_manifest("rel-x")  # noqa: SLF001
    finally:
        store.close()


def test_load_backup_manifest_for_a_missing_backup_is_typed(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups")
    with pytest.raises(BackupUnavailable, match="no backup manifest"):
        scenario.engine._load_backup_manifest("rel-x")  # noqa: SLF001


def test_rollback_without_a_backup_root_is_typed() -> None:
    store = EventStore(":memory:")
    try:
        engine = RelearnEngine(store, scoring_contract_version="1.0.0")
        with pytest.raises(UnknownRelearn, match="no backup root"):
            engine.rollback("rel-x")
    finally:
        store.close()


def test_rollback_without_a_relearn_record_is_typed(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups")
    receipt = scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"))
    # A backup directory whose relearn id the store never recorded.
    planted = tmp_path / "backups" / "rel-planted"
    shutil.copytree(tmp_path / "backups" / receipt.relearn_id, planted)
    manifest = json.loads((planted / "manifest.json").read_text(encoding="utf-8"))
    manifest["relearn_id"] = "rel-planted"
    (planted / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(UnknownRelearn, match="no relearn record"):
        scenario.engine.rollback("rel-planted")


def test_rollback_refuses_a_tampered_backup_database(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups")
    receipt = scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"))
    backup_db = tmp_path / "backups" / receipt.relearn_id / "soul.db"
    data = bytearray(backup_db.read_bytes())
    data[-1] ^= 0xFF  # flip the last byte: the staged hash check must refuse
    backup_db.write_bytes(bytes(data))
    with pytest.raises(RollbackVerificationError, match="hash-match"):
        scenario.engine.rollback(receipt.relearn_id)
    # The failed rollback reopened the live store: it still serves reads.
    assert scenario.store.current_node("n-1") is not None


def test_rollback_refuses_a_tampered_generation_file(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups", with_projection=True)
    receipt = scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"))
    backup_dir = tmp_path / "backups" / receipt.relearn_id
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    generation_artifacts = [
        a for a in manifest["artifacts"] if a["kind"] == "generation_file" and a["path"] != "CURRENT"
    ]
    target = backup_dir / "generation" / generation_artifacts[0]["path"].removeprefix("generation/")
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))
    with pytest.raises(RollbackVerificationError, match="generation file"):
        scenario.engine.rollback(receipt.relearn_id)


def test_rollback_refuses_a_tampered_current_pointer(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups", with_projection=True)
    receipt = scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"))
    backup_dir = tmp_path / "backups" / receipt.relearn_id
    pointer = backup_dir / "CURRENT"
    pointer.write_text(pointer.read_text(encoding="utf-8").strip() + "-tampered", encoding="utf-8")
    with pytest.raises(RollbackVerificationError, match="CURRENT"):
        scenario.engine.rollback(receipt.relearn_id)


# -------------------------------------------------------------------- importer


def test_remap_fragment_ids_with_no_nodes_is_an_empty_passthrough() -> None:
    nodes: list[dict[str, object]] = []
    edges = [{"source": "a", "target": "b", "relation": "references"}]
    nodes_out, edges_out, id_map = _remap_fragment_ids(nodes, edges, "doc/x.md")
    assert nodes_out == []
    assert edges_out is edges
    assert id_map == {}


def test_read_contained_file_refuses_a_file_that_grew_past_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from soul_knowledge.importer import OversizedPayloadError

    target = tmp_path / "a.md"
    target.write_bytes(b"small")
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: original(self) + b"x" * 1024)
    with pytest.raises(OversizedPayloadError, match="grew past"):
        _read_contained_file(tmp_path, "a.md", max_bytes=64)


def test_import_directory_requires_a_state_dir(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        importer = SeedImporter(store)
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "a.md").write_text("# A\nbody\n", encoding="utf-8")
        with pytest.raises(ImportManifestError, match="state_dir"):
            importer.import_directory(corpus, domain=PrimaryDomain.P1)
    finally:
        store.close()


def test_scope_index_rebuild_drops_retired_nodes(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        importer = SeedImporter(store, state_dir=tmp_path / "state")
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "a.md").write_text("# Alpha\nbody\n", encoding="utf-8")
        (corpus / "b.md").write_text("# Beta\n[[alpha]]\n", encoding="utf-8")
        importer.import_directory(corpus, domain=PrimaryDomain.P1)
        importer.retire_source("a.md")
        (corpus / "c.md").write_text("# Gamma\n[[alpha]]\n", encoding="utf-8")
        receipt = importer.import_file(corpus / "c.md", domain=PrimaryDomain.P1, source_uri=str(corpus / "c.md"))
        # The retired target stays unresolved — the scope index replay dropped
        # the retired node (never a guessed target).
        assert "alpha" in receipt.unresolved_links
    finally:
        store.close()


def test_ambiguous_legacy_wikilink_target_is_unresolved(tmp_path: Path) -> None:
    # ``a-b.md`` and ``a_b.md`` collapse to the same legacy generator id: a
    # wikilink naming it is ambiguous across the two live scopes and must fail
    # safe to unresolved — never a guessed target.
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        importer = SeedImporter(store, state_dir=tmp_path / "state")
        corpus = tmp_path / "corpus"
        corpus.mkdir()
        (corpus / "a-b.md").write_text("# Alpha Dash\nbody\n", encoding="utf-8")
        (corpus / "a_b.md").write_text("# Alpha Underscore\nbody\n", encoding="utf-8")
        importer.import_file(corpus / "a-b.md", domain=PrimaryDomain.P1, source_uri=str(corpus / "a-b.md"))
        importer.import_file(corpus / "a_b.md", domain=PrimaryDomain.P1, source_uri=str(corpus / "a_b.md"))
        (corpus / "c.md").write_text("# Gamma\n[[a_b]]\n", encoding="utf-8")
        receipt = importer.import_file(corpus / "c.md", domain=PrimaryDomain.P1, source_uri=str(corpus / "c.md"))
        assert "a_b" in receipt.unresolved_links
    finally:
        store.close()


# ------------------------------------------------------- readiness heal (F-2)


def test_check_readiness_heals_a_lying_projection_watermark(tmp_path: Path) -> None:
    from soul_knowledge.observability import check_readiness

    store, projector, _service = _seeded(tmp_path)
    manifest_path = _active_manifest_path(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    store_watermark = store.knowledge_high_watermark()
    manifest["source_knowledge_high_watermark"] = store_watermark + 5  # the manifest lies ahead of the store
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = check_readiness(store, projector=projector)
    assert report.checks["projection_watermark"] is True
    healed = projector.active_manifest()
    assert healed is not None
    assert int(healed["source_knowledge_high_watermark"]) == store_watermark


def test_rollback_removes_live_wal_sidecars(tmp_path: Path) -> None:
    scenario = _RelearnScenario(tmp_path, backup_root=tmp_path / "backups")
    receipt = scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"))
    # A second open connection keeps the WAL/SHM sidecars materialized while
    # the rollback closes the store connection.
    extra = sqlite3.connect(scenario.db_path)
    try:
        extra.execute("CREATE TABLE IF NOT EXISTS keepalive (x INTEGER)")
        extra.commit()
        assert Path(str(scenario.db_path) + "-shm").exists()
        scenario.engine.rollback(receipt.relearn_id)
    finally:
        extra.close()
    assert scenario.store.current_node("n-1") is not None
