"""In-process MCP adapter contract tests (Task 8).

Spec sections 8 and 13; the binding adaptation contract in
``docs/research/hermes-mcp-contract.md`` section 3.1 and the deep-research
adaptations X1/X2/X6/X8 (``docs/research/graphify-mcp-security-deep.md``).

Every test talks to the real MCP SDK server over in-memory JSON-RPC streams:
no transport shortcuts, no SDK mocks. The adapter is bound to one startup
graph root; a caller can never select a path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession, types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from mcp.shared.memory import create_client_server_memory_streams

from know_your_soul import runtime_guard
from know_your_soul.contracts import DEFAULT_MAX_TOKEN_BUDGET, QueryRequest
from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
    canonical_json,
)
from know_your_soul.projection import GraphProjector
from know_your_soul.service import KnowledgeService
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

# --------------------------------------------------------------------- setup


def _upsert(store: EventStore, node: Node) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=f"test:mcp:upsert:{node.node_id}",
    )


def _relate(store: EventStore, source: str, target: str, relation: str) -> None:
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id=source,
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind(relation), source_node_id=source, target_node_id=target),
        ),
        idempotency_key=f"test:mcp:rel:{source}:{relation}:{target}",
    )


def _child(node_id: str, *, summary: str, tags: tuple[str, ...] = ()) -> Node:
    return Node(
        node_id=node_id,
        parent_id="P1",
        kind=NodeKind.CHILD,
        domain=PrimaryDomain.P1,
        summary=summary,
        context_tags=tags,
        provenance=("soul://corpus/lesson.md",),
        source_version_id="sv-test",
    )


def _seeded(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    """Roots + one P1 child, projected and active (mirrors test_service)."""
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "graph")
    service = KnowledgeService(store, projector)
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
    _upsert(store, _child("n-gates", summary="Run the gates before commit", tags=("gates", "discipline")))
    _relate(store, "P1", "n-gates", "parent_of")
    projector.rebuild()
    return store, projector, service


def _server(tmp_path: Path) -> tuple[EventStore, KnowledgeService, Server]:
    from know_your_soul.mcp_server import build_server

    store, _, service = _seeded(tmp_path)
    server = build_server(service, graph_root=tmp_path / "graph")
    return store, service, server


@asynccontextmanager
async def _session(server: Server) -> AsyncIterator[ClientSession]:
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                server.run,
                server_streams[0],
                server_streams[1],
                server.create_initialization_options(),
                True,  # raise_exceptions: surface handler bugs in tests
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    yield session
            finally:
                tg.cancel_scope.cancel()


def _run(fn: Any, *args: Any) -> Any:
    return anyio.run(fn, *args)


async def _call_tool(session: ClientSession, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
    result = await session.call_tool(name, arguments)
    assert isinstance(result, types.CallToolResult), f"unexpected result type {type(result).__name__}"
    return result


def _result_text(result: types.CallToolResult) -> str:
    assert result.content, "a result must carry at least one content item"
    content = result.content[0]
    assert isinstance(content, types.TextContent), f"unexpected content {type(content).__name__}"
    return content.text


def _error_payload(result: types.CallToolResult) -> dict[str, Any]:
    assert result.is_error, f"expected is_error, got {result.content!r}"
    payload = json.loads(_result_text(result))
    assert "error" in payload, payload
    return payload["error"]  # type: ignore[no-any-return]


# ------------------------------------------------------------- discovery


def test_sixteen_tools_with_strict_schemas(tmp_path: Path) -> None:
    """The v1 pin was ten tools; contract v2 adds four; contract v3 (audit F2) adds two."""
    _, _, server = _server(tmp_path)

    async def scenario() -> None:
        async with _session(server) as session:
            listed = await session.list_tools()
            names = tuple(sorted(tool.name for tool in listed.tools))
            assert names == EXPECTED_TOOLS
            assert len(listed.tools) == 16
            for tool in listed.tools:
                schema = tool.input_schema
                assert schema.get("type") == "object", tool.name
                assert schema.get("additionalProperties") is False, tool.name
                properties = schema.get("properties", {})
                assert "project_path" not in properties, tool.name
                assert "projectPath" not in properties, tool.name
            by_name = {tool.name: tool for tool in listed.tools}
            query = by_name["knowledge_query"].input_schema
            assert set(query["required"]) == {"purpose", "token_budget"}
            depth = query["properties"]["depth"]
            assert depth["minimum"] == 1 and depth["maximum"] == 6
            assert query["properties"]["mode"]["enum"] == ["bfs", "dfs"]
            budget = query["properties"]["token_budget"]
            assert budget["minimum"] == 128
            assert budget["maximum"] == DEFAULT_MAX_TOKEN_BUDGET

    _run(scenario)


def test_no_pr_or_gh_surface(tmp_path: Path) -> None:
    _, _, server = _server(tmp_path)

    async def scenario() -> None:
        async with _session(server) as session:
            listed = await session.list_tools()
            for tool in listed.tools:
                lowered = tool.name.lower().split("_")
                assert "pr" not in lowered, tool.name
                assert "gh" not in lowered, tool.name
                description = (tool.description or "").lower()
                assert "pull request" not in description
                assert "github" not in description

    _run(scenario)


# ------------------------------------------------------------- capabilities


def test_capabilities_are_exactly_tools_and_resources(tmp_path: Path) -> None:
    _, _, server = _server(tmp_path)

    async def scenario() -> None:
        async with _session(server) as session:
            capabilities = session.server_capabilities
            assert capabilities is not None
            advertised = capabilities.model_dump(exclude_none=True, by_alias=True)
            assert set(advertised) == {"tools", "resources"}
            runtime_guard.assert_mcp_capabilities(advertised)
            assert advertised["resources"]["subscribe"] is False
            assert advertised["resources"]["listChanged"] is False
            assert advertised["tools"]["listChanged"] is False

    _run(scenario)
    runtime_guard.assert_no_sampling_handler(server)
    assert runtime_guard.forbidden_mcp_methods(server) == []
    report = runtime_guard.verify_runtime(mcp_server=server)
    assert report["mcp_capabilities"] == ["resources", "tools"]


def test_no_sampling_prompts_logging_or_completion_handlers(tmp_path: Path) -> None:
    _, _, server = _server(tmp_path)

    async def scenario() -> None:
        async with _session(server) as session:
            with pytest.raises(MCPError) as prompts_error:
                await session.list_prompts()
            assert prompts_error.value.error.code == -32601
            with pytest.raises(MCPError) as logging_error:
                await session.set_logging_level("debug")
            assert logging_error.value.error.code == -32601
            with pytest.raises(MCPError) as completion_error:
                await session.complete(
                    types.PromptReference(type="ref/prompt", name="nope"),
                    {"name": "x", "value": "y"},
                )
            assert completion_error.value.error.code == -32601
            with pytest.raises(MCPError) as sampling_error:
                await session.send_request(
                    types.Request(
                        method="sampling/createMessage",
                        params={"messages": [], "maxTokens": 1},
                    ),
                    types.Result,
                )
            assert sampling_error.value.error.code == -32601

    _run(scenario)


# ------------------------------------------------- strict input validation


def test_fixed_root_containment_rejects_project_path(tmp_path: Path) -> None:
    _, _, server = _server(tmp_path)

    async def scenario() -> None:
        async with _session(server) as session:
            listed = await session.list_tools()
            for tool in listed.tools:
                result = await _call_tool(session, tool.name, {"project_path": "/etc"})
                error = _error_payload(result)
                assert error["code"] == "INVALID_ARGUMENTS", (tool.name, error)
                assert "project_path" in error["message"]

    _run(scenario)


def test_strict_rejection_matrix(tmp_path: Path) -> None:
    _, _, server = _server(tmp_path)
    base = {"purpose": "run the gates", "domain": "P1", "token_budget": 512}
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("knowledge_query", {"token_budget": 512}, "purpose"),  # missing required
        ("knowledge_query", {**base, "purpose": ""}, "purpose"),  # empty string
        ("knowledge_query", {**base, "purpose": 123}, "purpose"),  # wrong type
        ("knowledge_query", {**base, "unknown_field": 1}, "unknown_field"),  # unknown property
        ("knowledge_query", {**base, "token_budget": "512"}, "token_budget"),  # numeric string
        ("knowledge_query", {**base, "token_budget": True}, "token_budget"),  # bool is not int
        ("knowledge_query", {**base, "token_budget": 127}, "token_budget"),  # below 128
        ("knowledge_query", {**base, "token_budget": 0}, "token_budget"),
        ("knowledge_query", {**base, "token_budget": -5}, "token_budget"),
        ("knowledge_query", {**base, "token_budget": DEFAULT_MAX_TOKEN_BUDGET + 1}, "token_budget"),
        ("knowledge_query", {**base, "access_only": "true"}, "access_only"),  # string boolean
        ("knowledge_query", {**base, "access_only": 1}, "access_only"),
        ("knowledge_query", {**base, "depth": 0}, "depth"),
        ("knowledge_query", {**base, "depth": 7}, "depth"),
        ("knowledge_query", {**base, "depth": -5}, "depth"),
        ("knowledge_query", {**base, "depth": "2"}, "depth"),
        ("knowledge_query", {**base, "mode": "bogus"}, "mode"),
        ("knowledge_query", {**base, "context_tags": "gates"}, "context_tags"),  # not an array
        ("knowledge_query", {**base, "context_tags": [42]}, "context_tags"),
        ("knowledge_query", {**base, "relation_allowlist": ["hack_the_planet"]}, "relation_allowlist"),
        ("knowledge_get_node", {}, "node_id"),
        ("knowledge_get_node", {"node_id": 42}, "node_id"),
        ("knowledge_get_subtree", {"node_id": "n-gates", "token_budget": 127}, "token_budget"),
        ("knowledge_get_subtree", {"node_id": "n-gates", "depth": 99}, "depth"),
        ("knowledge_evaluate", {"node_id": "n-gates", "include_descendants": "yes"}, "include_descendants"),
        ("knowledge_log_event", {"node": {"node_id": "x"}}, "node"),  # incomplete node
        ("knowledge_record_outcome", {"evidence": {"evidence_id": "e"}}, "evidence"),
        ("knowledge_propose_improvement", {"node_id": "n-gates", "diff": [], "rationale": "r"}, "diff"),
        ("knowledge_propose_improvement", {"node_id": "n-gates", "rationale": "r"}, "diff"),
        ("knowledge_apply_improvement", {}, "proposal_id"),
        ("knowledge_rollback", {}, "node_id"),
    ]

    async def scenario() -> None:
        async with _session(server) as session:
            for tool_name, arguments, needle in cases:
                result = await _call_tool(session, tool_name, arguments)
                error = _error_payload(result)
                assert error["code"] == "INVALID_ARGUMENTS", (tool_name, arguments, error)
                assert needle in error["message"], (tool_name, error)
                assert "Traceback" not in error["message"]

    _run(scenario)


def test_unknown_tool_is_a_typed_error_not_a_success(tmp_path: Path) -> None:
    _, _, server = _server(tmp_path)

    async def scenario() -> types.CallToolResult:
        async with _session(server) as session:
            return await _call_tool(session, "nope", {})

    result = _run(scenario)
    error = _error_payload(result)
    assert error["code"] == "UNKNOWN_TOOL"


def test_server_requires_an_existing_graph_root(tmp_path: Path) -> None:
    from know_your_soul.mcp_server import build_server

    _, _, service = _seeded(tmp_path)
    with pytest.raises(Exception) as caught:
        build_server(service, graph_root=tmp_path / "does-not-exist")
    assert type(caught.value).__name__ in {"PathEscapeError", "ConfigurationError"}


# ------------------------------------------------------- payloads and audit


def _mask(payload: Any) -> Any:
    """Strip per-invocation values (audit ids, recommendation expiries)."""
    if isinstance(payload, dict):
        return {key: _mask(value) for key, value in payload.items() if key not in {"audit_event_id", "expiry"}}
    if isinstance(payload, list):
        return [_mask(item) for item in payload]
    return payload


def test_query_payload_parity_with_service(tmp_path: Path) -> None:
    _, service, server = _server(tmp_path)

    async def scenario() -> None:
        async with _session(server) as session:
            result = await _call_tool(
                session,
                "knowledge_query",
                {"purpose": "run the gates", "domain": "P1", "token_budget": 512},
            )
            assert not result.is_error, result.content
            text = _result_text(result)
            via_mcp = json.loads(text)
            direct = service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
            assert _mask(via_mcp) == _mask(direct.canonical_payload())
            assert via_mcp["audit_event_id"]
            assert via_mcp["estimated_tokens"] <= 512
            # The wire text is canonical JSON, byte-identical to re-serialization.
            assert text == canonical_json(json.loads(text))

    _run(scenario)


def test_exactly_one_final_audit_event_per_invocation(tmp_path: Path) -> None:
    store, _, server = _server(tmp_path)
    before = store.audit_event_count()

    async def scenario() -> None:
        async with _session(server) as session:
            ok = await _call_tool(session, "knowledge_get_node", {"node_id": "n-gates"})
            assert not ok.is_error
            assert store.audit_event_count() == before + 1
            payload = json.loads(_result_text(ok))
            assert payload["audit_event_id"]

            missing = await _call_tool(session, "knowledge_get_node", {"node_id": "no-such-node"})
            error = _error_payload(missing)
            assert error["code"] == "NODE_NOT_FOUND"
            assert error["audit_event_id"]
            assert store.audit_event_count() == before + 2

            invalid = await _call_tool(session, "knowledge_get_node", {"node_id": 42})
            error = _error_payload(invalid)
            assert error["code"] == "INVALID_ARGUMENTS"
            assert error["audit_event_id"]
            assert store.audit_event_count() == before + 3

    _run(scenario)


def test_error_envelope_never_leaks_raw_exception_text(tmp_path: Path) -> None:
    _, _, server = _server(tmp_path)

    async def scenario() -> None:
        async with _session(server) as session:
            result = await _call_tool(session, "knowledge_query", {"purpose": 123, "token_budget": 512})
            text = _result_text(result)
            assert "argument of type" not in text
            assert "Traceback" not in text
            error = json.loads(text)["error"]
            assert set(error) <= {"code", "message", "audit_event_id"}

    _run(scenario)


# ------------------------------------------------------------- write tools


def _node_payload(node_id: str = "n-mcp") -> dict[str, Any]:
    return {
        "node_id": node_id,
        "kind": "leaf",
        "domain": "P1",
        "summary": "Written through MCP",
        "provenance": ["soul://corpus/lesson.md"],
        "source_version_id": "sv-test",
    }


def _evidence_payload(evidence_id: str, node_id: str) -> dict[str, Any]:
    return {
        "evidence_id": evidence_id,
        "node_id": node_id,
        "source_version_id": "sv-test",
        "source_uri": "soul://corpus/lesson.md",
        "observed_at": "2026-10-20T00:00:00+00:00",
        "context_tags": ["gates"],
        "independence_key": evidence_id,
        "outcome": "1",
        "correctness": "0.9",
        "reliability": "0.9",
        "reproducibility": "0.9",
        "scope_fit": "0.9",
        "successful_use": "1",
        "task_coverage": "1",
        "quality_at_use": "0.9",
        "contradiction": "0",
        "utilization_u": "0.5",
        "latency_ms": 10,
        "compute_ms": 5,
        "output_chars": 10,
    }


def test_write_tools_roundtrip_with_idempotent_replay(tmp_path: Path) -> None:
    store, _, server = _server(tmp_path)
    before = store.audit_event_count()

    async def scenario() -> None:
        async with _session(server) as session:
            written = await _call_tool(
                session,
                "knowledge_log_event",
                {"node": _node_payload(), "idempotency_key": "mcp:test:log:1"},
            )
            assert not written.is_error, written.content
            receipt = json.loads(_result_text(written))
            assert receipt["audit_event_id"]
            assert receipt["node_version"] == 1
            assert store.audit_event_count() == before + 1

            replay = await _call_tool(
                session,
                "knowledge_log_event",
                {"node": _node_payload(), "idempotency_key": "mcp:test:log:1"},
            )
            replayed = json.loads(_result_text(replay))
            assert replayed["idempotent_replay"] is True
            assert store.audit_event_count() == before + 2

            outcome = await _call_tool(
                session,
                "knowledge_record_outcome",
                {"evidence": _evidence_payload("ev-mcp-1", "n-mcp"), "idempotency_key": "mcp:test:ev:1"},
            )
            assert not outcome.is_error, outcome.content
            assert json.loads(_result_text(outcome))["audit_event_id"]
            assert store.audit_event_count() == before + 3

    _run(scenario)


def test_review_and_improvement_flow(tmp_path: Path) -> None:
    """The Task 5/6 operations through the wired service (one audit each)."""
    store, _, server = _server(tmp_path)
    before = store.audit_event_count()

    async def scenario() -> None:
        async with _session(server) as session:
            evaluated = await _call_tool(session, "knowledge_evaluate", {"node_id": "n-gates"})
            assert not evaluated.is_error, evaluated.content
            scores = json.loads(_result_text(evaluated))
            assert {"p", "m", "e", "audit_event_id"} <= set(scores)

            reviewed = await _call_tool(session, "knowledge_review", {"node_id": "n-gates"})
            assert not reviewed.is_error, reviewed.content
            report = json.loads(_result_text(reviewed))
            assert report["audit_event_id"]

            proposed = await _call_tool(
                session,
                "knowledge_propose_improvement",
                {
                    "node_id": "n-gates",
                    "diff": [{"op": "set_summary", "value": "Run every gate before each commit"}],
                    "rationale": "sharpen the summary",
                },
            )
            assert not proposed.is_error, proposed.content
            proposal = json.loads(_result_text(proposed))
            proposal_id = proposal["proposal"]["proposal_id"]

            applied = await _call_tool(session, "knowledge_apply_improvement", {"proposal_id": proposal_id})
            assert not applied.is_error, applied.content
            receipt = json.loads(_result_text(applied))
            assert receipt["status"] == "applied"

            rolled_back = await _call_tool(session, "knowledge_rollback", {"node_id": "n-gates"})
            assert not rolled_back.is_error, rolled_back.content
            assert json.loads(_result_text(rolled_back))["status"] == "rolled_back"

            assert store.audit_event_count() == before + 5

    _run(scenario)


# ---------------------------------------------------------------- resources


def test_resources_are_sanitized_and_size_capped(tmp_path: Path) -> None:
    from know_your_soul.mcp_server import build_server

    store, projector, service = _seeded(tmp_path)
    # A hostile label with ANSI escapes and zero-width markup must never reach
    # the wire raw; the resource view is sanitized on read.
    _upsert(store, _child("n-hostile", summary="\x1b[31mEVIL\x1b[0m <script>alert(1)</script>​zero-width"))
    _relate(store, "P1", "n-hostile", "parent_of")
    projector.rebuild()
    server = build_server(service, graph_root=tmp_path / "graph")

    async def scenario() -> None:
        async with _session(server) as session:
            listed = await session.list_resources()
            uris = {str(resource.uri) for resource in listed.resources}
            assert uris, "the server must expose sanitized resources"
            assert all(uri.startswith("soul://") for uri in uris)
            assert not any("graphify" in uri for uri in uris)
            for resource in listed.resources:
                read = await session.read_resource(str(resource.uri))
                assert read.contents, resource.uri
                contents = read.contents[0]
                assert isinstance(contents, types.TextResourceContents)
                text = contents.text
                assert "\x1b" not in text, resource.uri
                assert "​" not in text, resource.uri
                payload = json.loads(text)
                assert len(text.encode("utf-8")) <= 65_536
                assert text == canonical_json(payload)
            manifest = await session.read_resource("soul://projection/manifest")
            manifest_contents = manifest.contents[0]
            assert isinstance(manifest_contents, types.TextResourceContents)
            manifest_payload = json.loads(manifest_contents.text)
            assert manifest_payload["graph_content_hash"]
            assert manifest_payload["projection_version"]

    _run(scenario)


def test_unknown_resource_uri_fails_closed(tmp_path: Path) -> None:
    _, _, server = _server(tmp_path)

    async def scenario() -> None:
        async with _session(server) as session:
            with pytest.raises(MCPError):
                await session.read_resource("soul://does-not-exist")
            with pytest.raises(MCPError):
                await session.read_resource("file:///etc/passwd")
            with pytest.raises(MCPError):
                await session.read_resource("soul://projection/../../etc/passwd")

    _run(scenario)


# ------------------------------------------- wave-2 F3: http transport auth


def _asgi_request(
    app: Any,
    path: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 5000),
) -> tuple[int, bytes]:
    """One GET through an ASGI app; returns (status, body)."""
    header_list = list(headers or [])
    if not any(name.lower() == b"host" for name, _ in header_list):
        header_list.append((b"host", b"localhost"))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": header_list,
        "client": client,
        "server": ("127.0.0.1", 80),
    }
    messages: list[dict[str, Any]] = []
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    import asyncio

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return int(start["status"]), body


def test_f3_http_transport_wraps_streamable_app_with_boundary_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--transport http --api-key K must authenticate every request (F3).

    The old code only gated the bind decision; the wrapped app must now reject
    keyless remote *and* keyless loopback callers, and pass keyed callers to
    the real MCP streamable-http app.
    """
    from know_your_soul import mcp_server
    from know_your_soul.http_api import BoundaryMiddleware

    _seeded(tmp_path)  # graph root + store exist for main()'s startup path

    async def sentinel_app(scope: Any, receive: Any, send: Any) -> None:
        assert scope["type"] == "http"
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"INNER-MCP-APP"})

    monkeypatch.setattr(mcp_server.Server, "streamable_http_app", lambda self: sentinel_app)
    monkeypatch.setattr(mcp_server.runtime_guard, "verify_runtime", lambda **kwargs: {})
    monkeypatch.setattr(mcp_server.runtime_guard, "install_socket_guard", lambda: None)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.setdefault("app", app))

    code = mcp_server.main(
        [
            "--graph-root",
            str(tmp_path / "graph"),
            "--db",
            str(tmp_path / "soul.sqlite3"),
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            "18080",
            "--api-key",
            "wave2-mcp-key",
        ]
    )
    assert code == 0
    app = captured.get("app")
    assert isinstance(app, BoundaryMiddleware), "the http transport must install the boundary middleware"

    # The old attack: any caller reaches the MCP app with no key at all.
    status, _ = _asgi_request(app, "/mcp", client=("10.1.2.3", 9999))
    assert status == 401
    status, _ = _asgi_request(app, "/mcp", client=("127.0.0.1", 5000))
    assert status == 401
    status, _ = _asgi_request(app, "/mcp", headers=[(b"host", b"evil.example.com")])
    assert status == 403
    # The legitimate caller with the key reaches the inner app.
    status, body = _asgi_request(app, "/mcp", headers=[(b"x-api-key", b"wave2-mcp-key")])
    assert status == 200
    assert body == b"INNER-MCP-APP"


def test_f3_http_transport_without_key_serves_loopback_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No key configured: loopback is served, remote clients are denied."""
    from know_your_soul import mcp_server

    _seeded(tmp_path)

    async def sentinel_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"INNER"})

    monkeypatch.setattr(mcp_server.Server, "streamable_http_app", lambda self: sentinel_app)
    monkeypatch.setattr(mcp_server.runtime_guard, "verify_runtime", lambda **kwargs: {})
    monkeypatch.setattr(mcp_server.runtime_guard, "install_socket_guard", lambda: None)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.setdefault("app", app))

    code = mcp_server.main(
        [
            "--graph-root",
            str(tmp_path / "graph"),
            "--db",
            str(tmp_path / "soul.sqlite3"),
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            "18081",
        ]
    )
    assert code == 0
    app = captured["app"]
    status, _ = _asgi_request(app, "/mcp", client=("10.1.2.3", 9999))
    assert status == 401
    status, body = _asgi_request(app, "/mcp", client=("127.0.0.1", 5000))
    assert status == 200
    assert body == b"INNER"
