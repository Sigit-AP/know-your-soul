"""Service-level relearn lifecycle API (final flow audit F-4, second half).

The relearn happy path — offer → preview → approve/apply — was unreachable
from every shipped surface: the offer id an approval must name could not be
retrieved, and ``RelearnApproval`` could not be presented anywhere. The
service now exposes a clean, audited API that adapters (CLI/HTTP/MCP,
SUB-42) can bind directly:

* ``relearn_status()`` — the gate state, the pending offer (with its id) and
  the active baseline.
* ``offer_relearn(path, kind, parser_versions)`` — change detection.
* ``preview_relearn(mode)`` — the read-only deterministic preview.
* ``apply_relearn(mode, approval)`` — the operator-gated apply.

All four are audited invocations; failures are typed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from soul_knowledge.contracts import LifecycleError, OperatorDecisionRequiredError
from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, ScoreVector
from soul_knowledge.importer import DEFAULT_PARSER_VERSIONS
from soul_knowledge.projection import GraphProjector
from soul_knowledge.relearn import RelearnApproval, RelearnReceipt
from soul_knowledge.service import KnowledgeService
from soul_knowledge.source_registry import RELEARN_PENDING, RelearnOffer, SourceKind
from soul_knowledge.store import EventStore

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

A_V1 = b"# Alpha\n\n## Lesson One\n\nRoll back the database before retrying.\n"
A_V2 = (
    b"# Alpha\n\n## Lesson One\n\nRoll back the database before retrying.\n"
    b"\n## Lesson Two\n\nVerify the chain after every restore.\n"
)


def _service(tmp_path: Path) -> tuple[EventStore, KnowledgeService, Path]:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    service = KnowledgeService(store, projector)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_bytes(A_V1)
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-1",
            domain=PrimaryDomain.P1,
            node=Node(
                node_id="n-1",
                kind=NodeKind.CHILD,
                domain=PrimaryDomain.P1,
                summary="lesson one",
                provenance=("soul://corpus/a.md",),
                source_version_id="sv-test",
                scores=ScoreVector.zero(),
            ),
        ),
        idempotency_key="seed:n-1",
    )
    projector.rebuild()
    return store, service, corpus


def test_relearn_status_reports_the_gate(tmp_path: Path) -> None:
    _, service, _ = _service(tmp_path)
    status = service.relearn_status()
    assert status.state is None
    assert status.pending_offer is None


def test_offer_preview_apply_happy_path(tmp_path: Path) -> None:
    store, service, corpus = _service(tmp_path)

    # Baseline, then a change → offer + RELEARN_PENDING, retrievable offer id.
    assert service.offer_relearn(corpus, SourceKind.SOUL_CORPUS, DEFAULT_PARSER_VERSIONS) is None
    (corpus / "a.md").write_bytes(A_V2)
    offer = service.offer_relearn(corpus, SourceKind.SOUL_CORPUS, DEFAULT_PARSER_VERSIONS)
    assert isinstance(offer, RelearnOffer)
    assert offer.selected_mode is None

    status = service.relearn_status()
    assert status.state == RELEARN_PENDING
    assert status.pending_offer is not None
    assert status.pending_offer.offer_id == offer.offer_id

    # Read-only previews for both modes name the same offer.
    reset = service.preview_relearn("RESET")
    accumulate = service.preview_relearn("ACCUMULATE")
    assert reset.offer_id == offer.offer_id
    assert accumulate.offer_id == offer.offer_id
    assert reset.promotion_blocked is True

    # A mismatched approval fails typed; the gate stays set.
    with pytest.raises(OperatorDecisionRequiredError):
        service.apply_relearn(
            "RESET",
            RelearnApproval(offer_id="wrong", mode="RESET", approved_by="operator", decided_at=NOW),
        )
    assert service.relearn_status().state == RELEARN_PENDING

    receipt = service.apply_relearn(
        "RESET",
        RelearnApproval(offer_id=offer.offer_id, mode="RESET", approved_by="operator", decided_at=NOW),
    )
    assert isinstance(receipt, RelearnReceipt)
    assert receipt.offer_id == offer.offer_id
    assert receipt.mode == "RESET"
    assert receipt.promotion_blocked is True
    assert receipt.backup.verified is True
    assert service.relearn_status().state == "CLEAR"
    assert store.verify_chain().ok


def test_preview_without_pending_offer_is_typed(tmp_path: Path) -> None:
    _, service, _ = _service(tmp_path)
    with pytest.raises(LifecycleError, match="NoPendingRelearn|no RELEARN_PENDING"):
        service.preview_relearn("RESET")


def test_apply_without_pending_offer_is_typed(tmp_path: Path) -> None:
    _, service, _ = _service(tmp_path)
    with pytest.raises(LifecycleError, match="NoPendingRelearn|no RELEARN_PENDING"):
        service.apply_relearn(
            "RESET",
            RelearnApproval(offer_id="any", mode="RESET", approved_by="operator", decided_at=NOW),
        )


def test_relearn_api_calls_are_audited(tmp_path: Path) -> None:
    store, service, corpus = _service(tmp_path)
    service.relearn_status()
    service.offer_relearn(corpus, SourceKind.SOUL_CORPUS, DEFAULT_PARSER_VERSIONS)
    (corpus / "a.md").write_bytes(A_V2)
    service.offer_relearn(corpus, SourceKind.SOUL_CORPUS, DEFAULT_PARSER_VERSIONS)
    service.preview_relearn("RESET")
    with store.read() as conn:
        rows = conn.execute(
            "SELECT operation, status FROM invocation_journal WHERE operation LIKE 'knowledge.relearn%' "
            "ORDER BY started_at"
        ).fetchall()
    operations = [str(row["operation"]) for row in rows]
    assert "knowledge.relearn_status" in operations
    assert "knowledge.relearn_offer" in operations
    assert "knowledge.relearn_preview" in operations
    assert all(str(row["status"]) == "complete" for row in rows)
