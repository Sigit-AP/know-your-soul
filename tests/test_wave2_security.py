"""Wave-2 adapter security regression probes (audit findings F1-F6).

Report: ``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave2-adapters-security.md``.
Every test here pins one fixed attack shape: the old exploit must now fail
closed, and the legitimate flow must keep working (non-vacuous).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import stat
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from know_your_soul import http_api
from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
)
from know_your_soul.http_api import build_stack, create_app
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
        idempotency_key=f"test:wave2:upsert:{node.node_id}",
    )


def _seed(store: EventStore) -> None:
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
        idempotency_key="test:wave2:rel:P1:n-gates",
    )


@pytest.fixture()
def stack(tmp_path: Path):
    built = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    _seed(built.store)
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
    receive_override: Any = None,
) -> tuple[int, dict[str, str], bytes]:
    """Minimal ASGI client; JSON content-type is added for POST bodies."""
    header_list = list(headers or [])
    if not any(name.lower() == b"host" for name, _ in header_list):
        header_list.append((b"host", b"localhost"))
    if body and not any(name.lower() == b"content-length" for name, _ in header_list):
        header_list.append((b"content-length", str(len(body)).encode()))
    if method == "POST" and body and not any(name.lower() == b"content-type" for name, _ in header_list):
        header_list.append((b"content-type", b"application/json"))
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

    asyncio.run(app(scope, receive_override or receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    response_headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in start.get("headers", [])}
    response_body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return int(start["status"]), response_headers, response_body


def _post(app: Any, path: str, payload: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    status, _, body = asgi_call(app, "POST", path, body=json.dumps(payload).encode("utf-8"), **kwargs)
    return status, json.loads(body.decode("utf-8"))


def _audit_count(store: EventStore) -> int:
    row = store.execute_raw("SELECT COUNT(*) AS n FROM event_log WHERE kind = 'audit'").fetchone()
    return int(row["n"])


# ---------------------------------------------------- F1: CSRF + path escape


def test_f1_non_json_content_type_gets_415_and_never_reaches_the_handler(stack: Any) -> None:
    """A browser form (text/plain) carrying a valid JSON ops body is rejected."""
    app = create_app(stack)
    dest = Path(stack.ops_root) / "csrf-backup.sqlite3"
    body = json.dumps({"dest_path": str(dest)}).encode("utf-8")
    before = _audit_count(stack.store)
    status, _, raw = asgi_call(
        app,
        "POST",
        "/v1/ops/backup",
        body=body,
        headers=[(b"content-type", b"text/plain")],
    )
    assert status == 415
    error = json.loads(raw.decode("utf-8"))["error"]
    assert error["code"] == "UNSUPPORTED_MEDIA_TYPE"
    assert not dest.exists(), "the form post must never reach the backup handler"
    assert _audit_count(stack.store) == before, "boundary rejections are not invocations (spec 7.7)"


def test_f1_missing_content_type_gets_415(stack: Any) -> None:
    app = create_app(stack)
    status, _, raw = asgi_call(
        app,
        "POST",
        "/v1/knowledge/query",
        body=json.dumps({"purpose": "x", "token_budget": 512}).encode("utf-8"),
        headers=[(b"content-type", b"")],  # present but empty -> not application/json
    )
    assert status == 415


def test_f1_json_content_type_with_charset_still_works(stack: Any) -> None:
    """Legitimate clients (incl. charset parameters) are unaffected."""
    app = create_app(stack)
    status, out = _post(
        app,
        "/v1/knowledge/query",
        {"purpose": "run the gates", "domain": "P1", "token_budget": 512},
        headers=[(b"content-type", b"application/json; charset=utf-8")],
    )
    assert status == 200
    assert "n-gates" in {node["node_id"] for node in out["results"]}


def test_f1_loopback_also_needs_the_configured_key(stack: Any) -> None:
    """Fail-closed: a configured API key gates loopback and unix peers too."""
    app = create_app(stack, api_key="wave2-key")
    status, _, raw = asgi_call(app, "GET", "/v1/health", client=("127.0.0.1", 5000))
    assert status == 401
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == "UNAUTHORIZED"
    # unix-socket peer (no client address)
    status, _, _ = asgi_call(app, "GET", "/v1/health", client=None)
    assert status == 401
    # wrong key
    status, _, _ = asgi_call(app, "GET", "/v1/health", headers=[(b"x-api-key", b"nope")])
    assert status == 401
    # correct key
    status, _, _ = asgi_call(app, "GET", "/v1/health", headers=[(b"x-api-key", b"wave2-key")])
    assert status == 200


def test_f1_no_configured_key_still_serves_loopback(stack: Any) -> None:
    app = create_app(stack)
    status, _, _ = asgi_call(app, "GET", "/v1/health", client=("127.0.0.1", 5000))
    assert status == 200
    status, _, _ = asgi_call(app, "GET", "/v1/health", client=None)
    assert status == 200


def test_f1_backup_dest_outside_ops_root_rejected(stack: Any, tmp_path: Path) -> None:
    app = create_app(stack)
    outside = tmp_path.parent / f"wave2-evil-{os.getpid()}.sqlite3"
    assert not str(outside).startswith(str(stack.ops_root))
    try:
        status, out = _post(app, "/v1/ops/backup", {"dest_path": str(outside)})
        assert status == 400
        assert out["error"]["code"] == "PATH_ESCAPE"
        assert not outside.exists(), "the old arbitrary-path write must fail closed"
        # Relative traversal variant.
        status, out = _post(app, "/v1/ops/backup", {"dest_path": "../wave2-escape.sqlite3"})
        assert status == 400
        assert out["error"]["code"] == "PATH_ESCAPE"
    finally:
        outside.unlink(missing_ok=True)


def test_f1_restore_paths_outside_ops_root_rejected(stack: Any, tmp_path: Path) -> None:
    app = create_app(stack)
    backup_inside = Path(stack.ops_root) / "ok-backup.sqlite3"
    status, _ = _post(app, "/v1/ops/backup", {"dest_path": str(backup_inside)})
    assert status == 200
    # dest outside the root
    status, out = _post(
        app,
        "/v1/ops/restore",
        {"backup_path": str(backup_inside), "dest_path": str(tmp_path.parent / "wave2-evil-restore.sqlite3")},
    )
    assert status == 400
    assert out["error"]["code"] == "PATH_ESCAPE"
    # backup source outside the root
    status, out = _post(
        app,
        "/v1/ops/restore",
        {"backup_path": "/etc/hostname", "dest_path": str(Path(stack.ops_root) / "restored.sqlite3")},
    )
    assert status == 400
    assert out["error"]["code"] == "PATH_ESCAPE"


def test_f1_backup_restore_inside_ops_root_still_work(stack: Any) -> None:
    """Non-vacuous: the legitimate ops flow survives containment."""
    app = create_app(stack)
    backup_path = Path(stack.ops_root) / "legit-backup.sqlite3"
    status, out = _post(app, "/v1/ops/backup", {"dest_path": str(backup_path)})
    assert status == 200
    assert backup_path.exists()
    assert out["sha256"]
    status, out = _post(
        app,
        "/v1/ops/restore",
        {"backup_path": str(backup_path), "dest_path": str(Path(stack.ops_root) / "legit-restored.sqlite3")},
    )
    assert status == 200
    assert (Path(stack.ops_root) / "legit-restored.sqlite3").exists()


def test_f1_path_escape_message_does_not_leak_ops_root(stack: Any, tmp_path: Path) -> None:
    """Re-review observation: the PATH_ESCAPE wire message must not disclose
    the server's absolute ops_root; naming the caller's own candidate is
    enough diagnostics for a legitimate client."""
    app = create_app(stack)
    ops_root_text = str(Path(stack.ops_root).resolve())
    # relative traversal variant
    status, out = _post(app, "/v1/ops/backup", {"dest_path": "../wave2-escape.sqlite3"})
    assert status == 400
    assert out["error"]["code"] == "PATH_ESCAPE"
    assert ops_root_text not in out["error"]["message"], "the absolute ops_root must not leak onto the wire"
    assert "wave2-escape.sqlite3" in out["error"]["message"], "the caller's candidate must remain for diagnostics"
    # absolute-path variant
    outside = str(tmp_path.parent / "wave2-abs-escape.sqlite3")
    status, out = _post(app, "/v1/ops/backup", {"dest_path": outside})
    assert status == 400
    assert out["error"]["code"] == "PATH_ESCAPE"
    assert ops_root_text not in out["error"]["message"]


# ------------------------------------------------------ F2: unix socket mode


def test_f2_unix_socket_created_0600_regardless_of_umask(stack: Any, tmp_path: Path) -> None:
    """Even with a permissive ambient umask the socket is owner-only."""
    socket_path = tmp_path / "api.sock"
    old_umask = os.umask(0o000)
    try:
        listener = http_api._bind_unix_socket(socket_path)
    finally:
        os.umask(old_umask)
    try:
        mode = stat.S_IMODE(socket_path.stat().st_mode)
        assert mode == 0o600, f"socket mode {oct(mode)} must be exactly 0o600"
    finally:
        listener.close()


def test_f2_serve_over_prebound_socket_still_works(stack: Any, tmp_path: Path) -> None:
    """The fd-passed uvicorn path serves real requests end to end."""
    app = create_app(stack)
    socket_path = tmp_path / "api.sock"
    listener = http_api._bind_unix_socket(socket_path)
    config = uvicorn.Config(app, fd=listener.fileno(), log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 15
        while not server.started:
            assert time.monotonic() < deadline, "server did not start"
            time.sleep(0.05)
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(10)
            conn.connect(str(socket_path))
            conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        response = b"".join(chunks)
        assert b"200" in response.split(b"\r\n", 1)[0]
        assert json.loads(response.split(b"\r\n\r\n", 1)[1].decode("utf-8"))["ok"] is True
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
    assert not thread.is_alive()


# ------------------------------------------- F4: streaming cap + read timeout


def test_f4_chunked_body_is_cut_at_the_cap_without_full_buffering(stack: Any) -> None:
    """A chunked body with no Content-Length is rejected mid-stream (cap+1)."""
    app = create_app(stack, max_body_bytes=1024)
    chunks = [b"x" * 600, b"y" * 600, b"z" * 600]
    calls = 0

    async def chunked_receive() -> dict[str, Any]:
        nonlocal calls
        if calls < len(chunks):
            body = chunks[calls]
            calls += 1
            return {"type": "http.request", "body": body, "more_body": True}
        return {"type": "http.disconnect"}

    status, _, raw = asgi_call(
        app,
        "POST",
        "/v1/knowledge/query",
        headers=[(b"transfer-encoding", b"chunked")],
        receive_override=chunked_receive,
    )
    assert status == 413
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert calls == 2, f"server consumed {calls} chunks before cutting; the cap must stop at cap+1"


def test_f4_body_read_timeout_closes_slow_dribble(stack: Any) -> None:
    """A body dribbled slower than the read timeout is rejected with 408."""
    app = create_app(stack, body_timeout_seconds=0.1)

    async def slow_receive() -> dict[str, Any]:
        await asyncio.sleep(5.0)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    started = time.monotonic()
    status, _, raw = asgi_call(
        app,
        "POST",
        "/v1/knowledge/query",
        headers=[(b"content-length", b"2")],
        receive_override=slow_receive,
    )
    elapsed = time.monotonic() - started
    assert status == 408
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == "REQUEST_TIMEOUT"
    assert elapsed < 5.0, "the slow body must be cut by the timeout, not served"


def test_f4_normal_body_within_cap_still_works(stack: Any) -> None:
    app = create_app(stack, max_body_bytes=1024 * 1024, body_timeout_seconds=5.0)
    status, out = _post(app, "/v1/knowledge/query", {"purpose": "gates", "domain": "P1", "token_budget": 512})
    assert status == 200
    assert out["audit_event_id"]


def test_f4_sub_timeout_dribble_cut_by_total_body_deadline(stack: Any) -> None:
    """F4 residual (re-review): a dribble whose every gap stays *under* the
    timeout must still be cut — the timeout is a total body deadline, not
    merely per-read. Six 0.4s gaps against a 1.0s budget must die at ~1.0s,
    not complete at ~2.4s and hold the connection for the whole dribble."""
    app = create_app(stack, body_timeout_seconds=1.0)
    chunks = [b"x"] * 6
    calls = 0

    async def dribble_receive() -> dict[str, Any]:
        nonlocal calls
        await asyncio.sleep(0.4)  # every gap below the per-read budget
        body = chunks[calls]
        calls += 1
        return {"type": "http.request", "body": body, "more_body": calls < len(chunks)}

    started = time.monotonic()
    status, _, raw = asgi_call(
        app,
        "POST",
        "/v1/knowledge/query",
        headers=[(b"transfer-encoding", b"chunked")],
        receive_override=dribble_receive,
    )
    elapsed = time.monotonic() - started
    assert status == 408
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == "REQUEST_TIMEOUT"
    assert elapsed < 1.8, f"sub-timeout dribble held the connection for {elapsed:.2f}s; the total deadline must cut it"
    assert calls < len(chunks), "the full dribble was consumed before any cut"


def test_f4_chunked_normal_upload_within_deadline_still_works(stack: Any) -> None:
    """Non-vacuous positive control: a streamed multi-chunk body that keeps
    inside the total deadline is served normally."""
    app = create_app(stack, body_timeout_seconds=5.0)
    parts = [b'{"purpose": "gates", ', b'"domain": "P1", ', b'"token_budget": 512}']
    calls = 0

    async def chunked_receive() -> dict[str, Any]:
        nonlocal calls
        await asyncio.sleep(0.01)
        body = parts[calls]
        calls += 1
        return {"type": "http.request", "body": body, "more_body": calls < len(parts)}

    status, _, raw = asgi_call(
        app,
        "POST",
        "/v1/knowledge/query",
        headers=[(b"transfer-encoding", b"chunked"), (b"content-type", b"application/json")],
        receive_override=chunked_receive,
    )
    assert status == 200
    out = json.loads(raw.decode("utf-8"))
    assert "n-gates" in {node["node_id"] for node in out["results"]}


# -------------------------------------------- F5: INTERNAL envelope sanitation


def test_f5_internal_error_never_leaks_exception_text(
    stack: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Raw exception text goes to the server log; the wire says 'internal error'."""
    secret_detail = "/proc/1/no-dir boom: [Errno 2]"

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(secret_detail)

    monkeypatch.setattr("know_your_soul.http_api.dispatch_text", boom)
    app = create_app(stack)
    with caplog.at_level(logging.ERROR, logger="know_your_soul.http_api"):
        status, _, raw = asgi_call(
            app, "POST", "/v1/knowledge/query", body=json.dumps({"purpose": "x", "token_budget": 512}).encode()
        )
    assert status == 500
    error = json.loads(raw.decode("utf-8"))["error"]
    assert error["code"] == "INTERNAL"
    assert error["message"] == "internal error"
    assert secret_detail not in raw.decode("utf-8")
    assert any(record.exc_info is not None and secret_detail in str(record.exc_info[1]) for record in caplog.records), (
        "the detail must land in the server-side log"
    )
