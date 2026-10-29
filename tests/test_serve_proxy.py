"""Stdio proxy tests (SUB-100).

The proxy is exercised as a real subprocess over pipes: local handshake
parity with the reference MCP server, actor stamping, forwarding, daemon
auto-spawn and recovery, and the PPID watchdog proven with a real
``kill -9`` of the host process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from _serve_helpers import mask_volatile, seed_store, tool_payload, wait_for

from know_your_soul import runtime_guard
from know_your_soul.serve import rpc
from know_your_soul.serve.daemon_config import DaemonConfig
from know_your_soul.serve.superpower import daemon_status, stop_daemon
from know_your_soul.store import EventStore


def _scrubbed_env() -> dict[str, str]:
    dirty = {**os.environ, "OPENAI_API_KEY": "sk-injected", "ANTHROPIC_API_KEY": "***"}
    env = runtime_guard.deterministic_env(dirty)
    assert runtime_guard.provider_credentials(env) == {}
    return env


def _proxy_args(db: Path, graph_root: Path, *extra: str) -> list[str]:
    return [
        "-m",
        "know_your_soul.serve",
        "--proxy",
        "--db",
        str(db),
        "--graph-root",
        str(graph_root),
        *extra,
    ]


def _daemon_args(db: Path, graph_root: Path, *extra: str) -> list[str]:
    return [
        "-m",
        "know_your_soul.serve",
        *extra,
        "--db",
        str(db),
        "--graph-root",
        str(graph_root),
    ]


@pytest.fixture()
def seeded(
    tmp_path: Path,
) -> Generator[tuple[Path, Path, DaemonConfig]]:
    graph_root, db = seed_store(tmp_path)
    config = DaemonConfig(db_path=db, graph_root=graph_root)
    yield graph_root, db, config
    # Best-effort daemon teardown between tests.
    try:
        stop_daemon(config, timeout_seconds=5.0)
    except Exception:  # noqa: BLE001 - teardown only
        pass


class _RawProxy:
    """A proxy subprocess driven with raw newline JSON frames."""

    def __init__(self, db: Path, graph_root: Path, *extra: str) -> None:
        self.process = subprocess.Popen(
            [sys.executable, *_proxy_args(db, graph_root, *extra)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_scrubbed_env(),
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self._ids = 0

    def request(self, method: str, params: dict[str, Any] | None = None, *, timeout: float = 30.0) -> dict[str, Any]:
        self._ids += 1
        frame: dict[str, Any] = {"jsonrpc": "2.0", "id": self._ids, "method": method}
        if params is not None:
            frame["params"] = params
        assert self.process.stdin is not None and self.process.stdout is not None
        self.process.stdin.write(json.dumps(frame).encode() + b"\n")
        self.process.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.process.stdout.readline()
            if line:
                return json.loads(line)  # type: ignore[no-any-return]
            if self.process.poll() is not None:
                stderr = self.process.stderr.read() if self.process.stderr else "<no stderr>"
                raise AssertionError(f"proxy exited {self.process.returncode}: {stderr!r}")
        raise AssertionError("proxy did not answer in time")

    def close(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            self.process.wait(timeout=15)

    def __enter__(self) -> _RawProxy:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class TestLocalHandshake:
    def test_initialize_matches_reference_shape(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        _, db, _ = seeded
        with _RawProxy(db, seeded[0]) as proxy:
            response = proxy.request(
                "initialize",
                {"protocolVersion": "2026-07-28", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
            )
        assert response["result"] == rpc.initialize_result(
            {"protocolVersion": "2026-07-28", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}
        )

    def test_handshake_parity_with_reference_server(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        """Proxy initialize + tools/list equal the reference mcp_server answers."""
        graph_root, db, _ = seeded
        reference = subprocess.Popen(
            [sys.executable, "-m", "know_your_soul.mcp_server", "--graph-root", str(graph_root), "--db", str(db)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_scrubbed_env(),
        )
        assert reference.stdin is not None and reference.stdout is not None

        def ask(proc: subprocess.Popen, request_id: int, method: str, params: dict[str, Any] | None = None) -> dict:
            frame: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                frame["params"] = params
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps(frame).encode() + b"\n")
            proc.stdin.flush()
            return json.loads(proc.stdout.readline())  # type: ignore[no-any-return]

        try:
            with _RawProxy(db, graph_root) as proxy:
                init_params = {
                    "protocolVersion": "2026-07-28",
                    "capabilities": {},
                    "clientInfo": {"name": "parity", "version": "0"},
                }
                via_proxy_init = proxy.request("initialize", init_params)["result"]
                via_proxy_tools = proxy.request("tools/list")["result"]
            via_reference_init = ask(reference, 1, "initialize", init_params)["result"]
            via_reference_tools = ask(reference, 2, "tools/list")["result"]
        finally:
            reference.terminate()
            reference.wait(timeout=10)
        assert via_proxy_init == via_reference_init
        assert via_proxy_tools == via_reference_tools

    def test_tools_list_sixteen(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        _, db, _ = seeded
        with _RawProxy(db, seeded[0]) as proxy:
            result = proxy.request("tools/list")["result"]
        assert len(result["tools"]) == 16

    def test_unknown_method_forwarded_to_typed_rejection(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        _, db, _ = seeded
        with _RawProxy(db, seeded[0]) as proxy:
            response = proxy.request("sampling/createMessage")
        assert response["error"]["code"] == rpc.METHOD_NOT_FOUND

    def test_admin_methods_blocked_at_proxy(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        _, db, _ = seeded
        with _RawProxy(db, seeded[0]) as proxy:
            response = proxy.request("daemon.status")
        assert response["error"]["code"] == rpc.SCOPE_DENIED


class TestForwardingAndAudit:
    def test_query_forwarded_matches_daemon_result(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        graph_root, db, config = seeded
        args = {"purpose": "run the gates", "domain": "P1", "token_budget": 512}
        with _RawProxy(db, graph_root) as proxy:
            via_proxy = tool_payload(proxy.request("tools/call", {"name": "knowledge_query", "arguments": args}))
        from _serve_helpers import SocketClient

        with SocketClient(config.paths.socket) as direct:
            via_daemon = tool_payload(direct.call_tool("knowledge_query", args, actor="spawn-test"))
        assert mask_volatile(via_proxy) == mask_volatile(via_daemon)
        status = daemon_status(config)
        assert status["running"] is True
        # The proxy stamped its own actor; the daemon audited it.
        assert "mcp-proxy" in status["per_actor"]

    def test_host_actor_is_preserved(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        graph_root, db, config = seeded
        with _RawProxy(db, graph_root) as proxy:
            proxy.request(
                "tools/call",
                {
                    "name": "knowledge_query",
                    "arguments": {"purpose": "run the gates", "domain": "P1", "token_budget": 512},
                    "_meta": {"actor": "agent-007"},
                },
            )
        status = daemon_status(config)
        assert "agent-007" in status["per_actor"]

    def test_stamped_actor_reaches_audit_journal(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        graph_root, db, config = seeded
        with _RawProxy(db, graph_root) as proxy:
            proxy.request(
                "tools/call",
                {"name": "knowledge_query", "arguments": {"purpose": "run the gates", "token_budget": 512}},
            )
        stop_daemon(config)
        store = EventStore(db)
        try:
            with store.read() as conn:
                actors = [row[0] for row in conn.execute("SELECT actor FROM invocation_journal")]
        finally:
            store.close()
        assert "mcp-proxy" in actors

    def test_write_through_proxy_lands_in_store(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        graph_root, db, config = seeded
        node = {
            "node_id": "n-proxy-write",
            "kind": "leaf",
            "domain": "P1",
            "summary": "written through the proxy",
            "provenance": ["soul://test"],
            "source_version_id": "sv-test",
        }
        with _RawProxy(db, graph_root) as proxy:
            written = proxy.request("tools/call", {"name": "knowledge_log_event", "arguments": {"node": node}})
            assert not written["result"].get("isError"), written
            read = proxy.request(
                "tools/call", {"name": "knowledge_get_node", "arguments": {"node_id": "n-proxy-write"}}
            )
        assert "n-proxy-write" in json.dumps(tool_payload(read))


class TestOfficialClient:
    def test_official_mcp_client_session(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        graph_root, db, _ = seeded

        async def scenario() -> None:
            params = StdioServerParameters(
                command=sys.executable,
                args=_proxy_args(db, graph_root),
                env=_scrubbed_env(),
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    initialized = await session.initialize()
                    assert initialized.server_info is not None
                    assert initialized.server_info.name == "know-your-soul"
                    capabilities = session.server_capabilities
                    assert capabilities is not None
                    advertised = capabilities.model_dump(exclude_none=True)
                    assert set(advertised) == {"tools", "resources"}
                    listed = await session.list_tools()
                    assert len(listed.tools) == 16
                    result = await session.call_tool(
                        "knowledge_query",
                        {"purpose": "run the gates", "domain": "P1", "token_budget": 512},
                    )
                    assert not result.is_error
                    node = await session.call_tool("knowledge_get_node", {"node_id": "n-gates"})
                    assert not node.is_error

        anyio.run(scenario)


class TestLifecycle:
    def test_proxy_auto_spawns_daemon(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        graph_root, db, config = seeded
        assert daemon_status(config)["running"] is False
        with _RawProxy(db, graph_root) as proxy:
            assert proxy.request("ping")["result"] == {}
        assert daemon_status(config)["running"] is True

    def test_proxy_exits_on_stdin_eof_daemon_survives(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        graph_root, db, config = seeded
        with _RawProxy(db, graph_root) as proxy:
            proxy.request("ping")
        # close() ended stdin: the proxy must have exited cleanly.
        assert wait_for(lambda: proxy.process.poll() is not None, timeout=15)
        assert proxy.process.returncode == 0
        assert daemon_status(config)["running"] is True

    def test_proxy_recovers_from_daemon_death(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        graph_root, db, config = seeded
        with _RawProxy(db, graph_root) as proxy:
            proxy.request("ping")
            # Simulate a daemon crash.
            status = daemon_status(config)
            pid = status.get("pid")
            assert pid
            os.kill(int(pid), signal.SIGKILL)
            # The next forwarded call triggers respawn and succeeds.
            response = proxy.request(
                "tools/call",
                {"name": "knowledge_get_node", "arguments": {"node_id": "n-gates"}},
                timeout=30.0,
            )
            assert "result" in response

    def test_ppid_watchdog_kills_proxy_when_host_dies(self, seeded: tuple[Path, Path, DaemonConfig]) -> None:
        """kill -9 the host: no stdin EOF is owed; the watchdog must fire.

        The test process holds the proxy's stdin open across the host's
        death (the pipe write end stays here), so only the PPID watchdog can
        end the proxy.
        """
        graph_root, db, config = seeded
        proxy_stdin_r, proxy_stdin_w = os.pipe()
        host = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import subprocess, sys, time\n"
                f"proxy = subprocess.Popen([sys.executable, *{json.dumps(_proxy_args(db, graph_root))}], "
                "stdin=int(sys.argv[1]), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                "print(proxy.pid, flush=True)\n"
                "time.sleep(120)\n",
                str(proxy_stdin_r),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_scrubbed_env(),
            pass_fds=(proxy_stdin_r,),
        )
        os.close(proxy_stdin_r)
        try:
            assert host.stdout is not None
            proxy_pid = int(host.stdout.readline().strip())
            assert wait_for(lambda: _pid_alive(proxy_pid), timeout=10)
            # Wait until the proxy is fully serving (its auto-spawned daemon
            # is ready) so the watchdog has captured the host's pid before
            # the host dies.
            assert wait_for(lambda: config.paths.ready.exists(), timeout=30)
            os.kill(host.pid, signal.SIGKILL)
            host.wait(timeout=10)
            assert wait_for(lambda: not _pid_alive(proxy_pid), timeout=15), (
                f"proxy {proxy_pid} survived its host's SIGKILL"
            )
        finally:
            os.close(proxy_stdin_w)
            if host.poll() is None:
                host.kill()
                host.wait(timeout=5)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(pid, 0); it is not alive for our purposes.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[1] == "Z":
            return False
    except OSError, IndexError:
        pass
    return True
