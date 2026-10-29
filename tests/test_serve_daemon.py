"""In-process daemon tests (SUB-100).

The daemon runs threaded against a real Unix socket in tmp_path; clients are
real socket connections. These tests pin dispatch, capacity, scope,
lifecycle and audit behavior — everything except the detached spawn itself
(test_serve_spawn.py) and the stdio proxy (test_serve_proxy.py).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from _serve_helpers import (
    EXPECTED_TOOLS_16,
    SocketClient,
    make_config,
    tool_error_code,
    tool_payload,
    wait_for,
)
from know_your_soul.serve import rpc
from know_your_soul.serve.superpower import (
    READ_TOOLS,
    WRITE_TOOLS,
    LivenessWatchdog,
    SuperpowerDaemon,
    classify_tool,
    daemon_status,
    stop_daemon,
)


@pytest.fixture()
def daemon(tmp_path: Path):
    """A started in-process daemon; shut down on teardown."""
    config = make_config(tmp_path, idle_timeout_seconds=120.0)
    instance = SuperpowerDaemon(config)
    instance.start()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, config
    instance.shutdown("test-teardown")
    thread.join(timeout=10)


def _query_args() -> dict:
    return {"purpose": "run the gates", "domain": "P1", "token_budget": 512}


class TestClassification:
    def test_lanes_partition_the_frozen_sixteen(self) -> None:
        from know_your_soul.serve.superpower import HEAVY_TOOLS

        assert READ_TOOLS | WRITE_TOOLS | HEAVY_TOOLS == frozenset(EXPECTED_TOOLS_16)
        assert not (READ_TOOLS & WRITE_TOOLS)
        assert not (READ_TOOLS & HEAVY_TOOLS)
        assert not (WRITE_TOOLS & HEAVY_TOOLS)
        # Lane sizes follow write semantics: evaluate (score materialization)
        # and review (decision record) persist, so they ride the writer lane.
        assert len(READ_TOOLS) == 7
        assert len(WRITE_TOOLS) == 7
        assert len(HEAVY_TOOLS) == 2

    @pytest.mark.parametrize("tool", sorted(READ_TOOLS))
    def test_read_verbs(self, tool: str) -> None:
        assert classify_tool(tool) == "read"

    @pytest.mark.parametrize("tool", sorted(WRITE_TOOLS))
    def test_write_verbs(self, tool: str) -> None:
        assert classify_tool(tool) == "write"

    @pytest.mark.parametrize("tool", ["knowledge_ingest_file", "knowledge_ingest_directory"])
    def test_heavy_verbs(self, tool: str) -> None:
        assert classify_tool(tool) == "heavy"

    def test_unknown_verb_rejected(self) -> None:
        from know_your_soul.serve.superpower import UnknownVerb

        with pytest.raises(UnknownVerb):
            classify_tool("filesystem_delete")


class TestHandshakeAndMeta:
    def test_initialize_shape(self, daemon) -> None:
        instance, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.request(
                "initialize",
                {"protocolVersion": "2026-07-28", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
            )
        result = response["result"]
        assert result["protocolVersion"] == "2025-11-25"
        assert set(result["capabilities"]) == {"tools", "resources"}
        assert result["serverInfo"]["name"] == "know-your-soul"

    def test_tools_list_sixteen(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            result = client.request("tools/list")["result"]
        names = tuple(tool["name"] for tool in result["tools"])
        assert names == EXPECTED_TOOLS_16

    def test_tools_list_matches_reference_server(self, daemon) -> None:
        """tools/list through the daemon equals the reference adapter surface."""
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            via_daemon = client.request("tools/list")["result"]
        assert via_daemon == rpc.tool_listing_result()

    def test_ping(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            assert client.request("ping")["result"] == {}

    def test_notification_gets_no_response(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            client.notify("notifications/initialized")
            # A follow-up request must arrive; no frame answered the notification.
            response = client.request("ping")
            assert response["result"] == {}

    def test_unknown_method_is_method_not_found(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.request("os/system")
        assert response["error"]["code"] == rpc.METHOD_NOT_FOUND

    def test_resources_list_and_read(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            listed = client.request("resources/list")["result"]
            uris = [resource["uri"] for resource in listed["resources"]]
            assert "soul://projection/manifest" in uris
            read = client.request("resources/read", {"uri": "soul://projection/manifest"})["result"]
            assert read["contents"][0]["mimeType"] == "application/json"

    def test_unknown_resource_rejected(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.request("resources/read", {"uri": "file:///etc/passwd"})
        assert response["error"]["code"] == rpc.INVALID_PARAMS


class TestDispatch:
    def test_query_through_read_pool(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_query", _query_args())
        payload = tool_payload(response)
        assert "routing" in payload or "results" in payload

    def test_get_node(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_get_node", {"node_id": "n-gates"})
        payload = tool_payload(response)
        assert payload.get("node", {}).get("node_id") == "n-gates" or "n-gates" in json.dumps(payload)

    def test_write_then_read_back(self, daemon) -> None:
        _, config = daemon
        node = {
            "node_id": "n-daemon-write",
            "kind": "leaf",
            "domain": "P1",
            "summary": "written through the daemon write lane",
            "provenance": ["soul://test"],
            "source_version_id": "sv-test",
        }
        with SocketClient(config.paths.socket) as client:
            written = client.call_tool("knowledge_log_event", {"node": node})
            assert not written["result"].get("isError"), written
            read = client.call_tool("knowledge_get_node", {"node_id": "n-daemon-write"})
        assert "n-daemon-write" in json.dumps(tool_payload(read))

    def test_unknown_tool_is_typed(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_wipe_all", {})
        assert response["error"]["code"] == rpc.UNKNOWN_VERB

    def test_actor_required(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_query", _query_args(), actor=None)
        assert response["error"]["code"] == rpc.ACTOR_REQUIRED

    def test_empty_actor_rejected(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_query", _query_args(), actor="   ")
        assert response["error"]["code"] == rpc.ACTOR_REQUIRED

    def test_non_string_actor_rejected(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.request(
                "tools/call",
                {"name": "knowledge_query", "arguments": _query_args(), "_meta": {"actor": 42}},
            )
        assert response["error"]["code"] == rpc.ACTOR_REQUIRED

    def test_invalid_arguments_are_typed_iserror(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_query", {"purpose": "x"})  # token_budget missing
        assert response["result"]["isError"] is True
        assert tool_error_code(response) == "INVALID_ARGUMENTS"

    def test_envelope_escape_hatch_rejected(self, daemon) -> None:
        """No outer-envelope path field may reach the store (scope enforcement)."""
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            client.send_raw(
                json.dumps(
                    {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {}, "db_path": "/etc/passwd"}
                ).encode()
                + b"\n"
            )
            response = client.read_response()
        assert response["error"]["code"] == rpc.MALFORMED_REQUEST

    def test_ingest_path_escape_is_confined(self, daemon) -> None:
        """The v3 ingestion path is resolved inside the fixed root, never outside."""
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_ingest_file", {"path": "../../etc/hostname"})
        assert response["result"]["isError"] is True
        assert tool_error_code(response) != "INTERNAL_ERROR"

    def test_oversized_frame_typed_then_closed(self, daemon) -> None:
        instance, config = daemon
        with SocketClient(config.paths.socket) as client:
            pad = b"x" * (config.max_frame_bytes + 16)
            oversized = b'{"jsonrpc":"2.0","id":1,"method":"ping","pad":"' + pad + b'"}\n'
            client.send_raw(oversized)
            response = client.read_response()
            assert response["error"]["code"] == rpc.PAYLOAD_TOO_LARGE
            with pytest.raises(ConnectionError):
                client.read_response(timeout=2.0)


class TestCapacity:
    def test_agent_capacity_with_typed_busy(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, max_agents=2, idle_timeout_seconds=120.0)
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            clients = [SocketClient(config.paths.socket) for _ in range(2)]
            try:
                for client in clients:
                    assert client.request("ping")["result"] == {}
                # The third connection is accepted-then-refused with typed backpressure.
                third = socket_client_raw(config)
                frame = third.readline()
                payload = json.loads(frame.decode())
                assert payload["error"]["code"] == rpc.SERVER_BUSY
                assert payload["error"]["data"]["retry_after_ms"] > 0
                third.close()
            finally:
                for client in clients:
                    client.close()
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=10)

    def test_stats_track_actors(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            client.call_tool("knowledge_query", _query_args(), actor="agent-a")
            client.call_tool("knowledge_query", _query_args(), actor="agent-a")
            client.call_tool("knowledge_get_node", {"node_id": "n-gates"}, actor="agent-b")
            stats = client.request("daemon.status")["result"]
        assert stats["per_actor"]["agent-a"]["requests"] == 2
        assert stats["per_actor"]["agent-b"]["requests"] == 1
        assert stats["sessions_active"] >= 1

    def test_admin_status_succeeds_at_capacity(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, max_agents=1, idle_timeout_seconds=120.0)
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            with SocketClient(config.paths.socket):
                status = daemon_status(config)
                assert status["running"] is True
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=10)


class TestLifecycle:
    def test_double_start_fails_closed(self, daemon) -> None:
        from know_your_soul.serve.superpower import DaemonUnavailable

        instance, config = daemon
        with pytest.raises(DaemonUnavailable):
            SuperpowerDaemon(config).start()

    def test_shutdown_removes_artifacts_and_writes_exit(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, idle_timeout_seconds=120.0)
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        instance.shutdown("operator")
        assert thread.join(timeout=10) is None
        assert not thread.is_alive()
        assert not config.paths.socket.exists()
        assert not config.paths.lock.exists()
        assert not config.paths.pid.exists()
        exit_report = json.loads(config.paths.exit.read_text(encoding="utf-8"))
        assert exit_report["reason"] == "operator"

    def test_idle_timeout_reaps_quiet_daemon(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, idle_timeout_seconds=0.6)
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        assert wait_for(lambda: not config.paths.lock.exists(), timeout=10.0), "idle daemon never exited"
        thread.join(timeout=10)
        exit_report = json.loads(config.paths.exit.read_text(encoding="utf-8"))
        assert exit_report["reason"] == "idle"

    def test_stop_daemon_via_socket(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, idle_timeout_seconds=120.0)
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        report = stop_daemon(config, timeout_seconds=10.0)
        assert report["stopped"] is True
        thread.join(timeout=10)
        assert not config.paths.lock.exists()

    def test_status_when_not_running(self, tmp_path: Path) -> None:
        config = make_config(tmp_path)
        status = daemon_status(config)
        assert status["running"] is False


class TestLivenessWatchdog:
    def test_stall_fires_callback(self) -> None:
        fired = threading.Event()
        clock = {"now": 1000.0}
        watchdog = LivenessWatchdog(
            stall_seconds=5.0,
            interval_seconds=1.0,
            on_stall=fired.set,
            now=lambda: clock["now"],
        )
        assert watchdog.check_once() is False
        clock["now"] += 6.0
        assert watchdog.check_once() is True
        assert fired.is_set()

    def test_heartbeat_resets_stall(self) -> None:
        fired = threading.Event()
        clock = {"now": 0.0}
        watchdog = LivenessWatchdog(
            stall_seconds=5.0,
            interval_seconds=1.0,
            on_stall=fired.set,
            now=lambda: clock["now"],
        )
        clock["now"] = 4.0
        watchdog.beat()
        clock["now"] = 8.0
        assert watchdog.check_once() is False
        assert not fired.is_set()

    def test_watchdog_kills_starved_process(self, tmp_path: Path) -> None:
        """Real process proof: a child that never beats dies with exit 70."""
        import subprocess
        import sys

        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import time\n"
                "from know_your_soul.serve.superpower import LivenessWatchdog\n"
                "w = LivenessWatchdog(stall_seconds=0.4, interval_seconds=0.1)\n"
                "w.start()\n"
                "time.sleep(10)\n",
            ],
        )
        returncode = child.wait(timeout=15)
        assert returncode == 70


def socket_client_raw(config) -> AnySocket:
    import socket as _socket

    sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    sock.settimeout(5.0)
    sock.connect(str(config.paths.socket))
    return sock.makefile("rb")


from typing import IO as AnySocket  # noqa: E402
