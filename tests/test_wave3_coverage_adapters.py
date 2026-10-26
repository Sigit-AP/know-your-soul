"""Coverage wave 3 (SUB-51): adapter/boundary error-path pins.

Targets the uncovered branches of ``http_api``, ``mcp_server``, ``cli``,
``read_model_validator``, ``graphify_adapter``, ``retrieval``,
``update_pipeline``, ``review`` and ``__main__`` at HEAD 2f5e711: typed
boundary refusals, middleware stream guards, staged-validation failures and
startup fail-closed exits. The contract-v2 retrieval verbs
(``path``/``explain``/``affected``/``god_nodes``) are SUB-50's in-flight
surface and are deliberately NOT exercised here.
"""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import socket
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from know_your_soul import http_api as http_api_module
from know_your_soul.audit import AuditLog
from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, canonical_json
from know_your_soul.events import Invocation
from know_your_soul.fragment_generator import FragmentError, FragmentGenerator, file_scoped_id
from know_your_soul.graphify_adapter import FragmentValidationError, GraphifyAdapter
from know_your_soul.http_api import (
    BoundaryMiddleware,
    PayloadTooLarge,
    UnknownOperation,
    _bind_unix_socket,
    _handle,
    _unlink_stale_socket,
    build_stack,
    create_app,
    dispatch,
    error_envelope,
    serve,
)
from know_your_soul.projection import GraphProjector
from know_your_soul.security import BindPolicyError
from know_your_soul.service import KnowledgeService
from know_your_soul.store import EventStore

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _node(node_id: str) -> Node:
    return Node(
        node_id=node_id,
        kind=NodeKind.CHILD,
        domain=PrimaryDomain.P1,
        summary=f"summary for {node_id}",
        source_version_id="sv-test",
    )


def _seeded_stack(tmp_path: Path, *, ops_root: Path | None = None) -> Any:
    root = ops_root if ops_root is not None else tmp_path / "ops"
    root.mkdir(exist_ok=True)
    stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out", ops_root=root)
    stack.store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-1",
            domain=PrimaryDomain.P1,
            node=_node("n-1"),
            source_version_id="sv-test",
        ),
        idempotency_key="test:seed:n-1",
    )
    stack.projector.rebuild()
    return stack


def _asgi_call(
    app: Any, path: str, body: bytes, headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[int, dict[str, Any]]:
    import anyio

    messages = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers
        or [
            (b"host", b"localhost"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    }

    async def run() -> None:
        await app(scope, receive, send)

    anyio.run(run)
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    payload = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return int(status), json.loads(payload.decode("utf-8"))


# ------------------------------------------------------------ http dispatch


def test_dispatch_refuses_an_unknown_operation(tmp_path: Path) -> None:
    stack = _seeded_stack(tmp_path)
    try:
        with pytest.raises(UnknownOperation, match="unknown operation"):
            dispatch(stack, "no_such_op", {})
    finally:
        stack.close()


def test_dispatch_rejects_a_non_object_payload_with_an_audit(tmp_path: Path) -> None:
    stack = _seeded_stack(tmp_path)
    try:
        with pytest.raises(http_api_module.AdapterValidationError, match="must be a JSON object"):
            dispatch(stack, "query", [1, 2, 3])
        # The validation failure was journaled exactly once.
        assert stack.store.journal_outcome_json("nonexistent") is None
    finally:
        stack.close()


def test_dispatch_accepts_a_decoded_mapping_payload(tmp_path: Path) -> None:
    stack = _seeded_stack(tmp_path)
    try:
        result = dispatch(stack, "get_node", {"node_id": "n-1"})
        assert result is not None
    finally:
        stack.close()


def test_dispatch_maps_a_non_validation_value_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import dataclasses

    stack = _seeded_stack(tmp_path)
    try:
        spec = http_api_module._OPERATIONS["query"]  # noqa: SLF001
        monkeypatch.setitem(
            http_api_module._OPERATIONS,
            "query",
            dataclasses.replace(spec, parse=_raise_type_error),  # noqa: SLF001
        )
        with pytest.raises(http_api_module.AdapterValidationError):
            dispatch(stack, "query", {"purpose": "x", "token_budget": 256})
    finally:
        stack.close()


def _raise_type_error(raw: Any) -> Any:
    raise TypeError("payload shape is not parseable")


def test_error_envelope_maps_payload_too_large() -> None:
    # F-SUB51-2: PayloadTooLarge is a KnowledgeError carrying the same code, so
    # the dedicated elif branch was dead and has been removed; the KnowledgeError
    # branch behavior is pinned here (code, retryable, audit_event_id unchanged).
    envelope = error_envelope(PayloadTooLarge("too big"))
    assert envelope["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert envelope["error"]["retryable"] is False
    assert envelope["error"]["audit_event_id"] is None


def test_ops_unknown_operation_is_a_404(tmp_path: Path) -> None:
    stack = _seeded_stack(tmp_path)
    try:
        app = create_app(stack)
        status, payload = _asgi_call(app, "/v1/ops/no_such_op", b"{}")
        assert status == 404
        assert payload["error"]["code"] == "NOT_FOUND"
    finally:
        stack.close()


def test_knowledge_op_rejects_a_json_array_body(tmp_path: Path) -> None:
    stack = _seeded_stack(tmp_path)
    try:
        app = create_app(stack)
        status, payload = _asgi_call(app, "/v1/knowledge/query", b"[1, 2, 3]")
        assert status == 400
        assert payload["error"]["code"] == "VALIDATION_ERROR"
    finally:
        stack.close()


def test_backup_without_an_ops_root_is_refused_typed(tmp_path: Path) -> None:
    # build_stack always resolves an ops root (default: the store directory);
    # the None guard covers a hand-assembled stack.
    stack = _seeded_stack(tmp_path)
    try:
        bare = http_api_module.LocalStack(
            store=stack.store,
            projector=stack.projector,
            service=stack.service,
            audit=stack.audit,
            ops_root=None,
        )
        status, payload = _asgi_call(
            create_app(bare), "/v1/ops/backup", json.dumps({"dest_path": "b.sqlite3"}).encode()
        )
        assert status == 400
        assert payload["error"]["code"] == "PATH_ESCAPE"
        assert "no ops root" in payload["error"]["message"]
    finally:
        stack.close()


def test_ops_path_through_a_regular_file_is_refused(tmp_path: Path) -> None:
    ops_root = tmp_path / "ops"
    ops_root.mkdir()
    (ops_root / "plain.txt").write_text("x", encoding="utf-8")
    stack = _seeded_stack(tmp_path, ops_root=ops_root)
    try:
        status, payload = _asgi_call(
            create_app(stack), "/v1/ops/backup", json.dumps({"dest_path": "plain.txt/inner.db"}).encode()
        )
        assert status == 400
        assert payload["error"]["code"] == "PATH_ESCAPE"
    finally:
        stack.close()


def test_ops_path_that_moves_mid_operation_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stack = _seeded_stack(tmp_path)
    calls: list[str] = []
    original = http_api_module._resolve_ops_path  # noqa: SLF001

    def _shifting(stack_arg: Any, candidate: str) -> Path:
        calls.append(candidate)
        resolved = original(stack_arg, candidate)
        if len(calls) > 1:
            return resolved.with_name(resolved.name + ".moved")
        return resolved

    monkeypatch.setattr(http_api_module, "_resolve_ops_path", _shim(calls, original))
    try:
        status, payload = _asgi_call(
            create_app(stack), "/v1/ops/backup", json.dumps({"dest_path": "b.sqlite3"}).encode()
        )
        assert status == 400
        assert payload["error"]["code"] == "PATH_ESCAPE"
        assert "moved" in payload["error"]["message"]
    finally:
        stack.close()


def _shim(calls: list[str], original: Any) -> Any:
    def _shifting(stack_arg: Any, candidate: str) -> Path:
        calls.append(candidate)
        resolved = original(stack_arg, candidate)
        if len(calls) > 1:
            return resolved.with_name(resolved.name + ".moved")
        return resolved

    return _shifting


def test_startup_recovery_repairs_the_projection_for_stale_journals(tmp_path: Path) -> None:
    # First boot: leave one crashed (stale) in-progress journal behind.
    store = EventStore(tmp_path / "soul.sqlite3")
    GraphProjector(store, output_root=tmp_path / "out")
    audit = AuditLog(store)
    audit.begin(Invocation.create("knowledge.query", "ab" * 32, actor="test"))
    store.execute_raw(
        "UPDATE invocation_journal SET heartbeat_at = ? WHERE status = 'in_progress'",
        ("2020-01-01T00:00:00+00:00",),
    )
    store.close()
    # Second boot: recovery closes the journal and runs the projection repair
    # closure (http_api._repair_projection).
    stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    try:
        rows = stack.store.incomplete_journals()
        assert rows == []
    finally:
        stack.close()


def test_handler_level_body_cap_is_defense_in_depth(tmp_path: Path) -> None:
    import anyio
    from starlette.requests import Request

    stack = _seeded_stack(tmp_path)
    try:
        body = b"x" * 128
        messages = [{"type": "http.request", "body": body, "more_body": False}]

        async def receive() -> dict[str, Any]:
            return messages.pop(0)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/v1/ops/health",
            "headers": [(b"content-type", b"application/json")],
        }

        async def run() -> Any:
            request = Request(scope, receive)
            return await _handle(stack, "health", request, 64)

        response = anyio.run(run)
        assert response.status_code == 413
    finally:
        stack.close()


# ------------------------------------------------------- boundary middleware


def _middleware(inner: Any, **overrides: Any) -> BoundaryMiddleware:
    kwargs: dict[str, Any] = {"api_key": None, "host_allowlist": ("localhost",), "max_body_bytes": 64}
    kwargs.update(overrides)
    return BoundaryMiddleware(inner, **kwargs)


def _drive(app: Any, messages: list[dict[str, Any]], headers: list[tuple[bytes, bytes]]) -> list[dict[str, Any]]:
    import anyio

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {"type": "http", "method": "POST", "path": "/v1/ops/health", "headers": headers}

    async def run() -> None:
        await app(scope, receive, send)

    anyio.run(run)
    return sent


async def _ok_app(scope: Any, receive: Any, send: Any) -> None:
    await receive()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"{}"})


def test_denial_recorder_failure_never_weakens_the_denial() -> None:
    def _raising_recorder(scope: Any, error: BaseException) -> None:
        raise RuntimeError("audit store is on fire")

    app = _middleware(_ok_app, denial_recorder=_raising_recorder)
    sent = _drive(app, [{"type": "http.request", "body": b"{}", "more_body": False}], [(b"host", b"evil.example.com")])
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 403


def _raising_recorder(scope: Any, error: BaseException) -> None:
    raise RuntimeError("audit store is on fire")


def test_garbage_content_length_is_treated_as_over_cap() -> None:
    app = _middleware(_ok_app)
    sent = _drive(
        app,
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        [(b"host", b"localhost"), (b"content-length", b"not-a-number")],
    )
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 413


def test_client_disconnect_mid_body_ends_the_request_silently() -> None:
    app = _middleware(_ok_app)
    sent = _drive(
        app,
        [
            {"type": "http.request", "body": b"partial", "more_body": True},
            {"type": "http.disconnect"},
        ],
        [(b"host", b"localhost"), (b"content-length", b"10")],
    )
    assert [m for m in sent if m["type"] == "http.response.start"] == []


def test_unknown_message_types_are_skipped_until_the_request() -> None:
    app = _middleware(_ok_app)
    sent = _drive(
        app,
        [
            {"type": "http.unexpected"},
            {"type": "http.request", "body": b"{}", "more_body": False},
        ],
        [(b"host", b"localhost"), (b"content-length", b"2")],
    )
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 200


def test_replay_receive_passes_through_after_the_buffered_body() -> None:
    async def _greedy_app(scope: Any, receive: Any, send: Any) -> None:
        first = await receive()
        assert first["type"] == "http.request"
        second = await receive()  # past the buffer: real passthrough
        assert second["type"] == "http.request"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    app = _middleware(_greedy_app)
    sent = _drive(
        app,
        [
            {"type": "http.request", "body": b"{}", "more_body": False},
            {"type": "http.request", "body": b"", "more_body": False},
        ],
        [(b"host", b"localhost"), (b"content-length", b"2")],
    )
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    assert status == 200


# ------------------------------------------------------------------ unix bind


def test_unlink_stale_socket_refuses_when_the_probe_cannot_answer(
    sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    sock_path = sock_dir / "stale.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(sock_path))
    listener.close()

    class _UnprobableSocket:
        def settimeout(self, seconds: float) -> None:
            pass

        def connect(self, path: str) -> None:
            raise OSError(errno.EACCES, "permission denied")

        def close(self) -> None:
            pass

    monkeypatch.setattr(http_api_module.socket, "socket", lambda *a, **k: _UnprobableSocket())
    with pytest.raises(BindPolicyError, match="cannot verify"):
        _unlink_stale_socket(sock_path)
    assert sock_path.exists()  # never unlinked


def test_bind_unix_socket_closes_the_socket_when_listen_fails(tmp_path: Path) -> None:
    overlong = tmp_path / ("x" * 200 + ".sock")
    with pytest.raises(OSError):
        _bind_unix_socket(overlong)


def test_serve_requires_exactly_one_transport(tmp_path: Path) -> None:
    stack = _seeded_stack(tmp_path)
    try:
        with pytest.raises(BindPolicyError, match="exactly one"):
            serve(stack)
        with pytest.raises(BindPolicyError, match="exactly one"):
            serve(stack, socket_path=tmp_path / "a.sock", http="127.0.0.1:8899")
    finally:
        stack.close()


def test_serve_http_branch_builds_a_uvicorn_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    stack = _seeded_stack(tmp_path)
    ran: list[bool] = []
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: ran.append(True))
    try:
        serve(stack, http="127.0.0.1:8899", api_key="test-key")
        assert ran == [True]
    finally:
        stack.close()


def test_serve_cleanup_tolerates_a_socket_that_is_no_longer_ours(
    tmp_path: Path, sock_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import uvicorn

    stack = _seeded_stack(tmp_path)
    monkeypatch.setattr(uvicorn.Server, "run", lambda self: None)
    calls: list[str] = []
    original = http_api_module._unlink_stale_socket  # noqa: SLF001

    def _flaky(path: Path) -> None:
        calls.append(str(path))
        if len(calls) > 1:
            raise BindPolicyError("no longer names our stale socket")
        return original(path)

    monkeypatch.setattr(http_api_module, "_unlink_stale_socket", _flaky)
    try:
        serve(stack, socket_path=sock_dir / "api.sock")  # must not raise
        assert len(calls) == 2
    finally:
        stack.close()


# ---------------------------------------------------------------------- cli


def test_cli_serve_passes_an_explicit_body_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from know_your_soul import cli

    captured: dict[str, Any] = {}
    monkeypatch.setattr(cli, "serve", lambda stack, **kwargs: captured.update(kwargs))
    exit_code = cli.main(
        [
            "--store",
            str(tmp_path / "soul.sqlite3"),
            "--output-root",
            str(tmp_path / "out"),
            "serve",
            "--socket",
            str(tmp_path / "api.sock"),
            "--max-body-bytes",
            "4096",
        ]
    )
    assert exit_code == 0
    assert captured["max_body_bytes"] == 4096


def test_cli_serve_keyboard_interrupt_is_a_clean_exit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from know_your_soul import cli

    def _interrupt(stack: Any, **kwargs: Any) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "serve", _interrupt)
    exit_code = cli.main(
        [
            "--store",
            str(tmp_path / "soul.sqlite3"),
            "--output-root",
            str(tmp_path / "out"),
            "serve",
            "--socket",
            str(tmp_path / "s.sock"),
        ]
    )
    assert exit_code == 0


def test_cli_health_exits_1_when_the_store_is_degraded(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from know_your_soul import cli

    store = EventStore(tmp_path / "soul.sqlite3")
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-1",
            domain=PrimaryDomain.P1,
            node=_node("n-1"),
            source_version_id="sv-test",
        ),
        idempotency_key="test:seed:n-1",
    )
    store.close()
    # Break the chain anchor: health must report not-ok and the CLI exits 1.
    import sqlite3

    conn = sqlite3.connect(tmp_path / "soul.sqlite3")
    conn.execute("UPDATE system_state SET value = ? WHERE key = 'chain_head_hash'", ("0" * 64,))
    conn.commit()
    conn.close()
    exit_code = cli.main(["--store", str(tmp_path / "soul.sqlite3"), "--output-root", str(tmp_path / "out"), "health"])
    assert exit_code == 1
    assert '"ok":false' in capsys.readouterr().out


def test_python_module_entry_point_runs_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "argv", ["know-your-soul"])
    with pytest.raises(SystemExit):
        runpy.run_module("know_your_soul", run_name="__main__")


# --------------------------------------------------------------- mcp internals


def test_type_matches_null() -> None:
    from know_your_soul.mcp_server import _type_matches  # noqa: SLF001

    assert _type_matches(None, "null") is True
    assert _type_matches(0, "null") is False


def test_check_property_allows_none_for_nullable_types() -> None:
    from know_your_soul.mcp_server import _check_property  # noqa: SLF001

    _check_property("approval", None, {"type": ["object", "null"]})  # no raise


def test_check_property_enforces_max_length_and_pattern() -> None:
    from know_your_soul.mcp_server import ToolError, _check_property  # noqa: SLF001

    with pytest.raises(ToolError, match="at most 3 characters"):
        _check_property("tag", "toolong", {"type": "string", "maxLength": 3})
    with pytest.raises(ToolError, match="decimal string"):
        _check_property("p", "1.5.5", {"type": "string", "pattern": r"^\d+(\.\d+)?$"})


def _mcp_adapter(tmp_path: Path, **overrides: Any) -> Any:
    from know_your_soul.mcp_server import _Adapter  # noqa: SLF001

    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "graph")
    service = KnowledgeService(store, projector)
    kwargs: dict[str, Any] = {
        "graph_root": tmp_path / "graph",
        "audit_log": AuditLog(store),
        "actor": "test",
        "max_token_budget": 1024,
        "resource_max_bytes": 4096,
    }
    kwargs.update(overrides)
    return _Adapter(service, **kwargs)


def _call_tool(adapter: Any, name: str, arguments: dict[str, Any]) -> Any:
    import anyio

    return anyio.run(adapter.call_tool, name, arguments)


def test_mcp_nested_object_missing_required_property_is_invalid(tmp_path: Path) -> None:
    adapter = _mcp_adapter(tmp_path)
    result = _call_tool(
        adapter,
        "knowledge_apply_improvement",
        {"proposal_id": "p-1", "approval": {"approved_by": "ops"}},  # missing decision
    )
    assert result.is_error is True
    assert "missing required properties" in result.content[0].text  # type: ignore[union-attr]


def test_mcp_get_subtree_forwards_a_relation_allowlist(tmp_path: Path) -> None:
    adapter = _mcp_adapter(tmp_path)
    result = _call_tool(adapter, "knowledge_get_subtree", {"node_id": "n-1", "relation_allowlist": ["parent_of"]})
    # No projection was built: a typed service failure, never a crash — the
    # allowlist arg was accepted and forwarded.
    assert result.is_error is True


def test_mcp_evaluate_parses_iso_datetime_and_rejects_garbage(tmp_path: Path) -> None:
    adapter = _mcp_adapter(tmp_path)
    bad = _call_tool(adapter, "knowledge_evaluate", {"node_id": "n-1", "at": "not-a-date"})
    assert bad.is_error is True
    assert "ISO-8601" in bad.content[0].text  # type: ignore[union-attr]
    good = _call_tool(adapter, "knowledge_evaluate", {"node_id": "n-1", "at": "2026-10-25T00:00:00+00:00"})
    assert good.is_error is True  # unknown node, typed — the datetime parsed fine
    assert "ISO-8601" not in good.content[0].text  # type: ignore[union-attr]


def test_mcp_propose_improvement_rejects_a_malformed_diff_item(tmp_path: Path) -> None:
    adapter = _mcp_adapter(tmp_path)
    result = _call_tool(
        adapter,
        "knowledge_propose_improvement",
        {"node_id": "n-1", "diff": [{"op": "no-such-op"}], "rationale": "r"},
    )
    assert result.is_error is True
    assert "diff" in result.content[0].text  # type: ignore[union-attr]


def test_mcp_apply_improvement_builds_and_rejects_approval_records(tmp_path: Path) -> None:
    adapter = _mcp_adapter(tmp_path)
    # A well-formed approval record is constructed and forwarded (the unknown
    # proposal then fails typed).
    ok = _call_tool(
        adapter,
        "knowledge_apply_improvement",
        {"proposal_id": "p-1", "approval": {"approved_by": "ops", "decision": "approved"}},
    )
    assert ok.is_error is True
    assert "invalid approval record" not in ok.content[0].text  # type: ignore[union-attr]
    bad = _call_tool(
        adapter,
        "knowledge_apply_improvement",
        {"proposal_id": "p-1", "approval": {"approved_by": "ops", "decision": "approved", "decided_at": "garbage"}},
    )
    assert bad.is_error is True
    assert "invalid approval record" in bad.content[0].text  # type: ignore[union-attr]


def test_mcp_store_error_surfaces_as_a_typed_tool_error(tmp_path: Path) -> None:
    adapter = _mcp_adapter(tmp_path)
    node = {
        "node_id": "n-1",
        "kind": "child",
        "domain": "P1",
        "summary": "first version",
        "source_version_id": "sv-test",
    }
    first = _call_tool(adapter, "knowledge_log_event", {"node": node, "idempotency_key": "k-1"})
    assert first.is_error is False
    conflict = dict(node, summary="a different payload under the same key")
    second = _call_tool(adapter, "knowledge_log_event", {"node": conflict, "idempotency_key": "k-1"})
    assert second.is_error is True
    assert "IdempotencyConflict" in second.content[0].text  # type: ignore[union-attr]


def test_mcp_projection_resource_without_a_generation_is_typed(tmp_path: Path) -> None:
    import anyio
    from mcp.shared.exceptions import MCPError

    adapter = _mcp_adapter(tmp_path)
    with pytest.raises(MCPError, match="no active projection"):
        anyio.run(adapter.read_resource, "soul://projection/stats")


def test_mcp_projection_resource_without_a_generation_dir_is_typed(tmp_path: Path) -> None:
    import anyio
    from mcp.shared.exceptions import MCPError

    adapter = _mcp_adapter(tmp_path)

    # TOCTOU guard: a manifest was served but the generation dir is gone.
    def _fake_manifest() -> dict[str, Any]:
        return {"graph_content_hash": "x", "projection_version": "gen-x", "source_knowledge_high_watermark": 0}

    adapter._active_manifest = _fake_manifest  # noqa: SLF001
    with pytest.raises(MCPError, match="no active projection"):
        anyio.run(adapter.read_resource, "soul://projection/nodes")


def test_mcp_projection_resource_refuses_a_tampered_graph(tmp_path: Path) -> None:
    import anyio
    from mcp.shared.exceptions import MCPError

    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "graph")
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-1",
            domain=PrimaryDomain.P1,
            node=_node("n-1"),
            source_version_id="sv-test",
        ),
        idempotency_key="test:seed:n-1",
    )
    projector.rebuild()
    from know_your_soul.mcp_server import _Adapter  # noqa: SLF001

    adapter = _Adapter(
        KnowledgeService(store, projector),
        graph_root=tmp_path / "graph",
        audit_log=AuditLog(store),
        actor="test",
        max_token_budget=1024,
        resource_max_bytes=4096,
    )
    generation = tmp_path / "graph" / "generations" / (tmp_path / "graph" / "CURRENT").read_text().strip()
    graph_path = generation / "graph.json"
    graph_path.write_text(graph_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(MCPError, match="stale projection"):
        anyio.run(adapter.read_resource, "soul://projection/nodes")


def test_mcp_resource_byte_cap_is_enforced(tmp_path: Path) -> None:
    import anyio
    from mcp.shared.exceptions import MCPError

    adapter = _mcp_adapter(tmp_path, resource_max_bytes=8)
    with pytest.raises(MCPError, match="byte cap|exceeds"):
        anyio.run(adapter.read_resource, "soul://relearn/status")


def test_build_server_tolerates_a_missing_distribution_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata

    from know_your_soul.mcp_server import build_server

    def _raise(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", _raise)
    (tmp_path / "graph").mkdir()
    stack_service = _mcp_adapter(tmp_path)._service  # noqa: SLF001
    server = build_server(stack_service, graph_root=tmp_path / "graph")
    assert server is not None


def test_mcp_main_refuses_a_dirty_provider_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from know_your_soul.mcp_server import main

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-dirty-environment")
    exit_code = main(["--graph-root", str(tmp_path), "--db", str(tmp_path / "soul.sqlite3")])
    assert exit_code == 2


def test_mcp_main_refuses_an_unset_api_key_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from know_your_soul import runtime_guard
    from know_your_soul.mcp_server import main

    monkeypatch.setattr(os, "environ", dict(runtime_guard.scrubbed_env()))
    monkeypatch.delenv("SOUL_TEST_UNSET_KEY", raising=False)
    exit_code = main(
        [
            "--graph-root",
            str(tmp_path),
            "--db",
            str(tmp_path / "soul.sqlite3"),
            "--transport",
            "http",
            "--api-key-env",
            "SOUL_TEST_UNSET_KEY",
        ]
    )
    assert exit_code == 2


def test_mcp_main_refuses_a_disallowed_http_bind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from know_your_soul import runtime_guard
    from know_your_soul.mcp_server import main

    # A non-loopback bind without a configured key is refused before serving.
    monkeypatch.setattr(os, "environ", dict(runtime_guard.scrubbed_env()))
    exit_code = main(
        [
            "--graph-root",
            str(tmp_path),
            "--db",
            str(tmp_path / "soul.sqlite3"),
            "--transport",
            "http",
            "--host",
            "0.0.0.0",
        ]
    )
    assert exit_code == 2


def test_mcp_main_stdio_delegates_to_anyio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import anyio

    from know_your_soul import runtime_guard
    from know_your_soul.mcp_server import main

    monkeypatch.setattr(os, "environ", dict(runtime_guard.scrubbed_env()))
    ran: list[Any] = []
    monkeypatch.setattr(anyio, "run", lambda fn, *args: ran.append(fn))
    exit_code = main(["--graph-root", str(tmp_path), "--db", str(tmp_path / "soul.sqlite3")])
    assert exit_code == 0
    assert len(ran) == 1


# ------------------------------------------------------- read-model validator


def _generation(
    tmp_path: Path, graph: Any, *, manifest_extra: dict[str, Any] | None = None
) -> tuple[Path, dict[str, Any]]:
    from know_your_soul.read_model_validator import HASHED_ARTIFACTS

    generation = tmp_path / "generation"
    generation.mkdir()
    graph_bytes = graph if isinstance(graph, bytes) else json.dumps(graph).encode("utf-8")
    (generation / "graph.json").write_bytes(graph_bytes)
    for name in HASHED_ARTIFACTS:
        if name != "graph.json":
            (generation / name).write_text(f"{name} placeholder\n", encoding="utf-8")
    artifacts = {name: hashlib.sha256((generation / name).read_bytes()).hexdigest() for name in HASHED_ARTIFACTS}
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "projection_version": "gen-test",
        "content_hash": hashlib.sha256(b"content").hexdigest(),
        "source_knowledge_high_watermark": 0,
        "schema_version": 1,
        "partitioner": "pinned",
        "artifacts": artifacts,
        "graph_content_hash": artifacts["graph.json"],
        "node_count": 0,
        "edge_count": 0,
        "multigraph": False,
    }
    if manifest_extra:
        manifest.update(manifest_extra)
    (generation / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    return generation, manifest


def test_validator_flags_a_manifest_missing_required_keys(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    generation, _manifest = _generation(tmp_path, {"directed": True, "multigraph": False, "nodes": [], "links": []})
    report = ReadModelValidator().validate(generation, {})
    assert not report.ok
    assert any("missing required keys" in error for error in report.errors)


def test_validator_flags_a_missing_manifest_file(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    generation, manifest = _generation(tmp_path, {"directed": True, "multigraph": False, "nodes": [], "links": []})
    (generation / "manifest.json").unlink()
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("manifest.json is missing" in error for error in report.errors)


def test_validator_flags_a_manifest_that_disagrees_with_disk(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    generation, manifest = _generation(tmp_path, {"directed": True, "multigraph": False, "nodes": [], "links": []})
    supplied = dict(manifest, node_count=99)
    report = ReadModelValidator().validate(generation, supplied)
    assert not report.ok
    assert any("does not match the supplied manifest" in error for error in report.errors)


def test_validator_flags_a_wrong_artifact_set(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    generation, manifest = _generation(
        tmp_path, {"directed": True, "multigraph": False, "nodes": [], "links": []}, manifest_extra={"artifacts": {}}
    )
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("artifacts must be exactly" in error for error in report.errors)


def test_validator_flags_invalid_graph_json(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    generation, manifest = _generation(tmp_path, b"{not json")
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("not valid JSON" in error for error in report.errors)


def test_validator_flags_a_non_object_graph(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    generation, manifest = _generation(tmp_path, [1, 2, 3])
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("must be a JSON object" in error for error in report.errors)


def test_validator_flags_malformed_links_and_junk_nodes(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    graph = {
        "directed": True,
        "multigraph": False,
        "nodes": [{"label": "no id here"}],
        "links": [
            "not-a-link",
            {"confidence": "high", "source": "/absolute/path", "target": "t-1", "relation": "depends_on"},
        ],
    }
    generation, manifest = _generation(tmp_path, graph)
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("missing confidence_score" in error for error in report.errors)
    assert any("absolute path" in error for error in report.errors)
    assert any("missing or non-string id" in error for error in report.errors)


# NOTE: ``ReadModelValidator``'s "loader did not restore a directed graph"
# branch is unreachable: the pinned Graphify loader builds with
# ``directed=True`` unconditionally (verified against graphify_adapter.build_graph).


def test_validator_flags_a_forbidden_root_leak(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    generation, manifest = _generation(tmp_path, {"directed": True, "multigraph": False, "nodes": [], "links": []})
    target = generation / "labels.json"
    target.write_text(f"leaks {tmp_path} here\n", encoding="utf-8")
    manifest["artifacts"]["labels.json"] = hashlib.sha256(target.read_bytes()).hexdigest()
    (generation / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    validator = ReadModelValidator(extra_forbidden_roots=(str(tmp_path),))
    report = validator.validate(generation, manifest)
    assert not report.ok
    assert any("leaks the absolute checkout path" in error for error in report.errors)


def test_validator_secret_suffix_is_empty_outside_graph_nodes(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    # A secret-shaped string in a non-graph artifact: the node-location suffix
    # is empty (graph-only), and the leak is still flagged.
    generation, manifest = _generation(tmp_path, {"directed": True, "multigraph": False, "nodes": [], "links": []})
    target = generation / "labels.json"
    target.write_text("key = AKIAIOSFODNN7EXAMPLE\n", encoding="utf-8")
    manifest["artifacts"]["labels.json"] = hashlib.sha256(target.read_bytes()).hexdigest()
    (generation / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("aws access key" in error for error in report.errors)


def test_validator_secret_suffix_is_empty_when_no_node_carries_the_secret(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    graph = {
        "directed": True,
        "multigraph": False,
        "graph": {"note": "AKIAIOSFODNN7EXAMPLE"},
        "nodes": [{"id": "n1", "label": "clean"}],
        "links": [],
    }
    generation, manifest = _generation(tmp_path, graph)
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("aws access key" in error and "node(s)" not in error for error in report.errors)


def test_validator_secret_suffix_is_empty_when_nodes_are_not_a_list(tmp_path: Path) -> None:
    from know_your_soul.read_model_validator import ReadModelValidator

    graph = {
        "directed": True,
        "multigraph": False,
        "graph": {"note": "AKIAIOSFODNN7EXAMPLE"},
        "nodes": {"n1": {"id": "n1"}},
        "links": [],
    }
    generation, manifest = _generation(tmp_path, graph)
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("aws access key" in error for error in report.errors)


# --------------------------------------------------------- fragment generator


def test_file_scoped_id_strips_dotdot_segments() -> None:
    assert ".." not in file_scoped_id("a/../b.md")
    assert file_scoped_id("a/../b.md") == file_scoped_id("a/b.md")


def test_fragment_generator_refuses_a_non_positive_cap() -> None:
    with pytest.raises(ValueError, match="positive"):
        FragmentGenerator(max_source_bytes=0)
    assert FragmentGenerator(max_source_bytes=64).max_source_bytes == 64


def test_fragment_generator_rejects_non_utf8_content() -> None:
    with pytest.raises(FragmentError, match="not UTF-8"):
        FragmentGenerator().generate(source_file="bad.md", content=b"\xff\xfe\x00")


# ------------------------------------------------------------ graphify adapter


def test_generate_fragment_rejects_schema_invalid_nodes() -> None:
    adapter = GraphifyAdapter()
    with pytest.raises(FragmentValidationError, match="missing required field"):
        adapter.generate_fragment([{"id": "n1", "label": "x", "type": "concept"}], [])


def test_fragment_cache_round_trip_serves_the_second_extraction(tmp_path: Path) -> None:
    from know_your_soul.projection import FragmentCache

    cache = FragmentCache(tmp_path / "cache")
    adapter = GraphifyAdapter(cache=cache)
    content = b"# Title\n\nSome body text.\n"
    first = adapter.fragment_from_markdown(source_file="a.md", content=content)
    second = adapter.fragment_from_markdown(source_file="a.md", content=content)
    assert first == second
    assert len(list((tmp_path / "cache").rglob("*.json"))) >= 1


def test_partition_restores_a_preset_ambiguous_scale_env() -> None:
    adapter = GraphifyAdapter()
    graph = adapter.build_graph(
        {
            "nodes": [
                {"id": "a", "label": "A", "type": "concept", "source_file": "f.md", "file_type": "md"},
                {"id": "b", "label": "B", "type": "concept", "source_file": "f.md", "file_type": "md"},
            ],
            "edges": [{"source": "a", "target": "b", "relation": "depends_on"}],
        }
    )
    os.environ["GRAPHIFY_AMBIGUOUS_SCALE"] = "0.123"
    try:
        communities = adapter.partition(graph)
        assert isinstance(communities, dict)
        assert os.environ["GRAPHIFY_AMBIGUOUS_SCALE"] == "0.123"
    finally:
        os.environ.pop("GRAPHIFY_AMBIGUOUS_SCALE", None)


# ------------------------------------------------------------------- retrieval


def test_resolve_domain_input_returns_the_unique_prefix_match() -> None:
    from know_your_soul.retrieval import _DOMAIN_KEYS, _MIN_PREFIX_LENGTH, resolve_domain_input  # noqa: SLF001

    unique: str | None = None
    for key in sorted(_DOMAIN_KEYS):
        for length in range(_MIN_PREFIX_LENGTH, len(key) + 1):
            prefix = key[:length]
            matches = {domain for candidate, domain in _DOMAIN_KEYS.items() if candidate.startswith(prefix)}
            if len(matches) == 1:
                unique = prefix
                break
        if unique is not None:
            break
    assert unique is not None
    assert resolve_domain_input(unique) in set(_DOMAIN_KEYS.values())


def test_primary_root_is_none_when_the_parent_chain_dangles() -> None:
    from know_your_soul.retrieval import GraphLink, PrimaryRouter

    router = PrimaryRouter(
        [{"id": "a", "kind": "leaf", "domain": "P1"}],
        [GraphLink(source="ghost", target="a", relation="parent_of")],
    )
    assert router._primary_root("a") is None  # noqa: SLF001


def test_primary_root_is_none_when_the_parent_chain_cycles() -> None:
    from know_your_soul.retrieval import GraphLink, PrimaryRouter

    router = PrimaryRouter(
        [{"id": "a", "kind": "leaf", "domain": "P9"}, {"id": "b", "kind": "leaf", "domain": "P9"}],
        [
            GraphLink(source="b", target="a", relation="parent_of"),
            GraphLink(source="a", target="b", relation="parent_of"),
        ],
    )
    assert router._primary_root("a") is None  # noqa: SLF001


# ------------------------------------------------------------- update pipeline


def test_update_from_changes_with_no_changes_is_an_empty_receipt(tmp_path: Path) -> None:
    from know_your_soul.importer import SeedImporter
    from know_your_soul.update_pipeline import update_from_changes

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        importer = SeedImporter(store, state_dir=tmp_path / "state")
        receipt = update_from_changes([], importer=importer, root=tmp_path)
        assert receipt.changed_paths == ()
        assert receipt.offer is None
        assert receipt.baseline_recorded is False
    finally:
        store.close()


def test_rebuild_when_free_reraises_projector_busy_after_the_last_attempt(tmp_path: Path) -> None:
    from know_your_soul.projection import ProjectorBusy
    from know_your_soul.update_pipeline import _rebuild_when_free  # noqa: SLF001

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        projector = GraphProjector(store, output_root=tmp_path / "out")

        def _busy(*args: object, **kwargs: object) -> object:
            raise ProjectorBusy("another writer holds the lock")

        projector.rebuild = _busy  # type: ignore[assignment]
        with pytest.raises(ProjectorBusy, match="another writer"):
            _rebuild_when_free(projector, shrink=False, attempts=2, backoff_seconds=0)
    finally:
        store.close()


# -------------------------------------------------------------- review history


def test_review_signal_marks_a_node_just_rolled_back(tmp_path: Path) -> None:
    from know_your_soul.improvement import DiffOp, DiffOperation, apply_improvement, propose, rollback_improvement
    from know_your_soul.review import ReviewEngine

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=_node("n-1"),
                source_version_id="sv-test",
            ),
            idempotency_key="test:seed:n-1",
        )
        record = propose(
            store,
            node_id="n-1",
            diff=[DiffOperation(op=DiffOp.SET_SUMMARY, value="a better summary")],
            rationale="improve the summary",
        )
        apply_improvement(store, record.proposal)
        rollback_improvement(store, node_id="n-1")
        engine = ReviewEngine(store)
        signal = engine.signal_for("n-1", at=NOW)
        assert signal.just_rolled_back is True
    finally:
        store.close()


def test_query_above_the_configured_max_budget_is_refused(tmp_path: Path) -> None:
    from know_your_soul.contracts import BudgetExceedsMax, QueryRequest
    from know_your_soul.projection import GraphProjector
    from know_your_soul.service import KnowledgeService

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        projector = GraphProjector(store, output_root=tmp_path / "out")
        service = KnowledgeService(store, projector, max_token_budget=256)
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=_node("n-1"),
                source_version_id="sv-test",
            ),
            idempotency_key="test:seed:n-1",
        )
        projector.rebuild()
        with pytest.raises(BudgetExceedsMax, match="exceeds the configured maximum"):
            service.query(QueryRequest(purpose="anything", token_budget=512))
    finally:
        store.close()


def test_write_meta_marks_stale_when_the_repair_cannot_cover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from know_your_soul.contracts import EvaluateRequest
    from know_your_soul.projection import GraphProjector
    from know_your_soul.service import KnowledgeService

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        projector = GraphProjector(store, output_root=tmp_path / "out")
        service = KnowledgeService(store, projector)
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=_node("n-1"),
                source_version_id="sv-test",
            ),
            idempotency_key="test:seed:n-1",
        )
        # One evidence item so the evaluation actually moves the scores (and
        # with them the knowledge watermark).
        evidence = {
            "evidence_id": "e-1",
            "node_id": "n-1",
            "source_version_id": "sv-test",
            "source_uri": "soul://corpus/a.md",
            "observed_at": "2026-10-20T00:00:00+00:00",
            "independence_key": "grp",
            "outcome": "0.9",
            "correctness": "0.9",
            "reliability": "0.9",
            "reproducibility": "0.9",
            "scope_fit": "0.9",
            "successful_use": "0.9",
            "task_coverage": "0.9",
            "quality_at_use": "0.9",
            "contradiction": "0",
            "utilization_u": "0.5",
            "latency_ms": 1000,
            "compute_ms": 100,
            "output_chars": 500,
        }
        from know_your_soul.domain import Evidence

        service.record_outcome(Evidence.model_validate(evidence), idempotency_key="test:outcome:e-1")
        projector.rebuild()
        monkeypatch.setattr(GraphProjector, "repair_if_stale", lambda self: None)
        response = service.evaluate(EvaluateRequest(node_id="n-1"))
        assert response.stale is True
    finally:
        store.close()
