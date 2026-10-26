"""Hash-scoped RESET/ACCUMULATE relearn, quarantine, backup and rollback tests.

Task 6 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
frozen contract ``docs/contracts/relearn-contract.md`` and fixtures
``tests/fixtures/access_recommendation_relearn_cases.json`` (``relearn_cases``).

RESET starts from zero old evidence (P=M=E=0, U=1, level 1, promotion blocked).
ACCUMULATE reuses only evidence whose anchors map deterministically; unmapped
evidence is quarantined from scoring. Every apply takes a verified atomic
backup; rollback restores exact pre-relearn hashes, graph bytes and P/M/E with
exactly one linked audit event.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from know_your_soul.backup import BackupError
from know_your_soul.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
    ScoreVector,
)
from know_your_soul.relearn import (
    BackupUnavailable,
    MixedSourceVersions,
    NoPendingRelearn,
    OperatorDecisionRequired,
    RelearnApproval,
    RelearnEngine,
    RelearnError,
    UnknownRelearn,
)
from know_your_soul.source_registry import (
    RELEARN_PENDING,
    ParserVersions,
    RelearnOffer,
    SourceKind,
    SourceRegistry,
    SourceState,
)
from know_your_soul.store import EventStore

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

PARSER_VERSIONS = ParserVersions(
    parser_version="1.0.0",
    normalization_version="1.0.0",
    engine_version="0.9.95",
    scoring_contract_version="1.0.0",
)


# ------------------------------------------------------------------ builders


def sv(p: str, m: str, e: str, u: str, **extra: object) -> ScoreVector:
    return ScoreVector.model_validate({"p": p, "m": m, "e": e, "uncertainty": u, **extra})


def build_node(node_id: str, source_version_id: str, **overrides: object) -> Node:
    payload: dict[str, object] = {
        "node_id": node_id,
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": source_version_id,
        "payload": {"fact": node_id},
        "summary": f"node {node_id}",
        "scores": sv("0.5", "0.4", "0.3", "0.4", confidence="0.6", professional_level=2),
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def build_evidence(evidence_id: str, node_id: str, source_version_id: str, source_uri: str) -> Evidence:
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


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RelearnFixture:
    """A file-backed store with a two-file corpus, one node and four evidence."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.db_path = tmp_path / "soul.db"
        self.corpus = tmp_path / "corpus"
        self.corpus.mkdir()
        (self.corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
        (self.corpus / "b.md").write_text("beta v1\n", encoding="utf-8")
        self.generation = tmp_path / "generation"
        generation = self.generation / "generations" / "gen-1"
        generation.mkdir(parents=True)
        (generation / "graph.json").write_text('{"graph": 1}', encoding="utf-8")
        manifest = {"artifacts": {"graph.json": sha256_of(generation / "graph.json")}}
        (generation / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.generation / "CURRENT").write_text("gen-1", encoding="utf-8")

        self.store = EventStore(self.db_path)
        self.registry = SourceRegistry(self.store)
        self.v1 = self.registry.observe(self.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        self._seed_knowledge()
        self.engine = RelearnEngine(
            self.store,
            self.registry,
            backup_root=tmp_path / "backups",
            generation_dir=self.generation,
            scoring_contract_version="1.0.0",
        )

    def _seed_knowledge(self) -> None:
        self.store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=build_node("n-1", self.v1.source_version_id),
                source_version_id=self.v1.source_version_id,
                occurred_at=NOW,
            ),
            idempotency_key="seed-n1",
        )
        # e1/e2 anchor to b.md (will stay unchanged), e3 to a.md (will change),
        # e4 to a path that never existed in the manifest (unmapped).
        for evidence_id, uri in (("e1", "b.md"), ("e2", "b.md"), ("e3", "a.md"), ("e4", "ghost.md")):
            self.store.append_domain(
                Event(
                    kind=EventKind.EVIDENCE_APPEND,
                    node_id="n-1",
                    domain=PrimaryDomain.P1,
                    evidence=build_evidence(evidence_id, "n-1", self.v1.source_version_id, uri),
                    source_version_id=self.v1.source_version_id,
                    occurred_at=NOW,
                ),
                idempotency_key=f"seed-{evidence_id}",
            )

    def change_source(self) -> None:
        (self.corpus / "a.md").write_text("alpha v2 — changed\n", encoding="utf-8")

    def make_offer(self) -> RelearnOffer:
        self.change_source()
        offer = self.engine.offer(self.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        assert offer is not None
        return offer


@pytest.fixture()
def fx(tmp_path: Path) -> RelearnFixture:
    return RelearnFixture(tmp_path)


def approve(offer_id: str, mode: Literal["RESET", "ACCUMULATE"]) -> RelearnApproval:
    return RelearnApproval(offer_id=offer_id, mode=mode, approved_by="operator", decided_at=NOW)


# ------------------------------------------------------- offer / pending gate


def test_offer_sets_relearn_pending_and_never_chooses_a_mode(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    assert fx.registry.relearn_state() == RELEARN_PENDING
    assert offer.default_action == "NONE"
    assert offer.requires_operator_decision is True
    assert offer.selected_mode is None
    # no operator decision -> no change: the active baseline stays v1
    active = fx.registry.active(SourceKind.SOUL_CORPUS)
    assert active is not None
    assert active.source_version_id == fx.v1.source_version_id


def test_apply_without_pending_offer_refused(fx: RelearnFixture) -> None:
    with pytest.raises(NoPendingRelearn):
        fx.engine.apply("RESET", approve("whatever", "RESET"))


def test_apply_requires_operator_decision(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    with pytest.raises(OperatorDecisionRequired):
        fx.engine.apply("RESET", None)
    with pytest.raises(OperatorDecisionRequired):  # approval for the other mode
        fx.engine.apply("RESET", approve(offer.offer_id, "ACCUMULATE"))
    with pytest.raises(OperatorDecisionRequired):  # approval for another offer
        fx.engine.apply("RESET", approve("other-offer", "RESET"))
    # still pending; nothing applied
    assert fx.registry.relearn_state() == RELEARN_PENDING
    assert fx.engine.quarantined_evidence_ids() == frozenset()


# --------------------------------------------------------------------- RESET


def test_reset_zeroes_evidence_and_blocks_promotion(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
    # fixture rl-002: P=M=E=0, U=1, level 1, promotion blocked, zero carried
    assert receipt.scores_after == {
        "p": "0.000000000000",
        "m": "0.000000000000",
        "e": "0.000000000000",
        "u": "1.000000000000",
        "professional_level": 1,
    }
    assert receipt.promotion_blocked is True
    assert receipt.old_evidence_carried == 0
    assert receipt.carried_evidence_ids == ()
    assert sorted(receipt.quarantined_evidence_ids) == ["e1", "e2", "e3", "e4"]
    # the offer's previous version is superseded by the new one, never deleted
    previous = fx.store.retained_source_version(fx.v1.source_version_id)
    assert previous is not None
    assert previous["source_state"] == SourceState.SUPERSEDED.value
    assert previous["superseded_by"] == receipt.new_source_version_id
    assert fx.registry.relearn_state() != RELEARN_PENDING


# ---------------------------------------------------------------- ACCUMULATE


def test_accumulate_maps_anchors_and_quarantines_unmapped(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    receipt = fx.engine.apply("ACCUMULATE", approve(offer.offer_id, "ACCUMULATE"), at=NOW)
    # fixture rl-003: only evidence whose anchors map deterministically is scored
    assert sorted(receipt.carried_evidence_ids) == ["e1", "e2"]
    assert sorted(receipt.quarantined_evidence_ids) == ["e3", "e4"]
    assert fx.engine.quarantined_evidence_ids() == frozenset({"e3", "e4"})


def test_supersession_and_contradictions_are_retained(fx: RelearnFixture) -> None:
    # a contradiction relation recorded before the relearn
    fx.store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-2",
            domain=PrimaryDomain.P1,
            node=build_node("n-2", fx.v1.source_version_id),
            source_version_id=fx.v1.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key="seed-n2",
    )
    for source, target in (("n-1", "n-2"), ("n-2", "n-1")):
        fx.store.append_domain(
            Event(
                kind=EventKind.RELATION_ADD,
                node_id=source,
                domain=PrimaryDomain.P1,
                relation=Relation(
                    kind=RelationKind.CONTRADICTS, source_node_id=source, target_node_id=target, valid_from=NOW
                ),
                occurred_at=NOW,
            ),
            idempotency_key=f"contradiction-{source}",
        )
    evidence_before = fx.store.evidence_count()

    offer = fx.make_offer()
    fx.engine.apply("ACCUMULATE", approve(offer.offer_id, "ACCUMULATE"), at=NOW)

    # nothing is ever deleted: old evidence and both contradiction directions remain
    assert fx.store.evidence_count() == evidence_before
    assert any(r.kind is RelationKind.CONTRADICTS for r in fx.store.relations_from("n-1"))
    assert any(r.kind is RelationKind.CONTRADICTS for r in fx.store.relations_from("n-2"))
    retained = fx.store.retained_source_version(fx.v1.source_version_id)
    assert retained is not None and retained["source_state"] == SourceState.SUPERSEDED.value


# -------------------------------------------------------------- mixed source


def test_mixed_source_versions_refused_by_default(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    # no stale mixing: evidence from the new (pending) version is incompatible
    # with the still-active baseline while the offer awaits a decision
    with pytest.raises(MixedSourceVersions) as captured:
        fx.engine.assert_source_compatible({offer.current_source_version_id})
    assert "MIXED_SOURCE_VERSIONS" in str(captured.value)


def test_mixed_source_versions_allowed_only_with_explicit_approval(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    fx.engine.assert_source_compatible(
        {offer.current_source_version_id}, mixed_source_versions=True, operator_approval=True
    )
    with pytest.raises(MixedSourceVersions):
        fx.engine.assert_source_compatible({offer.current_source_version_id}, mixed_source_versions=True)
    with pytest.raises(MixedSourceVersions):
        fx.engine.assert_source_compatible({offer.current_source_version_id}, operator_approval=True)


def test_accumulate_keeps_carried_version_compatible(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    fx.engine.apply("ACCUMULATE", approve(offer.offer_id, "ACCUMULATE"), at=NOW)
    compatible = fx.engine.compatible_source_version_ids()
    assert fx.v1.source_version_id in compatible  # approved anchor mapping still holds
    assert offer.current_source_version_id in compatible


def test_reset_breaks_old_version_compatibility(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
    compatible = fx.engine.compatible_source_version_ids()
    assert fx.v1.source_version_id not in compatible
    with pytest.raises(MixedSourceVersions):
        fx.engine.assert_source_compatible({fx.v1.source_version_id})


# ----------------------------------------------------- zero evidence weight


def test_relearn_carries_zero_evidence_weight_and_no_promotion(fx: RelearnFixture) -> None:
    watermark_before = fx.store.knowledge_high_watermark()
    offer = fx.make_offer()
    receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
    assert fx.store.knowledge_high_watermark() == watermark_before  # relearn never moves P/M/E
    assert receipt.knowledge_seq is None
    assert receipt.evidence_weight == 0
    assert receipt.promotion_blocked is True
    # the relearn record itself is a zero-weight admin domain event
    with fx.store.read() as conn:
        row = conn.execute(
            "SELECT knowledge_seq, knowledge_affecting FROM event_log "
            "WHERE kind = 'domain' ORDER BY event_seq DESC LIMIT 1"
        ).fetchone()
    assert row is not None
    assert row["knowledge_seq"] is None
    assert row["knowledge_affecting"] == 0


# -------------------------------------------------------------------- backup


def test_apply_takes_verified_atomic_backup(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)

    backup = receipt.backup
    assert backup.verified is True
    assert backup.scoring_contract_version == "1.0.0"
    kinds = {artifact.kind for artifact in backup.artifacts}
    assert "sqlite" in kinds
    assert "generation_file" in kinds
    assert "current_pointer" in kinds  # the protected set includes the CURRENT pointer
    sqlite_artifact = next(artifact for artifact in backup.artifacts if artifact.kind == "sqlite")
    # the backup directory holds copies whose bytes hash-match the recorded manifest
    backup_dir = fx.tmp_path / "backups" / receipt.relearn_id
    assert (backup_dir / "manifest.json").exists()
    assert sha256_of(backup_dir / "soul.db") == sqlite_artifact.sha256
    assert (backup_dir / "CURRENT").read_text(encoding="utf-8") == "gen-1"
    generation_artifacts = [a for a in backup.artifacts if a.kind == "generation_file"]
    assert generation_artifacts
    for artifact in generation_artifacts:
        assert sha256_of(backup_dir / "generation" / artifact.path) == artifact.sha256


def test_memory_store_cannot_backup(fx: RelearnFixture) -> None:
    memory_store = EventStore(":memory:")
    memory_registry = SourceRegistry(memory_store)
    memory_registry.observe(fx.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    fx.change_source()
    engine = RelearnEngine(memory_store, memory_registry, backup_root=fx.tmp_path / "mem-backups")
    offer = engine.offer(fx.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    assert offer is not None
    with pytest.raises(BackupUnavailable):
        engine.apply("RESET", approve(offer.offer_id, "RESET"))
    assert memory_registry.relearn_state() == RELEARN_PENDING  # nothing applied


def test_backup_manifest_is_staged_before_activation(fx: RelearnFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-review minor: the relearn manifest.json must sit inside the staged
    backup directory *before* the atomic rename activates it (the pre-fix
    pattern) — a post-activation write leaves an active-but-unusable backup
    if the process crashes in between."""
    backup_root = fx.tmp_path / "backups"
    real_replace = os.replace

    def spy_replace(src: object, dst: object) -> None:
        dst_path = Path(str(dst))
        if dst_path.parent == backup_root and not dst_path.name.startswith("."):
            assert (Path(str(src)) / "manifest.json").is_file(), (
                "the relearn manifest must be staged before the backup directory is activated"
            )
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "replace", spy_replace)
    offer = fx.make_offer()
    receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
    backup_dir = backup_root / receipt.relearn_id
    assert (backup_dir / "manifest.json").is_file()  # sanity: activated backup is complete


def test_backup_crash_leaves_no_activated_backup_without_manifest(
    fx: RelearnFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the protected-set copy but before activation must leave
    no activated backup directory lacking its manifest (fail-closed debris in
    a hidden staging dir is acceptable; a live partial backup is not)."""
    from know_your_soul import relearn as relearn_module

    real_helper = relearn_module.backup_protected_artifacts

    def crash_after_copy(*args: object, **kwargs: object) -> object:
        receipt = real_helper(*args, **kwargs)  # type: ignore[arg-type]
        raise BackupError(f"simulated crash after staging {receipt.backup_path}")

    monkeypatch.setattr(relearn_module, "backup_protected_artifacts", crash_after_copy)
    offer = fx.make_offer()
    with pytest.raises(BackupError, match="simulated crash"):
        fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
    backup_root = fx.tmp_path / "backups"
    activated = (
        [child for child in backup_root.iterdir() if not child.name.startswith(".")] if backup_root.exists() else []
    )
    for child in activated:
        assert (child / "manifest.json").is_file(), f"activated backup {child.name} has no manifest"


# ------------------------------------------------------------------ rollback


def test_rollback_restores_exact_pre_relearn_state(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    pre_graph_bytes = (fx.generation / "generations" / "gen-1" / "graph.json").read_bytes()
    pre_audit_count = fx.store.audit_event_count()
    node_before = fx.store.current_node("n-1")
    assert node_before is not None

    receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
    # post-apply the world moved on: old version superseded, graph reprojected, pointer moved
    (fx.generation / "generations" / "gen-1" / "graph.json").write_text('{"graph": 2}', encoding="utf-8")
    (fx.generation / "CURRENT").write_text("gen-2", encoding="utf-8")

    rollback = fx.engine.rollback(receipt.relearn_id, actor="operator")
    assert rollback.verified is True
    sqlite_artifact = next(a for a in receipt.backup.artifacts if a.kind == "sqlite")
    assert rollback.restored_db_sha256 == sqlite_artifact.sha256  # byte-exact restore of the verified backup

    reopened = EventStore(fx.db_path)
    try:
        # exact pre-relearn graph bytes and CURRENT pointer
        assert (fx.generation / "generations" / "gen-1" / "graph.json").read_bytes() == pre_graph_bytes
        assert (fx.generation / "CURRENT").read_text(encoding="utf-8") == "gen-1"
        # exact pre-relearn source-version states (pending offer restored too)
        previous = reopened.retained_source_version(fx.v1.source_version_id)
        assert previous is not None
        assert previous["source_state"] == SourceState.CURRENT.value
        assert previous["superseded_by"] is None
        assert SourceRegistry(reopened).relearn_state() == RELEARN_PENDING
        # exact pre-relearn P/M/E values
        restored_node = reopened.current_node("n-1")
        assert restored_node is not None
        assert restored_node.scores.model_dump() == node_before.scores.model_dump()
        # exactly one linked audit event
        assert reopened.audit_event_count() == pre_audit_count + 1
        with reopened.read() as conn:
            row = conn.execute(
                "SELECT payload_json FROM event_log WHERE kind = 'audit' ORDER BY event_seq DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert receipt.relearn_id in str(row["payload_json"])
    finally:
        reopened.close()


def test_rollback_twice_refused(fx: RelearnFixture) -> None:
    offer = fx.make_offer()
    receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
    fx.engine.rollback(receipt.relearn_id)
    with pytest.raises(RelearnError):
        fx.engine.rollback(receipt.relearn_id)


def test_rollback_unknown_relearn_raises(fx: RelearnFixture) -> None:
    with pytest.raises(UnknownRelearn):
        fx.engine.rollback("no-such-relearn")


# ------------------------------------------------------------------- preview


def test_preview_is_read_only_and_matches_apply(fx: RelearnFixture) -> None:
    fx.make_offer()
    events_before = fx.store.event_count()

    reset = fx.engine.preview("RESET")
    assert reset.evidence_carried == 0
    assert reset.evidence_quarantined == 4
    assert reset.scores_after is not None
    assert reset.scores_after["professional_level"] == 1

    accumulate = fx.engine.preview("ACCUMULATE")
    assert sorted(accumulate.carried_evidence_ids) == ["e1", "e2"]
    assert sorted(accumulate.quarantined_evidence_ids) == ["e3", "e4"]

    assert fx.store.event_count() == events_before
    assert fx.registry.relearn_state() == RELEARN_PENDING


def test_preview_without_pending_offer_refused(fx: RelearnFixture) -> None:
    with pytest.raises(NoPendingRelearn):
        fx.engine.preview("RESET")


def test_compatible_versions_empty_without_active_baseline(tmp_path: Path) -> None:
    store = EventStore(":memory:")
    engine = RelearnEngine(store, SourceRegistry(store), backup_root=tmp_path / "b")
    assert engine.compatible_source_version_ids() == frozenset()


def test_relearn_record_keeps_domain_replay_parseable(fx: RelearnFixture) -> None:
    """Spec 7.10: rebuild-from-events must survive a relearn record in the log."""
    offer = fx.make_offer()
    fx.engine.apply("ACCUMULATE", approve(offer.offer_id, "ACCUMULATE"), at=NOW)
    events = list(fx.store.replay_domain())
    assert events  # no ValidationError on the relearn admin record
    relearn_events = [event for event in events if event.node_id and event.payload.get("relearn")]
    assert len(relearn_events) == 1
    assert relearn_events[0].knowledge_affecting is False
