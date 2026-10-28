"""The operational ``ingest`` CLI command (Wave 2, SUB-89).

Runs :class:`know_your_soul.ingest.pipeline.IngestionPipeline` over a file
or directory and prints a one-line summary (or canonical JSON with
``--json``): files proposed/skipped/failed, node/edge counts, refs
resolved/pending/failed, and the duration. Typed failures use the standard
error envelope on stderr.

OPS surface only — like ``export``/``analytics`` this is not a knowledge
operation: it never joins ``KNOWLEDGE_OPERATIONS``/``OPS_OPERATIONS`` and is
never dispatched over MCP or HTTP. The frozen fourteen operations are
untouched. Ingestion output is proposal-only (contract v3); applying it to
the knowledge graph goes through the operator-gated propose/apply path.
"""

from __future__ import annotations

import argparse
import sys

from know_your_soul.domain import canonical_json
from know_your_soul.ingest.pipeline import (
    ExtractorUnavailableError,
    IngestionBudget,
    IngestionError,
    IngestionPipeline,
    IngestionReport,
)
from know_your_soul.store import EventStore

__all__ = ["run_ingest_command"]


def _usage_error(message: str) -> int:
    envelope = {"error": {"code": "CLI_USAGE", "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)
    return 2


def _operation_error(code: str, message: str) -> int:
    envelope = {"error": {"code": code, "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)
    return 1


def render_summary(report: IngestionReport) -> str:
    """The deterministic one-line human summary."""
    stats = report.stats
    totals = report.totals
    parts = [
        f"files {stats['scanned']} (proposed {stats['proposed']}, skipped {stats['skipped']}, "
        f"failed {stats['failed']}, deleted {stats['deleted']})",
        f"nodes {totals['nodes']}",
        f"edges {totals['edges']}",
    ]
    if report.resolution is not None:
        parts.append(
            f"refs parked {totals['refs_parked']}, resolved {report.resolution['resolved']}, "
            f"failed {report.resolution['failed']}"
        )
    else:
        parts.append(f"refs parked {totals['refs_parked']}, resolution {report.resolution_status}")
    if report.guards_hit:
        parts.append(f"guards_hit {','.join(report.guards_hit)}")
    if report.primary_suggestions:
        parts.append(f"primary_suggestions {len(report.primary_suggestions)}")
    parts.append(f"duration {report.duration_ms} ms")
    return "ingest: " + " | ".join(parts)


def run_ingest_command(args: argparse.Namespace) -> int:
    """The ``ingest`` ops command. Exit 0 success, 1 typed failure, 2 usage."""
    try:
        budget = IngestionBudget(
            max_files=args.max_files,
            max_nodes=args.max_nodes,
            max_edges=args.max_edges,
            max_refs=args.max_refs,
            max_file_bytes=args.max_file_bytes,
        )
    except ValueError as error:
        return _usage_error(str(error))
    store = EventStore(args.store)
    try:
        pipeline = IngestionPipeline(store, budget=budget, actor=args.actor)
        report = pipeline.ingest_path(
            args.path,
            incremental=args.incremental,
            recursive=args.recursive,
            resolve=not args.no_resolve,
        )
    except (IngestionError, ExtractorUnavailableError) as error:
        return _operation_error(getattr(error, "code", "INGESTION_ERROR"), str(error))
    finally:
        store.close()
    if args.json:
        print(canonical_json(report.to_dict()))
    else:
        print(render_summary(report))
    return 0
