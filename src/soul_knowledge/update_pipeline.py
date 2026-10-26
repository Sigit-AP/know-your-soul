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

import logging
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from soul_knowledge.domain import PrimaryDomain
from soul_knowledge.importer import SeedImporter
from soul_knowledge.projection import GraphProjector, ProjectionReceipt, ProjectorBusy
from soul_knowledge.source_registry import RELEARN_PENDING, RelearnOffer, SourceKind
from soul_knowledge.source_watcher import SourceChange, SourceWatcher
from soul_knowledge.store import StoreError

__all__ = [
    "ApprovedUpdateReceipt",
    "SourceWatchLoop",
    "UpdatePipelineError",
    "UpdateReceipt",
    "WatchPollReceipt",
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


class WatchPollReceipt(BaseModel):
    """The outcome of one watch-loop poll cycle; errors are data, never raised."""

    model_config = ConfigDict(frozen=True)

    changes: tuple[SourceChange, ...] = ()
    offer_emitted: bool = False
    error: str | None = None


class SourceWatchLoop:
    """Drive a :class:`SourceWatcher` for the lifetime of a server (F-1).

    The loop polls on ``interval_seconds`` and routes every change batch
    through the injected ``on_changes`` gate callback — in production the
    service's audited ``offer_relearn``, so a detected change publishes the
    deterministic offer plus ``RELEARN_PENDING`` and nothing more: detection
    never flips the baseline and never applies (final-audit F-4 semantics).

    Discipline:

    * Lifecycle: :meth:`start` spawns one daemon thread (``soul-source-watch``),
      :meth:`stop` joins it; a double start is a typed
      :class:`UpdatePipelineError` and :meth:`stop` is idempotent.
    * Cooldown: after a poll whose callback emitted an offer, the loop pauses
      for ``cooldown_seconds`` — edits during the pause accumulate on disk and
      arrive as one batched poll, so a save storm cannot mint a stream of
      offers. The watcher's own snapshot is untouched while paused, so nothing
      is silently absorbed.
    * Fail-isolated: a watcher error (``WatcherStateError``, ``OSError``) or a
      callback failure becomes a typed :class:`WatchPollReceipt` error, is
      logged, and backs the poll off exponentially up to
      ``max_error_backoff_seconds`` — the loop never takes its server down.
    """

    def __init__(
        self,
        watcher: SourceWatcher,
        on_changes: Callable[[list[SourceChange]], object],
        *,
        interval_seconds: float = 30.0,
        cooldown_seconds: float = 300.0,
        max_error_backoff_seconds: float = 300.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise UpdatePipelineError(f"watch interval must be positive, got {interval_seconds}")
        if cooldown_seconds < 0:
            raise UpdatePipelineError(f"watch cooldown must be non-negative, got {cooldown_seconds}")
        if max_error_backoff_seconds <= 0:
            raise UpdatePipelineError(f"watch error backoff cap must be positive, got {max_error_backoff_seconds}")
        self._watcher = watcher
        self._on_changes = on_changes
        self._interval_seconds = float(interval_seconds)
        self._cooldown_seconds = float(cooldown_seconds)
        self._max_error_backoff_seconds = float(max_error_backoff_seconds)
        self._logger = logger if logger is not None else logging.getLogger(__name__)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._cooldown_until = 0.0
        self._consecutive_errors = 0

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def cooldown_remaining(self) -> float:
        """Seconds left of the post-offer pause (0 when none is active)."""
        return max(0.0, self._cooldown_until - time.monotonic())

    def start(self) -> None:
        """Spawn the daemon watch thread; a double start fails typed."""
        if self._thread is not None:
            raise UpdatePipelineError("the source watch loop is already started")
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="soul-source-watch", daemon=True)
        self._thread.start()
        self._logger.info(
            "source watch started: interval %.2fs, cooldown %.2fs",
            self._interval_seconds,
            self._cooldown_seconds,
        )

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop the watch thread; safe to call without a start or twice."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=timeout)
            if thread.is_alive():  # pragma: no cover - the interruptible wait makes this unreachable
                self._logger.warning("source watch thread did not stop within %.2fs", timeout)
            else:
                self._logger.info("source watch stopped")

    def poll_once(self) -> WatchPollReceipt:
        """One detect-and-gate cycle; the error path is a receipt, never a raise.

        Exposed for tests and one-shot drivers; the thread loop is a thin
        ``poll_once`` + sleep wrapper around exactly this method.
        """
        try:
            changes = self._watcher.poll()
        except Exception as error:  # noqa: BLE001 - the loop must never take its server down
            return self._note_error(f"{type(error).__name__}: {error}")
        if not changes:
            self._consecutive_errors = 0
            return WatchPollReceipt()
        try:
            outcome = self._on_changes(changes)
        except Exception as error:  # noqa: BLE001 - same isolation rule for the gate callback
            return self._note_error(f"{type(error).__name__}: {error}", changes=changes)
        self._consecutive_errors = 0
        offer_emitted = bool(outcome)
        if offer_emitted:
            self._cooldown_until = time.monotonic() + self._cooldown_seconds
            offer_id = getattr(outcome, "offer_id", None)
            self._logger.info(
                "source change gated: %d file(s); relearn offer %s emitted, RELEARN_PENDING awaits the operator",
                len(changes),
                offer_id,
            )
        else:
            self._logger.info(
                "source change batch of %d file(s) recorded without an offer (baseline or unchanged fingerprint)",
                len(changes),
            )
        return WatchPollReceipt(changes=tuple(changes), offer_emitted=offer_emitted)

    def _note_error(self, message: str, *, changes: list[SourceChange] | None = None) -> WatchPollReceipt:
        self._consecutive_errors += 1
        self._logger.error("source watch poll failed (%s); backing off", message)
        return WatchPollReceipt(changes=tuple(changes or ()), error=message)

    def _next_delay(self, receipt: WatchPollReceipt) -> float:
        if receipt.error is not None:
            return min(
                self._interval_seconds * (2 ** max(0, self._consecutive_errors - 1)),
                self._max_error_backoff_seconds,
            )
        if receipt.offer_emitted:
            return max(self._interval_seconds, self.cooldown_remaining())
        return self._interval_seconds

    def _run(self) -> None:
        delay = self._interval_seconds
        while not self._stop.wait(delay):
            receipt = self.poll_once()
            delay = self._next_delay(receipt)


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
