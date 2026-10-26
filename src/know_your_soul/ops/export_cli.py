"""The operational ``export``/``analytics`` CLI commands (ATM F-D1).

Both commands read the *active* generation's ``graph.json`` (never the store
directly, never a rebuild), run the export registry over that read model —
round-trip-validated and cap-checked, fail-closed — and write the artifacts
into a contained output directory (default ``<output-root>/exports``),
printing a canonical JSON summary on stdout. Structured errors go to stderr.

Argument validation is strict: unknown format names and empty selections are
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
from know_your_soul.exporters.orchestrator import (
    ExportError,
    UnknownExportFormatError,
    default_registry,
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
    select: tuple[str, ...] | None = None
    if requested is not None:
        parts = [part.strip() for part in str(requested).split(",")]
        if not parts or any(not part for part in parts):
            return _usage_error(f"--formats/--modules needs a comma-separated list of names, got {requested!r}")
        select = tuple(parts)

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

        registry = default_registry()
        if select is None:
            select = registry.names(kind=kind)
        unknown = sorted(set(select) - set(registry.names()))
        if unknown:
            return _usage_error(
                f"unknown export format(s): {', '.join(unknown)}; registered: {', '.join(registry.names())}"
            )
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
