"""Wave-3 audit residuals: W3-1 (relearn rollback wedges the shared store) and
W3-10 (FragmentCache.prune dead in production).

W3-1: ``RelearnEngine.rollback`` used to close the *shared* ``EventStore``,
byte-replace the database, and rebind a fresh handle on the engine alone —
every other holder (service, projector, audit, lifecycle engines) was left on
a closed connection, so ``service.rollback(relearn_id=...)`` committed durably
and then raised an untyped ``sqlite3.ProgrammingError`` (HTTP 500 on success),
wedging every subsequent operation until restart. The fix reopens the shared
store **in place**, so the one object every holder references becomes valid
again. These tests drive the service surface directly — no manual reopen.

W3-10: ``FragmentCache.prune`` is never called from production code; the class
itself is never instantiated in ``src/``. Decision (docs/recovery/
wave3-W3-10-fragment-cache-prune.md): keep the cache unwired for now and guard
the dangerous middle state — a production ``FragmentCache`` with no ``prune``
call would grow unboundedly across corpus edits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from know_your_soul.contracts import KnowledgeError, QueryRequest
from know_your_soul.domain import Event, EventKind, PrimaryDomain
from know_your_soul.relearn import RelearnError
from know_your_soul.source_registry import RELEARN_PENDING, SourceKind, SourceRegistry
from test_relearn import NOW, PARSER_VERSIONS, RelearnFixture, approve, build_evidence
from test_service import _child, _relate, _roots, _stack, _upsert


def _corpus(root: Path) -> Path:
    corpus = root / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
    (corpus / "b.md").write_text("beta v1\n", encoding="utf-8")
    return corpus


def test_service_rollback_relearn_typed_success_and_service_stays_live(tmp_path: Path) -> None:
    """W3-1 happy path through the shipped surface: typed success, then the
    same service instance keeps serving — no manual store reopen anywhere."""
    corpus = _corpus(tmp_path)
    store, projector, service = _stack(tmp_path / "svc")
    registry = SourceRegistry(store)
    v1 = registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    _roots(store)
    _upsert(store, _child("n-gates", summary="Run the gates before commit", tags=("gates", "discipline")))
    _relate(store, "P1", "n-gates", "parent_of")
    store.append_domain(
        Event(
            kind=EventKind.EVIDENCE_APPEND,
            node_id="n-gates",
            domain=PrimaryDomain.P1,
            evidence=build_evidence("e1", "n-gates", v1.source_version_id, "b.md"),
            source_version_id=v1.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key="seed-e1",
    )
    projector.rebuild()

    engine = service._relearn_engine  # noqa: SLF001 - the service's own engine is the shipped wiring
    (corpus / "a.md").write_text("alpha v2 — changed\n", encoding="utf-8")
    offer = engine.offer(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    assert offer is not None
    receipt = engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)

    response = service.rollback(relearn_id=receipt.relearn_id, actor="operator")
    assert response.kind == "relearn"
    assert response.status == "restored"
    assert response.verified is True
    assert response.audit_event_id
    sqlite_artifact = next(a for a in receipt.backup.artifacts if a.kind == "sqlite")
    assert response.restored_db_sha256 == sqlite_artifact.sha256  # byte-exact restore

    # The same service instance (and the same shared store object) keeps working.
    node = service.get_node("n-gates")
    assert node.node.node_id == "n-gates"
    queried = service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
    assert "n-gates" in {result.node_id for result in queried.results}
    assert store.current_node("n-gates") is not None

    # A second rollback is refused with a typed error, not an untyped crash.
    with pytest.raises(KnowledgeError):
        service.rollback(relearn_id=receipt.relearn_id, actor="operator")


def test_relearn_rollback_heals_the_shared_store_in_place(tmp_path: Path) -> None:
    """W3-1 engine level: after rollback, every holder of the original store
    object is usable again without constructing a new EventStore."""
    fx = RelearnFixture(tmp_path)
    offer = fx.make_offer()
    receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)

    rollback = fx.engine.rollback(receipt.relearn_id, actor="operator")
    assert rollback.verified is True

    # Same object the whole stack shares — no manual reopen.
    restored = fx.store.current_node("n-1")
    assert restored is not None
    assert SourceRegistry(fx.store).relearn_state() == RELEARN_PENDING
    with pytest.raises(RelearnError):
        fx.engine.rollback(receipt.relearn_id)


def test_fragment_cache_never_wired_without_prune() -> None:
    """W3-10 guard: a production ``FragmentCache`` is acceptable exactly two
    ways — unwired (today's state) or wired *with* a production ``prune`` call.
    The forbidden middle state (instantiated, never pruned) grows unboundedly
    across corpus edits. Fails the moment anyone wires the cache without
    bounding it."""
    src_root = Path(__file__).resolve().parent.parent / "src" / "know_your_soul"
    sources = {path: path.read_text(encoding="utf-8") for path in sorted(src_root.rglob("*.py"))}
    assert sources, "guard must see real sources"
    instantiated = [path.name for path, text in sources.items() if "FragmentCache(" in text]
    pruned = [path.name for path, text in sources.items() if ".prune(" in text]
    if instantiated:
        assert pruned, (
            f"FragmentCache is instantiated in production ({instantiated}) without any "
            "production prune() call — the cache grows unboundedly (W3-10)"
        )
