"""The ``soul-knowledge`` command-line interface (Task 7).

Task 7 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7, 8 and 13.1.

A thin adapter over :mod:`soul_knowledge.http_api`'s shared dispatch: every
command validates its JSON payload, invokes the ten-operation
``KnowledgeService`` (or the operational commands ``rebuild``, ``backup``,
``restore``, ``health``) through the same code path as the HTTP API, prints
the canonical JSON result on stdout, and prints the consistent error envelope
on stderr. Exit codes:

* ``0`` — success (``health`` additionally requires ``ok: true``);
* ``1`` — the service rejected a valid request (typed error, journaled by the
  service exactly once);
* ``2`` — the adapter rejected invalid input (journaled with exactly one
  completion audit record by the adapter).

``serve`` starts the persistent local API: a Unix socket by default, or
opt-in loopback HTTP with ``--http HOST:PORT``; wider binds fail closed
without ``--api-key``.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from soul_knowledge.domain import canonical_json
from soul_knowledge.http_api import (
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
from soul_knowledge.security import BindPolicyError

__all__ = ["build_parser", "main"]

DEFAULT_STORE_PATH = "soul-knowledge.sqlite3"
DEFAULT_OUTPUT_ROOT = "soul-knowledge-projection"
DEFAULT_SOCKET_PATH = "soul-knowledge.sock"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soul-knowledge",
        description="Deterministic local knowledge service: ten audited operations plus ops commands.",
    )
    parser.add_argument("--store", default=DEFAULT_STORE_PATH, help="path to the SQLite event store")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT, help="projection output directory")
    parser.add_argument("--actor", default="operator", help="actor recorded on the invocation audit")
    parser.add_argument("--idempotency-key", default=None, help="idempotency key for mutation commands")
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
    serve_parser.add_argument("--api-key", default=None, help="API key required for non-loopback binds/clients")
    serve_parser.add_argument(
        "--host-allowlist",
        default=None,
        help="comma-separated Host allowlist (default: localhost,127.0.0.1,::1)",
    )
    serve_parser.add_argument("--max-body-bytes", type=int, default=None, help="request body size cap")
    return parser


def _print_error(error: BaseException) -> None:
    print(canonical_json(error_envelope(error)), file=sys.stderr)


def _serve_command(args: argparse.Namespace) -> int:
    socket_path: str | None = args.socket
    http: str | None = args.http
    if socket_path is None and http is None:
        socket_path = DEFAULT_SOCKET_PATH
    allowlist = (
        tuple(entry.strip() for entry in args.host_allowlist.split(",") if entry.strip())
        if args.host_allowlist
        else DEFAULT_HOST_ALLOWLIST
    )
    stack = build_stack(args.store, args.output_root)
    try:
        kwargs: dict[str, Any] = {"api_key": args.api_key, "host_allowlist": allowlist}
        if args.max_body_bytes is not None:
            kwargs["max_body_bytes"] = args.max_body_bytes
        serve(stack, socket_path=socket_path, http=http, **kwargs)
    except BindPolicyError as error:
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

    payload_text: str = args.payload if args.payload is not None else "{}"
    if payload_text == "-":
        payload_text = sys.stdin.read()

    stack = build_stack(args.store, args.output_root)
    try:
        result = dispatch_text(
            stack, command, payload_text, actor=args.actor, idempotency_key=args.idempotency_key
        )
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
