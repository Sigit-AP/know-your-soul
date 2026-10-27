"""The operational ``export``/``analytics`` CLI commands (ATM F-D1).

Both commands read the *active* generation's ``graph.json`` (never a rebuild,
never a store write), run the built-in ATM registry
(:mod:`know_your_soul.exporters.builtin`) over that read model —
round-trip-validated and cap-checked, fail-closed — and write the artifacts into a
contained output directory (default ``<output-root>/exports``), printing a
canonical JSON summary on stdout. Structured errors go to stderr.

Context-dependent artifacts (timeline, generation-diff, components,
review-signals, report-v2, book) are fed by
:func:`know_your_soul.exporters.builtin.gather_context`, which reads the
store with strictly read-only ``SELECT`` statements. Selections accept full
artifact names, short aliases (``dot``, ``mermaid``, ``tabular``, ``graphml``,
``svg``, ``html``, ``book``, ``gexf``, ``cytoscape``, ``d3force``,
``adjacency``, ``tgf``, ``centrality``, ``components``, ``cycles``,
``communities``, ``bridges``, ``review-signals``, ``health``, ``report-v2``,
``generation-diff``, ``timeline``, ``graph-summary``, ``hits``,
``eigenvector``, ``katz``, ``clustering``, ``louvain``, ``reachability``,
``motifs``, ``assortativity``, ``paths``, ``aggregates``,
``near-duplicates``) or ``all``.

Argument validation is strict: unknown names/aliases and empty selections are
usage errors (exit 2), and every output path resolves through
:func:`know_your_soul.security.resolve_within_root` — escapes fail closed.
These commands are OPS surface only: they are not knowledge operations and
are never dispatched over MCP or HTTP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from know_your_soul.domain import canonical_json
from know_your_soul.exporters import builtin
from know_your_soul.exporters.orchestrator import (
    ExportError,
    UnknownExportFormatError,
    produce_exports,
)
from know_your_soul.http_api import build_stack
from know_your_soul.security import PathEscapeError, resolve_within_root

__all__ = ["DEFAULT_EXPORT_DIRNAME", "run_export_command"]

DEFAULT_EXPORT_DIRNAME = "exports"

# The two subcommands differ only in which registry kind they select by
# default; an explicit --formats/--modules list crosses kinds.
_COMMAND_KIND = {"export": "format", "analytics": "analytics"}


def _usage_error(message: str) -> int:
    """A CLI-usage failure envelope (exit 2); nothing is written."""
    envelope = {"error": {"code": "CLI_USAGE", "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)
    return 2


def _operation_error(code: str, message: str) -> int:
    """A runtime failure envelope (exit 1); no artifact set is partial-claimed."""
    envelope = {"error": {"code": code, "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)
    return 1


def _write_bytes_atomic(path: Path, data: bytes) -> bool:
    """Idempotent atomic write, mirroring the projector's discipline."""
    try:
        if path.read_bytes() == data:
            return False
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return True


def run_export_command(args: argparse.Namespace, *, command: str) -> int:
    """Run the ``export`` or ``analytics`` command; return the exit code."""
    kind = _COMMAND_KIND[command]
    requested = getattr(args, "formats", None)

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

        # The store feeds the context-dependent producers through a strictly
        # read-only snapshot (SELECTs only; the event log is never written).
        try:
            context = builtin.gather_context(stack.store, generation_dir=generation)
        except ExportError as error:
            return _operation_error("EXPORT_CONTEXT_FAILED", str(error))
        registry = builtin.build_registry(context)

        select: tuple[str, ...] | None = None
        if requested is not None:
            try:
                select = builtin.resolve_selection(str(requested), kind=kind, registry=registry)
            except ValueError as error:
                return _usage_error(str(error))
        if select is None:
            select = registry.names(kind=kind)
        try:
            artifacts = produce_exports(registry, read_model, select=select)
        except UnknownExportFormatError as error:
            return _usage_error(str(error))
        except ExportError as error:
            return _operation_error("EXPORT_FAILED", str(error))

        # Containment: the output directory resolves inside the ops root when
        # given, else inside the projection output root — escapes fail closed
        # as usage errors before any byte is written.
        root_candidate = Path(args.ops_root) if args.ops_root else Path(args.output_root)
        try:
            containment = resolve_within_root(root_candidate, ".")
            out_dir = resolve_within_root(containment, args.out or DEFAULT_EXPORT_DIRNAME)
        except PathEscapeError as error:
            return _usage_error(f"output path escapes the containment root: {error}")

        summary_entries: list[dict[str, Any]] = []
        for name in sorted(artifacts):
            data = artifacts[name]
            changed = _write_bytes_atomic(out_dir / name, data)
            spec = registry.get(name)
            summary_entries.append(
                {
                    "name": name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "version": spec.version,
                    "kind": spec.kind,
                    "written": changed,
                }
            )
        out_dir_fd = os.open(out_dir, os.O_RDONLY)
        try:
            os.fsync(out_dir_fd)
        finally:
            os.close(out_dir_fd)
        summary = {
            "ok": True,
            "command": command,
            "generation": generation.name,
            "registry_fingerprint": registry.fingerprint(),
            "output_dir": str(out_dir),
            "artifacts": summary_entries,
        }
        print(canonical_json(summary))
        return 0
    finally:
        stack.close()
