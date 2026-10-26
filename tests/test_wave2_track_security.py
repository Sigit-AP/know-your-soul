"""Wave-2 TRACK security items: F7 (secret quarantine + prefix leak), F8
(pydantic value echo), F9 (boundary-denial audit + ClientDisconnect noise).

Report: ``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave2-adapters-security.md``.
Every test pins one attack shape: the old behavior must now fail closed
per-node/per-request, the legitimate flow keeps working (non-vacuous), and
the absolute invariant holds — a secret-shaped string never reaches any read
surface.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    canonical_json,
)
from know_your_soul.http_api import build_stack, create_app
from know_your_soul.importer import SeedImporter
from know_your_soul.projection import GraphProjector
from know_your_soul.read_model_validator import ReadModelValidator
from know_your_soul.store import EventStore

# Canonical AWS documentation example key shape (matches AKIA[0-9A-Z]{16}).
SECRET = "AKIA" + "IOSFODNN7EXAMPLE"

# --------------------------------------------------------------------- setup


def _upsert(store: EventStore, node: Node, *, key: str) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=key,
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
        key="test:f7:upsert:KROOT",
    )
    _upsert(
        store,
        Node(
            node_id="P1",
            parent_id="KROOT",
            kind=NodeKind.PRIMARY,
            domain=PrimaryDomain.P1,
            summary=PrimaryDomain.P1.title,
            provenance=("soul://roots",),
            source_version_id="sv-test",
        ),
        key="test:f7:upsert:P1",
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
        key="test:f7:upsert:n-gates",
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


def _journal_count(store: EventStore) -> int:
    row = store.execute_raw("SELECT COUNT(*) AS n FROM invocation_journal").fetchone()
    return int(row["n"])


# ------------------------------------------------------------------ F7(a)


def test_f7_secret_bearing_node_is_quarantined_and_reads_survive(stack: Any, caplog: pytest.LogCaptureFixture) -> None:
    """One poisoned node must not take down every read (blast radius fix).

    Fail closed per node: the offending node is excluded from the published
    graph, every other node keeps serving, the quarantine is reported as a
    typed audit event, and the secret never reaches a read surface.
    """
    before = stack.projector.active_manifest()
    assert before is not None
    _upsert(
        stack.store,
        Node(
            node_id="n-evil",
            parent_id="P1",
            kind=NodeKind.CHILD,
            domain=PrimaryDomain.P1,
            summary=f"operator notes with a pasted credential {SECRET} inside",
            provenance=("soul://corpus/evil.md",),
            source_version_id="sv-test",
        ),
        key="test:f7:upsert:n-evil",
    )
    with caplog.at_level(logging.WARNING, logger="know_your_soul.projection"):
        receipt = stack.projector.rebuild()  # RED: raises ProjectionValidationError
    assert receipt.node_count == int(before["node_count"]), "the poisoned node must be excluded, not published"

    # The graph keeps serving every other node.
    app = create_app(stack)
    status, out = _post(app, "/v1/knowledge/get_node", {"node_id": "n-gates"})
    assert status == 200
    assert out["node"]["node_id"] == "n-gates"
    status, out = _post(app, "/v1/knowledge/query", {"purpose": "gates", "domain": "P1", "token_budget": 512})
    assert status == 200
    assert "n-gates" in {node["node_id"] for node in out["results"]}

    # The offending node is quarantined: absent from the read surface.
    status, out = _post(app, "/v1/knowledge/get_node", {"node_id": "n-evil"})
    assert status == 404
    assert out["error"]["code"] == "NODE_NOT_FOUND"

    # Absolute invariant: the secret appears in NO read surface or artifact.
    generation = stack.projector.active_generation_dir()
    assert generation is not None
    graph_text = (generation / "graph.json").read_text(encoding="utf-8")
    assert SECRET not in graph_text
    assert "n-evil" not in graph_text

    # Typed quarantine audit, tamper-evident, naming the node — never the secret.
    rows = stack.store.execute_raw(
        "SELECT event_type, payload_json FROM event_log WHERE kind = 'audit' "
        "AND event_type = 'security.projection_quarantine'"
    ).fetchall()
    assert len(rows) == 1, "exactly one quarantine audit event for the rebuild"
    payload = json.loads(str(rows[0]["payload_json"]))
    assert any(entry["node_id"] == "n-evil" for entry in payload["quarantined"])
    assert SECRET not in str(rows[0]["payload_json"])
    assert SECRET[:8] not in str(rows[0]["payload_json"])
    assert stack.store.verify_chain().ok

    # One typed operator warning, no secret material in the log line.
    warnings = [r for r in caplog.records if "quarantin" in r.getMessage().lower()]
    assert warnings, "the quarantine must be loud in the server log"
    assert all(SECRET not in r.getMessage() and SECRET[:8] not in r.getMessage() for r in warnings)


def test_f7_secret_update_to_existing_node_quarantines_without_shrink_outage(stack: Any) -> None:
    """A *new version* of an existing node carrying a secret is quarantined
    without tripping the shrink guard into the same full-read outage."""
    before = stack.projector.active_manifest()
    assert before is not None
    _upsert(
        stack.store,
        Node(
            node_id="n-gates",
            parent_id="P1",
            kind=NodeKind.CHILD,
            domain=PrimaryDomain.P1,
            summary=f"edited notes now carrying {SECRET}",
            provenance=("soul://corpus/lesson.md",),
            source_version_id="sv-test",
        ),
        key="test:f7:upsert:n-gates:v2",
    )
    receipt = stack.projector.rebuild()
    assert receipt.node_count == int(before["node_count"]) - 1, "only the poisoned node is accounted out"
    app = create_app(stack)
    status, _ = _post(app, "/v1/knowledge/get_node", {"node_id": "KROOT"})
    assert status == 200
    status, out = _post(app, "/v1/knowledge/get_node", {"node_id": "n-gates"})
    assert status == 404
    assert out["error"]["code"] == "NODE_NOT_FOUND"


def test_f7_clean_rebuild_emits_no_quarantine_event(stack: Any) -> None:
    """Non-vacuous negative control: no poisoned node, no quarantine noise."""
    stack.projector.rebuild()
    rows = stack.store.execute_raw(
        "SELECT COUNT(*) AS n FROM event_log WHERE event_type = 'security.projection_quarantine'"
    ).fetchone()
    assert int(rows["n"]) == 0


# ------------------------------------------------------------------ F7(b)


ALPHA = b"# Alpha Lessons\n\n## First Lesson\n\nAlways roll the database back before retrying.\n"
BETA = b"# Beta\n\n## Counterexample\n\nA rollback that was itself rolled back.\n"


def _generation(tmp_path: Path) -> tuple[EventStore, Path, dict[str, Any]]:
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    projector = GraphProjector(store, output_root=tmp_path / "out")
    receipt = projector.rebuild()
    generation = Path(receipt.generation_dir)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    return store, generation, manifest


def test_f7_validator_message_names_category_and_node_without_secret_prefix(tmp_path: Path) -> None:
    """The validator backstop still fails the whole generation closed, but the
    message carries only the secret *category* and the node location — never
    a prefix of the secret itself."""
    _, generation, manifest = _generation(tmp_path)
    graph_path = generation / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    poisoned_id = str(graph["nodes"][0]["id"])
    graph["nodes"][0]["label"] = f"leaked {SECRET}"
    graph_path.write_text(canonical_json(graph), encoding="utf-8")
    tampered = dict(manifest)
    tampered["artifacts"] = dict(manifest["artifacts"])
    tampered["artifacts"]["graph.json"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    (generation / "manifest.json").write_text(canonical_json(tampered) + "\n", encoding="utf-8")

    report = ReadModelValidator().validate(generation, tampered)
    assert not report.ok, "the whole-generation backstop must still fail closed"
    message = next(error for error in report.errors if "secret" in error.lower())
    assert SECRET not in message, "no secret material in the validator message"
    assert SECRET[:8] not in message, "F7(b): the 8-char prefix leak must be gone"
    assert "aws access key" in message, "the secret *category* stays for diagnostics"
    assert poisoned_id in message, "the node location stays for diagnostics"


# --------------------------------------------------------------------- F8


def test_f8_http_validation_error_strips_input_values(stack: Any) -> None:
    """Pydantic wire errors carry field/type summaries, never raw input values
    (mirrors the MCP adapter's ``_validation_summary``)."""
    app = create_app(stack)
    status, _, raw = asgi_call(
        app,
        "POST",
        "/v1/knowledge/query",
        body=json.dumps({"purpose": 31337.1337, "token_budget": 512}).encode("utf-8"),
    )
    assert status == 400
    body = raw.decode("utf-8")
    out = json.loads(body)
    assert out["error"]["code"] == "VALIDATION_ERROR"
    assert out["error"]["audit_event_id"]
    assert "31337.1337" not in body, "F8: the attacker's raw input value must not be echoed"
    assert "purpose" in out["error"]["message"], "the field location stays for diagnostics"
    # The journaled audit message is the same value-free summary.
    row = stack.store.execute_raw(
        "SELECT payload_json FROM event_log WHERE kind = 'audit' ORDER BY event_seq DESC LIMIT 1"
    ).fetchone()
    assert "31337.1337" not in str(row["payload_json"])


def test_f8_valid_payload_still_works(stack: Any) -> None:
    """Non-vacuous positive control for the touched dispatch path."""
    app = create_app(stack)
    status, out = _post(app, "/v1/knowledge/query", {"purpose": "gates", "domain": "P1", "token_budget": 512})
    assert status == 200
    assert "n-gates" in {node["node_id"] for node in out["results"]}


# --------------------------------------------------------------------- F9


def test_f9_boundary_denials_leave_tamper_evident_security_audit(stack: Any) -> None:
    """401/403 boundary rejections are not invocations (spec 7.7) but they are
    security-relevant: each must leave one hash-chained audit event."""
    api_key = "wave9-secret-key-DO-NOT-LEAK"
    app = create_app(stack, api_key=api_key)
    before_audits = _audit_count(stack.store)
    before_journals = _journal_count(stack.store)

    status, _, _ = asgi_call(app, "GET", "/v1/health")  # missing key -> 401
    assert status == 401
    status, _, _ = asgi_call(app, "GET", "/v1/health", headers=[(b"x-api-key", b"wrong-key")])
    assert status == 401
    status, _, _ = asgi_call(
        app, "GET", "/v1/health", headers=[(b"host", b"evil.example.com"), (b"x-api-key", api_key.encode())]
    )
    assert status == 403

    assert _audit_count(stack.store) == before_audits + 3, "one security-denial event per 401/403"
    assert _journal_count(stack.store) == before_journals, "denials are not invocations: no journal rows"
    rows = stack.store.execute_raw(
        "SELECT event_type, payload_json FROM event_log WHERE kind = 'audit' "
        "AND event_type = 'security.boundary_denial' ORDER BY event_seq"
    ).fetchall()
    assert len(rows) == 3
    payloads = [json.loads(str(row["payload_json"])) for row in rows]
    assert sorted(p["reason"] for p in payloads) == ["host_not_allowed", "unauthorized", "unauthorized"]
    joined = " ".join(str(row["payload_json"]) for row in rows)
    assert api_key not in joined, "key material never enters the audit trail"
    assert "wrong-key" not in joined, "presented key material never enters the audit trail"
    assert "evil.example.com" in joined, "the offending host is recorded for forensics"
    assert stack.store.verify_chain().ok, "denial events ride the same tamper-evident chain"

    # Non-vacuous: an authorized request is unaffected and records no denial.
    status, _, _ = asgi_call(app, "GET", "/v1/health", headers=[(b"x-api-key", api_key.encode())])
    assert status == 200
    assert _audit_count(stack.store) == before_audits + 3


def test_f9_client_disconnect_is_caught_and_logged_without_traceback(
    stack: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A client vanishing mid-request is one clean log line, never a traceback."""

    async def disconnect_receive() -> dict[str, Any]:
        return {"type": "http.disconnect"}

    app = create_app(stack)
    with caplog.at_level(logging.INFO, logger="know_your_soul.http_api"):
        status, _, raw = asgi_call(
            app,
            "POST",
            "/v1/knowledge/query",
            headers=[(b"content-type", b"application/json")],
            receive_override=disconnect_receive,
        )
    assert status == 400
    assert json.loads(raw.decode("utf-8"))["error"]["code"] == "CLIENT_DISCONNECTED"
    assert not any(record.exc_info for record in caplog.records), "no traceback may be logged"
    lines = [r for r in caplog.records if "disconnect" in r.getMessage().lower()]
    assert len(lines) == 1, "exactly one operator log line per dropped connection"


def test_f9_completed_request_after_disconnect_probe_still_works(stack: Any) -> None:
    """Non-vacuous positive control: normal bodies are unaffected."""
    app = create_app(stack)
    started = time.monotonic()
    status, out = _post(app, "/v1/knowledge/query", {"purpose": "gates", "domain": "P1", "token_budget": 512})
    assert status == 200
    assert out["audit_event_id"]
    assert time.monotonic() - started < 10
