"""The ``know-your-soul`` command-line interface (Task 7).

Task 7 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7, 8 and 13.1.

A thin adapter over :mod:`know_your_soul.http_api`'s shared dispatch: every
command validates its JSON payload, invokes the fourteen-operation
``KnowledgeService`` (the frozen ten v1 operations plus the contract-v2 G1
retrieval verbs ``path`` / ``explain`` / ``affected`` / ``god_nodes`` —
operator ruling 2026-10-26, addendum ``docs/contracts/retrieval-verbs-v2.md``)
(or the operational commands ``rebuild``, ``backup``,
``restore``, ``health`` and the operator-gated relearn lifecycle
``relearn-scan``/``relearn-status``/``relearn-preview``/``relearn-apply``)
through the same code path as the HTTP API, prints the canonical JSON result
on stdout, and prints the consistent error envelope on stderr. Exit codes:

* ``0`` — success (``health`` additionally requires ``ok: true``);
* ``1`` — the service rejected a valid request (typed error, journaled by the
  service exactly once);
* ``2`` — the adapter rejected invalid input (journaled with exactly one
  completion audit record by the adapter).

``serve`` starts the persistent local API: a Unix socket by default, or
opt-in loopback HTTP with ``--http HOST:PORT``; wider binds fail closed
without ``--api-key``. Prefer ``--api-key-env NAME`` over ``--api-key`` — an
inline key is visible to every local user via process argv (wave-2 F6).
``--ops-root`` confines ``backup``/``restore`` paths (default: the store's
directory; wave-2 F1).
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from typing import Any

from know_your_soul.domain import canonical_json
from know_your_soul.http_api import (
    DEFAULT_HOST_ALLOWLIST,
    KNOWLEDGE_OPERATIONS,
    OPS_OPERATIONS,
    AdapterValidationError,
    build_stack,
    dispatch_text,
    error_envelope,
    serve,
    to_jsonable,
)
from know_your_soul.security import BindPolicyError
from know_your_soul.update_pipeline import UpdatePipelineError

__all__ = ["build_parser", "main"]

DEFAULT_STORE_PATH = "know-your-soul.sqlite3"
DEFAULT_OUTPUT_ROOT = "know-your-soul-projection"
DEFAULT_SOCKET_PATH = "know-your-soul.sock"

STORE_ENV_VAR = "KNOW_YOUR_SOUL_STORE"
LEGACY_STORE_ENV_VAR = "SOUL_STORE"  # deprecated pre-rename name; still honored


def _default_store_path() -> str:
    """Store path default: KNOW_YOUR_SOUL_STORE, then deprecated SOUL_STORE, then the built-in default."""
    new = os.environ.get(STORE_ENV_VAR)
    if new:
        return new
    legacy = os.environ.get(LEGACY_STORE_ENV_VAR)
    if legacy:
        print(f"warning: {LEGACY_STORE_ENV_VAR} is deprecated; set {STORE_ENV_VAR} instead", file=sys.stderr)
        return legacy
    return DEFAULT_STORE_PATH


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="know-your-soul",
        description="Deterministic local knowledge service: fourteen audited operations plus ops commands.",
    )
    parser.add_argument(
        "--store",
        default=_default_store_path(),
        help=(
            f"path to the SQLite event store (default: ${STORE_ENV_VAR}, "
            f"deprecated ${LEGACY_STORE_ENV_VAR}, or {DEFAULT_STORE_PATH})"
        ),
    )
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, help="projection output directory")
    parser.add_argument("--actor", default="operator", help="actor recorded on the invocation audit")
    parser.add_argument("--idempotency-key", default=None, help="idempotency key for mutation commands")
    parser.add_argument(
        "--ops-root",
        default=None,
        help="fixed containment root for backup/restore paths (default: the store's directory)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    for operation in (*KNOWLEDGE_OPERATIONS, *OPS_OPERATIONS):
        sub = subparsers.add_parser(
            operation.replace("_", "-"),
            help=f"knowledge.{operation}" if operation in KNOWLEDGE_OPERATIONS else f"ops: {operation}",
        )
        sub.add_argument(
            "payload",
            nargs="?",
            default=None,
            help="JSON payload object; '-' reads it from stdin; omitted means {}",
        )

    serve_parser = subparsers.add_parser("serve", help="run the persistent local API server")
    bind = serve_parser.add_mutually_exclusive_group()
    bind.add_argument("--socket", default=None, help=f"unix socket path (default {DEFAULT_SOCKET_PATH})")
    bind.add_argument("--http", default=None, help="loopback HTTP bind as HOST:PORT (e.g. 127.0.0.1:8080)")
    key = serve_parser.add_mutually_exclusive_group()
    key.add_argument(
        "--api-key",
        default=None,
        help="API key required for non-loopback binds/clients (WARNING: visible in process argv; prefer --api-key-env)",
    )
    key.add_argument(
        "--api-key-env",
        default=None,
        metavar="NAME",
        help="read the API key from environment variable NAME (keeps it out of argv)",
    )
    serve_parser.add_argument(
        "--host-allowlist",
        default=None,
        help="comma-separated Host allowlist (default: localhost,127.0.0.1,::1)",
    )
    serve_parser.add_argument("--max-body-bytes", type=int, default=None, help="request body size cap")
    serve_parser.add_argument(
        "--watch",
        default=None,
        metavar="PATH",
        help="watch a corpus root and emit gated relearn offers on change (spec 13.5; detect-only, never applies)",
    )
    serve_parser.add_argument(
        "--watch-interval",
        type=float,
        default=30.0,
        help="seconds between watcher polls (default 30)",
    )
    serve_parser.add_argument(
        "--watch-cooldown",
        type=float,
        default=300.0,
        help="seconds the watcher pauses after emitting an offer (default 300)",
    )
    serve_parser.add_argument(
        "--watch-state",
        default=None,
        metavar="PATH",
        help="watcher snapshot path (default: alongside the store; must live outside the watched root)",
    )

    # ATM F-D1: operational export commands. These are OPS surface only —
    # they are NOT knowledge operations, never join KNOWLEDGE_OPERATIONS /
    # OPS_OPERATIONS, and are never dispatched over MCP or HTTP.
    export_parser = subparsers.add_parser(
        "export",
        help="ops: write export artifacts from the active generation (not a knowledge op)",
    )
    export_parser.add_argument(
        "--formats",
        default=None,
        help=(
            "comma-separated artifact names, short aliases (dot, mermaid, tabular, graphml, svg, html, book, "
            "gexf, cytoscape, d3force, adjacency, tgf) or 'all' (default: all registered 'format' exports)"
        ),
    )
    export_parser.add_argument(
        "--out",
        default=None,
        help="output directory (default: <output-root>/exports; resolved inside the containment root)",
    )
    analytics_parser = subparsers.add_parser(
        "analytics",
        help="ops: write analytics artifacts from the active generation (not a knowledge op)",
    )
    analytics_parser.add_argument(
        "--modules",
        dest="formats",
        default=None,
        help=(
            "comma-separated artifact names, short aliases (centrality, components, cycles, communities, "
            "bridges, review-signals, health, report-v2, generation-diff, timeline, graph-summary, hits, "
            "eigenvector, katz, clustering, louvain, reachability, motifs, assortativity, paths, aggregates, "
            "near-duplicates) or 'all' (default: all registered 'analytics' modules)"
        ),
    )
    analytics_parser.add_argument(
        "--out",
        default=None,
        help="output directory (default: <output-root>/exports; resolved inside the containment root)",
    )

    # Mega-wave 2 OPS tooling (SUB-77 modules, wired by SUB-80): layered
    # configuration introspection, operational metrics, and the fail-closed
    # self-check. OPS surface only — never knowledge ops, never MCP/HTTP.
    config_parser = subparsers.add_parser(
        "config",
        help="ops: inspect the layered operational configuration (not a knowledge op)",
    )
    config_parser.add_argument(
        "--config",
        default=None,
        metavar="FILE",
        help="optional TOML config file (precedence: defaults < file < env < CLI)",
    )
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("describe", help="markdown table of the resolved configuration with provenance")
    config_sub.add_parser("effective", help="canonical JSON of the flat resolved key=value configuration")
    config_sub.add_parser("dump", help="canonical JSON of {key: {value, source}} with per-key provenance")

    metrics_parser = subparsers.add_parser(
        "metrics",
        help="ops: collect operational metrics over the store and active projection (not a knowledge op)",
    )
    metrics_parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="output format (default: json)",
    )
    metrics_parser.add_argument(
        "--stale-after",
        type=float,
        default=300.0,
        help="seconds after which an in-progress journal counts as stale (default 300)",
    )

    diagnose_parser = subparsers.add_parser(
        "diagnose",
        help="ops: run the read-only self-check; exit 0 when no error findings, 1 otherwise",
    )
    diagnose_parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="json",
        help="output format (default: json)",
    )
    diagnose_parser.add_argument(
        "--stale-after",
        type=float,
        default=300.0,
        help="seconds after which an in-progress journal counts as stale (default 300)",
    )
    diagnose_parser.add_argument(
        "--no-permissions",
        action="store_true",
        help="skip the file-permission check (diagnose.check_permissions)",
    )
    return parser


def _print_error(error: BaseException) -> None:
    print(canonical_json(error_envelope(error)), file=sys.stderr)


def _print_usage_error(message: str) -> None:
    """A CLI-usage failure envelope (exit 2); nothing reaches the store."""
    envelope = {"error": {"code": "CLI_USAGE", "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)


def _config_command(args: argparse.Namespace) -> int:
    """The ``config`` OPS command: layered configuration introspection (SUB-77/SUB-80).

    Never touches the store. The CLI's own explicit flags join the resolution
    as the top ``cli`` layer so the resolved view matches the invocation.
    """
    from know_your_soul.ops import config as ops_config

    cli_overrides: dict[str, Any] = {}
    if args.store != _default_store_path():
        cli_overrides["store.path"] = args.store
    if args.output_root != DEFAULT_OUTPUT_ROOT:
        cli_overrides["projection.output_root"] = args.output_root
    if args.actor != "operator":
        cli_overrides["ops.actor"] = args.actor
    if args.ops_root is not None:
        cli_overrides["ops.root"] = args.ops_root
    try:
        config = ops_config.load_config(file_path=args.config, cli_overrides=cli_overrides)
    except ops_config.ConfigError as error:
        _print_usage_error(str(error))
        return 2
    if args.config_command == "describe":
        print(ops_config.describe_config(config))
    elif args.config_command == "dump":
        print(canonical_json(config.dump()))
    else:  # effective
        print(canonical_json(config.effective()))
    return 0


def _metrics_command(args: argparse.Namespace) -> int:
    """The ``metrics`` OPS command: read-only operational metrics (SUB-77/SUB-80)."""
    from know_your_soul.ops import metrics as ops_metrics

    stack = build_stack(args.store, args.output_root, ops_root=args.ops_root)
    try:
        collected = ops_metrics.collect_ops_metrics(
            stack.store, projection_root=args.output_root, stale_after_seconds=args.stale_after
        )
    finally:
        stack.close()
    if args.format == "markdown":
        print(ops_metrics.render_markdown(collected))
    else:
        print(canonical_json(to_jsonable(collected)))
    return 0


def _diagnose_command(args: argparse.Namespace) -> int:
    """The ``diagnose`` OPS command: read-only self-check, exit code from the report (SUB-77/SUB-80)."""
    from know_your_soul.ops import diagnose as ops_diagnose

    stack = build_stack(args.store, args.output_root, ops_root=args.ops_root)
    try:
        report = ops_diagnose.run_diagnostics(
            stack.store,
            projection_root=args.output_root,
            stale_after_seconds=args.stale_after,
            check_permissions=not args.no_permissions,
        )
    finally:
        stack.close()
    if args.format == "markdown":
        print(ops_diagnose.render_markdown(report))
    else:
        payload = {
            "ok": report.ok,
            "error_count": report.error_count,
            "warning_count": report.warning_count,
            "worst_severity": report.worst_severity,
            "checked": list(report.checked),
            "findings": [
                {
                    "code": finding.code,
                    "severity": finding.severity,
                    "message": finding.message,
                    "details": finding.details,
                }
                for finding in report.findings
            ],
        }
        print(canonical_json(to_jsonable(payload)))
    return ops_diagnose.exit_code(report)


def _serve_command(args: argparse.Namespace) -> int:
    socket_path: str | None = args.socket
    http: str | None = args.http
    if socket_path is None and http is None:
        socket_path = DEFAULT_SOCKET_PATH
    # Wave-2 F6: the key can come from the environment so it never sits in
    # argv; a named-but-unset variable fails closed rather than serving
    # unauthenticated.
    api_key: str | None = args.api_key
    if args.api_key_env is not None:
        api_key = os.environ.get(args.api_key_env) or None
        if api_key is None:
            _print_usage_error(f"environment variable {args.api_key_env} is not set or is empty")
            return 2
    allowlist = (
        tuple(entry.strip() for entry in args.host_allowlist.split(",") if entry.strip())
        if args.host_allowlist
        else DEFAULT_HOST_ALLOWLIST
    )
    stack = build_stack(args.store, args.output_root, ops_root=args.ops_root)
    try:
        kwargs: dict[str, Any] = {"api_key": api_key, "host_allowlist": allowlist}
        if args.max_body_bytes is not None:
            kwargs["max_body_bytes"] = args.max_body_bytes
        if args.watch is not None:
            # Arch review F-1: the spec-13.5 doc-change watcher rides the
            # server's lifespan; detect-only, offers gated on the operator.
            kwargs["watch_root"] = args.watch
            kwargs["watch_interval_seconds"] = args.watch_interval
            kwargs["watch_cooldown_seconds"] = args.watch_cooldown
            if args.watch_state is not None:
                kwargs["watch_state_path"] = args.watch_state
        serve(stack, socket_path=socket_path, http=http, **kwargs)
    except (BindPolicyError, UpdatePipelineError) as error:
        _print_error(error)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        stack.close()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    command = str(args.command).replace("-", "_")
    if command == "serve":
        return _serve_command(args)
    if command in ("export", "analytics"):
        from know_your_soul.ops.export_cli import run_export_command

        return run_export_command(args, command=command)
    if command == "config":
        return _config_command(args)
    if command == "metrics":
        return _metrics_command(args)
    if command == "diagnose":
        return _diagnose_command(args)

    payload_text: str = args.payload if args.payload is not None else "{}"
    if payload_text == "-":
        payload_text = sys.stdin.read()

    stack = build_stack(args.store, args.output_root, ops_root=args.ops_root)
    try:
        result = dispatch_text(stack, command, payload_text, actor=args.actor, idempotency_key=args.idempotency_key)
    except AdapterValidationError as error:
        _print_error(error)
        return 2
    except Exception as error:  # noqa: BLE001 - every failure uses the envelope
        _print_error(error)
        return 1
    finally:
        stack.close()
    payload = to_jsonable(result)
    print(canonical_json(payload))
    if command == "health" and not payload.get("ok"):
        return 1
    return 0
