"""The deterministic watcher-driven update pipeline, offer-gated (spec 13.3/13.5).

Final flow audit F-4: the pipeline used to re-import a changed corpus through
``registry.observe``, inserting the new version as ``CURRENT`` *without* a
``RelearnOffer`` and *without* ``RELEARN_PENDING`` — a silent baseline flip on
exactly the path spec 13.5 names for document changes, violating spec 13.3
("the system emits a deterministic RelearnOffer and sets RELEARN_PENDING …
No operator decision means no change").

The pipeline is now split at the gate:

* :func:`update_from_changes` — the watcher path. It only *detects*: every
  change batch routes through ``SourceRegistry.detect_change``, which records
  the new source version, emits the deterministic offer and sets
  ``RELEARN_PENDING`` in one transaction. The operator-approved baseline, the
  store's knowledge and the projection stay untouched until an operator
  decision. Detection is idempotent: a replayed batch re-derives the same
  fingerprint and emits nothing new.
* :func:`apply_approved_update` — the post-decision executor. It refuses to
  run while a decision is pending (typed :class:`UpdatePipelineError`); once
  the operator has approved and the relearn engine has applied the mode, it
  retires the sources the offer's diff reports as removed, re-imports, and
  rebuilds with ``allow_shrink`` scoped to this pipeline — never a
  projector-wide setting.

The very first observation of a source kind establishes the detection
baseline (``baseline_recorded``); the initial content import itself remains
operator-explicit (``SeedImporter.import_directory``).
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from soul_knowledge.domain import PrimaryDomain
from soul_knowledge.importer import SeedImporter
from soul_knowledge.projection import GraphProjector, ProjectionReceipt, ProjectorBusy
from soul_knowledge.source_registry import RELEARN_PENDING, RelearnOffer, SourceKind
from soul_knowledge.source_watcher import SourceChange
from soul_knowledge.store import StoreError

__all__ = [
    "ApprovedUpdateReceipt",
    "UpdatePipelineError",
    "UpdateReceipt",
    "apply_approved_update",
    "update_from_changes",
]


class UpdatePipelineError(StoreError):
    """The pipeline was asked to apply a change before the operator decided."""


class UpdateReceipt(BaseModel):
    """The outcome of detecting one watcher change batch (no mutation of knowledge)."""

    model_config = ConfigDict(frozen=True)

    changed_paths: tuple[str, ...] = ()
    offer: RelearnOffer | None = None
    relearn_pending: bool = False
    baseline_recorded: bool = False


class ApprovedUpdateReceipt(BaseModel):
    """The outcome of executing one operator-approved update batch."""

    model_config = ConfigDict(frozen=True)

    retired_node_ids: tuple[str, ...] = ()
    imported_files: int = 0
    projection: ProjectionReceipt | None = None


def update_from_changes(
    changes: Sequence[SourceChange],
    *,
    importer: SeedImporter,
    root: str | Path,
    kind: SourceKind = SourceKind.SOUL_CORPUS,
) -> UpdateReceipt:
    """Route one watcher change batch through the relearn gate (spec 13.3).

    Emits the deterministic offer and sets ``RELEARN_PENDING`` when the source
    fingerprint moved; the baseline, the store's knowledge and the projection
    are *not* touched — no operator decision means no change. When no baseline
    exists yet, the first observation records it (``baseline_recorded``) and
    emits no offer: the initial import stays operator-explicit.
    """
    if not changes:
        return UpdateReceipt()
    registry = importer._registry  # noqa: SLF001 - same-package pipeline wiring
    baseline_missing = registry.latest_observed(kind) is None
    offer = registry.detect_change(root, kind, importer._parser_versions)  # noqa: SLF001 - wiring
    return UpdateReceipt(
        changed_paths=tuple(sorted(change.path for change in changes)),
        offer=offer,
        relearn_pending=registry.relearn_state() == RELEARN_PENDING,
        baseline_recorded=baseline_missing and offer is None,
    )


def apply_approved_update(
    *,
    importer: SeedImporter,
    projector: GraphProjector,
    root: str | Path,
    domain: PrimaryDomain,
    rebuild_attempts: int = 20,
    rebuild_backoff_seconds: float = 0.05,
) -> ApprovedUpdateReceipt:
    """Execute an update batch after the operator's relearn decision.

    Fails closed with a typed :class:`UpdatePipelineError` while
    ``RELEARN_PENDING`` is set — the pipeline never applies an undecided
    change. After the decision (relearn state ``CLEAR``), the sources the
    latest offer's diff reports as removed are retired through
    idempotency-keyed ``node.retire`` events, the directory is re-imported
    incrementally, and the rebuild runs with ``allow_shrink`` scoped to this
    pipeline exactly when the batch carried removals.

    A concurrent projector (a reader-triggered ``repair_if_stale``, another
    writer) holds the single rebuild lock and makes ``rebuild`` raise
    ``ProjectorBusy``. Dropping the batch on that signal would silently lose a
    filesystem change the operator already approved, so the rebuild is
    retried with a bounded backoff and only the exhausted case propagates the
    typed error.
    """
    registry = importer._registry  # noqa: SLF001 - same-package pipeline wiring
    if registry.relearn_state() == RELEARN_PENDING:
        raise UpdatePipelineError(
            "a relearn offer is pending: the update pipeline may not apply changes "
            "before the operator's RESET/ACCUMULATE decision (spec 13.3)"
        )
    offer = registry.latest_offer()
    removed = tuple(offer.diff.removed) if offer is not None else ()
    retired: list[str] = []
    for relative in removed:
        retired.extend(importer.retire_source(relative))
    receipts = importer.import_directory(Path(root), domain=domain)
    projection = _rebuild_when_free(
        projector,
        shrink=bool(removed),
        attempts=rebuild_attempts,
        backoff_seconds=rebuild_backoff_seconds,
    )
    return ApprovedUpdateReceipt(
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
