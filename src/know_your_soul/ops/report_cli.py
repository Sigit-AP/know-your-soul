"""The operational ``report`` CLI command (W8 SUB-107).

Reads the *active* generation's ``graph.json`` (never a rebuild, never a
store write), builds the composite graph-analysis report
(:mod:`know_your_soul.report.graph_report`), prints it to stdout
(markdown by default, canonical JSON with ``--format json``) and the
token benchmark to stderr — the Graphify "print after build" pattern,
except the benchmark is also embedded in both artifacts.

With ``--out DIR`` the report is additionally written as
``graph-report.json`` + ``graph-report.md`` into a contained directory
(resolved inside the ops root / projection output root — escapes fail
closed as usage errors before any byte is written).

OPS surface only: not a knowledge operation, never dispatched over MCP
or HTTP.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from know_your_soul.domain import canonical_json
from know_your_soul.http_api import build_stack
from know_your_soul.ops.export_cli import _write_bytes_atomic
from know_your_soul.report.graph_report import (
    GraphReportError,
    build_graph_analysis,
    render_graph_analysis_markdown,
)
from know_your_soul.security import PathEscapeError, resolve_within_root

__all__ = ["run_report_command"]

REPORT_JSON_NAME = "graph-report.json"
REPORT_MARKDOWN_NAME = "graph-report.md"


def _usage_error(message: str) -> int:
    envelope = {"error": {"code": "CLI_USAGE", "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)
    return 2


def _operation_error(code: str, message: str) -> int:
    envelope = {"error": {"code": code, "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)
    return 1


def run_report_command(args: argparse.Namespace) -> int:
    """Run the ``report`` command; return the exit code."""
    for flag in ("top_god", "top_surprising", "top_questions"):
        if int(getattr(args, flag)) < 1:
            return _usage_error(f"--{flag.replace('_', '-')} must be >= 1")

    stack = build_stack(args.store, args.output_root, ops_root=args.ops_root)
    try:
        generation = stack.projector.active_generation_dir()
        if generation is None:
            return _operation_error(
                "NO_ACTIVE_GENERATION",
                "no active generation found; run the rebuild command first",
            )
        try:
            read_model: Any = json.loads((generation / "graph.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return _operation_error("GENERATION_UNREADABLE", f"active generation graph.json is unreadable: {error}")
        if not isinstance(read_model, dict):
            return _operation_error("GENERATION_UNREADABLE", "active generation graph.json is not an object")

        try:
            report = build_graph_analysis(
                read_model,
                top_god=args.top_god,
                top_surprising=args.top_surprising,
                top_questions=args.top_questions,
            )
            markdown = render_graph_analysis_markdown(report)
        except (ValueError, GraphReportError) as error:
            return _operation_error("REPORT_FAILED", str(error))

        if args.out is not None:
            root_candidate = Path(args.ops_root) if args.ops_root else Path(args.output_root)
            try:
                containment = resolve_within_root(root_candidate, ".")
                out_dir = resolve_within_root(containment, args.out)
            except PathEscapeError as error:
                return _usage_error(f"output path escapes the containment root: {error}")
            _write_bytes_atomic(out_dir / REPORT_JSON_NAME, (canonical_json(report) + "\n").encode("utf-8"))
            _write_bytes_atomic(out_dir / REPORT_MARKDOWN_NAME, markdown.encode("utf-8"))

        benchmark = report["token_benchmark"]
        print(
            "token benchmark: report ~{md} tokens markdown / ~{js} tokens json vs ~{raw} raw graph tokens "
            "(savings {savings}, {compression}x smaller)".format(
                md=benchmark["report_markdown_tokens"],
                js=benchmark["report_json_tokens"],
                raw=benchmark["graph_tokens"],
                savings=benchmark["savings_ratio"],
                compression=benchmark["compression_ratio"],
            ),
            file=sys.stderr,
        )
        if args.format == "json":
            print(canonical_json(report))
        else:
            print(markdown, end="")
        return 0
    finally:
        stack.close()
