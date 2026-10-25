"""The deterministic watcher-driven update pipeline.

Task 3 fix round 1 (finding F2): the watcher's ``on_change`` callback runs
this pipeline for each poll batch. ``ADDED`` and ``CHANGED`` sources are
re-imported incrementally; a ``REMOVED`` source is *retired* — every node
imported from it gets an idempotency-keyed ``node.retire`` domain event — and
the rebuild runs with ``allow_shrink=True`` scoped to this pipeline, so the
shrink guard keeps blocking every unscoped shrink. Every step is idempotent:
a replayed change batch appends nothing and re-derives the same generation.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from soul_knowledge.domain import PrimaryDomain
from soul_knowledge.importer import SeedImporter
from soul_knowledge.projection import GraphProjector, ProjectionReceipt, ProjectorBusy
from soul_knowledge.source_watcher import ChangeKind, SourceChange

__all__ = ["UpdateReceipt", "update_from_changes"]


class UpdateReceipt(BaseModel):
    """The outcome of applying one watcher change batch."""

    model_config = ConfigDict(frozen=True)

    retired_node_ids: tuple[str, ...] = ()
    imported_files: int = 0
    projection: ProjectionReceipt | None = None


def update_from_changes(
    changes: Sequence[SourceChange],
    *,
    importer: SeedImporter,
    projector: GraphProjector,
    root: str | Path,
    domain: PrimaryDomain,
    rebuild_attempts: int = 20,
    rebuild_backoff_seconds: float = 0.05,
) -> UpdateReceipt:
    """Run the deterministic update pipeline for one watcher poll batch.

    Retire first, then re-import, then rebuild. The rebuild's
    ``allow_shrink`` is ``True`` exactly when the batch carried a removal —
    the scope of the retire pipeline — and never a projector-wide setting.

    A concurrent projector (a reader-triggered ``repair_if_stale``, another
    writer) holds the single rebuild lock and makes ``rebuild`` raise
    ``ProjectorBusy``. Dropping the batch on that signal would silently lose a
    filesystem change the watcher has already consumed (finding K2), so the
    rebuild is retried with a bounded backoff and only the exhausted case
    propagates the typed error.
    """
    if not changes:
        return UpdateReceipt()
    retired: list[str] = []
    for change in changes:
        if change.kind is ChangeKind.REMOVED:
            retired.extend(importer.retire_source(change.path))
    receipts = importer.import_directory(Path(root), domain=domain)
    shrink = any(change.kind is ChangeKind.REMOVED for change in changes)
    projection = _rebuild_when_free(
        projector,
        shrink=shrink,
        attempts=rebuild_attempts,
        backoff_seconds=rebuild_backoff_seconds,
    )
    return UpdateReceipt(
        retired_node_ids=tuple(retired),
        imported_files=len(receipts),
        projection=projection,
    )


def _rebuild_when_free(
    projector: GraphProjector,
    *,
    shrink: bool,
    attempts: int,
    backoff_seconds: float,
) -> ProjectionReceipt:
    """Rebuild under a busy lock: bounded retry, then the typed failure."""
    total = max(1, attempts)
    last: ProjectorBusy | None = None
    for attempt in range(total):
        try:
            return projector.rebuild(allow_shrink=True if shrink else None)
        except ProjectorBusy as error:
            last = error
            if attempt + 1 < total:
                time.sleep(backoff_seconds)
    assert last is not None  # only reachable after a ProjectorBusy
    raise last
