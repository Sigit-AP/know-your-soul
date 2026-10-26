"""Regression probes for audit wave-1 findings F1, F2 and F3 (SUB-11).

Report: ``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave1-security.md``.

* F3 (launch-blocking, store.py): an idempotency key reused for a
  *different* request must raise :class:`IdempotencyConflict` (fail loud),
  never silently drop the write behind ``idempotent_replay=True``. A genuine
  retry (same semantic payload under the same key) still replays.
* F2 (fast-follow, relearn.py): an idempotent retry of ``apply`` after a
  success returns the original :class:`RelearnReceipt`; it must not raise
  ``NoPendingRelearn`` once the pending offer cleared.
* F1 (regression probe only — the ``evidence_scope`` wiring landed in
  3a90f51): after RESET quarantines the old evidence, ``service.evaluate``
  returns the frozen zero vector (P=M=E=0, U=1, level 1) and no
  recommendation promotes the quarantined node.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pytest

from know_your_soul.contracts import EvaluateRequest, ReviewRequest
from know_your_soul.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    ScoreVector,
)
from know_your_soul.projection import GraphProjector
from know_your_soul.relearn import RelearnApproval, RelearnEngine
from know_your_soul.service import KnowledgeService
from know_your_soul.source_registry import ParserVersions, SourceKind, SourceRegistry
from know_your_soul.store import EventStore, IdempotencyConflict

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

PARSER_VERSIONS = ParserVersions(
    parser_version="1.0.0",
    normalization_version="1.0.0",
    engine_version="0.9.95",
    scoring_contract_version="1.0.0",
)


def build_node(node_id: str, source_version_id: str | None = "sv-test", *, summary: str | None = None) -> Node:
    return Node.model_validate(
        {
            "node_id": node_id,
            "kind": NodeKind.LEAF,
            "domain": PrimaryDomain.P1,
            "source_version_id": source_version_id,
            "payload": {"fact": node_id},
            "summary": summary or f"node {node_id}",
            "scores": ScoreVector.zero().model_dump(mode="json"),
            "created_at": NOW,
            "updated_at": NOW,
            "version": 1,
        }
    )


def build_evidence(evidence_id: str, node_id: str, source_version_id: str | None, source_uri: str) -> Evidence:
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


def node_event(node: Node, *, source_version_id: str | None = None) -> Event:
    return Event(
        kind=EventKind.NODE_UPSERT,
        node_id=node.node_id,
        domain=node.domain,
        node=node,
        source_version_id=source_version_id,
        occurred_at=NOW,
    )


def evidence_event(evidence: Evidence) -> Event:
    return Event(
        kind=EventKind.EVIDENCE_APPEND,
        node_id=evidence.node_id,
        domain=PrimaryDomain.P1,
        evidence=evidence,
        source_version_id=evidence.source_version_id,
        occurred_at=NOW,
    )


# --------------------------------------------------------------------- F3


def test_f3_reused_key_with_different_payload_fails_loud() -> None:
    """The audit P3 probe: upsert then evidence-append under one shared key."""
    store = EventStore(":memory:")
    first = store.append_domain(node_event(build_node("n-1")), idempotency_key="shared-key")
    assert first.idempotent_replay is False

    with pytest.raises(IdempotencyConflict):
        store.append_domain(
            evidence_event(build_evidence("ev-1", "n-1", "sv-test", "soul://corpus/a.md")),
            idempotency_key="shared-key",
        )
    # The conflicting write was refused loud — nothing was silently dropped or replayed.
    assert store.get_evidence("ev-1") is None


def test_f3_reused_key_with_different_node_fails_loud() -> None:
    store = EventStore(":memory:")
    store.append_domain(node_event(build_node("n-1")), idempotency_key="k1")
    with pytest.raises(IdempotencyConflict):
        store.append_domain(node_event(build_node("n-2")), idempotency_key="k1")
    assert store.current_node("n-2") is None


def test_f3_same_key_same_payload_still_replays() -> None:
    """Existing behaviour preserved: a genuine retry returns the original receipt."""
    store = EventStore(":memory:")
    original = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1")
    # A retry rebuilds the Event: fresh event_id/occurred_at, same content.
    retry = store.append_domain(node_event(build_node("n-1")), idempotency_key="k1")
    assert retry.idempotent_replay is True
    assert retry.domain_event_id == original.domain_event_id
    assert retry.event_seq == original.event_seq
    assert store.verify_chain().ok


# --------------------------------------------------------------------- F2


class _RelearnFixture:
    """File-backed store + two-file corpus + one node with two evidence rows."""

    def __init__(self, tmp_path: Path) -> None:
        self.corpus = tmp_path / "corpus"
        self.corpus.mkdir()
        (self.corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
        (self.corpus / "b.md").write_text("beta v1\n", encoding="utf-8")
        self.store = EventStore(tmp_path / "soul.db")
        self.registry = SourceRegistry(self.store)
        self.v1 = self.registry.observe(self.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        self.store.append_domain(
            node_event(build_node("n-1", self.v1.source_version_id), source_version_id=self.v1.source_version_id),
            idempotency_key="seed-n1",
        )
        for evidence_id in ("e1", "e2"):
            self.store.append_domain(
                evidence_event(build_evidence(evidence_id, "n-1", self.v1.source_version_id, "a.md")),
                idempotency_key=f"seed-{evidence_id}",
            )
        self.engine = RelearnEngine(
            self.store,
            self.registry,
            backup_root=tmp_path / "backups",
            generation_dir=tmp_path / "generation",
            scoring_contract_version="1.0.0",
        )

    def make_offer(self):  # noqa: ANN202 - test helper
        (self.corpus / "a.md").write_text("alpha v2 — changed\n", encoding="utf-8")
        offer = self.engine.offer(self.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        assert offer is not None
        return offer


def approve(offer_id: str, mode: Literal["RESET", "ACCUMULATE"]) -> RelearnApproval:
    return RelearnApproval(offer_id=offer_id, mode=mode, approved_by="operator", decided_at=NOW)


def test_f2_apply_retry_after_success_returns_the_original_receipt(tmp_path: Path) -> None:
    fx = _RelearnFixture(tmp_path)
    offer = fx.make_offer()
    first = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW, idempotency_key="apply-1")
    assert first.idempotent_replay is False

    # The pending offer cleared on apply; a retry with the same key must
    # replay the original receipt, not raise NoPendingRelearn (spec 7.9).
    retry = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), idempotency_key="apply-1")
    assert retry.idempotent_replay is True
    assert retry.relearn_id == first.relearn_id
    assert retry.offer_id == first.offer_id
    assert retry.mode == first.mode
    assert retry.quarantined_evidence_ids == first.quarantined_evidence_ids
    assert retry.carried_evidence_ids == first.carried_evidence_ids
    assert retry.scores_after == first.scores_after
    # The retry is its own audited invocation (exactly one final audit each).
    assert retry.audit_event_id != first.audit_event_id
    assert fx.store.verify_chain().ok


def test_f2_retry_with_a_different_mode_under_one_key_fails_loud(tmp_path: Path) -> None:
    fx = _RelearnFixture(tmp_path)
    offer = fx.make_offer()
    fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW, idempotency_key="apply-1")
    with pytest.raises(IdempotencyConflict):
        fx.engine.apply("ACCUMULATE", approve(offer.offer_id, "ACCUMULATE"), idempotency_key="apply-1")


# --------------------------------------------------------------------- F1


def test_f1_reset_quarantined_evidence_is_excluded_from_scoring_and_recommendations(tmp_path: Path) -> None:
    """End-to-end at HEAD: real RESET through the engine, reads via the service."""
    store = EventStore(tmp_path / "soul.db")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    service = KnowledgeService(store, projector)
    registry = SourceRegistry(store)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
    v1 = registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    store.append_domain(
        node_event(build_node("n-1", v1.source_version_id), source_version_id=v1.source_version_id),
        idempotency_key="seed-n1",
    )
    for evidence_id in ("e1", "e2"):
        store.append_domain(
            evidence_event(build_evidence(evidence_id, "n-1", v1.source_version_id, "a.md")),
            idempotency_key=f"seed-{evidence_id}",
        )

    before = service.evaluate(EvaluateRequest(node_id="n-1", at=NOW, include_descendants=False))
    assert Decimal(before.p) > 0, "pre-RESET baseline must score the old evidence"

    engine = RelearnEngine(store, registry, backup_root=tmp_path / "backups", scoring_contract_version="1.0.0")
    (corpus / "a.md").write_text("alpha v2 — changed\n", encoding="utf-8")
    offer = engine.offer(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    assert offer is not None
    receipt = engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
    assert sorted(receipt.quarantined_evidence_ids) == ["e1", "e2"]

    # Frozen relearn contract: RESET restarts from zero old evidence.
    after = service.evaluate(EvaluateRequest(node_id="n-1", at=NOW, include_descendants=False))
    assert after.p == "0.000000000000"
    assert after.m == "0.000000000000"
    assert after.e == "0.000000000000"
    assert after.overall_uncertainty == "1.000000000000"
    assert after.professional_level == 1
    assert after.promotable is False

    # Recommendations never promote a node whose evidence is quarantined.
    review = service.review(ReviewRequest(node_id="n-1", at=NOW))
    assert review.recommendations.best_actions == ()
    items = (
        *review.recommendations.best_actions,
        *review.recommendations.recommendations,
        *review.recommendations.unnecessary_actions,
    )
    assert all(not item.promoted for item in items)
    quarantined = set(receipt.quarantined_evidence_ids)
    promoted_items = [item for item in items if item.promoted]
    assert not any(quarantined & set(item.evidence_ids) for item in promoted_items)
