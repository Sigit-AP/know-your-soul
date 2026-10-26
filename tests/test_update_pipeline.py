"""Regression probes for the deleted-source retire path (Task 3 fix round 1, finding F2).

A ``REMOVED`` watcher change must retire every node imported from the
deleted source through ``node.retire`` domain events (idempotency-keyed, so a
replayed batch is a no-op), then rebuild with ``allow_shrink=True`` scoped to
the retire pipeline. Retired nodes disappear from the next projection and
stay in the append-only store history as superseded revisions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from soul_knowledge.domain import EventKind, LifecycleStatus, PrimaryDomain
from soul_knowledge.fragment_generator import file_scoped_id
from soul_knowledge.importer import SeedImporter, injective_section_key
from soul_knowledge.projection import GraphProjector, ProjectorBusy, ShrinkGuardError
from soul_knowledge.source_watcher import ChangeKind, SourceChange, SourceWatcher
from soul_knowledge.store import EventStore
from soul_knowledge.update_pipeline import UpdateReceipt, update_from_changes

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


def _watched(
    tmp_path: Path,
    store: EventStore,
    importer: SeedImporter,
    projector: GraphProjector,
) -> tuple[SourceWatcher, list[UpdateReceipt]]:
    receipts: list[UpdateReceipt] = []

    def _update(changes: list[SourceChange]) -> None:
        receipts.append(
            update_from_changes(
                changes,
                importer=importer,
                projector=projector,
                root=tmp_path / "corpus",
                domain=PrimaryDomain.P1,
            )
        )

    watcher = SourceWatcher(tmp_path / "corpus", state_path=tmp_path / "watch-state.json", on_change=_update)
    watcher.poll()  # baseline snapshot; import is unchanged so this only re-derives
    receipts.clear()
    return watcher, receipts


def test_removed_source_is_retired_and_leaves_the_projection(tmp_path: Path) -> None:
    store, importer, projector = _populated(tmp_path)
    alpha_root = file_scoped_id("alpha.md")
    alpha_lesson = f"{alpha_root}.{injective_section_key('First Lesson')}"
    before_ids = _graph_ids(tmp_path)
    assert {alpha_root, alpha_lesson, file_scoped_id("beta.md")} <= before_ids
    alpha_version = store.node_version(alpha_root)

    watcher, receipts = _watched(tmp_path, store, importer, projector)
    (tmp_path / "corpus" / "alpha.md").unlink()
    changes = watcher.poll()

    assert [change.kind for change in changes] == [ChangeKind.REMOVED]
    assert changes[0].path == "alpha.md"
    assert receipts and set(receipts[-1].retired_node_ids) >= {alpha_root, alpha_lesson}

    # The store retains the retired node as a superseded revision (append-only).
    retired = store.current_node(alpha_root)
    assert retired is not None
    assert retired.lifecycle_status is LifecycleStatus.SUPERSEDED
    assert store.node_version(alpha_root) == alpha_version + 1
    assert any(event.kind is EventKind.NODE_RETIRE and event.node_id == alpha_root for event in store.replay_domain())

    # The next projection no longer contains the retired nodes.
    after_ids = _graph_ids(tmp_path)
    assert alpha_root not in after_ids
    assert alpha_lesson not in after_ids
    assert file_scoped_id("beta.md") in after_ids
    assert len(after_ids) < len(before_ids)
    manifest = projector.active_manifest()
    assert manifest is not None
    assert manifest["node_count"] == len(after_ids)

    # A second identical poll is a no-op: no changes, no new events, same graph.
    watermark = store.knowledge_high_watermark()
    event_count = store.event_count()
    assert watcher.poll() == []
    assert store.knowledge_high_watermark() == watermark
    assert store.event_count() == event_count
    assert _graph_ids(tmp_path) == after_ids


def test_retire_batch_replay_is_idempotent(tmp_path: Path) -> None:
    store, importer, projector = _populated(tmp_path)
    watcher, _ = _watched(tmp_path, store, importer, projector)
    (tmp_path / "corpus" / "alpha.md").unlink()
    changes = watcher.poll()

    watermark = store.knowledge_high_watermark()
    domain_events = store.domain_event_count()
    replayed = update_from_changes(
        changes,
        importer=importer,
        projector=projector,
        root=tmp_path / "corpus",
        domain=PrimaryDomain.P1,
    )
    assert replayed.retired_node_ids == ()
    assert store.knowledge_high_watermark() == watermark
    assert store.domain_event_count() == domain_events
    assert file_scoped_id("alpha.md") not in _graph_ids(tmp_path)


def test_shrink_guard_still_blocks_an_unscoped_shrink(tmp_path: Path) -> None:
    """The retire pipeline's allow_shrink is scoped: a plain rebuild still refuses to shrink."""
    store, importer, projector = _populated(tmp_path)
    watcher, _ = _watched(tmp_path, store, importer, projector)
    (tmp_path / "corpus" / "alpha.md").unlink()
    changes = watcher.poll()
    assert [change.kind for change in changes] == [ChangeKind.REMOVED]

    # Retire one more live node without the pipeline, then a plain rebuild must trip the guard.
    retired = importer.retire_source("beta.md")
    assert retired
    with pytest.raises(ShrinkGuardError):
        projector.rebuild()
    # The scoped pipeline rebuild succeeds and serves the shrunk graph.
    receipt = update_from_changes(
        changes,
        importer=importer,
        projector=projector,
        root=tmp_path / "corpus",
        domain=PrimaryDomain.P1,
    )
    assert receipt.projection is not None
    assert receipt.projection.activated is True


def test_rebuild_retries_while_another_projector_holds_the_lock(tmp_path: Path) -> None:
    """K2: a busy rebuild lock must not drop the watcher batch.

    A reader-triggered ``repair_if_stale`` holds the single rebuild lock, so a
    watcher batch can arrive while ``rebuild`` would raise ``ProjectorBusy``.
    The pipeline retries with a bounded backoff instead of losing the change.
    """
    store, importer, projector = _populated(tmp_path)
    watcher, receipts = _watched(tmp_path, store, importer, projector)

    real_rebuild = projector.rebuild
    calls = {"count": 0}

    def busy_twice(**kwargs: object) -> object:
        calls["count"] += 1
        if calls["count"] <= 2:
            raise ProjectorBusy("another projector holds the rebuild lock for this corpus root")
        return real_rebuild(**kwargs)  # type: ignore[arg-type]

    projector.rebuild = busy_twice  # type: ignore[method-assign]
    (tmp_path / "corpus" / "alpha.md").write_bytes(
        ALPHA + b"\n## Third Lesson\n\nRetry the rebuild while the lock is busy.\n"
    )
    changes = watcher.poll()
    projector.rebuild = real_rebuild  # type: ignore[method-assign]

    assert [change.kind for change in changes] == [ChangeKind.CHANGED]
    assert calls["count"] == 3, "two busy signals then one successful rebuild"
    assert receipts and receipts[-1].projection is not None
    assert receipts[-1].projection.activated is True


def test_busy_lock_exhaustion_keeps_the_typed_error(tmp_path: Path) -> None:
    """The retry is bounded: an exhausted budget still surfaces ``ProjectorBusy``."""
    _, importer, projector = _populated(tmp_path)
    (tmp_path / "corpus" / "alpha.md").write_bytes(ALPHA + b"\n## Fourth Lesson\n\nNever lose a batch.\n")
    changes = [SourceChange(path="alpha.md", kind=ChangeKind.CHANGED, content_sha256="0" * 64)]

    def always_busy(**kwargs: object) -> object:
        raise ProjectorBusy("another projector holds the rebuild lock for this corpus root")

    projector.rebuild = always_busy  # type: ignore[method-assign]
    with pytest.raises(ProjectorBusy):
        update_from_changes(
            changes,
            importer=importer,
            projector=projector,
            root=tmp_path / "corpus",
            domain=PrimaryDomain.P1,
            rebuild_attempts=1,
        )
