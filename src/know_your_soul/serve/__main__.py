"""CLI entry for the superpower server: ``python -m know_your_soul.serve``.

Modes:

* ``--daemon`` — spawn the detached daemon for one store and exit (the
  double-fork/setsid path; refuses when invoked from inside a daemon, so
  recursive spawn is impossible).
* ``--daemon-foreground`` — internal: the daemon process entry the spawner
  execs; not for operators.
* ``--proxy`` — run the thin MCP stdio proxy (host on stdin/stdout, daemon
  on the Unix socket), auto-spawning the daemon when absent.
* ``daemon status`` / ``daemon stop`` — inspect or stop the daemon bound to
  the store.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from know_your_soul.serve.daemon_config import DaemonConfig
from know_your_soul.serve.superpower import (
    DaemonUnavailable,
    daemon_status,
    run_daemon,
    spawn_daemon,
    stop_daemon,
)

_INTERNAL_ENV = "KNOW_YOUR_SOUL_DAEMON_INTERNAL"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="know-your-soul serve",
        description="Superpower server: detached multi-agent daemon + thin MCP stdio proxy.",
    )
    parser.add_argument("--db", required=True, help="path to the SQLite event store")
    parser.add_argument("--graph-root", required=True, help="the single allowlisted projection root")
    parser.add_argument(
        "--runtime-dir", default=None, help="daemon runtime directory (default: <store dir>/superpower)"
    )
    parser.add_argument("--max-agents", type=int, default=50)
    parser.add_argument("--read-pool-size", type=int, default=8)
    parser.add_argument(
        "--idle-timeout", type=float, default=300.0, help="seconds of no clients before the daemon exits"
    )
    parser.add_argument("--max-token-budget", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--actor", default=None, help="proxy mode: actor stamped on host requests without _meta.actor")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true", help="spawn the detached daemon and exit")
    mode.add_argument("--daemon-foreground", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--proxy", action="store_true", help="run the MCP stdio proxy")
    parser.add_argument("daemon_command", nargs="?", choices=("daemon",), default=None)
    parser.add_argument("daemon_action", nargs="?", choices=("status", "stop"), default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="with 'daemon stop': skip the drain window (queued work fails typed, exit code 2)",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> DaemonConfig:
    kwargs: dict = {
        "db_path": args.db,
        "graph_root": args.graph_root,
        "runtime_dir": args.runtime_dir,
        "max_agents": args.max_agents,
        "read_pool_size": args.read_pool_size,
        "idle_timeout_seconds": args.idle_timeout,
    }
    if args.max_token_budget is not None:
        kwargs["max_token_budget"] = args.max_token_budget
    return DaemonConfig(**kwargs)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _config_from_args(args)
    except Exception as error:  # noqa: BLE001 - config errors fail closed
        print(f"know-your-soul serve: invalid configuration: {error}", file=sys.stderr)
        return 2

    if args.daemon_command == "daemon":
        if args.daemon_action == "status":
            print(json.dumps(daemon_status(config), indent=2, sort_keys=True))
            return 0
        if args.daemon_action == "stop":
            try:
                print(json.dumps(stop_daemon(config, force=args.force), indent=2, sort_keys=True))
            except DaemonUnavailable as error:
                print(f"know-your-soul daemon stop: {error}", file=sys.stderr)
                return 1
            return 0
        print("know-your-soul daemon: expected 'status' or 'stop'", file=sys.stderr)
        return 2

    if args.daemon_foreground:
        return run_daemon(config)

    if args.daemon:
        if os.environ.get(_INTERNAL_ENV):
            print("know-your-soul serve: refusing recursive daemon spawn", file=sys.stderr)
            return 2
        try:
            result = spawn_daemon(config)
        except DaemonUnavailable as error:
            print(f"know-your-soul serve --daemon: {error}", file=sys.stderr)
            return 1
        status = "already_running" if result.already_running else "started"
        print(json.dumps({"status": status, "pid": result.pid, "socket": str(result.socket_path)}, sort_keys=True))
        return 0

    if args.proxy:
        from know_your_soul.serve.proxy import run_proxy

        return run_proxy(config, actor=args.actor)

    print("know-your-soul serve: choose --daemon, --proxy, or 'daemon status|stop'", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
