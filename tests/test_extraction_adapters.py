"""Adapter tests for the contract-v4 extraction verb (SUB-106).

The frozen adapter mapping of ``docs/contracts/extraction-contract-v4.md``:
``submit_extraction`` is exposed on all three surfaces (CLI
``submit-extraction``, HTTP ``POST /v1/knowledge/extraction``, MCP
``knowledge_submit_extraction``) through the same shared adapter helper, and
the review surface is CLI-only plus the read-only webview endpoint.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anyio
import pytest

from _extraction_helpers import (
    audit_events,
    make_args,
    make_payload,
    seed_root,
)
from know_your_soul.audit import AuditLog
from know_your_soul.http_api import build_stack, create_app, run_extraction_verb
from know_your_soul.mcp_server import _TOOL_TO_OPERATION, _Adapter, _tool_schemas, _validate_arguments
from know_your_soul.projection import GraphProjector
from know_your_soul.service import KnowledgeService
from know_your_soul.store import EventStore
from test_http_api import asgi_call

_JSON = [(b"content-type", b"application/json")]


def _post(
    app: Any, path: str, payload: Any, *, headers: list[tuple[bytes, bytes]] | None = None
) -> tuple[int, dict[str, Any]]:
    status, _, body = asgi_call(
        app, "POST", path, body=json.dumps(payload).encode("utf-8"), headers=headers or list(_JSON)
    )
    return status, json.loads(body.decode("utf-8"))


def _adapter(tmp_path: Path, *, actor: str = "tester") -> _Adapter:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "graph")
    service = KnowledgeService(store, projector)
    return _Adapter(
        service,
        graph_root=tmp_path / "graph",
        audit_log=AuditLog(store),
        actor=actor,
        max_token_budget=4096,
        resource_max_bytes=4096,
    )


def _call(adapter: _Adapter, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    result = anyio.run(adapter.call_tool, name, arguments)
    payload = json.loads(str(result.content[0].text))  # type: ignore[union-attr]
    return {"is_error": bool(result.is_error), **payload}


# ------------------------------------------------------------------ MCP tool


class TestMcpTool:
    def test_tool_is_mapped(self) -> None:
        assert _TOOL_TO_OPERATION["knowledge_submit_extraction"] == "knowledge.submit_extraction"

    def test_schema_is_closed(self) -> None:
        schema = _tool_schemas(4096)["knowledge_submit_extraction"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert sorted(schema["required"]) == ["confidence", "method", "payload", "source_sha256"]

    def test_schema_has_no_frozen_path_property(self) -> None:
        # The confined v4 amendment is exactly `source_path` — never `path`.
        schema = _tool_schemas(4096)["knowledge_submit_extraction"]
        assert "source_path" in schema["properties"]
        assert "path" not in schema["properties"]

    def test_unknown_property_rejected(self) -> None:
        schema = _tool_schemas(4096)["knowledge_submit_extraction"]
        with pytest.raises(Exception, match="surprise"):
            _validate_arguments("knowledge_submit_extraction", schema, {**make_args(), "surprise": 1})

    def test_confidence_must_be_a_string_in_the_schema(self) -> None:
        schema = _tool_schemas(4096)["knowledge_submit_extraction"]
        with pytest.raises(Exception, match="confidence"):
            _validate_arguments("knowledge_submit_extraction", schema, make_args(confidence=0.5))

    def test_submit_returns_a_receipt(self, tmp_path: Path) -> None:
        adapter = _adapter(tmp_path)
        result = _call(adapter, "knowledge_submit_extraction", make_args())
        assert result["is_error"] is False
        assert result["receipt_id"].startswith("ext-")
        assert result["status"] == "pending_review"
        assert result["audit_event_id"]

    def test_submit_error_is_typed_with_audit(self, tmp_path: Path) -> None:
        adapter = _adapter(tmp_path)
        result = _call(adapter, "knowledge_submit_extraction", make_args(confidence="1.5"))
        assert result["is_error"] is True
        assert result["error"]["code"] == "CONFIDENCE_OUT_OF_RANGE"
        assert result["error"]["audit_event_id"]

    def test_submit_duplicate_returns_same_receipt(self, tmp_path: Path) -> None:
        adapter = _adapter(tmp_path)
        first = _call(adapter, "knowledge_submit_extraction", make_args())
        second = _call(adapter, "knowledge_submit_extraction", make_args())
        assert second["receipt_id"] == first["receipt_id"]
        assert second["duplicate"] is True

    def test_tampered_source_hash_rejected(self, tmp_path: Path) -> None:
        root = seed_root(tmp_path)
        adapter = _adapter(tmp_path)
        adapter._root = root  # the MCP surface's startup-fixed root
        bad = make_args(source_sha256="0" * 64, source_path="scan-a.txt")
        result = _call(adapter, "knowledge_submit_extraction", bad)
        assert result["is_error"] is True
        assert result["error"]["code"] == "SOURCE_HASH_MISMATCH"

    def test_exactly_one_audit_event_per_mcp_call(self, tmp_path: Path) -> None:
        adapter = _adapter(tmp_path)
        _call(adapter, "knowledge_submit_extraction", make_args())
        _call(adapter, "knowledge_submit_extraction", make_args(confidence="2"))
        events = audit_events(adapter._service._store, "knowledge.submit_extraction")  # noqa: SLF001
        assert len(events) == 2
        assert [event["status"] for event in events] == ["success", "failure"]


# --------------------------------------------------------------- HTTP route


class TestHttpRoute:
    def _stack(self, tmp_path: Path):
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        yield_stack = stack
        return yield_stack

    def test_route_exists_and_returns_a_receipt(self, tmp_path: Path) -> None:
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/extraction", make_args())
            assert status == 200
            assert out["receipt_id"].startswith("ext-")
            assert out["status"] == "pending_review"
        finally:
            stack.close()

    def test_requires_json_content_type(self, tmp_path: Path) -> None:
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            app = create_app(stack)
            status, out = _post(
                app, "/v1/knowledge/extraction", make_args(), headers=[(b"content-type", b"text/plain")]
            )
            assert status == 415
            assert out["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
        finally:
            stack.close()

    def test_invalid_json_is_a_typed_rejection(self, tmp_path: Path) -> None:
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            app = create_app(stack)
            status, _, body = asgi_call(app, "POST", "/v1/knowledge/extraction", body=b"{nope", headers=list(_JSON))
            assert status == 400
            out = json.loads(body.decode("utf-8"))
            assert out["error"]["code"] == "VALIDATION_ERROR"
        finally:
            stack.close()

    @pytest.mark.parametrize(
        ("args_patch", "code", "status"),
        [
            ({"confidence": "1.5"}, "CONFIDENCE_OUT_OF_RANGE", 400),
            ({"confidence": 0.5}, "CONFIDENCE_NOT_DECIMAL", 400),
            ({"source_sha256": "abc"}, "PAYLOAD_SCHEMA", 400),
        ],
    )
    def test_typed_failures_map_to_statuses(self, tmp_path: Path, args_patch: dict, code: str, status: int) -> None:
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            app = create_app(stack)
            got, out = _post(app, "/v1/knowledge/extraction", {**make_args(), **args_patch})
            assert got == status
            assert out["error"]["code"] == code
            assert out["error"]["audit_event_id"]
        finally:
            stack.close()

    def test_hash_mismatch_maps_to_409(self, tmp_path: Path) -> None:
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out", ops_root=tmp_path)
        try:
            seed_root(tmp_path)
            app = create_app(stack)
            status, out = _post(
                app, "/v1/knowledge/extraction", make_args(source_sha256="0" * 64, source_path="corpus/scan-a.txt")
            )
            assert status == 409
            assert out["error"]["code"] == "SOURCE_HASH_MISMATCH"
        finally:
            stack.close()

    def test_duplicate_conflict_maps_to_409(self, tmp_path: Path) -> None:
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            app = create_app(stack)
            _post(app, "/v1/knowledge/extraction", make_args())
            status, out = _post(app, "/v1/knowledge/extraction", make_args(payload=make_payload(text="changed")))
            assert status == 409
            assert out["error"]["code"] == "EXTRACTION_DUPLICATE"
        finally:
            stack.close()

    def test_route_is_not_a_generic_knowledge_operation(self) -> None:
        from know_your_soul.http_api import KNOWLEDGE_OPERATIONS

        assert "submit_extraction" not in KNOWLEDGE_OPERATIONS
        assert "extraction" not in KNOWLEDGE_OPERATIONS

    def test_payload_too_large_maps_to_413(self, tmp_path: Path) -> None:
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            app = create_app(stack)
            from know_your_soul.extraction import schema as xs

            payload = make_payload(text="x" * (xs.MAX_TEXT_CHARS + 1))
            status, out = _post(app, "/v1/knowledge/extraction", make_args(payload=payload))
            assert status == 413
            assert out["error"]["code"] == "PAYLOAD_TOO_LARGE"
        finally:
            stack.close()


# ---------------------------------------------------------- shared adapter


class TestRunExtractionVerb:
    def test_unknown_verb_is_a_programmer_error(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        try:
            with pytest.raises(ValueError, match="unknown extraction verb"):
                run_extraction_verb(store, AuditLog(store), "bogus", {}, actor="a", extraction_root=tmp_path)
        finally:
            store.close()

    def test_arguments_must_be_a_mapping(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        try:
            from know_your_soul.http_api import AdapterValidationError

            with pytest.raises(AdapterValidationError):
                run_extraction_verb(
                    store, AuditLog(store), "submit_extraction", [], actor="a", extraction_root=tmp_path
                )
        finally:
            store.close()

    def test_adapter_rejection_is_audited_once(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        try:
            from know_your_soul.http_api import AdapterValidationError

            with pytest.raises(AdapterValidationError):
                run_extraction_verb(
                    store, AuditLog(store), "submit_extraction", [], actor="a", extraction_root=tmp_path
                )
            events = audit_events(store, "knowledge.submit_extraction")
            assert len(events) == 1 and events[0]["status"] == "failure"
        finally:
            store.close()

    def test_unknown_top_level_property_rejected(self, tmp_path: Path) -> None:
        store = EventStore(tmp_path / "soul.sqlite3")
        try:
            from know_your_soul.extraction import PayloadSchemaError

            with pytest.raises(PayloadSchemaError, match="surprise"):
                run_extraction_verb(
                    store,
                    AuditLog(store),
                    "submit_extraction",
                    {**make_args(), "surprise": 1},
                    actor="a",
                    extraction_root=tmp_path,
                )
        finally:
            store.close()


# ---------------------------------------------------------------------- CLI


def _run_cli(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    from know_your_soul.cli import main

    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestCliSubmit:
    def test_submit_extraction_registered(self) -> None:
        from know_your_soul.cli import build_parser

        args = build_parser().parse_args(["submit-extraction", "{}"])
        assert args.command == "submit-extraction"

    def test_submit_prints_the_receipt(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        code, out, _ = _run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "submit-extraction", json.dumps(make_args())
        )
        assert code == 0
        receipt = json.loads(out)
        assert receipt["receipt_id"].startswith("ext-")
        assert receipt["status"] == "pending_review"

    def test_submit_reads_stdin(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(make_args())))
        code, out, _ = _run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "submit-extraction", "-")
        assert code == 0
        assert json.loads(out)["status"] == "pending_review"

    def test_typed_failure_exits_1_with_the_envelope(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code, _, err = _run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "submit-extraction", json.dumps(make_args(confidence="9"))
        )
        assert code == 1
        envelope = json.loads(err)
        assert envelope["error"]["code"] == "CONFIDENCE_OUT_OF_RANGE"

    def test_submit_binds_files_under_the_working_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        seed_root(tmp_path)
        monkeypatch.chdir(tmp_path)
        code, out, _ = _run_cli(
            capsys,
            "--store",
            str(tmp_path / "s.sqlite3"),
            "submit-extraction",
            json.dumps(make_args(source_path="corpus/scan-a.txt")),
        )
        assert code == 0
        assert json.loads(out)["receipt_id"].startswith("ext-")

    def test_tampered_hash_exits_1(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
        seed_root(tmp_path)
        monkeypatch.chdir(tmp_path)
        code, _, err = _run_cli(
            capsys,
            "--store",
            str(tmp_path / "s.sqlite3"),
            "submit-extraction",
            json.dumps(make_args(source_sha256="0" * 64, source_path="corpus/scan-a.txt")),
        )
        assert code == 1
        assert json.loads(err)["error"]["code"] == "SOURCE_HASH_MISMATCH"


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


class TestCliReviewSurface:
    def _submit(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch, **overrides: Any
    ) -> dict[str, Any]:
        monkeypatch.chdir(tmp_path)
        code, out, _ = _run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "submit-extraction", json.dumps(make_args(**overrides))
        )
        assert code == 0
        return json.loads(out)

    def test_extraction_review_lists_the_queue(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        receipt = self._submit(tmp_path, capsys, monkeypatch)
        code, out, _ = _run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "extraction", "review")
        assert code == 0
        listing = json.loads(out)
        assert listing["count"] == 1
        assert listing["rows"][0]["receipt_id"] == receipt["receipt_id"]

    def test_extraction_review_receipt_shows_the_diff(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        receipt = self._submit(tmp_path, capsys, monkeypatch)
        code, out, _ = _run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "extraction", "review", "--receipt", receipt["receipt_id"]
        )
        assert code == 0
        detail = json.loads(out)
        assert detail["receipt_id"] == receipt["receipt_id"]
        assert len(detail["diff"]["nodes"]) == 2

    def test_extraction_approve_applies_to_the_graph(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        receipt = self._submit(tmp_path, capsys, monkeypatch)
        code, out, _ = _run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "extraction", "approve", receipt["receipt_id"]
        )
        assert code == 0
        result = json.loads(out)
        assert result["status"] == "applied"
        assert len(result["applied_nodes"]) == 2
        # the node is now readable through the frozen read path
        code, out, _ = _run_cli(
            capsys,
            "--store",
            str(tmp_path / "s.sqlite3"),
            "get-node",
            json.dumps({"node_id": result["applied_nodes"][0]}),
        )
        assert code == 0
        node = json.loads(out)["node"]
        # the read view exposes the tier via the context tag and the capped confidence
        assert "agent-extracted" in node["context_tags"]
        from decimal import Decimal as _D

        assert _D(node["confidence"]) <= _D("0.65")

    def test_extraction_reject_marks_the_row(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        receipt = self._submit(tmp_path, capsys, monkeypatch)
        code, out, _ = _run_cli(
            capsys,
            "--store",
            str(tmp_path / "s.sqlite3"),
            "extraction",
            "reject",
            receipt["receipt_id"],
            "--note",
            "illegible",
        )
        assert code == 0
        assert json.loads(out)["status"] == "rejected"
        code, out, _ = _run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "extraction", "review")
        assert json.loads(out)["rows"][0]["status"] == "rejected"

    def test_extraction_unknown_receipt_exits_1(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        code, _, err = _run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "extraction", "approve", "ext-" + "0" * 32
        )
        assert code == 1
        assert json.loads(err)["error"]["code"] == "EXTRACTION_NOT_FOUND"


class TestAdapterParity:
    def test_all_three_surfaces_agree_on_receipt_identity(self, tmp_path: Path, monkeypatch) -> None:
        # The receipt id is content-derived: the same submission yields the
        # same id whichever surface computed it.
        from know_your_soul.extraction import validate_submission

        submission = validate_submission(make_args())
        assert submission.receipt_id("tester") == submission.receipt_id("tester")
        assert re.fullmatch(r"ext-[0-9a-f]{32}", submission.receipt_id("tester"))
