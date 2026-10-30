"""Shared fixtures and helpers for the superpower server tests (SUB-100).

The seed layout mirrors ``tests/test_mcp_stdio_e2e.py``: one KROOT, five
primary domains, one child node and one relation, plus a published
projection, so every surface sees an identical deterministic graph.
"""

from __future__ import annotations

import itertools
import json
import socket
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
)
from know_your_soul.projection import GraphProjector
from know_your_soul.store import EventStore

EXPECTED_TOOLS_ALL = tuple(
    sorted(
        (
            "knowledge_query",
            "knowledge_get_node",
            "knowledge_get_subtree",
            "knowledge_log_event",
            "knowledge_record_outcome",
            "knowledge_evaluate",
            "knowledge_review",
            "knowledge_propose_improvement",
            "knowledge_apply_improvement",
            "knowledge_rollback",
            "knowledge_path",
            "knowledge_explain",
            "knowledge_affected",
            "knowledge_god_nodes",
            "knowledge_ingest_file",
            "knowledge_ingest_directory",
            # Contract v4 (extraction-contract-v4.md): the external-agent
            # extraction verb joins the write lane.
            "knowledge_submit_extraction",
        )
    )
)


def upsert_node(store: EventStore, node: Node, *, key: str | None = None) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=key or f"test:serve:upsert:{node.node_id}",
    )


def seed_store(tmp_path: Path) -> tuple[Path, Path]:
    """A seeded store plus an active projection; returns (graph_root, db)."""
    graph_root = tmp_path / "graph"
    db = tmp_path / "soul.sqlite3"
    store = EventStore(db)
    projector = GraphProjector(store, output_root=graph_root)
    upsert_node(
        store,
        Node(
            node_id="KROOT",
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            provenance=("soul://roots",),
            source_version_id="sv-test",
        ),
    )
    for domain in PrimaryDomain:
        upsert_node(
            store,
            Node(
                node_id=domain.value,
                parent_id="KROOT",
                kind=NodeKind.PRIMARY,
                domain=domain,
                summary=domain.title,
                provenance=("soul://roots",),
                source_version_id="sv-test",
            ),
        )
    upsert_node(
        store,
        Node(
            node_id="n-gates",
            parent_id="P1",
            kind=NodeKind.CHILD,
            domain=PrimaryDomain.P1,
            summary="Run the gates before commit",
            context_tags=("gates",),
            provenance=("soul://corpus/lesson.md",),
            source_version_id="sv-test",
        ),
    )
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id="P1",
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.PARENT_OF, source_node_id="P1", target_node_id="n-gates"),
        ),
        idempotency_key="test:serve:rel:p1:n-gates",
    )
    projector.rebuild()
    store.close()
    return graph_root, db


def make_config(tmp_path: Path, **overrides: Any) -> Any:
    """A DaemonConfig over a freshly seeded store (graph_root, db under tmp_path)."""
    from know_your_soul.serve.daemon_config import DaemonConfig

    graph_root, db = seed_store(tmp_path)
    kwargs: dict[str, Any] = {"db_path": db, "graph_root": graph_root}
    kwargs.update(overrides)
    return DaemonConfig(**kwargs)


def wait_for(predicate: Callable[[], bool], *, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class SocketClient:
    """A newline-delimited JSON-RPC client for the daemon's Unix socket."""

    def __init__(self, socket_path: Path | str, *, timeout: float = 15.0) -> None:
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.settimeout(timeout)
        self._sock.connect(str(socket_path))
        self._reader = self._sock.makefile("rb")
        self._writer = self._sock.makefile("wb")
        self._ids = itertools.count(1)

    def send_raw(self, payload: bytes) -> None:
        self._writer.write(payload)
        self._writer.flush()

    def read_response(self, *, timeout: float | None = None) -> dict[str, Any]:
        if timeout is not None:
            self._sock.settimeout(timeout)
        try:
            line = self._reader.readline()
        finally:
            if timeout is not None:
                self._sock.settimeout(15.0)
        if not line:
            raise ConnectionError("daemon closed the connection")
        return json.loads(line.decode("utf-8"))  # type: ignore[no-any-return]

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.send_request(method, params)
        return self.read_response()

    def send_request(self, method: str, params: dict[str, Any] | None = None) -> int:
        """Pipelined send: the response is read later via read_response()."""
        request_id = next(self._ids)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send_raw(json.dumps(message).encode("utf-8") + b"\n")
        return request_id

    def send_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        actor: str | None = "tester",
    ) -> int:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if actor is not None:
            params["_meta"] = {"actor": actor}
        return self.send_request("tools/call", params)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send_raw(json.dumps(message).encode("utf-8") + b"\n")

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        actor: str | None = "tester",
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": arguments or {}}
        if actor is not None:
            params["_meta"] = {"actor": actor}
        return self.request("tools/call", params)

    def close(self) -> None:
        try:
            self._writer.close()
        except OSError:
            pass
        try:
            self._reader.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> SocketClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def tool_result_text(response: dict[str, Any]) -> str:
    """The text payload of a successful or isError CallToolResult response."""
    assert "result" in response, f"expected a result frame, got {response!r}"
    result = response["result"]
    assert result["content"], "a CallToolResult must carry content"
    return str(result["content"][0]["text"])


def tool_payload(response: dict[str, Any]) -> dict[str, Any]:
    return json.loads(tool_result_text(response))  # type: ignore[no-any-return]


def tool_error_code(response: dict[str, Any]) -> str:
    """The typed code from an isError tool result's error envelope."""
    result = response["result"]
    assert result.get("isError") is True, f"expected isError, got {response!r}"
    return str(json.loads(tool_result_text(response))["error"]["code"])


def mask_volatile(payload: Any) -> Any:
    """Strip per-invocation values (audit ids, recommendation expiries)."""
    if isinstance(payload, dict):
        return {key: mask_volatile(value) for key, value in payload.items() if key not in {"audit_event_id", "expiry"}}
    if isinstance(payload, list):
        return [mask_volatile(item) for item in payload]
    return payload
