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
    run_ingest_verb,
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
    # Wave-7 SUB-100: superpower server modes. Without these flags `serve`
    # remains the HTTP/unix local API; with them it manages the detached
    # multi-agent daemon or runs the thin MCP stdio proxy.
    superpower = serve_parser.add_mutually_exclusive_group()
    superpower.add_argument(
        "--daemon",
        action="store_true",
        help="superpower: spawn the detached multi-agent daemon (double-fork/setsid) and exit",
    )
    superpower.add_argument(
        "--proxy",
        action="store_true",
        help="superpower: run the thin MCP stdio proxy (auto-spawns the daemon)",
    )
    superpower.add_argument("--daemon-foreground", action="store_true", help=argparse.SUPPRESS)
    serve_parser.add_argument(
        "--max-agents", type=int, default=50, help="superpower: concurrent agent cap (default 50)"
    )
    serve_parser.add_argument(
        "--read-pool-size", type=int, default=8, help="superpower: read-only pool size (default 8)"
    )
    serve_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=300.0,
        help="superpower: seconds with no clients before the daemon exits (default 300)",
    )

    daemon_parser = subparsers.add_parser("daemon", help="superpower: inspect or stop the daemon bound to --store")
    daemon_parser.add_argument("daemon_action", choices=("status", "stop"))
    daemon_parser.add_argument(
        "--force",
        action="store_true",
        help="with stop: skip the drain window (queued work fails typed, daemon exits 2)",
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

    # W7 SUB-101: loopback-only read-only web viewer. OPS surface only — not
    # a knowledge operation, never dispatched over MCP/HTTP.
    webview_parser = subparsers.add_parser(
        "webview",
        help="ops: open the loopback-only token-authenticated web viewer (not a knowledge op)",
    )
    webview_parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="loopback port; 0 picks a free one (default)",
    )
    webview_parser.add_argument(
        "--no-open",
        action="store_true",
        help="print the URL but do not open a browser",
    )

    # Wave-2 SUB-89: universal-graph ingestion. OPS surface only — NOT a
    # knowledge operation, never joins KNOWLEDGE_OPERATIONS / OPS_OPERATIONS,
    # never dispatched over MCP or HTTP; the frozen fourteen are untouched.
    ingest_parser = subparsers.add_parser(
        "ingest",
        help="ops: ingest a file or directory into the v3 ingestion tables (proposal-only, not a knowledge op)",
    )
    ingest_parser.add_argument("path", help="file or directory to ingest (the ingestion root)")
    ingest_parser.add_argument(
        "--incremental",
        dest="incremental",
        action="store_true",
        default=True,
        help="skip unchanged files via the (size, mtime) pre-filter + sha256 confirm (default)",
    )
    ingest_parser.add_argument(
        "--no-incremental",
        dest="incremental",
        action="store_false",
        help="force a full re-ingest of every file",
    )
    ingest_parser.add_argument(
        "--recursive",
        dest="recursive",
        action="store_true",
        default=True,
        help="walk subdirectories (default)",
    )
    ingest_parser.add_argument("--no-recursive", dest="recursive", action="store_false", help="only the top level")
    ingest_parser.add_argument(
        "--no-resolve",
        action="store_true",
        help="park cross-file references without running the resolve pass",
    )
    ingest_parser.add_argument("--max-files", type=int, default=1024, help="walk cap, [1, 65536] (default 1024)")
    ingest_parser.add_argument("--max-nodes", type=int, default=65536, help="fail-closed total node cap")
    ingest_parser.add_argument("--max-edges", type=int, default=131072, help="fail-closed total edge cap")
    ingest_parser.add_argument("--max-refs", type=int, default=65536, help="fail-closed total unresolved-ref cap")
    ingest_parser.add_argument(
        "--max-file-bytes", type=int, default=4 * 1024 * 1024, help="per-file size cap (default 4 MiB)"
    )
    ingest_parser.add_argument("--json", action="store_true", help="print the canonical JSON report")

    # Contract v3 (addendum docs/contracts/ingestion-contract-v3.md; audit
    # F2): the frozen adapter mapping names ``ingest-file`` /
    # ``ingest-directory`` as the CLI surface of the two ingestion verbs.
    # They run the same shared adapter helper as the HTTP/MCP surfaces
    # (contract input validation, path discipline against the fixed
    # ingestion root — the process working directory for the CLI — and the
    # output budget shaping) and print the canonical JSON proposal payload.
    # The ops ``ingest`` command above is untouched.
    ingest_file_parser = subparsers.add_parser(
        "ingest-file",
        help="knowledge.ingest_file: ingest one file under the working directory (contract v3, proposal-only)",
    )
    ingest_file_parser.add_argument("path", help="file to ingest (resolved against the working directory)")
    ingest_file_parser.add_argument(
        "--token-budget", type=int, default=1024, help="response token budget, [128, max] (default 1024)"
    )
    ingest_directory_parser = subparsers.add_parser(
        "ingest-directory",
        help="knowledge.ingest_directory: ingest a directory under the working directory (contract v3, proposal-only)",
    )
    ingest_directory_parser.add_argument("path", help="directory to ingest (resolved against the working directory)")
    ingest_directory_parser.add_argument(
        "--recursive",
        dest="recursive",
        action="store_true",
        default=True,
        help="walk subdirectories (default)",
    )
    ingest_directory_parser.add_argument(
        "--no-recursive", dest="recursive", action="store_false", help="only the top level"
    )
    ingest_directory_parser.add_argument(
        "--max-files", type=int, default=1024, help="walk cap, [1, 65536] (default 1024)"
    )
    ingest_directory_parser.add_argument(
        "--token-budget", type=int, default=1024, help="response token budget, [128, max] (default 1024)"
    )

    # Wave-4 SUB-92: polling watcher over the ingestion pipeline. OPS surface
    # only — same rules as `ingest`: never a knowledge op, never MCP/HTTP.
    watch_parser = subparsers.add_parser(
        "watch",
        help="ops: poll a directory and incrementally ingest changes (proposal-only, not a knowledge op)",
    )
    watch_parser.add_argument("path", help="directory to watch (the ingestion root)")
    watch_parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="seconds between polls (default 2.0; hard floor 0.05 — no busy-loop)",
    )
    watch_parser.add_argument("--once", action="store_true", help="run a single cycle and exit (tests/CI)")
    watch_parser.add_argument("--max-cycles", type=int, default=None, help="stop after N cycles")
    watch_parser.add_argument(
        "--debounce", type=float, default=2.0, help="full debounce window in seconds for bursts (default 2.0)"
    )
    watch_parser.add_argument(
        "--quick-quiet",
        type=float,
        default=0.3,
        help="quiet window in seconds when at most --quick-max-changes files are pending (default 0.3)",
    )
    watch_parser.add_argument("--quick-max-changes", type=int, default=2, help="quick-path quiet threshold (default 2)")
    watch_parser.add_argument(
        "--full-scan-ceiling",
        type=int,
        default=500,
        help="above this many pending changes (or any removal) a full scan-diff replaces per-file ingest",
    )
    watch_parser.add_argument(
        "--max-failures",
        type=int,
        default=5,
        help="auto-degrade (log + stop, never crash) after this many consecutive cycle failures",
    )
    watch_parser.add_argument(
        "--force",
        action="store_true",
        help="override the WSL/remote-mount refusal (KNOW_YOUR_SOUL_NO_WATCH still wins)",
    )
    watch_parser.add_argument("--json", action="store_true", help="print the canonical JSON watch report")
    return parser


def _print_error(error: BaseException) -> None:
    print(canonical_json(error_envelope(error)), file=sys.stderr)


def _print_usage_error(message: str) -> None:
    """A CLI-usage failure envelope (exit 2); nothing reaches the store."""
    envelope = {"error": {"code": "CLI_USAGE", "message": message, "retryable": False, "audit_event_id": None}}
    print(canonical_json(envelope), file=sys.stderr)


def _ingest_verb_command(args: argparse.Namespace, command: str) -> int:
    """The contract-v3 ingestion verbs (``ingest-file``/``ingest-directory``, audit F2).

    The same shared adapter helper as the HTTP/MCP surfaces: contract input
    validation, path discipline against the fixed ingestion root (the process
    working directory — fixed at process startup) and the output budget
    shaping; the canonical JSON proposal goes to stdout, typed failures use
    the standard envelope on stderr. Exit 0 success, 1 typed failure.
    """
    from know_your_soul.audit import AuditLog
    from know_your_soul.ingest.pipeline import ExtractorUnavailableError, IngestionError
    from know_your_soul.store import EventStore

    arguments: dict[str, Any] = {"path": args.path, "token_budget": args.token_budget}
    if command == "ingest_directory":
        arguments["recursive"] = args.recursive
        arguments["max_files"] = args.max_files
    store = EventStore(args.store)
    try:
        payload = run_ingest_verb(
            store,
            AuditLog(store),
            command,
            arguments,
            actor=args.actor,
            ingestion_root=os.getcwd(),
        )
    except (IngestionError, ExtractorUnavailableError) as error:
        envelope = {
            "error": {
                "code": error.code,
                "message": str(error),
                "retryable": False,
                "audit_event_id": getattr(error, "audit_event_id", None),
            }
        }
        print(canonical_json(envelope), file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - every failure uses the envelope
        _print_error(error)
        return 1
    finally:
        store.close()
    print(canonical_json(payload))
    return 0


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


def _serve_superpower_command(args: argparse.Namespace) -> int:
    """Wave-7 SUB-100: detached superpower daemon + thin MCP stdio proxy."""
    import json as _json

    from know_your_soul.serve.daemon_config import ConfigError, DaemonConfig
    from know_your_soul.serve.superpower import DaemonUnavailable, run_daemon, spawn_daemon

    try:
        config = DaemonConfig(
            db_path=args.store,
            graph_root=args.output_root,
            max_agents=args.max_agents,
            read_pool_size=args.read_pool_size,
            idle_timeout_seconds=args.idle_timeout,
        )
    except ConfigError as error:
        _print_usage_error(f"invalid superpower configuration: {error}")
        return 2
    if args.daemon_foreground:
        return run_daemon(config)
    if args.proxy:
        from know_your_soul.serve.proxy import run_proxy

        return run_proxy(config, actor=args.actor)
    if os.environ.get("KNOW_YOUR_SOUL_DAEMON_INTERNAL"):
        print("know-your-soul serve --daemon: refusing recursive daemon spawn", file=sys.stderr)
        return 2
    try:
        result = spawn_daemon(config)
    except DaemonUnavailable as error:
        print(f"know-your-soul serve --daemon: {error}", file=sys.stderr)
        return 1
    status = "already_running" if result.already_running else "started"
    print(_json.dumps({"status": status, "pid": result.pid, "socket": str(result.socket_path)}, sort_keys=True))
    return 0


def _daemon_command(args: argparse.Namespace) -> int:
    """Wave-7 SUB-100: `daemon status|stop` against the store's daemon."""
    import json as _json

    from know_your_soul.serve.daemon_config import ConfigError, DaemonConfig
    from know_your_soul.serve.superpower import DaemonUnavailable, daemon_status, stop_daemon

    try:
        config = DaemonConfig(db_path=args.store, graph_root=args.output_root)
    except ConfigError as error:
        _print_usage_error(f"invalid superpower configuration: {error}")
        return 2
    if args.daemon_action == "status":
        print(_json.dumps(daemon_status(config), indent=2, sort_keys=True))
        return 0
    try:
        print(_json.dumps(stop_daemon(config, force=args.force), indent=2, sort_keys=True))
    except DaemonUnavailable as error:
        print(f"know-your-soul daemon stop: {error}", file=sys.stderr)
        return 1
    return 0


def _serve_command(args: argparse.Namespace) -> int:
    if args.daemon or args.proxy or args.daemon_foreground:
        return _serve_superpower_command(args)
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
    if command == "daemon":
        return _daemon_command(args)
    if command in ("export", "analytics"):
        from know_your_soul.ops.export_cli import run_export_command

        return run_export_command(args, command=command)
    if command == "config":
        return _config_command(args)
    if command == "metrics":
        return _metrics_command(args)
    if command == "diagnose":
        return _diagnose_command(args)
    if command == "webview":
        from know_your_soul.serve.webview import main as webview_main

        webview_argv = ["--store", args.store, "--port", str(args.port)]
        if not args.no_open:
            webview_argv.append("--open")
        return webview_main(webview_argv)
    if command == "ingest":
        from know_your_soul.ops.ingest_cli import run_ingest_command

        return run_ingest_command(args)
    if command in ("ingest_file", "ingest_directory"):
        return _ingest_verb_command(args, command)
    if command == "watch":
        from know_your_soul.ops.watch_cli import run_watch_command

        return run_watch_command(args)

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
