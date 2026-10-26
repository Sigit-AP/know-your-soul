"""Regression probes for the offer-gated update pipeline (final flow audit F-4).

Spec 13.3: when the corpus changes, the system emits a deterministic
``RelearnOffer`` and sets ``RELEARN_PENDING`` — *no operator decision means
no change*. The watcher-driven pipeline (spec 13.5) used to bypass that
gate: it re-imported through ``registry.observe``, flipping the active
baseline silently with no offer and no pending state.

The pipeline is now split at the gate:

* ``update_from_changes`` — the watcher path. It only *detects*: every
  change batch routes through ``SourceRegistry.detect_change``, producing
  the offer and ``RELEARN_PENDING`` while the operator-approved baseline,
  the store and the projection stay untouched.
* ``apply_approved_update`` — the post-decision executor. It refuses to run
  while a decision is pending (typed ``UpdatePipelineError``); once the
  operator has approved and the relearn engine applied the mode, it retires
  the removed sources, re-imports and rebuilds (shrink scoped to the
  pipeline, as before).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soul_knowledge.domain import PrimaryDomain
from soul_knowledge.fragment_generator import file_scoped_id
from soul_knowledge.importer import SeedImporter
from soul_knowledge.projection import GraphProjector, ProjectorBusy
from soul_knowledge.relearn import RelearnApproval, RelearnEngine
from soul_knowledge.source_registry import RELEARN_PENDING, SourceKind
from soul_knowledge.source_watcher import ChangeKind, SourceChange, SourceWatcher
from soul_knowledge.store import EventStore
from soul_knowledge.update_pipeline import (
    UpdatePipelineError,
    apply_approved_update,
    update_from_changes,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

ALPHA = (
    b"# Alpha Lessons\n"
    b"\n"
    b"## First Lesson\n"
    b"\n"
    b"Always roll the database back before retrying.\n"
    b"\n"
    b"## Second Lesson\n"
    b"\n"
    b"See [[beta]] for the counterexample.\n"
)
BETA = b"# Beta\n\n## Counterexample\n\nA rollback that was itself rolled back.\n"


def _populated(tmp_path: Path) -> tuple[EventStore, SeedImporter, GraphProjector]:
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    projector = GraphProjector(store, output_root=tmp_path / "out")
    projector.rebuild()
    return store, importer, projector


def _graph_ids(tmp_path: Path) -> set[str]:
    pointer = (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8").strip()
    graph = json.loads((tmp_path / "out" / "generations" / pointer / "graph.json").read_text(encoding="utf-8"))
    return {str(node["id"]) for node in graph["nodes"]}


def _poll(watcher: SourceWatcher) -> list[SourceChange]:
    return watcher.poll()


def _watcher(tmp_path: Path) -> SourceWatcher:
    watcher = SourceWatcher(tmp_path / "corpus", state_path=tmp_path / "watch-state.json")
    watcher.poll()  # baseline snapshot
    return watcher


# ------------------------------------------------------- detection is gated


def test_changed_source_emits_offer_and_holds_the_baseline(tmp_path: Path) -> None:
    store, importer, projector = _populated(tmp_path)
    registry = importer._registry  # noqa: SLF001 - pipeline wiring
    # The corpus baseline exists once detection has seen it.
    assert update_from_changes(
        [SourceChange(path="alpha.md", kind=ChangeKind.CHANGED, content_sha256="0" * 64)],
        importer=importer,
        root=tmp_path / "corpus",
    ).baseline_recorded

    baseline = registry.active(SourceKind.SOUL_CORPUS)
    assert baseline is not None
    watermark = store.knowledge_high_watermark()
    graph_before = _graph_ids(tmp_path)

    watcher = _watcher(tmp_path)
    (tmp_path / "corpus" / "alpha.md").write_bytes(ALPHA + b"\n## Third Lesson\n\nA new lesson.\n")
    changes = _poll(watcher)
    receipt = update_from_changes(changes, importer=importer, root=tmp_path / "corpus")

    # The offer is emitted and the gate is set...
    assert receipt.offer is not None
    assert receipt.relearn_pending is True
    assert receipt.offer.default_action == "NONE"
    assert receipt.offer.requires_operator_decision is True
    assert registry.relearn_state() == RELEARN_PENDING
    # ...but nothing else moved: baseline, store and projection are untouched.
    assert registry.active(SourceKind.SOUL_CORPUS) == baseline
    assert store.knowledge_high_watermark() == watermark
    assert _graph_ids(tmp_path) == graph_before
    assert projector.active_manifest() is not None


def test_removed_source_is_offer_gated_not_retired(tmp_path: Path) -> None:
    store, importer, _ = _populated(tmp_path)
    update_from_changes(
        [SourceChange(path="alpha.md", kind=ChangeKind.ADDED, content_sha256="0" * 64)],
        importer=importer,
        root=tmp_path / "corpus",
    )
    alpha_root = file_scoped_id("alpha.md")
    version_before = store.node_version(alpha_root)

    watcher = _watcher(tmp_path)
    (tmp_path / "corpus" / "alpha.md").unlink()
    changes = _poll(watcher)
    assert [change.kind for change in changes] == [ChangeKind.REMOVED]
    receipt = update_from_changes(changes, importer=importer, root=tmp_path / "corpus")

    assert receipt.offer is not None
    assert "alpha.md" in receipt.offer.diff.removed
    assert receipt.relearn_pending is True
    # No decision yet: the nodes stay live, no retire event was written.
    assert store.node_version(alpha_root) == version_before
    assert alpha_root in _graph_ids(tmp_path)


def test_replayed_change_batch_is_a_noop(tmp_path: Path) -> None:
    store, importer, _ = _populated(tmp_path)
    update_from_changes(
        [SourceChange(path="alpha.md", kind=ChangeKind.ADDED, content_sha256="0" * 64)],
        importer=importer,
        root=tmp_path / "corpus",
    )
    watcher = _watcher(tmp_path)
    (tmp_path / "corpus" / "alpha.md").write_bytes(ALPHA + b"\n## Third Lesson\n\nA new lesson.\n")
    changes = _poll(watcher)
    first = update_from_changes(changes, importer=importer, root=tmp_path / "corpus")
    assert first.offer is not None
    offers = store.event_count()

    replayed = update_from_changes(changes, importer=importer, root=tmp_path / "corpus")
    assert replayed.offer is None  # fingerprint unchanged: detection is idempotent
    assert replayed.relearn_pending is True
    assert store.event_count() == offers


# ---------------------------------------------------- post-decision executor


def test_apply_approved_update_refused_while_decision_pending(tmp_path: Path) -> None:
    _, importer, projector = _populated(tmp_path)
    update_from_changes(
        [SourceChange(path="alpha.md", kind=ChangeKind.ADDED, content_sha256="0" * 64)],
        importer=importer,
        root=tmp_path / "corpus",
    )
    watcher = _watcher(tmp_path)
    (tmp_path / "corpus" / "alpha.md").write_bytes(ALPHA + b"\n## Third Lesson\n\nA new lesson.\n")
    update_from_changes(_poll(watcher), importer=importer, root=tmp_path / "corpus")

    with pytest.raises(UpdatePipelineError, match="operator|pending"):
        apply_approved_update(importer=importer, projector=projector, root=tmp_path / "corpus", domain=PrimaryDomain.P1)


def test_apply_approved_update_after_operator_decision(tmp_path: Path) -> None:
    store, importer, projector = _populated(tmp_path)
    registry = importer._registry  # noqa: SLF001 - pipeline wiring
    engine = RelearnEngine(store, registry, generation_dir=tmp_path / "out")
    update_from_changes(
        [SourceChange(path="alpha.md", kind=ChangeKind.ADDED, content_sha256="0" * 64)],
        importer=importer,
        root=tmp_path / "corpus",
    )

    watcher = _watcher(tmp_path)
    (tmp_path / "corpus" / "alpha.md").unlink()
    receipt = update_from_changes(_poll(watcher), importer=importer, root=tmp_path / "corpus")
    assert receipt.offer is not None
    alpha_root = file_scoped_id("alpha.md")

    engine.apply(
        "RESET",
        RelearnApproval(offer_id=receipt.offer.offer_id, mode="RESET", approved_by="operator", decided_at=NOW),
    )
    applied = apply_approved_update(
        importer=importer, projector=projector, root=tmp_path / "corpus", domain=PrimaryDomain.P1
    )

    assert alpha_root in set(applied.retired_node_ids)
    assert applied.projection is not None and applied.projection.activated is True
    assert alpha_root not in _graph_ids(tmp_path)
    assert file_scoped_id("beta.md") in _graph_ids(tmp_path)


def test_apply_approved_update_retries_a_busy_rebuild(tmp_path: Path) -> None:
    store, importer, projector = _populated(tmp_path)
    registry = importer._registry  # noqa: SLF001 - pipeline wiring
    engine = RelearnEngine(store, registry, generation_dir=tmp_path / "out")
    update_from_changes(
        [SourceChange(path="alpha.md", kind=ChangeKind.ADDED, content_sha256="0" * 64)],
        importer=importer,
        root=tmp_path / "corpus",
    )
    watcher = _watcher(tmp_path)
    (tmp_path / "corpus" / "alpha.md").write_bytes(ALPHA + b"\n## Third Lesson\n\nA new lesson.\n")
    receipt = update_from_changes(_poll(watcher), importer=importer, root=tmp_path / "corpus")
    assert receipt.offer is not None
    engine.apply(
        "RESET",
        RelearnApproval(offer_id=receipt.offer.offer_id, mode="RESET", approved_by="operator", decided_at=NOW),
    )

    real_rebuild = projector.rebuild
    calls = {"count": 0}

    def busy_twice(**kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] <= 2:
            raise ProjectorBusy("another projector holds the rebuild lock for this corpus root")
        return real_rebuild(**kwargs)  # type: ignore[arg-type]

    projector.rebuild = busy_twice  # type: ignore[method-assign]
    applied = apply_approved_update(
        importer=importer, projector=projector, root=tmp_path / "corpus", domain=PrimaryDomain.P1
    )
    projector.rebuild = real_rebuild  # type: ignore[method-assign]

    assert calls["count"] == 3, "two busy signals then one successful rebuild"
    assert applied.projection is not None and applied.projection.activated is True
