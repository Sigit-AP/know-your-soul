"""Tests for ``know_your_soul.serve.webview`` (W7 SUB-101).

Pins the contract of the loopback-only, token-authenticated, read-only web
viewer:

* **Security boundary** (codegraph §4.5 threat model = DNS rebinding):
  token in the URL gates every byte, the ``Host`` header must be loopback
  with the server's own port, a non-loopback ``Origin`` is refused, no CORS
  header is ever emitted, only GET/HEAD exist, and every response carries
  ``nosniff``.
* **Endpoint shape contracts** for the ten JSON endpoints plus the static
  assets; every payload is deterministic and JSON-safe (``<``/``>``/``&``
  escaped), so a hostile node label cannot break out (stored-XSS probe).
* **Read-only guarantee**: the store's event watermark, counts and file
  bytes are unchanged after exercising every endpoint.
* **CLI**: ``webview [--port N]`` argument contract, fail-closed port
  validation, and the printed URL carries the token.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from know_your_soul import runtime_guard
from know_your_soul.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
    ScoreVector,
)
from know_your_soul.serve.webview import (
    ANALYTICS_MAX_NODES,
    DEFAULT_HOST_ALLOWLIST,
    MAX_GRAPH_NODES,
    StoreUnavailableError,
    WebviewApp,
    WebviewError,
    WebviewServer,
    escape_json_bytes,
    generate_token,
    host_allowed,
    main,
    origin_allowed,
)
from know_your_soul.store import EventStore

NOW = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
XSS_LABEL = "<script>alert(1)</script>"


# ------------------------------------------------------- socket-guard truce
#
# ``mcp_server.main()`` installs a process-lifetime
# :class:`runtime_guard.SocketGuard` by design; the wave-3 adapter tests run
# ``main()`` with ``anyio.run`` monkeypatched, so the guard is never
# uninstalled and leaks process-wide, denying even loopback AF_INET. This
# module serves a real loopback-only HTTP server, so each test temporarily
# restores the true originals and puts the leaked state back afterwards.


def _suspend_socket_guard() -> dict[str, Any] | None:
    if runtime_guard._SOCKET_GUARD_DEPTH == 0:  # noqa: SLF001
        return None
    state: dict[str, Any] = {
        "depth": runtime_guard._SOCKET_GUARD_DEPTH,  # noqa: SLF001
        "originals": dict(runtime_guard._SOCKET_ORIGINALS),  # noqa: SLF001
        "connect": socket.socket.connect,
        "connect_ex": socket.socket.connect_ex,
        "sendto": socket.socket.sendto,
        "sendmsg": socket.socket.sendmsg,
        "create_connection": socket.create_connection,
    }
    originals = state["originals"]
    if originals:
        socket.socket.connect = originals["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = originals["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = originals["sendto"]  # type: ignore[method-assign]
        socket.socket.sendmsg = originals["sendmsg"]  # type: ignore[method-assign]
        socket.create_connection = originals["create_connection"]  # type: ignore[assignment]
    runtime_guard._SOCKET_GUARD_DEPTH = 0  # noqa: SLF001
    runtime_guard._SOCKET_ORIGINALS.clear()  # noqa: SLF001
    return state


def _resume_socket_guard(state: dict[str, Any]) -> None:
    socket.socket.connect = state["connect"]  # type: ignore[method-assign]
    socket.socket.connect_ex = state["connect_ex"]  # type: ignore[method-assign]
    socket.socket.sendto = state["sendto"]  # type: ignore[method-assign]
    socket.socket.sendmsg = state["sendmsg"]  # type: ignore[method-assign]
    socket.create_connection = state["create_connection"]  # type: ignore[assignment]
    runtime_guard._SOCKET_ORIGINALS.update(state["originals"])  # noqa: SLF001
    runtime_guard._SOCKET_GUARD_DEPTH = state["depth"]  # noqa: SLF001


@pytest.fixture(autouse=True)
def _loopback_sockets() -> Any:
    state = _suspend_socket_guard()
    try:
        yield
    finally:
        if state is not None:
            _resume_socket_guard(state)


# --------------------------------------------------------------------- seeds


def _node(
    node_id: str,
    *,
    domain: PrimaryDomain = PrimaryDomain.P1,
    parent: str | None = None,
    summary: str | None = None,
    provenance: tuple[str, ...] = ("soul://corpus/lesson.md",),
    scores: ScoreVector | None = None,
) -> Node:
    return Node(
        node_id=node_id,
        parent_id=parent,
        kind=NodeKind.CHILD if parent else NodeKind.ROOT,
        domain=domain,
        summary=summary if summary is not None else node_id,
        provenance=provenance,
        source_version_id="sv-webview",
        scores=scores if scores is not None else ScoreVector.zero(),
        created_at=NOW,
        updated_at=NOW,
    )


def _upsert(store: EventStore, node: Node) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key=f"webview:upsert:{node.node_id}:{store.node_version(node.node_id)}",
    )


def _seed(store: EventStore) -> None:
    scored = ScoreVector.model_validate({"p": "0.5", "m": "0.4", "e": "0.3", "uncertainty": "0.4", "confidence": "0.6"})
    _upsert(store, _node("n-root"))
    _upsert(store, _node("n-alpha", parent="n-root", summary="alpha lesson", scores=scored))
    _upsert(store, _node("n-beta", parent="n-root", domain=PrimaryDomain.P2, summary="beta lesson"))
    _upsert(store, _node("n-xss", parent="n-root", summary=XSS_LABEL, provenance=()))
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id="n-alpha",
            domain=PrimaryDomain.P1,
            relation=Relation(
                kind=RelationKind.SUPPORTS,
                source_node_id="n-alpha",
                target_node_id="n-beta",
            ),
            occurred_at=NOW,
        ),
        idempotency_key="webview:rel:alpha-beta",
    )
    store.append_domain(
        Event(
            kind=EventKind.EVIDENCE_APPEND,
            node_id="n-alpha",
            domain=PrimaryDomain.P1,
            evidence=Evidence.model_validate(
                {
                    "evidence_id": "ev-alpha-1",
                    "node_id": "n-alpha",
                    "source_version_id": "sv-webview",
                    "source_uri": "soul://corpus/lesson.md",
                    "observed_at": NOW,
                    "independence_key": "indep-1",
                    "outcome": "0.5",
                    "correctness": "1",
                    "reliability": "1",
                    "reproducibility": "1",
                    "scope_fit": "1",
                    "successful_use": "1",
                    "utilization_u": "0.5",
                    "task_coverage": "0.5",
                    "quality_at_use": "0.5",
                    "contradiction": "0",
                    "latency_ms": 1,
                    "compute_ms": 1,
                    "output_chars": 4,
                }
            ),
            occurred_at=NOW,
        ),
        idempotency_key="webview:ev:alpha-1",
    )
    # Universal-graph (code) side: files, symbols, call edges, unresolved refs.
    store.upsert_file(
        path="a.py", sha256="a" * 64, size=10, mtime=1, language="python", generated_hint=False, extraction_version=1
    )
    store.upsert_file(
        path="b.js",
        sha256="b" * 64,
        size=10,
        mtime=1,
        language="javascript",
        generated_hint=False,
        extraction_version=1,
    )
    store.upsert_symbol(node_id="function:aaa", name="alpha_fn", kind="function", path="a.py", line=1)
    store.upsert_symbol(node_id="function:bbb", name="beta_fn", kind="function", path="b.js", line=2)
    store.upsert_symbol(node_id="class:ccc", name="AlphaClass", kind="class", path="a.py", line=9)
    store.add_graph_edge("function:aaa", "function:bbb", "calls", line=3)
    pending_a = store.insert_unresolved_ref(source_node_id="function:aaa", target_name="gamma_fn", kind="calls")
    store.insert_unresolved_ref(source_node_id="function:bbb", target_name="delta_fn", kind="calls")
    store.record_resolution_attempt(pending_a, outcome="failed", reason="no_match")
    # Audit events, one carrying a credential-shaped payload that must be redacted.
    for index, payload in enumerate(
        (
            {"operation": "remember", "api_key": "secret-value-123"},
            {"operation": "recall", "ok": True},
            {"operation": "improve", "node_id": "n-alpha"},
        )
    ):
        with store.transaction() as conn:
            store.insert_event(
                conn,
                event_id=f"audit-seed-{index}",
                kind="audit",
                event_type="op.complete",
                knowledge_affecting=False,
                actor="tester",
                occurred_at="2026-01-01T00:00:00+00:00",
                payload=payload,
                idempotency_key=f"webview:audit:{index}",
            )


@pytest.fixture(scope="module")
def store_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("webview") / "webview.sqlite3"
    store = EventStore(path)
    _seed(store)
    store.close()
    return path


@pytest.fixture(scope="module")
def app(store_path: Path) -> Any:
    instance = WebviewApp(store_path)
    yield instance
    instance.close()


@pytest.fixture(scope="module")
def server(store_path: Path) -> Any:
    instance = WebviewServer(store_path, port=0, token="test-token-0000000000")
    instance.start()
    yield instance
    instance.shutdown()


def _http(
    server: WebviewServer,
    path: str,
    params_q: str | None = None,
    *,
    token: str | None = "test-token-0000000000",
    headers: dict[str, str] | None = None,
    method: str = "GET",
) -> tuple[int, dict[str, str], bytes]:
    if params_q is not None:
        path = f"{path}?q={quote(params_q)}"
    query = f"token={token}" if token is not None else None
    full_path = f"{path}{'&' if '?' in path else '?'}{query}" if query else path
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        connection.request(method, full_path, headers=headers or {})
        response = connection.getresponse()
        body = response.read()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, body
    finally:
        connection.close()


def _http_raw_host(server: WebviewServer, path: str, host_header: str | None) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        connection.putrequest("GET", path, skip_host=True, skip_accept_encoding=True)
        if host_header is not None:
            connection.putheader("Host", host_header)
        connection.endheaders()
        response = connection.getresponse()
        body = response.read()
        return response.status, {key.lower(): value for key, value in response.getheaders()}, body
    finally:
        connection.close()


def _json(body: bytes) -> Any:
    return json.loads(body.decode("utf-8"))


# ====================================================================== auth


class TestTokenAuth:
    ENDPOINTS = (
        "/",
        "/static/app.js",
        "/static/style.css",
        "/api/graph",
        "/api/node/n-root",
        "/api/search?q=alpha",
        "/api/hierarchy/function:aaa",
        "/api/metrics",
        "/api/health",
        "/api/analytics",
        "/api/audit",
        "/api/proposals",
        "/api/refs",
    )

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_missing_token_is_401(self, server: WebviewServer, endpoint: str) -> None:
        status, _, body = _http(server, endpoint, token=None)
        assert status == 401
        assert _json(body)["error"]["code"] == "UNAUTHORIZED"

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_wrong_token_is_401(self, server: WebviewServer, endpoint: str) -> None:
        status, _, _ = _http(server, endpoint, token="wrong-token")
        assert status == 401

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_correct_token_passes(self, server: WebviewServer, endpoint: str) -> None:
        status, _, _ = _http(server, endpoint)
        assert status == 200

    def test_error_body_is_json(self, server: WebviewServer) -> None:
        status, headers, body = _http(server, "/api/graph", token=None)
        assert status == 401
        assert headers["content-type"].startswith("application/json")
        assert set(_json(body)["error"]) >= {"code", "message"}

    def test_token_not_echoed_in_error(self, server: WebviewServer) -> None:
        _, _, body = _http(server, "/api/graph", token="leak-me-not")
        assert b"leak-me-not" not in body

    def test_generated_token_is_url_safe_and_long(self) -> None:
        token = generate_token()
        assert len(token) >= 32
        assert all(char.isalnum() or char in "-_" for char in token)

    def test_generated_tokens_differ(self) -> None:
        assert generate_token() != generate_token()


# ================================================================ host guard


class TestHostGuard:
    def test_localhost_with_port_passes(self, server: WebviewServer) -> None:
        status, _, _ = _http_raw_host(server, "/api/health?token=test-token-0000000000", f"localhost:{server.port}")
        assert status == 200

    def test_loopback_ip_with_port_passes(self, server: WebviewServer) -> None:
        status, _, _ = _http_raw_host(server, "/api/health?token=test-token-0000000000", f"127.0.0.1:{server.port}")
        assert status == 200

    def test_ipv6_loopback_passes(self, server: WebviewServer) -> None:
        status, _, _ = _http_raw_host(server, "/api/health?token=test-token-0000000000", f"[::1]:{server.port}")
        assert status == 200

    def test_dns_rebinding_host_refused(self, server: WebviewServer) -> None:
        status, _, body = _http_raw_host(server, "/api/health?token=test-token-0000000000", "evil.example.com")
        assert status == 403
        assert _json(body)["error"]["code"] == "HOST_NOT_ALLOWED"

    @pytest.mark.parametrize("host", ["localhost.evil.com", "127.0.0.1.evil.com", "localhost:", "[::2]:1"])
    def test_lookalike_hosts_refused(self, server: WebviewServer, host: str) -> None:
        status, _, _ = _http_raw_host(server, "/api/health?token=test-token-0000000000", host)
        assert status == 403

    def test_wrong_port_refused(self, server: WebviewServer) -> None:
        status, _, _ = _http_raw_host(server, "/api/health?token=test-token-0000000000", "localhost:1")
        assert status == 403

    def test_host_case_insensitive(self, server: WebviewServer) -> None:
        status, _, _ = _http_raw_host(server, "/api/health?token=test-token-0000000000", f"LOCALHOST:{server.port}")
        assert status == 200

    def test_host_guard_runs_before_auth(self, server: WebviewServer) -> None:
        # Even a valid token cannot rescue a foreign Host (rebinding order).
        status, _, _ = _http_raw_host(server, "/api/health?token=test-token-0000000000", "attacker.test")
        assert status == 403

    def test_host_allowed_unit(self) -> None:
        assert host_allowed("localhost:8000", 8000, DEFAULT_HOST_ALLOWLIST)
        assert host_allowed("127.0.0.1:8000", 8000, DEFAULT_HOST_ALLOWLIST)
        assert host_allowed("[::1]:8000", 8000, DEFAULT_HOST_ALLOWLIST)
        assert not host_allowed("evil.com:8000", 8000, DEFAULT_HOST_ALLOWLIST)
        assert not host_allowed("localhost:9000", 8000, DEFAULT_HOST_ALLOWLIST)
        assert not host_allowed("localhost", 8000, DEFAULT_HOST_ALLOWLIST)
        assert not host_allowed("", 8000, DEFAULT_HOST_ALLOWLIST)
        assert not host_allowed("localhost:abc", 8000, DEFAULT_HOST_ALLOWLIST)

    def test_origin_guard(self, server: WebviewServer) -> None:
        status, _, _ = _http(server, "/api/health", headers={"Origin": "http://evil.example.com"})
        assert status == 403
        status, _, _ = _http(server, "/api/health", headers={"Origin": f"http://localhost:{server.port}"})
        assert status == 200

    def test_origin_allowed_unit(self) -> None:
        assert origin_allowed(None, 8000)
        assert origin_allowed("http://localhost:8000", 8000)
        assert origin_allowed("http://127.0.0.1:8000", 8000)
        assert origin_allowed("http://[::1]:8000", 8000)
        assert not origin_allowed("http://evil.com", 8000)
        assert not origin_allowed("https://localhost:9000", 8000)
        assert not origin_allowed("not-a-url", 8000)


# ============================================================ transport rules


class TestTransport:
    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
    def test_non_get_methods_are_405(self, server: WebviewServer, method: str) -> None:
        status, _, body = _http(server, "/api/graph", method=method)
        assert status == 405
        assert _json(body)["error"]["code"] == "METHOD_NOT_ALLOWED"

    def test_head_works_like_get_without_body(self, server: WebviewServer) -> None:
        status, headers, body = _http(server, "/api/graph", method="HEAD")
        assert status == 200
        assert body == b""
        assert headers["content-type"].startswith("application/json")

    def test_no_cors_headers_anywhere(self, server: WebviewServer) -> None:
        for endpoint in ("/", "/api/graph", "/static/app.js"):
            _, headers, _ = _http(server, endpoint)
            assert not any(key.startswith("access-control") for key in headers)

    def test_nosniff_everywhere(self, server: WebviewServer) -> None:
        for endpoint in ("/", "/api/graph", "/static/app.js", "/static/style.css"):
            _, headers, _ = _http(server, endpoint)
            assert headers["x-content-type-options"] == "nosniff"

    def test_html_has_strict_csp(self, server: WebviewServer) -> None:
        _, headers, _ = _http(server, "/")
        csp = headers["content-security-policy"]
        assert "default-src 'none'" in csp
        assert "script-src 'self'" in csp

    def test_json_responses_have_no_store_cache(self, server: WebviewServer) -> None:
        _, headers, _ = _http(server, "/api/graph")
        assert "no-store" in headers["cache-control"]

    def test_unknown_path_is_404(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/nope")
        assert status == 404
        assert _json(body)["error"]["code"] == "NOT_FOUND"

    @pytest.mark.parametrize(
        "path",
        [
            "/static/../webview.py",
            "/static/..%2F..%2Fwebview.py",
            "/static/%2e%2e%2fsecret",
            "/static/app.js/extra",
            "/static/",
            "/api/node/",
            "/api/hierarchy/",
        ],
    )
    def test_traversal_and_malformed_paths_refused(self, server: WebviewServer, path: str) -> None:
        status, _, _ = _http(server, path)
        assert status in (400, 404)

    def test_static_unknown_asset_is_404(self, server: WebviewServer) -> None:
        status, _, _ = _http(server, "/static/evil.js")
        assert status == 404


# ==================================================================== assets


class TestAssets:
    def test_index_served_as_html(self, server: WebviewServer) -> None:
        status, headers, body = _http(server, "/")
        assert status == 200
        assert headers["content-type"].startswith("text/html")
        assert body.lstrip().lower().startswith(b"<!doctype html")

    def test_index_snapshot_deterministic(self, server: WebviewServer) -> None:
        first = _http(server, "/")[2]
        second = _http(server, "/")[2]
        assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()

    def test_index_matches_asset_file(self, server: WebviewServer) -> None:
        asset = (
            Path(__file__).resolve().parent.parent
            / "src"
            / "know_your_soul"
            / "serve"
            / "webview_assets"
            / "index.html"
        )
        assert _http(server, "/")[2] == asset.read_bytes()

    @pytest.mark.parametrize("tab", ["Graph", "Search", "Metrics", "Health", "Analytics", "Audit", "Proposals"])
    def test_index_declares_all_tabs(self, server: WebviewServer, tab: str) -> None:
        body = _http(server, "/")[2].decode("utf-8")
        assert f'data-tab="{tab.lower()}"' in body

    def test_index_uses_no_cdn(self, server: WebviewServer) -> None:
        body = _http(server, "/")[2].decode("utf-8").lower()
        assert "https://" not in body.replace("https://www.w3.org", "")
        assert "http-equiv" not in body or "cdn" not in body
        assert "integrity=" not in body  # no external subresource at all

    def test_index_dark_theme_default(self, server: WebviewServer) -> None:
        body = _http(server, "/")[2]
        assert b'data-theme="dark"' in body

    def test_app_js_served(self, server: WebviewServer) -> None:
        status, headers, body = _http(server, "/static/app.js")
        assert status == 200
        assert "javascript" in headers["content-type"]
        assert len(body) > 100

    def test_style_css_served(self, server: WebviewServer) -> None:
        status, headers, body = _http(server, "/static/style.css")
        assert status == 200
        assert headers["content-type"].startswith("text/css")
        assert len(body) > 100

    def test_assets_carry_no_token(self, server: WebviewServer) -> None:
        for endpoint in ("/", "/static/app.js", "/static/style.css"):
            _, _, body = _http(server, endpoint)
            assert b"test-token-0000000000" not in body

    def test_app_js_renders_via_textcontent_only(self, server: WebviewServer) -> None:
        # Structural XSS guard: graph data must never become markup.
        body = _http(server, "/static/app.js")[2]
        assert b"innerHTML" not in body
        assert b"eval(" not in body


# ==================================================================== graph


class TestGraphEndpoint:
    def test_shape(self, app: WebviewApp) -> None:
        payload = app.graph_payload()
        assert payload["schema_version"] == 1
        assert isinstance(payload["nodes"], list)
        assert isinstance(payload["edges"], list)
        assert payload["truncated"] is False
        assert payload["total_nodes"] == 4
        assert payload["total_edges"] >= 2  # parent_of x3 + supports

    def test_node_fields(self, app: WebviewApp) -> None:
        nodes = {node["id"]: node for node in app.graph_payload()["nodes"]}
        alpha = nodes["n-alpha"]
        assert alpha["label"] == "alpha lesson"
        assert alpha["domain"] == "P1"
        assert alpha["scores"]["p"] == "0.500000000000"
        assert alpha["scores"]["m"] == "0.400000000000"
        assert alpha["scores"]["e"] == "0.300000000000"
        assert 0.0 <= alpha["x"] <= payload_frame(app)["width"]
        assert 0.0 <= alpha["y"] <= payload_frame(app)["height"]

    def test_layout_metadata_present(self, app: WebviewApp) -> None:
        layout_meta = app.graph_payload()["layout"]
        assert layout_meta["mode"] in ("radial", "force")
        assert layout_meta["node_count"] == 4
        assert layout_meta["quantum"] == 0.001

    def test_edges_reference_known_nodes(self, app: WebviewApp) -> None:
        payload = app.graph_payload()
        ids = {node["id"] for node in payload["nodes"]}
        for edge in payload["edges"]:
            assert edge["source"] in ids
            assert edge["target"] in ids
            assert isinstance(edge["kind"], str)

    def test_parent_edges_present(self, app: WebviewApp) -> None:
        edges = app.graph_payload()["edges"]
        assert {"source": "n-root", "target": "n-alpha", "kind": "parent_of"} in edges

    def test_deterministic_across_calls(self, app: WebviewApp) -> None:
        assert escape_json_bytes(app.graph_payload()) == escape_json_bytes(app.graph_payload())

    def test_over_http(self, server: WebviewServer) -> None:
        status, headers, body = _http(server, "/api/graph")
        assert status == 200
        assert headers["content-type"].startswith("application/json")
        payload = _json(body)
        assert payload["total_nodes"] == 4
        assert {node["id"] for node in payload["nodes"]} >= {"n-root", "n-alpha"}


def payload_frame(app: WebviewApp) -> dict[str, float]:
    layout_meta = app.graph_payload()["layout"]
    return {"width": float(layout_meta["width"]), "height": float(layout_meta["height"])}


class TestXssEscape:
    def test_json_escapes_html_metacharacters(self) -> None:
        blob = escape_json_bytes({"label": "<script>&\u2028"})
        assert b"<script>" not in blob
        assert b"&" not in blob.replace(b"\\u0026", b"")
        assert b"\\u003c" in blob

    def test_graph_response_has_no_raw_script_label(self, server: WebviewServer) -> None:
        body = _http(server, "/api/graph")[2]
        assert XSS_LABEL.encode() not in body
        assert b"\\u003cscript\\u003e" in body

    def test_node_detail_escapes_label(self, server: WebviewServer) -> None:
        body = _http(server, "/api/node/n-xss")[2]
        assert XSS_LABEL.encode() not in body
        assert b"\\u003cscript\\u003e" in body

    def test_search_response_escapes(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/search", params_q="<img src=x onerror=alert(1)>")
        assert status in (200, 400)
        assert b"<img" not in body


# ==================================================================== node


class TestNodeEndpoint:
    def test_detail_shape(self, app: WebviewApp) -> None:
        detail = app.node_payload("n-alpha")
        assert detail["node_id"] == "n-alpha"
        assert detail["domain"] == "P1"
        assert detail["summary"] == "alpha lesson"
        assert detail["scores"]["p"] == "0.500000000000"
        assert detail["scores"]["confidence"] == "0.600000000000"
        assert detail["professional_level"] == 1
        assert detail["provenance"] == ["soul://corpus/lesson.md"]
        assert detail["lifecycle_status"] == "active"
        assert detail["version"] >= 1
        assert detail["evidence_ids"] == ["ev-alpha-1"]

    def test_relations_listed(self, app: WebviewApp) -> None:
        detail = app.node_payload("n-alpha")
        relations = detail["relations"]
        assert any(rel["kind"] == "supports" and rel["target"] == "n-beta" for rel in relations)

    def test_incident_edges_listed(self, app: WebviewApp) -> None:
        detail = app.node_payload("n-alpha")
        endpoints = {(edge["source"], edge["target"]) for edge in detail["incident_edges"]}
        assert ("n-root", "n-alpha") in endpoints
        assert ("n-alpha", "n-beta") in endpoints

    def test_unknown_node_is_404(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/node/n-ghost")
        assert status == 404
        assert _json(body)["error"]["code"] == "NOT_FOUND"

    def test_empty_provenance_is_empty_list(self, app: WebviewApp) -> None:
        assert app.node_payload("n-xss")["provenance"] == []


# ================================================================== search


class TestSearchEndpoint:
    def test_search_finds_symbol(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/search?q=alpha_fn")
        assert status == 200
        payload = _json(body)
        assert payload["count"] >= 1
        hit = payload["results"][0]
        assert hit["name"] == "alpha_fn"
        assert hit["kind"] == "function"
        assert hit["path"] == "a.py"
        assert hit["source"] in ("fts", "exact", "like", "fuzzy", "filter")

    def test_kind_filter(self, server: WebviewServer) -> None:
        _, _, body = _http(server, "/api/search?q=alpha&kind=class")
        payload = _json(body)
        assert payload["count"] == 1
        assert payload["results"][0]["kind"] == "class"
        assert payload["filters"]["kind"] == "class"

    def test_lang_filter(self, server: WebviewServer) -> None:
        _, _, body = _http(server, "/api/search?q=fn&lang=javascript")
        payload = _json(body)
        assert all(result["path"].endswith(".js") for result in payload["results"])
        assert payload["count"] == 1

    def test_kind_and_lang_combined(self, server: WebviewServer) -> None:
        _, _, body = _http(server, "/api/search?q=alpha&kind=function&lang=python")
        payload = _json(body)
        assert payload["count"] == 1
        assert payload["results"][0]["name"] == "alpha_fn"

    def test_empty_query_is_400(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/search")
        assert status == 400
        assert _json(body)["error"]["code"] == "BAD_REQUEST"

    def test_invalid_kind_is_400(self, server: WebviewServer) -> None:
        status, _, _ = _http(server, "/api/search?q=x&kind=FUNCTION!")
        assert status == 400

    def test_invalid_lang_is_400(self, server: WebviewServer) -> None:
        status, _, _ = _http(server, "/api/search?q=x&lang=py<thon")
        assert status == 400

    def test_invalid_limit_is_400(self, server: WebviewServer) -> None:
        for bad in ("0", "101", "abc", "1.5"):
            status, _, _ = _http(server, f"/api/search?q=alpha&limit={bad}")
            assert status == 400, bad

    def test_limit_caps_results(self, server: WebviewServer) -> None:
        _, _, body = _http(server, "/api/search?q=alpha&limit=1")
        assert _json(body)["count"] == 1

    def test_no_match_is_empty_not_error(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/search?q=zzz_no_such_symbol_zzz")
        assert status == 200
        assert _json(body)["count"] == 0


# =============================================================== hierarchy


class TestHierarchyEndpoint:
    def test_callers_and_callees(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/hierarchy/function:bbb")
        assert status == 200
        payload = _json(body)
        assert payload["node_id"] == "function:bbb"
        caller_names = [entry["name"] for entry in payload["callers"]["entries"]]
        assert "alpha_fn" in caller_names
        assert payload["callees"]["entries"] == []

    def test_callees_direction(self, server: WebviewServer) -> None:
        _, _, body = _http(server, "/api/hierarchy/function:aaa")
        payload = _json(body)
        callee_names = [entry["name"] for entry in payload["callees"]["entries"]]
        assert "beta_fn" in callee_names
        entry = payload["callees"]["entries"][0]
        assert entry["via_edge_kind"] == "calls"
        assert entry["depth"] == 1

    def test_depth_two(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/hierarchy/function:aaa?depth=2")
        assert status == 200
        assert _json(body)["depth"] == 2

    @pytest.mark.parametrize("bad", ["0", "9", "x", "1.5"])
    def test_invalid_depth_is_400(self, server: WebviewServer, bad: str) -> None:
        status, _, _ = _http(server, f"/api/hierarchy/function:aaa?depth={bad}")
        assert status == 400

    def test_unknown_symbol_is_404(self, server: WebviewServer) -> None:
        status, _, _ = _http(server, "/api/hierarchy/function:ghost")
        assert status == 404


# ================================================================= metrics


class TestMetricsEndpoint:
    def test_shape(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/metrics")
        assert status == 200
        payload = _json(body)
        for key in ("counts", "health", "latency_ms", "publish_capacity"):
            assert key in payload
        assert payload["counts"]["domain_events"] >= 4
        assert 0 <= payload["health"]["score"] <= payload["health"]["max_score"]

    def test_metrics_is_deterministic(self, server: WebviewServer) -> None:
        assert _http(server, "/api/metrics")[2] == _http(server, "/api/metrics")[2]


# ================================================================== health


class TestHealthEndpoint:
    def test_shape(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/health")
        assert status == 200
        payload = _json(body)
        assert payload["ok"] is True
        assert payload["error_count"] == 0
        assert isinstance(payload["findings"], list)
        assert isinstance(payload["checked"], list)
        assert "store_select" in payload["checked"]
        assert payload["exit_code"] == 0

    def test_finding_fields_when_present(self, server: WebviewServer) -> None:
        payload = _json(_http(server, "/api/health")[2])
        for finding in payload["findings"]:
            assert set(finding) >= {"code", "severity", "message"}
            assert finding["severity"] in ("info", "warning", "error")


# =============================================================== analytics


class TestAnalyticsEndpoint:
    def test_shape(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/analytics")
        assert status == 200
        payload = _json(body)
        for key in ("centrality", "communities", "bridges", "partial", "node_count"):
            assert key in payload
        assert payload["partial"] is False
        assert payload["node_count"] == 4

    def test_centrality_rankings(self, server: WebviewServer) -> None:
        centrality = _json(_http(server, "/api/analytics")[2])["centrality"]
        assert "global" in centrality
        global_block = centrality["global"]
        for metric in ("degree", "pagerank"):
            assert metric in global_block
        top_ids = [entry["node_id"] for entry in global_block["degree"]]
        assert "n-root" in top_ids

    def test_communities_have_members(self, server: WebviewServer) -> None:
        communities = _json(_http(server, "/api/analytics")[2])["communities"]
        members = [member for community in communities["communities"] for member in community["members"]]
        assert set(members) == {"n-root", "n-alpha", "n-beta", "n-xss"}

    def test_bridges_cross_domain(self, server: WebviewServer) -> None:
        bridges = _json(_http(server, "/api/analytics")[2])["bridges"]
        # n-alpha (P1) SUPPORTS n-beta (P2): one cross-domain link.
        assert bridges["counts"]["candidates"] >= 1
        kinds = {bridge["kind"] for bridge in bridges["bridges"]}
        assert "cross_domain" in kinds


# =================================================================== audit


class TestAuditEndpoint:
    def test_recent_events_descending(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/audit")
        assert status == 200
        payload = _json(body)
        assert payload["count"] == 3
        seqs = [event["event_seq"] for event in payload["events"]]
        assert seqs == sorted(seqs, reverse=True)

    def test_event_fields(self, server: WebviewServer) -> None:
        event = _json(_http(server, "/api/audit")[2])["events"][0]
        assert set(event) >= {"event_seq", "event_type", "actor", "occurred_at", "payload"}
        assert event["actor"] == "tester"

    def test_payload_redacted(self, server: WebviewServer) -> None:
        body = _http(server, "/api/audit")[2]
        assert b"secret-value-123" not in body

    def test_limit(self, server: WebviewServer) -> None:
        payload = _json(_http(server, "/api/audit?limit=2")[2])
        assert payload["count"] == 2

    @pytest.mark.parametrize("bad", ["0", "501", "abc"])
    def test_invalid_limit_is_400(self, server: WebviewServer, bad: str) -> None:
        assert _http(server, f"/api/audit?limit={bad}")[0] == 400


# =============================================================== proposals


class TestProposalsEndpoint:
    def test_shape(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/proposals")
        assert status == 200
        payload = _json(body)
        assert payload["schema_version"] == 1
        assert isinstance(payload["signals"], list)
        assert payload["count"] == len(payload["signals"])

    def test_isolated_node_signal(self, server: WebviewServer) -> None:
        # Every seeded node is connected, but the catalog order and item shape
        # are pinned for any signal that fires.
        payload = _json(_http(server, "/api/proposals")[2])
        for signal in payload["signals"]:
            assert set(signal) >= {"signal", "reason_codes", "source_node_ids", "low_confidence"}
            assert signal["signal"] in (
                "low_confidence_edge",
                "isolated_node",
                "low_cohesion_community",
                "stale_evidence",
                "missing_provenance",
            )

    def test_missing_provenance_fires_for_xss_node(self, server: WebviewServer) -> None:
        payload = _json(_http(server, "/api/proposals")[2])
        hits = [s for s in payload["signals"] if s["signal"] == "missing_provenance"]
        assert any("n-xss" in hit["source_node_ids"] for hit in hits)


# ==================================================================== refs


class TestRefsEndpoint:
    def test_shape(self, server: WebviewServer) -> None:
        status, _, body = _http(server, "/api/refs")
        assert status == 200
        payload = _json(body)
        assert payload["pending"] == 1
        assert payload["failed"] == 1
        assert payload["resolved"] == 0
        assert payload["total"] == 2

    def test_top_failed(self, server: WebviewServer) -> None:
        payload = _json(_http(server, "/api/refs")[2])
        assert payload["top_failed"][0]["name_tail"] == "gamma_fn"
        assert payload["top_failed"][0]["count"] == 1

    def test_failed_samples_carry_reason(self, server: WebviewServer) -> None:
        payload = _json(_http(server, "/api/refs")[2])
        assert payload["failed_samples"][0]["failure_reason"] == "no_match"


# ================================================================ read-only


class TestReadOnly:
    def test_store_bytes_unchanged_after_all_endpoints(self, server: WebviewServer, store_path: Path) -> None:
        before = hashlib.sha256(store_path.read_bytes()).hexdigest()
        endpoints = (
            "/",
            "/api/graph",
            "/api/node/n-root",
            "/api/node/n-alpha",
            "/api/search?q=alpha",
            "/api/search?q=fn&kind=function&lang=python",
            "/api/hierarchy/function:aaa?depth=2",
            "/api/metrics",
            "/api/health",
            "/api/analytics",
            "/api/audit?limit=10",
            "/api/proposals",
            "/api/refs",
        )
        for endpoint in endpoints:
            status, _, _ = _http(server, endpoint)
            assert status == 200, endpoint
        after = hashlib.sha256(store_path.read_bytes()).hexdigest()
        assert before == after

    def test_event_count_unchanged(self, server: WebviewServer, store_path: Path) -> None:
        reader = EventStore(store_path)
        try:
            before = reader.event_count()
        finally:
            reader.close()
        _http(server, "/api/graph")
        _http(server, "/api/audit")
        _http(server, "/api/analytics")
        reader = EventStore(store_path)
        try:
            after = reader.event_count()
        finally:
            reader.close()
        assert before == after


# ============================================================== app/config


class TestAppConfig:
    def test_missing_store_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(StoreUnavailableError):
            WebviewApp(tmp_path / "does-not-exist.sqlite3")

    def test_store_path_must_be_file(self, tmp_path: Path) -> None:
        with pytest.raises(StoreUnavailableError):
            WebviewApp(tmp_path)

    def test_graph_cap_constant(self) -> None:
        assert MAX_GRAPH_NODES == 4096
        assert ANALYTICS_MAX_NODES >= 1

    def test_double_close_is_safe(self, store_path: Path) -> None:
        instance = WebviewApp(store_path)
        instance.close()
        instance.close()


class TestServerLifecycle:
    def test_port_zero_gets_assigned(self, store_path: Path) -> None:
        server = WebviewServer(store_path, port=0)
        try:
            assert server.port > 0
        finally:
            server.shutdown()

    def test_url_contains_token_and_loopback(self, store_path: Path) -> None:
        server = WebviewServer(store_path, port=0, token="abc123abc123abc123")
        try:
            assert server.url.startswith("http://127.0.0.1:")
            assert "token=abc123abc123abc123" in server.url
        finally:
            server.shutdown()

    def test_fixed_port_respected(self, store_path: Path) -> None:
        server = WebviewServer(store_path, port=0)
        port = server.port
        server.shutdown()
        server2 = WebviewServer(store_path, port=port)
        try:
            assert server2.port == port
        finally:
            server2.shutdown()

    def test_invalid_port_fails_closed(self, store_path: Path) -> None:
        with pytest.raises(WebviewError):
            WebviewServer(store_path, port=-1)
        with pytest.raises(WebviewError):
            WebviewServer(store_path, port=70000)

    def test_invalid_token_fails_closed(self, store_path: Path) -> None:
        with pytest.raises(WebviewError):
            WebviewServer(store_path, port=0, token="short")
        with pytest.raises(WebviewError):
            WebviewServer(store_path, port=0, token="has space in it 1234567890123456")

    def test_shutdown_idempotent(self, store_path: Path) -> None:
        server = WebviewServer(store_path, port=0)
        server.shutdown()
        server.shutdown()


# ====================================================================== cli


class TestCli:
    def test_main_rejects_missing_store(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--store", str(tmp_path / "nope.sqlite3"), "--port", "0"])
        assert code == 2
        assert "store" in capsys.readouterr().err.lower()

    def test_main_rejects_bad_port(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "ok.sqlite3")
        store.close()
        with pytest.raises(SystemExit):
            main(["--store", str(tmp_path / "ok.sqlite3"), "--port", "not-a-port"])

    def test_main_port_out_of_range(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store = EventStore(tmp_path / "ok.sqlite3")
        store.close()
        code = main(["--store", str(tmp_path / "ok.sqlite3"), "--port", "99999"])
        assert code == 2
        capsys.readouterr()

    def test_main_serve_once_prints_url(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        store = EventStore(tmp_path / "ok.sqlite3")
        store.close()
        code = main(
            [
                "--store",
                str(tmp_path / "ok.sqlite3"),
                "--port",
                "0",
                "--token",
                "cli-token-0000000000000000",
                "--serve-once",
            ]
        )
        assert code == 0
        out = capsys.readouterr().out
        assert "http://127.0.0.1:" in out
        assert "token=cli-token-0000000000000000" in out

    def test_cli_parser_has_webview_subcommand(self) -> None:
        from know_your_soul.cli import build_parser

        args = build_parser().parse_args(["webview", "--port", "8123", "--no-open"])
        assert args.command == "webview"
        assert args.port == 8123
        assert args.no_open is True

    def test_cli_parser_defaults(self) -> None:
        from know_your_soul.cli import build_parser

        args = build_parser().parse_args(["webview"])
        assert args.port == 0
        assert args.no_open is False
