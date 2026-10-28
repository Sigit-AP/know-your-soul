"""Fresh-process stdio end-to-end tests for the MCP adapter (Task 8).

A real ``python -m know_your_soul.mcp_server`` child is spawned under a
scrubbed environment (provider credentials injected, then scrubbed away) and
driven both by the official MCP client and by raw JSON-RPC frames. This is
the wire-level proof of the binding contract in
``docs/research/hermes-mcp-contract.md`` section 3.1.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client

from know_your_soul import runtime_guard
from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, Relation, RelationKind
from know_your_soul.projection import GraphProjector
from know_your_soul.store import EventStore

# The MCP tool surface (contract v2 — operator ruling 2026-10-26, progress.md;
# addendum docs/contracts/retrieval-verbs-v2.md): the frozen ten v1 tools plus
# the four G1 retrieval verbs; contract v3 (addendum
# docs/contracts/ingestion-contract-v3.md, audit F2) adds the two ingestion
# verbs.
EXPECTED_TOOLS = tuple(
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
        )
    )
)


def _seed(tmp_path: Path) -> tuple[Path, Path]:
    """A seeded store plus an active projection; returns (graph_root, db)."""
    graph_root = tmp_path / "graph"
    db = tmp_path / "soul.sqlite3"
    store = EventStore(db)
    projector = GraphProjector(store, output_root=graph_root)
    _upsert(
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
        _upsert(
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
    _upsert(
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
        idempotency_key="test:e2e:rel:p1:n-gates",
    )
    projector.rebuild()
    store.close()
    return graph_root, db


def _upsert(store: EventStore, node: Node) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=f"test:e2e:upsert:{node.node_id}",
    )


def _scrubbed_env() -> dict[str, str]:
    """A child environment with provider credentials provably scrubbed out."""
    dirty = {**os.environ, "OPENAI_API_KEY": "sk-injected", "ANTHROPIC_API_KEY": "sk-ant-injected"}
    env = runtime_guard.deterministic_env(dirty)
    assert runtime_guard.provider_credentials(env) == {}
    return env


def _server_args(graph_root: Path, db: Path) -> list[str]:
    return ["-m", "know_your_soul.mcp_server", "--graph-root", str(graph_root), "--db", str(db)]


def _result_text(result: types.CallToolResult) -> str:
    assert result.content, "a result must carry at least one content item"
    content = result.content[0]
    assert isinstance(content, types.TextContent), f"unexpected content {type(content).__name__}"
    return content.text


@asynccontextmanager
async def _stdio_session(graph_root: Path, db: Path) -> AsyncIterator[ClientSession]:
    params = StdioServerParameters(
        command=sys.executable,
        args=_server_args(graph_root, db),
        env=_scrubbed_env(),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


def test_fresh_client_discovers_ten_tools(tmp_path: Path) -> None:
    graph_root, db = _seed(tmp_path)

    async def scenario() -> None:
        async with _stdio_session(graph_root, db) as session:
            capabilities = session.server_capabilities
            assert capabilities is not None
            advertised = capabilities.model_dump(exclude_none=True)
            assert set(advertised) == {"tools", "resources"}, advertised
            listed = await session.list_tools()
            assert tuple(sorted(tool.name for tool in listed.tools)) == EXPECTED_TOOLS
            assert len(listed.tools) == 16  # contract v2 (ten v1 + four G1) + contract v3 (two ingestion, F2)
            for tool in listed.tools:
                assert "project_path" not in tool.input_schema.get("properties", {}), tool.name
            resources = await session.list_resources()
            assert resources.resources
            assert all(str(resource.uri).startswith("soul://") for resource in resources.resources)

    anyio.run(scenario)


def test_stdio_forbidden_methods_return_method_not_found(tmp_path: Path) -> None:
    """Raw JSON-RPC: sampling/prompts/logging/completion are never answered."""
    graph_root, db = _seed(tmp_path)
    process = subprocess.Popen(
        [sys.executable, *_server_args(graph_root, db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_scrubbed_env(),
    )
    assert process.stdin is not None and process.stdout is not None
    stdin = process.stdin
    stdout = process.stdout
    try:
        from mcp.types import LATEST_PROTOCOL_VERSION

        def rpc(request_id: int, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            frame = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                frame["params"] = params
            stdin.write(json.dumps(frame).encode("utf-8") + b"\n")
            stdin.flush()
            deadline = 200
            for _ in range(deadline):
                line = stdout.readline()
                assert line, f"no response to {method}; the server exited with {process.poll()}"
                message = json.loads(line.decode("utf-8"))
                if message.get("id") == request_id:
                    return message
            raise AssertionError(f"no response with id {request_id} for {method}")

        initialize = rpc(
            1,
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "raw-e2e", "version": "0"},
            },
        )
        advertised = initialize["result"]["capabilities"]
        assert set(advertised) == {"tools", "resources"}, advertised

        stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        stdin.flush()

        forbidden = (
            ("sampling/createMessage", {"messages": [], "maxTokens": 1}),
            ("prompts/list", {}),
            ("prompts/get", {"name": "nope"}),
            ("logging/setLevel", {"level": "debug"}),
            (
                "completion/complete",
                {"ref": {"type": "ref/prompt", "name": "nope"}, "argument": {"name": "a", "value": "b"}},
            ),
        )
        for request_id, (method, params) in enumerate(forbidden, start=2):
            response = rpc(request_id, method, params)
            assert "error" in response, (method, response)
            # Refused, never answered: method-not-found, or the SDK's own
            # params gate rejecting before dispatch — never a model call.
            assert response["error"]["code"] in {-32601, -32602}, (method, response)
            assert "result" not in response
    finally:
        process.kill()
        process.wait(timeout=30)


def _mask(payload: Any) -> Any:
    """Strip per-invocation values (audit ids, recommendation expiries)."""
    if isinstance(payload, dict):
        return {key: _mask(value) for key, value in payload.items() if key not in {"audit_event_id", "expiry"}}
    if isinstance(payload, list):
        return [_mask(item) for item in payload]
    return payload


def test_canonical_payload_parity(tmp_path: Path) -> None:
    """The stdio wire payload equals the direct service canonical payload."""
    graph_root, db = _seed(tmp_path)

    async def scenario() -> None:
        async with _stdio_session(graph_root, db) as session:
            result = await session.call_tool(
                "knowledge_query",
                {"purpose": "run the gates", "domain": "P1", "token_budget": 512},
            )
            assert isinstance(result, types.CallToolResult)
            assert not result.is_error, result.content
            via_wire = json.loads(_result_text(result))
            assert via_wire["routing"] == "direct"
            assert "n-gates" in {node["node_id"] for node in via_wire["results"]}
            assert via_wire["estimated_tokens"] <= 512

    anyio.run(scenario)

    # Direct in-process call against the same store: identical payload shape,
    # differing only in the per-invocation audit id.
    from know_your_soul.contracts import QueryRequest
    from know_your_soul.service import KnowledgeService

    store = EventStore(db)
    service = KnowledgeService(store, GraphProjector(store, output_root=graph_root))
    direct = service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
    store.close()

    async def second_session() -> dict[str, Any]:
        async with _stdio_session(graph_root, db) as session:
            result = await session.call_tool(
                "knowledge_query",
                {"purpose": "run the gates", "domain": "P1", "token_budget": 512},
            )
            assert isinstance(result, types.CallToolResult)
            return json.loads(_result_text(result))  # type: ignore[no-any-return]

    via_wire = anyio.run(second_session)
    assert _mask(via_wire) == _mask(direct.canonical_payload())


def test_persistence_across_server_restart(tmp_path: Path) -> None:
    """A node written through one server process is read back by a fresh one."""
    graph_root, db = _seed(tmp_path)
    node = {
        "node_id": "n-persisted",
        "kind": "leaf",
        "domain": "P1",
        "summary": "Persisted through one server and read by the next",
        "provenance": ["soul://corpus/lesson.md"],
        "source_version_id": "sv-test",
    }

    async def write_phase() -> str:
        async with _stdio_session(graph_root, db) as session:
            written = await session.call_tool(
                "knowledge_log_event",
                {"node": node, "idempotency_key": "e2e:persist:1"},
            )
            assert isinstance(written, types.CallToolResult)
            assert not written.is_error, written.content
            return json.loads(_result_text(written))["audit_event_id"]  # type: ignore[no-any-return]

    audit_id = anyio.run(write_phase)
    assert audit_id

    async def read_phase() -> None:
        async with _stdio_session(graph_root, db) as session:
            read = await session.call_tool("knowledge_get_node", {"node_id": "n-persisted"})
            assert isinstance(read, types.CallToolResult)
            assert not read.is_error, read.content
            payload = json.loads(_result_text(read))
            assert payload["node"]["node_id"] == "n-persisted"
            assert payload["node"]["summary"] == node["summary"]
            assert payload["audit_event_id"]

    anyio.run(read_phase)


def test_guards_hold_inside_the_scrubbed_server_process(tmp_path: Path) -> None:
    """The real server child verifies its own runtime boundary at startup.

    The probe builds the product server inside a scrubbed fresh process and
    runs the same guard battery as ``tests/test_no_ai_runtime.py``: forbidden
    imports denied, outbound sockets denied, capability surface exactly
    tools+resources, injected provider credentials gone.
    """
    graph_root, db = _seed(tmp_path)
    probe = tmp_path / "probe.py"
    probe.write_text(
        "\n".join(
            [
                "import json, socket, sys",
                "from know_your_soul import runtime_guard as guard",
                "from know_your_soul.mcp_server import build_server, build_service",
                f"service = build_service(graph_root={str(graph_root)!r}, db={str(db)!r})",
                f"server = build_server(service, graph_root={str(graph_root)!r})",
                "report = {'credentials': sorted(guard.provider_credentials())}",
                "with guard.deny_outbound_sockets():",
                "    try:",
                "        socket.create_connection(('192.0.2.1', 9), timeout=0.05)",
                "        report['socket_denied'] = False",
                "    except guard.OutboundSocketDenied:",
                "        report['socket_denied'] = True",
                "with guard.import_guard():",
                "    try:",
                "        import graphify.llm  # noqa: F401",
                "        report['import_denied'] = False",
                "    except guard.ForbiddenImportError:",
                "        report['import_denied'] = True",
                "report['verify'] = guard.verify_runtime(mcp_server=server)",
                "print(json.dumps(report))",
            ]
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(probe)],
        env=_scrubbed_env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["credentials"] == []
    assert report["socket_denied"] is True
    assert report["import_denied"] is True
    assert report["verify"]["status"] == "ok"
    assert report["verify"]["mcp_capabilities"] == ["resources", "tools"]
