"""Contract tests for the persistent local JSON API (Task 7).

Versioned ``/v1`` HTTP surface over the same ten-operation
``KnowledgeService``: consistent error envelopes, request size limits, Host
allowlist + loopback/socket boundary enforcement, exactly one final audit per
valid invocation (validation failures included), and restart survival.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
)
from know_your_soul.http_api import build_stack, create_app  # noqa: E402  (RED until http_api.py lands)
from know_your_soul.store import EventStore

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
        idempotency_key=f"test:upsert:{node.node_id}",
    )


def _seed_store(store: EventStore) -> None:
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
            context_tags=("gates", "discipline"),
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
        idempotency_key="test:rel:P1:n-gates",
    )


@pytest.fixture()
def stack(tmp_path: Path):
    built = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    _seed_store(built.store)
    built.projector.rebuild()
    yield built
    built.close()


def asgi_call(
    app: Any,
    method: str,
    path: str,
    *,
    body: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
    client: tuple[str, int] | None = ("127.0.0.1", 5000),
) -> tuple[int, dict[str, str], bytes]:
    """Minimal dependency-free ASGI HTTP client (httpx is not installed)."""
    header_list = list(headers or [])
    if not any(name.lower() == b"host" for name, _ in header_list):
        header_list.append((b"host", b"localhost"))
    if body and not any(name.lower() == b"content-length" for name, _ in header_list):
        header_list.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
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
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    response_headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start.get("headers", [])}
    response_body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return int(start["status"]), response_headers, response_body


def _post(app: Any, path: str, payload: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    headers = list(kwargs.pop("headers", []) or [])
    if not any(name.lower() == b"content-type" for name, _ in headers):
        headers.append((b"content-type", b"application/json"))
    status, _, body = asgi_call(app, "POST", path, body=json.dumps(payload).encode("utf-8"), headers=headers, **kwargs)
    return status, json.loads(body.decode("utf-8"))


def _audit_count(store: EventStore) -> int:
    row = store.execute_raw("SELECT COUNT(*) AS n FROM event_log WHERE kind = 'audit'").fetchone()
    return int(row["n"])


# --------------------------------------------------------------- happy paths


def test_health_endpoint(stack: Any) -> None:
    app = create_app(stack)
    status, _, body = asgi_call(app, "GET", "/v1/health")
    assert status == 200
    report = json.loads(body.decode("utf-8"))
    assert report["ok"] is True
    assert report["checks"]["store_select"] is True


def test_query_contract_preserved(stack: Any) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/knowledge/query", {"purpose": "run the gates", "domain": "P1", "token_budget": 512})
    assert status == 200
    assert out["routing"] == "direct"
    assert "n-gates" in {node["node_id"] for node in out["results"]}
    assert out["node_versions"]["n-gates"] == 1
    assert out["audit_event_id"]
    assert str(out["projection_version"]).startswith("gen-")
    assert out["required_knowledge_watermark"] == out["source_knowledge_high_watermark"]
    assert out["stale"] is False
    assert out["estimated_tokens"] <= 512
    assert set(out["recommendations"].keys()) == {"best_actions", "recommendations", "unnecessary_actions"}


def test_get_node_and_get_subtree(stack: Any) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/knowledge/get_node", {"node_id": "n-gates"})
    assert status == 200
    assert out["node"]["node_id"] == "n-gates"
    assert out["node"]["version"] == 1
    assert out["audit_event_id"]

    status, out = _post(app, "/v1/knowledge/get_subtree", {"node_id": "P1", "token_budget": 512})
    assert status == 200
    assert "n-gates" in {node["node_id"] for node in out["nodes"]}
    assert out["audit_event_id"]


def test_log_event_and_record_outcome(stack: Any) -> None:
    app = create_app(stack)
    node_payload = {
        "node_id": "n-api",
        "parent_id": "P1",
        "kind": "child",
        "domain": "P1",
        "summary": "Added through the API",
        "provenance": ["soul://corpus/api.md"],
        "source_version_id": "sv-test",
    }
    status, out = _post(app, "/v1/knowledge/log_event", node_payload)
    assert status == 200
    assert out["audit_event_id"]
    assert out["node_version"] == 1

    evidence_payload = {
        "evidence_id": "ev-api-1",
        "node_id": "n-api",
        "source_version_id": "sv-test",
        "source_uri": "soul://corpus/api.md",
        "observed_at": "2026-10-20T00:00:00+00:00",
        "context_tags": ["gates"],
        "independence_key": "ev-api-1",
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
    status, out = _post(app, "/v1/knowledge/record_outcome", evidence_payload)
    assert status == 200
    assert out["audit_event_id"]
    assert out["audit_event_seq"]


def test_evaluate_review_and_improvement_lifecycle(stack: Any) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/knowledge/evaluate", {"node_id": "n-gates"})
    assert status == 200
    assert out["node_id"] == "n-gates"
    for field in ("p", "m", "e", "u_p", "u_m", "u_e", "overall_uncertainty", "g"):
        assert field in out
    assert out["audit_event_id"]

    status, out = _post(app, "/v1/knowledge/review", {"node_id": "n-gates"})
    assert status == 200
    assert out["node_id"] == "n-gates"
    assert "decision" in out
    assert out["audit_event_id"]

    status, out = _post(
        app,
        "/v1/knowledge/propose_improvement",
        {
            "node_id": "n-gates",
            "diff": [{"op": "set_summary", "value": "Run every gate before each commit"}],
            "rationale": "sharpen the summary",
        },
    )
    assert status == 200
    proposal = out["proposal"] if "proposal" in out else out
    proposal_id = proposal["proposal_id"]
    assert str(proposal_id).startswith("prop_")
    assert out["audit_event_id"]

    status, out = _post(app, "/v1/knowledge/apply_improvement", {"proposal_id": proposal_id})
    assert status == 200
    assert out["status"] == "applied"
    # A preceding review may have persisted a transition, so versions move monotonically.
    assert out["to_version"] == out["from_version"] + 1
    assert out["audit_event_id"]

    status, out = _post(app, "/v1/knowledge/rollback", {"node_id": "n-gates"})
    assert status == 200
    assert out["status"] == "rolled_back"
    assert out["audit_event_id"]


def test_ops_rebuild_and_backup(stack: Any, tmp_path: Path) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/ops/rebuild", {})
    assert status == 200
    assert str(out["projection_version"]).startswith("gen-")

    backup_path = tmp_path / "api-backup.sqlite3"
    status, out = _post(app, "/v1/ops/backup", {"dest_path": str(backup_path)})
    assert status == 200
    assert backup_path.exists()
    assert out["sha256"]


# ------------------------------------------------------------- audit contract


def test_exactly_one_final_audit_per_valid_invocation(stack: Any) -> None:
    app = create_app(stack)
    before = _audit_count(stack.store)
    status, out = _post(app, "/v1/knowledge/query", {"purpose": "gates", "domain": "P1", "token_budget": 512})
    assert status == 200
    assert _audit_count(stack.store) == before + 1
    rows = stack.store.execute_raw(
        "SELECT COUNT(*) AS n FROM event_log WHERE kind = 'audit' AND event_id = ?", (out["audit_event_id"],)
    ).fetchall()
    assert int(rows[0]["n"]) == 1


def test_service_rejection_is_not_double_journaled(stack: Any) -> None:
    """A typed service failure already carries the service's one final audit."""
    app = create_app(stack)
    before = _audit_count(stack.store)
    status, out = _post(app, "/v1/knowledge/get_node", {"node_id": "n-absent"})
    assert status == 404
    assert out["error"]["code"] == "NODE_NOT_FOUND"
    assert _audit_count(stack.store) == before + 1


# ------------------------------------------------------------ error envelopes


def test_typed_error_envelope(stack: Any) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/knowledge/get_node", {"node_id": "n-absent"})
    assert status == 404
    error = out["error"]
    assert error["code"] == "NODE_NOT_FOUND"
    assert error["retryable"] is False
    assert error["audit_event_id"]
    assert error["message"]


def test_malformed_json_gets_completion_audit(stack: Any) -> None:
    app = create_app(stack)
    before = _audit_count(stack.store)
    status, _, body = asgi_call(
        app, "POST", "/v1/knowledge/query", body=b"{not json", headers=[(b"content-type", b"application/json")]
    )
    assert status == 400
    error = json.loads(body.decode("utf-8"))["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["audit_event_id"]
    assert _audit_count(stack.store) == before + 1


def test_unknown_field_gets_validation_audit(stack: Any) -> None:
    app = create_app(stack)
    before = _audit_count(stack.store)
    status, out = _post(app, "/v1/knowledge/query", {"purpose": "x", "token_budget": 512, "bogus_field": True})
    assert status == 400
    assert out["error"]["code"] == "VALIDATION_ERROR"
    assert _audit_count(stack.store) == before + 1


def test_unknown_operation_route_envelope(stack: Any) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/knowledge/definitely_not_an_op", {})
    assert status == 404
    assert out["error"]["code"] == "NOT_FOUND"


def test_unversioned_route_absent(stack: Any) -> None:
    app = create_app(stack)
    status, _, _ = asgi_call(app, "POST", "/knowledge/query", body=b"{}")
    assert status == 404


def test_oversized_request_rejected(stack: Any) -> None:
    app = create_app(stack)
    big = json.dumps({"purpose": "x" * 2_000_000, "token_budget": 512}).encode("utf-8")
    status, _, body = asgi_call(app, "POST", "/v1/knowledge/query", body=big)
    assert status == 413
    assert json.loads(body.decode("utf-8"))["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_oversized_content_length_rejected_before_read(stack: Any) -> None:
    app = create_app(stack)
    status, _, body = asgi_call(
        app,
        "POST",
        "/v1/knowledge/query",
        body=b"{}",
        headers=[(b"content-length", str(10_000_000).encode())],
    )
    assert status == 413
    assert json.loads(body.decode("utf-8"))["error"]["code"] == "PAYLOAD_TOO_LARGE"


# ------------------------------------------------------- boundary enforcement


def test_host_allowlist_enforced(stack: Any) -> None:
    app = create_app(stack)
    status, _, body = asgi_call(
        app, "POST", "/v1/knowledge/query", body=b"{}", headers=[(b"host", b"evil.example.com")]
    )
    assert status == 403
    assert json.loads(body.decode("utf-8"))["error"]["code"] == "HOST_NOT_ALLOWED"


def test_remote_client_requires_api_key(stack: Any) -> None:
    app = create_app(stack, api_key="test-key-123")
    remote = ("10.1.2.3", 9999)
    status, _, body = asgi_call(app, "GET", "/v1/health", client=remote)
    assert status == 401
    assert json.loads(body.decode("utf-8"))["error"]["code"] == "UNAUTHORIZED"

    status, _, _ = asgi_call(app, "GET", "/v1/health", client=remote, headers=[(b"x-api-key", b"wrong-key")])
    assert status == 401

    status, _, _ = asgi_call(app, "GET", "/v1/health", client=remote, headers=[(b"x-api-key", b"test-key-123")])
    assert status == 200


def test_loopback_client_also_needs_the_configured_key(stack: Any) -> None:
    """Wave-2 F1: a configured API key gates loopback and unix peers too (CSRF)."""
    app = create_app(stack, api_key="test-key-123")
    status, _, _ = asgi_call(app, "GET", "/v1/health", client=("127.0.0.1", 5000))
    assert status == 401
    status, _, _ = asgi_call(app, "GET", "/v1/health", client=("::1", 5000))
    assert status == 401
    # A unix-socket peer (no client address) is local — still gated when a key is set.
    status, _, _ = asgi_call(app, "GET", "/v1/health", client=None)
    assert status == 401
    # The configured key passes from any local peer.
    status, _, _ = asgi_call(
        app, "GET", "/v1/health", client=("127.0.0.1", 5000), headers=[(b"x-api-key", b"test-key-123")]
    )
    assert status == 200


# ------------------------------------------------------------------ lifecycle


def test_restart_persistence(tmp_path: Path) -> None:
    db_path, out_path = tmp_path / "soul.sqlite3", tmp_path / "out"
    first = build_stack(db_path, out_path)
    _seed_store(first.store)
    first.projector.rebuild()
    first.close()

    second = build_stack(db_path, out_path)
    try:
        app = create_app(second)
        status, out = _post(
            app, "/v1/knowledge/query", {"purpose": "run the gates", "domain": "P1", "token_budget": 512}
        )
        assert status == 200
        assert "n-gates" in {node["node_id"] for node in out["results"]}
    finally:
        second.close()


def test_live_unix_socket_server_and_graceful_shutdown(stack: Any, tmp_path: Path) -> None:
    """The app serves over a real unix socket and shuts down cleanly."""
    app = create_app(stack)
    socket_path = tmp_path / "api.sock"
    config = uvicorn.Config(app, uds=str(socket_path), log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not socket_path.exists():
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)

        def raw_http(request: bytes) -> bytes:
            with socket.socket(socket.AF_UNIX) as conn:
                conn.settimeout(10)
                conn.connect(str(socket_path))
                conn.sendall(request)
                chunks = []
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks)

        response = raw_http(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        assert b"200" in response.split(b"\r\n", 1)[0]
        assert json.loads(response.split(b"\r\n\r\n", 1)[1].decode("utf-8"))["ok"] is True

        query = json.dumps({"purpose": "gates", "domain": "P1", "token_budget": 512}).encode("utf-8")
        request = (
            b"POST /v1/knowledge/query HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(query)).encode() + b"\r\nConnection: close\r\n\r\n" + query
        )
        response = raw_http(request)
        assert b"200" in response.split(b"\r\n", 1)[0]
        payload = json.loads(response.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
        assert payload["audit_event_id"]
    finally:
        server.should_exit = True
        thread.join(timeout=15)
    assert not thread.is_alive()
