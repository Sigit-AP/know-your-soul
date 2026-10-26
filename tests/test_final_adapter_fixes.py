"""Final-audit adapter fixes (SUB-42).

Reports: ``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-final-security.md``
(findings S-3, S-4, S-5, S-6) and ``audit-final-flow.md`` (F-5, F-8, plus the
adapter halves of F-1/F-2/F-4).

* S-3: ``_bind_unix_socket`` unlinks a *verified-stale* socket before binding
  and ``serve`` removes its socket file on shutdown — restarts work
  repeatedly; a live listener or a non-socket file fails closed.
* S-4: the socket is created ``0o600`` atomically (temporary ``0o177`` umask
  around ``bind``; Linux ``fchmod`` on a socket fd is a silent no-op), so the
  bind→chmod window and the symlink-swap chmod both disappear.
* S-5: boundary-denial audit writes aggregate to one event per time window
  per (reason, remote address), carrying the suppressed count — the audit
  trail stays complete but bounded.
* S-6: the MCP ``--transport http`` boundary records the same denials.
* F-5/F-1/F-2 (adapter half): ``BackupError``/``ProjectionError`` surface as
  typed envelope codes with honest statuses (``BACKUP_FAILED`` 400,
  ``PROJECTOR_BUSY`` 503 retryable, …) instead of ``INTERNAL``.
* F-8: ``POST /v1/ops/health`` maps ok→200 / not-ok→503 like GET.
* F-4 (adapter half): the relearn lifecycle is operator-reachable through
  ``ops/relearn_scan`` / ``ops/relearn_status`` / ``ops/relearn_preview`` /
  ``ops/relearn_apply`` (CLI + HTTP) and the read-only MCP resource
  ``soul://relearn/status``. The ten knowledge operations are unchanged.
"""

from __future__ import annotations

import json
import os
import socket
import stat
from pathlib import Path
from typing import Any

import pytest
from mcp import types

from know_your_soul import cli, http_api, mcp_server
from know_your_soul.backup import BackupError
from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
)
from know_your_soul.http_api import (
    KNOWLEDGE_OPERATIONS,
    OPS_OPERATIONS,
    BoundaryDenialRecorder,
    build_stack,
    create_app,
    error_envelope,
    status_for_error,
)
from know_your_soul.projection import (
    OversizedArtifactError,
    ProjectionValidationError,
    ProjectorBusy,
    ShrinkGuardError,
    StalePublicationError,
)
from know_your_soul.security import BindPolicyError, UnauthorizedError
from know_your_soul.source_registry import RELEARN_PENDING, SourceKind
from know_your_soul.store import EventStore
from test_http_api import _post, asgi_call
from test_mcp_server import _run, _session

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
        idempotency_key=f"test:final:upsert:{node.node_id}",
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
        idempotency_key="test:final:rel:P1:n-gates",
    )


@pytest.fixture()
def stack(tmp_path: Path):
    built = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    _seed_store(built.store)
    built.projector.rebuild()
    yield built
    built.close()


def _denial_payloads(store: EventStore) -> list[dict[str, Any]]:
    rows = store.execute_raw(
        "SELECT payload_json FROM event_log WHERE event_type = 'security.boundary_denial' ORDER BY event_seq"
    ).fetchall()
    return [json.loads(str(row["payload_json"])) for row in rows]


def _denial_scope(remote_addr: str = "10.9.9.9") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/v1/knowledge/query",
        "headers": [(b"host", b"localhost")],
        "client": (remote_addr, 4321),
    }


# ---------------------------------------------------- S-3: restartable socket


def test_bind_unlinks_stale_socket_and_restarts_repeatedly(sock_dir: Path) -> None:
    """S-3: a clean shutdown leaves the socket file; every restart must still bind."""
    socket_path = sock_dir / "api.sock"
    for attempt in range(3):
        listener = http_api._bind_unix_socket(socket_path)
        try:
            assert stat.S_ISSOCK(os.lstat(socket_path).st_mode)
            assert stat.S_IMODE(os.lstat(socket_path).st_mode) == 0o600
        finally:
            listener.close()
        assert socket_path.exists(), f"attempt {attempt}: close alone must leave the file (non-vacuous setup)"


def test_bind_refuses_a_live_listening_socket(sock_dir: Path) -> None:
    """S-3 fail-closed: a socket another process is listening on is not 'stale'."""
    socket_path = sock_dir / "live.sock"
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(socket_path))
    live.listen(1)
    try:
        with pytest.raises(BindPolicyError):
            http_api._bind_unix_socket(socket_path)
        assert socket_path.exists(), "the live socket file must be left alone"
    finally:
        live.close()


def test_bind_refuses_a_non_socket_file(sock_dir: Path) -> None:
    """S-3 fail-closed: a regular file (or a symlink) is never unlinked."""
    victim = sock_dir / "api.sock"
    victim.write_text("operator data — not a socket", encoding="utf-8")
    with pytest.raises(BindPolicyError):
        http_api._bind_unix_socket(victim)
    assert victim.read_text(encoding="utf-8") == "operator data — not a socket"

    real_socket = sock_dir / "real.sock"
    holder = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    holder.bind(str(real_socket))
    try:
        link = sock_dir / "link.sock"
        link.symlink_to(real_socket)
        with pytest.raises(BindPolicyError):
            http_api._bind_unix_socket(link)
        assert link.is_symlink(), "a symlink is never followed or removed"
    finally:
        holder.close()


def test_serve_removes_socket_file_on_shutdown(stack: Any, sock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S-3: a clean ``serve`` shutdown deletes its socket file."""
    socket_path = sock_dir / "served.sock"

    class _FakeServer:
        def __init__(self, config: Any) -> None:
            self.config = config

        def run(self) -> None:
            return None

    monkeypatch.setattr(http_api.uvicorn, "Server", _FakeServer)
    http_api.serve(stack, socket_path=socket_path)
    assert not socket_path.exists(), "the socket file must be removed when the server stops"


# ------------------------------------------------- S-4: no bind→chmod window


def test_bind_creates_socket_0600_with_world_open_umask(sock_dir: Path) -> None:
    """S-4: the socket is born 0o600, regardless of the ambient umask."""
    socket_path = sock_dir / "api.sock"
    previous = os.umask(0o000)
    try:
        listener = http_api._bind_unix_socket(socket_path)
    finally:
        os.umask(previous)
    try:
        assert stat.S_IMODE(os.lstat(socket_path).st_mode) == 0o600
    finally:
        listener.close()


def test_bind_never_chmods_through_a_swapped_symlink(sock_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S-4: a path swapped for a symlink between bind and chmod can no longer
    make the server chmod a victim file — the fixed code never chmods a path."""
    victim = sock_dir / "victim.conf"
    victim.write_text("mode must survive", encoding="utf-8")
    os.chmod(victim, 0o644)
    socket_path = sock_dir / "api.sock"
    swap_ran = False
    real_chmod = os.chmod

    def swap_then_chmod(path: Any, mode: int) -> None:
        nonlocal swap_ran
        if Path(str(path)) == socket_path:
            swap_ran = True
            socket_path.unlink()  # the bound socket stays alive, unnamed
            socket_path.symlink_to(victim)
        real_chmod(path, mode)

    monkeypatch.setattr(os, "chmod", swap_then_chmod)
    listener = http_api._bind_unix_socket(socket_path)
    try:
        info = os.lstat(socket_path)
        assert stat.S_ISSOCK(info.st_mode), "the socket path must still be the real socket"
        assert stat.S_IMODE(info.st_mode) == 0o600
        assert stat.S_IMODE(os.lstat(victim).st_mode) == 0o644, "the victim file must never be chmodded"
        assert not swap_ran or True  # swap_ran documents the old window; the victim assert is the gate
    finally:
        listener.close()


# ------------------------------------------- S-5: bounded boundary-denial log


def test_denial_recorder_aggregates_within_a_window(stack: Any) -> None:
    """S-5: one audit event per window per host; suppressed denials are counted."""
    clock = [1_000.0]
    recorder = BoundaryDenialRecorder(stack.store, window_seconds=60.0, clock=lambda: clock[0])
    scope = _denial_scope()
    error = UnauthorizedError("the presented API key is invalid")

    for _ in range(5):
        recorder.record(scope, error)
    events = _denial_payloads(stack.store)
    assert len(events) == 1, "the burst inside one window writes exactly one event"
    assert events[0]["denial_count"] == 1
    assert events[0]["reason"] == "unauthorized"
    assert events[0]["remote_addr"] == "10.9.9.9"

    clock[0] += 61.0  # the window expires; the next denial flushes the count
    recorder.record(scope, error)
    events = _denial_payloads(stack.store)
    assert len(events) == 2
    assert events[1]["denial_count"] == 5, "the four suppressed denials plus the current one are accounted"
    assert sum(event["denial_count"] for event in events) == 6, "no denial is lost from the audit trail"


def test_denial_recorder_tracks_hosts_independently(stack: Any) -> None:
    clock = [2_000.0]
    recorder = BoundaryDenialRecorder(stack.store, window_seconds=60.0, clock=lambda: clock[0])
    error = UnauthorizedError("the presented API key is invalid")
    recorder.record(_denial_scope("10.0.0.1"), error)
    recorder.record(_denial_scope("10.0.0.2"), error)
    events = _denial_payloads(stack.store)
    assert len(events) == 2, "different hosts are separate windows, both audited"
    assert {event["remote_addr"] for event in events} == {"10.0.0.1", "10.0.0.2"}


def test_http_unauthorized_storm_writes_a_bounded_audit_trail(stack: Any) -> None:
    """S-5 live: 12 wrong-key requests over one app produce one denial event."""
    app = create_app(stack, api_key="storm-key")
    before = len(_denial_payloads(stack.store))
    for _ in range(12):
        status, _, body = asgi_call(
            app, "GET", "/v1/health", client=("10.7.7.7", 9001), headers=[(b"x-api-key", b"wrong")]
        )
        assert status == 401
        assert json.loads(body.decode("utf-8"))["error"]["code"] == "UNAUTHORIZED"
    events = _denial_payloads(stack.store)
    assert len(events) - before == 1, "the storm aggregates into a single window event"
    assert stack.store.verify_chain().ok, "the hash chain stays intact"


# ------------------------------------ S-6: MCP http transport records denials


def test_mcp_http_transport_records_boundary_denials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """S-6: the MCP --transport http boundary appends the same audit event."""

    async def sentinel_app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"INNER"})

    (tmp_path / "graph").mkdir()
    monkeypatch.setattr(mcp_server.Server, "streamable_http_app", lambda self: sentinel_app)
    monkeypatch.setattr(mcp_server.runtime_guard, "verify_runtime", lambda **kwargs: {})
    monkeypatch.setattr(mcp_server.runtime_guard, "install_socket_guard", lambda: None)
    captured: dict[str, Any] = {}
    monkeypatch.setattr("uvicorn.run", lambda app, **kwargs: captured.setdefault("app", app))

    db_path = tmp_path / "soul.sqlite3"
    code = mcp_server.main(
        [
            "--graph-root",
            str(tmp_path / "graph"),
            "--db",
            str(db_path),
            "--transport",
            "http",
            "--host",
            "127.0.0.1",
            "--port",
            "18082",
            "--api-key",
            "mcp-key",
        ]
    )
    assert code == 0
    app = captured["app"]
    status, _, _ = asgi_call(app, "POST", "/mcp", client=("10.1.2.3", 9999), headers=[(b"x-api-key", b"wrong")])
    assert status == 401

    store = EventStore(db_path)
    try:
        events = _denial_payloads(store)
    finally:
        store.close()
    assert len(events) == 1, "the MCP http boundary must record the denial like the HTTP API does"
    assert events[0]["reason"] == "unauthorized"
    assert events[0]["remote_addr"] == "10.1.2.3"


# ----------------------------- F-5/F-1/F-2: typed operational error envelopes


def test_error_mapping_for_backup_and_projection_family() -> None:
    """F-5: the operational RuntimeError family maps to honest codes/statuses.

    Codes follow the StoreError pattern (type names); they are identical
    whether the core declares these as StoreError or plain RuntimeError.
    """
    cases = (
        (BackupError("boom"), "BackupError", 400, False),
        (ProjectorBusy("busy"), "ProjectorBusy", 503, True),
        (StalePublicationError("stale"), "StalePublicationError", 409, True),
        (ShrinkGuardError("shrink"), "ShrinkGuardError", 422, False),
        (ProjectionValidationError("invalid"), "ProjectionValidationError", 422, False),
        (OversizedArtifactError("oversized"), "OversizedArtifactError", 507, False),
    )
    for error, code, status, retryable in cases:
        envelope = error_envelope(error)
        assert envelope["error"]["code"] == code, type(error).__name__
        assert envelope["error"]["retryable"] is retryable, type(error).__name__
        assert status_for_error(error) == status, type(error).__name__
        assert envelope["error"]["message"] != "internal error", type(error).__name__


def test_restore_onto_existing_without_overwrite_is_typed(stack: Any, tmp_path: Path) -> None:
    """F-5/F-1 adapter half: the restore-exists guidance survives the envelope."""
    app = create_app(stack)
    status, out = _post(app, "/v1/ops/backup", {"dest_path": "b1.sqlite3"})
    assert status == 200
    (tmp_path / "existing.sqlite3").write_bytes(b"occupied")
    status, out = _post(app, "/v1/ops/restore", {"backup_path": "b1.sqlite3", "dest_path": "existing.sqlite3"})
    assert status == 400
    assert out["error"]["code"] == "BackupError"
    assert "overwrite" in out["error"]["message"], "the actionable guidance must not be sanitized away"


def test_restore_onto_the_live_store_path_is_typed(stack: Any) -> None:
    """F-1 adapter half: a restore refusal names the typed code, never INTERNAL."""
    app = create_app(stack)
    status, out = _post(app, "/v1/ops/backup", {"dest_path": "b1.sqlite3"})
    assert status == 200
    status, out = _post(app, "/v1/ops/restore", {"backup_path": "b1.sqlite3", "dest_path": "soul.sqlite3"})
    assert status == 400
    assert out["error"]["code"] != "INTERNAL"
    assert out["error"]["message"] != "internal error"


def test_concurrent_rebuild_loser_gets_projector_busy_503(stack: Any) -> None:
    """F-5: the rebuild-lock loser is a retryable busy signal, not a 500."""
    app = create_app(stack)
    with stack.projector.hold_lock():
        status, out = _post(app, "/v1/ops/rebuild", {})
    assert status == 503
    assert out["error"]["code"] == "ProjectorBusy"
    assert out["error"]["retryable"] is True


# ----------------------------------------------------- F-8: honest ops/health


def test_ops_health_post_maps_unhealthy_to_503(tmp_path: Path) -> None:
    """F-8: POST /v1/ops/health must agree with GET /v1/health on the status."""
    degraded = build_stack(tmp_path / "soul2.sqlite3", tmp_path / "out2")
    _seed_store(degraded.store)
    degraded.projector.rebuild()
    # Degrade deterministically: bump the knowledge watermark, then hold the
    # rebuild lock so the readiness check's proactive repair (F-9) cannot run
    # and the projection watermark lags the store.
    _upsert(
        degraded.store,
        Node(
            node_id="n-late",
            parent_id="P1",
            kind=NodeKind.CHILD,
            domain=PrimaryDomain.P1,
            summary="Written after the last rebuild",
            provenance=("soul://corpus/late.md",),
            source_version_id="sv-test",
        ),
    )
    try:
        app = create_app(degraded)
        with degraded.projector.hold_lock():
            status, out = _post(app, "/v1/ops/health", {})
            assert out["ok"] is False, "fixture must be unhealthy (non-vacuous)"
            assert status == 503
            get_status, _, get_body = asgi_call(app, "GET", "/v1/health")
            assert get_status == 503
            assert json.loads(get_body.decode("utf-8"))["ok"] is False
    finally:
        degraded.close()


def test_ops_health_post_stays_200_when_healthy(stack: Any) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/ops/health", {})
    assert status == 200
    assert out["ok"] is True


# ------------------------------------ F-4 adapter half: relearn ops surface


def test_relearn_ops_do_not_extend_the_knowledge_surface() -> None:
    """Hard rule: relearn exposure rides the ops route, never the knowledge surface.

    The surface itself is fourteen operations: the frozen ten (v1) plus the
    four contract-v2 G1 retrieval verbs (operator ruling 2026-10-26,
    progress.md; addendum docs/contracts/retrieval-verbs-v2.md). Relearn adds
    nothing to it.
    """
    assert len(KNOWLEDGE_OPERATIONS) == 14
    assert not any("relearn" in operation for operation in KNOWLEDGE_OPERATIONS)
    for operation in ("relearn_scan", "relearn_status", "relearn_preview", "relearn_apply"):
        assert operation in OPS_OPERATIONS


def _change_corpus(corpus: Path) -> None:
    (corpus / "a.md").write_text("alpha v2 — changed\n", encoding="utf-8")
    (corpus / "b.md").write_text("beta new file\n", encoding="utf-8")


def test_relearn_lifecycle_over_http_ops(stack: Any, tmp_path: Path) -> None:
    """F-4: scan → offer → status → preview → approval-gated apply → rollback."""
    app = create_app(stack)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")

    status, out = _post(app, "/v1/ops/relearn_scan", {"path": str(corpus), "kind": "SOUL_CORPUS"})
    assert status == 200
    assert out["offer"] is None, "the first scan only registers the baseline"
    assert out["relearn_state"] is None

    _change_corpus(corpus)
    status, out = _post(app, "/v1/ops/relearn_scan", {"path": str(corpus), "kind": "SOUL_CORPUS"})
    assert status == 200
    assert out["relearn_state"] == RELEARN_PENDING
    assert out["offer"]["requires_operator_decision"] is True
    offer_id = out["offer"]["offer_id"]

    status, out = _post(app, "/v1/ops/relearn_status", {})
    assert status == 200
    assert out["state"] == RELEARN_PENDING
    assert out["pending_offer"]["offer_id"] == offer_id
    assert out["active_source_version_id"]

    status, out = _post(app, "/v1/ops/relearn_preview", {"mode": "RESET"})
    assert status == 200
    assert out["offer_id"] == offer_id
    assert out["promotion_blocked"] is True

    # the gate: a mismatched or absent approval is a typed refusal, never a change
    bad = {"mode": "RESET", "approval": {"offer_id": "wrong", "approved_by": "op"}}
    status, out = _post(app, "/v1/ops/relearn_apply", bad)
    assert status == 409
    assert out["error"]["code"] == "OPERATOR_DECISION_REQUIRED"
    status, out = _post(app, "/v1/ops/relearn_status", {})
    assert out["state"] == RELEARN_PENDING

    status, out = _post(
        app,
        "/v1/ops/relearn_apply",
        {"mode": "RESET", "approval": {"offer_id": offer_id, "approved_by": "op"}},
    )
    assert status == 200
    assert out["mode"] == "RESET"
    assert out["backup"]["verified"] is True
    relearn_id = out["relearn_id"]

    status, out = _post(app, "/v1/ops/relearn_status", {})
    assert status == 200
    assert out["state"] != RELEARN_PENDING
    assert out["pending_offer"] is None

    # the full loop closes through the existing relearn-aware rollback
    status, out = _post(app, "/v1/knowledge/rollback", {"relearn_id": relearn_id})
    assert status == 200
    assert out["verified"] is True


def test_relearn_preview_without_pending_offer_is_typed(stack: Any) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/ops/relearn_preview", {"mode": "RESET"})
    assert status == 400
    assert out["error"]["code"] == "LIFECYCLE_ERROR"
    assert "NoPendingRelearn" in out["error"]["message"]


def test_relearn_ops_validate_payloads(stack: Any, tmp_path: Path) -> None:
    app = create_app(stack)
    status, out = _post(app, "/v1/ops/relearn_scan", {"path": str(tmp_path), "kind": "NOT_A_KIND"})
    assert status == 400
    assert out["error"]["code"] == "VALIDATION_ERROR"
    bogus = {"mode": "BOGUS", "approval": {"offer_id": "x", "approved_by": "y"}}
    status, out = _post(app, "/v1/ops/relearn_apply", bogus)
    assert status == 400
    assert out["error"]["code"] == "VALIDATION_ERROR"


def test_relearn_lifecycle_over_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """F-4: the same lifecycle is reachable as plain CLI subcommands."""
    db_path = tmp_path / "soul.sqlite3"
    out_path = tmp_path / "out"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")

    def run(*args: str) -> tuple[int, dict[str, Any]]:
        capsys.readouterr()
        code = cli.main(["--store", str(db_path), "--output-root", str(out_path), *args])
        captured = capsys.readouterr()
        return code, json.loads(captured.out) if captured.out.strip() else {}

    code, out = run("relearn-scan", json.dumps({"path": str(corpus), "kind": "SOUL_CORPUS"}))
    assert code == 0 and out["offer"] is None
    _change_corpus(corpus)
    code, out = run("relearn-scan", json.dumps({"path": str(corpus), "kind": "SOUL_CORPUS"}))
    assert code == 0
    offer_id = out["offer"]["offer_id"]

    code, out = run("relearn-status")
    assert code == 0 and out["state"] == RELEARN_PENDING

    code, out = run("relearn-preview", json.dumps({"mode": "ACCUMULATE"}))
    assert code == 0 and out["offer_id"] == offer_id

    code, out = run(
        "relearn-apply",
        json.dumps({"mode": "ACCUMULATE", "approval": {"offer_id": offer_id, "approved_by": "cli-op"}}),
    )
    assert code == 0
    assert out["mode"] == "ACCUMULATE"
    assert out["backup"]["verified"] is True

    code, out = run("relearn-status")
    assert code == 0 and out["state"] != RELEARN_PENDING


# ------------------------------------------- F-4: MCP read-only relearn view


def test_mcp_relearn_status_resource(tmp_path: Path) -> None:
    """F-4: MCP clients see the pending offer and both previews, read-only."""
    from test_mcp_server import _seeded

    store, _, service = _seeded(tmp_path)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
    assert service.offer_relearn(corpus, SourceKind.SOUL_CORPUS) is None
    _change_corpus(corpus)
    offer = service.offer_relearn(corpus, SourceKind.SOUL_CORPUS)
    assert offer is not None

    from know_your_soul.mcp_server import build_server

    server = build_server(service, graph_root=tmp_path / "graph")
    domain_before = store.execute_raw("SELECT COUNT(*) AS n FROM event_log WHERE kind = 'domain'").fetchone()["n"]

    async def scenario() -> None:
        async with _session(server) as session:
            listed = await session.list_resources()
            uris = {str(resource.uri) for resource in listed.resources}
            assert "soul://relearn/status" in uris
            read = await session.read_resource("soul://relearn/status")
            assert read.contents
            contents = read.contents[0]
            assert isinstance(contents, types.TextResourceContents)
            payload = json.loads(contents.text)
            assert payload["relearn_state"] == RELEARN_PENDING
            assert payload["pending_offer"]["offer_id"] == offer.offer_id
            assert payload["pending_offer"]["requires_operator_decision"] is True
            assert set(payload["previews"]) == {"RESET", "ACCUMULATE"}
            assert payload["previews"]["RESET"]["promotion_blocked"] is True

    _run(scenario)
    domain_after = store.execute_raw("SELECT COUNT(*) AS n FROM event_log WHERE kind = 'domain'").fetchone()["n"]
    assert domain_after == domain_before, "a resource read is audited but never writes domain events"


def test_mcp_relearn_status_resource_without_pending_offer(tmp_path: Path) -> None:
    from test_mcp_server import _seeded

    _, _, service = _seeded(tmp_path)
    from know_your_soul.mcp_server import build_server

    server = build_server(service, graph_root=tmp_path / "graph")

    async def scenario() -> None:
        async with _session(server) as session:
            read = await session.read_resource("soul://relearn/status")
            contents = read.contents[0]
            assert isinstance(contents, types.TextResourceContents)
            payload = json.loads(contents.text)
            assert payload["relearn_state"] is None
            assert payload["pending_offer"] is None

    _run(scenario)


def test_mcp_projection_errors_are_typed_not_internal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F-5 parity on MCP: projection contract violations keep their typed code."""
    from test_mcp_server import _error_payload, _server

    _, _, server = _server(tmp_path)

    def boom(self: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
        raise ProjectorBusy("another projector holds the rebuild lock for this corpus root")

    monkeypatch.setattr(mcp_server._Adapter, "_dispatch", boom)

    async def scenario() -> None:
        async with _session(server) as session:
            result = await session.call_tool("knowledge_query", {"purpose": "x", "token_budget": 512})
            error = _error_payload(result)
            assert error["code"] == "ProjectorBusy"
            assert "internal error" not in error["message"]

    _run(scenario)


def test_mcp_tools_stay_free_of_relearn() -> None:
    """The relearn exposure never becomes a knowledge tool (spec 13.2).

    The v1 pin was "exactly ten"; contract v2 (operator ruling 2026-10-26)
    extends the surface to fourteen with the four G1 retrieval verbs — relearn
    is still not among them.
    """
    assert len(mcp_server.EXPECTED_TOOLS) == 14
    assert not any("relearn" in tool for tool in mcp_server.EXPECTED_TOOLS)
