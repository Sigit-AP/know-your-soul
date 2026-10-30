"""Superpower-server integration for the contract-v4 extraction verb (SUB-106).

``knowledge_submit_extraction`` is a write-lane tool: the per-agent admission
token bucket applies exactly as to every other write, the lane sets still
partition the tool surface, and a real socket round-trip lands the
submission in the durable queue.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from _extraction_helpers import make_args
from _serve_helpers import SocketClient, make_config, tool_error_code, tool_payload
from know_your_soul.serve import rpc
from know_your_soul.serve.superpower import (
    HEAVY_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    SuperpowerDaemon,
    classify_tool,
)


class TestLanePlacement:
    def test_submit_extraction_is_a_write_tool(self) -> None:
        assert "knowledge_submit_extraction" in WRITE_TOOLS
        assert "knowledge_submit_extraction" not in HEAVY_TOOLS
        assert classify_tool("knowledge_submit_extraction") == "write"

    def test_lanes_partition_the_extended_surface(self) -> None:
        from know_your_soul.mcp_server import EXPECTED_TOOLS

        assert frozenset(EXPECTED_TOOLS) == frozenset(READ_TOOLS | WRITE_TOOLS | HEAVY_TOOLS)
        assert len(EXPECTED_TOOLS) == 17


@pytest.fixture()
def daemon(tmp_path: Path):
    config = make_config(tmp_path, idle_timeout_seconds=120.0)
    instance = SuperpowerDaemon(config)
    instance.start()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, config
    instance.shutdown("test-teardown")
    thread.join(timeout=10)


class TestDaemonRoundTrip:
    def test_submit_over_the_socket(self, daemon) -> None:
        instance, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_submit_extraction", make_args(), actor="agent-a")
            payload = tool_payload(response)
            assert payload["receipt_id"].startswith("ext-")
            assert payload["status"] == "pending_review"
            assert payload["audit_event_id"]

    def test_typed_failure_over_the_socket(self, daemon) -> None:
        _instance, config = daemon
        with SocketClient(config.paths.socket) as client:
            response = client.call_tool("knowledge_submit_extraction", make_args(confidence="2"), actor="agent-a")
            assert tool_error_code(response) == "CONFIDENCE_OUT_OF_RANGE"

    def test_duplicate_returns_the_same_receipt_over_the_socket(self, daemon) -> None:
        _instance, config = daemon
        with SocketClient(config.paths.socket) as client:
            first = tool_payload(client.call_tool("knowledge_submit_extraction", make_args(), actor="agent-a"))
            second = tool_payload(client.call_tool("knowledge_submit_extraction", make_args(), actor="agent-a"))
            assert second["receipt_id"] == first["receipt_id"]
            assert second["duplicate"] is True


class TestPerAgentQuota:
    def test_the_admission_bucket_throttles_submit_extraction(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, idle_timeout_seconds=120.0, agent_rate_per_second=0.01, agent_burst=2)
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            with SocketClient(config.paths.socket) as client:
                codes = []
                for _ in range(6):
                    response = client.call_tool("knowledge_submit_extraction", make_args(), actor="flooder")
                    if "error" in response:
                        codes.append(response["error"]["code"])
                    else:
                        result = response["result"]
                        if result.get("isError"):
                            codes.append("TOOL_ERROR")
                        else:
                            codes.append("OK")
                assert rpc.SERVER_BUSY in codes  # the bucket refused the flood
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=10)
