"""The operational ``watch`` CLI command (Wave 4, SUB-92).

Drives :class:`know_your_soul.ingest.watcher.IngestionWatcher` over a
directory: each cycle prints a deterministic one-line summary (or the whole
:class:`WatchReport` as canonical JSON with ``--json``). ``--once`` runs a
single cycle for tests/CI.

OPS surface only — like ``ingest``/``export`` this is not a knowledge
operation: it never joins ``KNOWLEDGE_OPERATIONS``/``OPS_OPERATIONS`` and is
never dispatched over MCP or HTTP. The frozen fourteen operations are
untouched. Ingestion output stays proposal-only (contract v3).

Exit codes: 0 completed, 1 typed failure (policy-disabled ``WATCH_DISABLED``
or auto-degraded), 2 usage error (bad knobs, missing/non-directory root).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from know_your_soul.domain import canonical_json
from know_your_soul.ingest.pipeline import IngestionPipeline
from know_your_soul.ingest.watcher import (
    ENV_FORCE_WATCH,
    CycleReport,
    IngestionWatcher,
    WatcherConfig,
    WatcherRootError,
    WatchReport,
    evaluate_watch_policy,
)
from know_your_soul.store import EventStore

__all__ = ["render_cycle", "run_watch_command"]


def _usage_error(message: str) -> int:
    envelope = {"error": {"code": "CLI_USAGE", "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)
    return 2


def _operation_error(code: str, message: str) -> int:
    envelope = {"error": {"code": code, "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)
    return 1


def render_cycle(cycle: CycleReport) -> str:
    """The deterministic one-line per-cycle summary."""
    added = sum(1 for c in cycle.changes if c.kind == "added")
    changed = sum(1 for c in cycle.changes if c.kind == "changed")
    removed = sum(1 for c in cycle.changes if c.kind == "removed")
    parts = [
        f"watch: cycle {cycle.cycle} {cycle.mode}",
        f"changes {len(cycle.changes)} (+{added} ~{changed} -{removed})",
        f"files proposed {cycle.files_proposed} skipped {cycle.files_skipped} "
        f"failed {cycle.files_failed} deleted {cycle.files_deleted}",
        f"nodes {cycle.nodes} edges {cycle.edges}",
        f"refs resolved {cycle.refs_resolved} failed {cycle.refs_failed}",
        f"{cycle.duration_ms} ms",
    ]
    if cycle.error is not None:
        parts.append(f"error {cycle.error}")
    return " | ".join(parts)


def run_watch_command(args: argparse.Namespace) -> int:
    """The ``watch`` ops command. Exit 0 success, 1 typed failure, 2 usage."""
    try:
        config = WatcherConfig(
            interval_seconds=args.interval,
            debounce_seconds=args.debounce,
            quick_quiet_seconds=args.quick_quiet,
            quick_max_changes=args.quick_max_changes,
            full_scan_ceiling=args.full_scan_ceiling,
            max_consecutive_failures=args.max_failures,
        )
    except ValueError as error:
        return _usage_error(str(error))
    root = Path(args.path)
    if not root.is_dir():
        return _usage_error(f"watched path {args.path!r} is not an existing directory")
    env = dict(os.environ)
    if args.force:
        env[ENV_FORCE_WATCH] = "1"
    decision = evaluate_watch_policy(root, env=env)
    if not decision.enabled:
        return _operation_error("WATCH_DISABLED", decision.reason)
    store = EventStore(args.store)
    try:
        pipeline = IngestionPipeline(store, actor=args.actor)
        watcher = IngestionWatcher(root, pipeline, config=config)
        try:
            report: WatchReport = watcher.run(once=args.once, max_cycles=args.max_cycles)
        except KeyboardInterrupt:
            report = WatchReport(root=str(root.resolve()), status="completed")
    except WatcherRootError as error:
        return _usage_error(str(error))
    finally:
        store.close()
    if args.json:
        print(canonical_json(report.to_dict()))
    else:
        for cycle in report.cycles:
            print(render_cycle(cycle))
        print(f"watch: status {report.status} | cycles {len(report.cycles)} | changes {report.total_changes}")
    return 1 if report.status == "degraded" else 0
