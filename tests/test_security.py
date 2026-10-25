"""Contract tests for the security primitives (Task 9 core).

Covers the task-9 security matrix rows minus the HTTP-server wiring owned by
Task 7: secret/PII redaction on write and read surfaces, control/bidi/
zero-width sanitization, path traversal and symlink escape rejection under a
fixed root, cross-tree root rejection, oversized payload guards, and the pure
bind/host/key policy functions that Task 7 wires into the API server.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from soul_knowledge.events import Invocation
from soul_knowledge.security import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    BindPolicyError,
    HostNotAllowedError,
    OversizedPayloadError,
    PathEscapeError,
    RequestView,
    UnauthorizedError,
    assert_bind_allowed,
    authorize_request,
    ensure_payload_size,
    ensure_text_size,
    host_allowed,
    is_loopback_host,
    make_host_allowlist,
    make_request_authorizer,
    redact_text,
    redact_value,
    resolve_within_root,
    sanitize_text,
)
from soul_knowledge.store import EventStore

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

SECRET_KEY = "sk-live0123456789abcdef0123"
SECRET_AWS = "AKIA0123456789ABCDEF"
SECRET_GH = "ghp_abcdefghijklmnopqrstuvwxyz012345"
SECRET_ASSIGNMENT = "password = \"hunter2hunter2\""
EMAIL = "jane.doe@example.com"


def build_node(node_id: str, **overrides: object) -> Node:
    payload: dict[str, object] = {
        "node_id": node_id,
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": "sv-1",
        "payload": {"fact": node_id},
        "summary": f"node {node_id}",
        "provenance": ("soul/01-identity-compliance.md",),
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def upsert(store: EventStore, node: Node, key: str) -> None:
    event = Event.model_validate(
        {
            "kind": EventKind.NODE_UPSERT,
            "node_id": node.node_id,
            "domain": node.domain,
            "node": node,
            "source_version_id": node.source_version_id,
            "occurred_at": NOW,
        }
    )
    invocation = Invocation.create("knowledge.log_event", request_hash=f"rh:{key}", idempotency_key=key)
    store.commit(event, invocation=invocation)


# ---------------------------------------------------------------- sanitization


def test_sanitize_strips_bidi_controls() -> None:
    raw = "payl\u202eoad\u202c tex\u202dt"  # RLO/PDF/LRO embedded
    cleaned = sanitize_text(raw)
    assert "\u202e" not in cleaned and "\u202c" not in cleaned and "\u202d" not in cleaned
    assert cleaned == "payload text"


def test_sanitize_strips_zero_width_and_bom() -> None:
    raw = "\u200bin\u200bva\u200cri\u200dant\ufeff"
    cleaned = sanitize_text(raw)
    assert cleaned == "invariant"


def test_sanitize_strips_control_chars_but_keeps_whitespace() -> None:
    raw = "line\x00 one\x07\ntwo\x1b[31m\r\nthree\x7f\ttab"
    cleaned = sanitize_text(raw)
    assert cleaned == "line one\ntwo[31m\r\nthree\ttab"


def test_sanitize_rejects_overlong_text() -> None:
    with pytest.raises(OversizedPayloadError):
        sanitize_text("x" * 2048, max_length=1024)


def test_sanitize_is_idempotent() -> None:
    raw = "a\u200bb\x00c\u202ed"
    once = sanitize_text(raw)
    assert sanitize_text(once) == once


# ------------------------------------------------------------------- redaction


def test_redact_text_covers_secret_shapes() -> None:
    raw = f"key {SECRET_KEY}; aws {SECRET_AWS}; gh {SECRET_GH}; {SECRET_ASSIGNMENT}"
    cleaned = redact_text(raw)
    for secret in (SECRET_KEY, SECRET_AWS, SECRET_GH, "hunter2hunter2"):
        assert secret not in cleaned
    assert "[REDACTED" in cleaned


def test_redact_text_covers_email_pii() -> None:
    cleaned = redact_text(f"contact {EMAIL} for access")
    assert EMAIL not in cleaned
    assert "[REDACTED" in cleaned


def test_redact_value_redacts_credential_keys_wholesale() -> None:
    payload = {
        "config": {"api_key": "not-even-a-known-shape", "note": "keep me"},
        "items": ["plain", f"token: {SECRET_GH}"],
        "count": 7,
    }
    cleaned = redact_value(payload)
    config = cleaned["config"]
    assert isinstance(config, dict)
    assert config["api_key"] != "not-even-a-known-shape"
    assert config["note"] == "keep me"
    items = cleaned["items"]
    assert isinstance(items, list)
    assert items[0] == "plain"
    assert SECRET_GH not in str(items[1])
    assert cleaned["count"] == 7


def test_redaction_on_write_surface_stores_only_redacted_payload(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    secret_node = build_node("n-secret", summary=f"leaked {SECRET_KEY}")
    safe_node = Node.model_validate(redact_value(secret_node.model_dump(mode="python")))
    upsert(store, safe_node, "k-write")
    with store.read() as conn:
        raw = "\n".join(str(row["payload_json"]) for row in conn.execute("SELECT payload_json FROM event_log"))
    assert SECRET_KEY not in raw
    assert "[REDACTED" in raw
    store.close()


def test_redaction_on_read_surfaces(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    # A pre-redaction (or imported) store may hold raw secrets; every read
    # surface must pass through redact_value before leaving the process.
    upsert(store, build_node("n-raw", summary=f"raw {SECRET_AWS}", payload={"contact": EMAIL}), "k-read")
    node = store.current_node("n-raw")
    assert node is not None
    view = redact_value(node.model_dump(mode="json"))
    assert SECRET_AWS not in json.dumps(view)
    assert EMAIL not in json.dumps(view)
    replayed = [redact_value(event.model_dump(mode="json")) for event in store.replay_domain()]
    assert SECRET_AWS not in json.dumps(replayed)
    store.close()


# -------------------------------------------------------------- size guards


def test_ensure_payload_size_returns_size_under_limit() -> None:
    size = ensure_payload_size({"hello": "world"})
    assert size > 0


def test_ensure_payload_size_rejects_oversized() -> None:
    with pytest.raises(OversizedPayloadError):
        ensure_payload_size({"blob": "x" * (DEFAULT_MAX_PAYLOAD_BYTES + 1)})


def test_ensure_text_size_rejects_oversized() -> None:
    with pytest.raises(OversizedPayloadError):
        ensure_text_size("x" * 1025, max_bytes=1024)
    ensure_text_size("x" * 1024, max_bytes=1024)


# ----------------------------------------------------- fixed-root resolution


def test_resolve_within_root_accepts_inside_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    target = root / "sub" / "file.md"
    target.write_text("ok", encoding="utf-8")
    assert resolve_within_root(root, "sub/file.md") == target.resolve()
    assert resolve_within_root(root, root / "sub" / "file.md") == target.resolve()
    assert resolve_within_root(root, ".") == root.resolve()


def test_resolve_within_root_accepts_new_files_inside(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    resolved = resolve_within_root(root, "new/nested.md")
    assert resolved == (root / "new" / "nested.md").resolve()


def test_resolve_within_root_rejects_dotdot_traversal(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (tmp_path / "escape.md").write_text("nope", encoding="utf-8")
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, "../escape.md")
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, "sub/../../escape.md")


def test_resolve_within_root_rejects_absolute_outside(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("nope", encoding="utf-8")
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, outside)


def test_resolve_within_root_rejects_symlink_escape(tmp_path: Path) -> None:
    """Closes the deferred Task 3 symlink finding: a symlink inside the root
    that points outside must fail closed even when the target exists."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("nope", encoding="utf-8")
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, "link/secret.md")
    with pytest.raises(PathEscapeError):
        resolve_within_root(root, "link")


def test_resolve_within_root_rejects_cross_tree_roots(tmp_path: Path) -> None:
    root_a = tmp_path / "tree-a"
    root_b = tmp_path / "tree-b"
    root_a.mkdir()
    root_b.mkdir()
    victim = root_b / "graph.json"
    victim.write_text("{}", encoding="utf-8")
    with pytest.raises(PathEscapeError):
        resolve_within_root(root_a, victim)
    with pytest.raises(PathEscapeError):
        resolve_within_root(root_a, root_a / ".." / "tree-b" / "graph.json")


def test_resolve_within_root_requires_existing_directory_root(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_within_root(tmp_path / "missing-root", "file.md")


# -------------------------------------------------------------- bind policy


def test_is_loopback_host() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("::1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")
    assert not is_loopback_host("10.0.0.5")
    assert not is_loopback_host("example.internal")


def test_bind_policy() -> None:
    assert_bind_allowed("127.0.0.1:8080", api_key=None)
    assert_bind_allowed("::1", api_key=None)
    assert_bind_allowed("[::1]:8080", api_key=None)
    assert_bind_allowed("unix:/run/soul/api.sock", api_key=None)
    with pytest.raises(BindPolicyError):
        assert_bind_allowed("0.0.0.0:8080", api_key=None)
    with pytest.raises(BindPolicyError):
        assert_bind_allowed("10.0.0.5:8080", api_key=None)
    assert_bind_allowed("0.0.0.0:8080", api_key="configured-key")


def test_host_allowlist_normalization_and_fail_closed() -> None:
    assert host_allowed("example.com", ["example.com"])
    assert host_allowed("Example.COM:8443", ["example.com"])
    assert not host_allowed("evil.example.com", ["example.com"])
    assert not host_allowed(None, ["example.com"])
    assert not host_allowed("example.com", [])  # empty allowlist fails closed


def test_authorize_request_loopback_needs_no_key() -> None:
    request = RequestView(remote_addr="127.0.0.1", host="localhost", api_key=None)
    authorize_request(request, api_key=None, host_allowlist=["localhost"])
    authorize_request(request, api_key="configured", host_allowlist=["localhost"])


def test_authorize_request_beyond_loopback_requires_key() -> None:
    request = RequestView(remote_addr="10.0.0.5", host="api.internal", api_key=None)
    with pytest.raises(UnauthorizedError):
        authorize_request(request, api_key="configured", host_allowlist=["api.internal"])
    with pytest.raises(UnauthorizedError):
        authorize_request(request, api_key=None, host_allowlist=["api.internal"])
    ok = RequestView(remote_addr="10.0.0.5", host="api.internal", api_key="configured")
    authorize_request(ok, api_key="configured", host_allowlist=["api.internal"])
    wrong = RequestView(remote_addr="10.0.0.5", host="api.internal", api_key="wrong")
    with pytest.raises(UnauthorizedError):
        authorize_request(wrong, api_key="configured", host_allowlist=["api.internal"])


def test_authorize_request_host_allowlist_applies_even_on_loopback() -> None:
    request = RequestView(remote_addr="127.0.0.1", host="unknown.host", api_key=None)
    with pytest.raises(HostNotAllowedError):
        authorize_request(request, api_key=None, host_allowlist=["localhost"])


def test_middleware_factories_for_task7_wiring() -> None:
    check_host = make_host_allowlist(["api.internal"])
    assert check_host("api.internal")
    assert not check_host("nope.internal")
    authorizer = make_request_authorizer(api_key="k", host_allowlist=["api.internal"])
    authorizer(RequestView(remote_addr="127.0.0.1", host="api.internal", api_key=None))
    with pytest.raises(UnauthorizedError):
        authorizer(RequestView(remote_addr="10.1.1.1", host="api.internal", api_key="bad"))
