"""Contract-v3 ingestion adapter tests (F2, audit-final-v020).

The frozen adapter mapping of ``docs/contracts/ingestion-contract-v3.md``
(``know-your-soul ingest-file``/``ingest-directory``,
``POST /v1/knowledge/ingest/file|directory``,
``knowledge_ingest_file``/``knowledge_ingest_directory``) is exercised
end-to-end against the real extractor and a real store:

* closed schemas / closed payloads with the contract ranges
  (``max_files`` ``[1,65536]``, ``token_budget`` ``[128, max]``);
* contract path discipline (``PATH_OUTSIDE_ROOT`` / ``PATH_NOT_FOUND``);
* the frozen budget semantics (``BUDGET_TOO_SMALL`` /
  ``BUDGET_EXCEEDS_MAX``; atomic ``METADATA_EXCEEDS_BUDGET`` for the file
  verb; tail-first ``truncated: true`` for the directory verb);
* exactly one final audit event per invocation, successes and typed
  failures alike, named ``knowledge.ingest_file`` /
  ``knowledge.ingest_directory``;
* HTTP rides the same boundary middleware as every other route (API-key
  auth + JSON content-type guard); the v1/v2 knowledge surface is
  untouched.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from know_your_soul.cli import build_parser, main
from know_your_soul.contracts import estimated_tokens_of_payload
from know_your_soul.http_api import build_stack, create_app
from know_your_soul.mcp_server import build_server
from know_your_soul.projection import GraphProjector
from know_your_soul.service import KnowledgeService
from know_your_soul.store import EventStore
from test_http_api import _post
from test_mcp_server import _call_tool, _error_payload, _result_text, _session

CORPUS = {
    "alpha.py": "def alpha():\n    return 1\n\n\ndef beta():\n    return alpha()\n",
    "beta.py": "class Gamma:\n    pass\n",
    "notes.md": "# Notes\n\n## Part\n\nSome text.\n",
}


def _write_corpus(root: Path, files: dict[str, str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, content in (files or CORPUS).items():
        (root / name).write_text(content, encoding="utf-8")


def _audit_rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return list(
            conn.execute(
                "SELECT event_type, payload_json FROM event_log WHERE kind = 'audit' ORDER BY event_seq"
            ).fetchall()
        )
    finally:
        conn.close()


def _mcp_server(tmp_path: Path, corpus: Path, **kwargs: Any) -> tuple[EventStore, Any]:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    service = KnowledgeService(store, projector)
    server = build_server(service, graph_root=corpus, **kwargs)
    return store, server


def _json(result_text: str) -> dict[str, Any]:
    payload = json.loads(result_text)
    assert isinstance(payload, dict)
    return payload


# A larger deterministic corpus for the truncation tests: eight real files so
# the full report cannot fit a small token budget while the envelope can.
WIDE_CORPUS = {f"mod_{index}.py": f"def handler_{index}():\n    return {index}\n" for index in range(8)}


# ------------------------------------------------------------------- MCP


class TestMcpIngestionTools:
    def test_tools_are_listed_with_closed_contract_schemas(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> None:
            async with _session(server) as session:
                listed = await session.list_tools()
                by_name = {tool.name: tool for tool in listed.tools}
                for name in ("knowledge_ingest_file", "knowledge_ingest_directory"):
                    assert name in by_name, f"{name} missing from the MCP surface"
                file_schema = by_name["knowledge_ingest_file"].input_schema
                assert file_schema["type"] == "object"
                assert file_schema["additionalProperties"] is False
                assert file_schema["required"] == ["path"]
                assert set(file_schema["properties"]) == {"path", "token_budget"}
                assert file_schema["properties"]["token_budget"]["minimum"] == 128
                directory_schema = by_name["knowledge_ingest_directory"].input_schema
                assert directory_schema["additionalProperties"] is False
                assert set(directory_schema["properties"]) == {"path", "recursive", "max_files", "token_budget"}
                max_files = directory_schema["properties"]["max_files"]
                assert max_files["minimum"] == 1 and max_files["maximum"] == 65536
                assert directory_schema["properties"]["recursive"]["type"] == "boolean"

        import anyio

        anyio.run(scenario)

    def test_ingest_file_end_to_end(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        store, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_file", {"path": "alpha.py"})
                assert not result.is_error, result.content
                return _json(_result_text(result))

        import anyio

        payload = anyio.run(scenario)
        assert payload["status"] == "proposal"
        assert payload["input"]["path"] == "alpha.py"
        expected_sha = hashlib.sha256((corpus / "alpha.py").read_bytes()).hexdigest()
        # The file's own sha256 rides the per-file entry; the envelope digest
        # is the pipeline's deterministic walk digest over the input.
        assert payload["files"][0]["sha256"] == expected_sha
        assert len(str(payload["input"]["sha256"])) == 64
        assert isinstance(payload["extraction_version"], int)
        assert payload["stats"]["proposed"] == 1
        assert payload["audit_event_id"]
        # The real pipeline ran: the v3 tables carry the file and its symbols.
        assert store.get_file("alpha.py") is not None
        assert any(symbol.name == "alpha" for symbol in store.symbols_in_path("alpha.py"))
        # Exactly one final audit event, named by the contract.
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_ingest_directory_end_to_end(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        store, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(
                    session, "knowledge_ingest_directory", {"path": ".", "recursive": True, "max_files": 16}
                )
                assert not result.is_error, result.content
                return _json(_result_text(result))

        import anyio

        payload = anyio.run(scenario)
        assert payload["status"] == "proposal"
        assert payload["input"]["path"] == "."
        assert payload["stats"]["proposed"] == len(CORPUS)
        assert payload["guards_hit"] == []
        assert [entry["relpath"] for entry in payload["files"]] == sorted(CORPUS)
        assert store.get_file("alpha.py") is not None and store.get_file("beta.py") is not None
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_directory"]

    def test_directory_max_files_guard_is_reported(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_directory", {"path": ".", "max_files": 1})
                assert not result.is_error, result.content
                return _json(_result_text(result))

        import anyio

        payload = anyio.run(scenario)
        assert payload["guards_hit"] == ["max_files"]
        assert payload["stats"]["scanned"] == 1

    @pytest.mark.parametrize("path", ["../outside.py", "/etc/hostname", "sub/../../escape.py"])
    def test_path_escape_is_typed_and_audited_exactly_once(self, tmp_path: Path, path: str) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_file", {"path": path})
                return _error_payload(result)

        import anyio

        error = anyio.run(scenario)
        assert error["code"] == "PATH_OUTSIDE_ROOT"
        assert error["audit_event_id"]
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_missing_path_is_typed_and_audited_exactly_once(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_file", {"path": "ghost.py"})
                return _error_payload(result)

        import anyio

        error = anyio.run(scenario)
        assert error["code"] == "PATH_NOT_FOUND"
        assert error["audit_event_id"]
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_ingest_file_on_a_directory_is_typed(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_file", {"path": "."})
                return _error_payload(result)

        import anyio

        error = anyio.run(scenario)
        assert error["code"] == "PATH_NOT_FOUND"

    @pytest.mark.parametrize("budget", [0, 64, 127])
    def test_token_budget_below_the_floor_is_rejected(self, tmp_path: Path, budget: int) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(
                    session, "knowledge_ingest_file", {"path": "alpha.py", "token_budget": budget}
                )
                return _error_payload(result)

        import anyio

        error = anyio.run(scenario)
        assert error["code"] == "INVALID_ARGUMENTS"
        assert "token_budget" in error["message"]
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_token_budget_above_the_configured_max_is_rejected(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus, max_token_budget=512)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_file", {"path": "alpha.py", "token_budget": 513})
                return _error_payload(result)

        import anyio

        error = anyio.run(scenario)
        assert error["code"] == "INVALID_ARGUMENTS"

    def test_unknown_property_is_rejected(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_file", {"path": "alpha.py", "graph_root": "/etc"})
                return _error_payload(result)

        import anyio

        error = anyio.run(scenario)
        assert error["code"] == "INVALID_ARGUMENTS"

    def test_file_payload_is_atomic_over_budget(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_file", {"path": "alpha.py", "token_budget": 128})
                return _error_payload(result)

        import anyio

        error = anyio.run(scenario)
        assert error["code"] == "METADATA_EXCEEDS_BUDGET"
        # The pipeline's own event is the invocation's single audit event.
        assert error["audit_event_id"]
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_directory_truncates_tail_first_under_budget(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus, WIDE_CORPUS)
        _, server = _mcp_server(tmp_path, corpus)

        async def scenario() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_ingest_directory", {"path": ".", "token_budget": 256})
                assert not result.is_error, result.content
                return _json(_result_text(result))

        import anyio

        payload = anyio.run(scenario)
        assert payload["truncated"] is True
        assert 0 < len(payload["files"]) < len(WIDE_CORPUS)
        # Tail-first: the bytewise-first entries survive.
        assert [entry["relpath"] for entry in payload["files"]] == sorted(WIDE_CORPUS)[: len(payload["files"])]
        # The envelope never truncates away.
        assert payload["input"]["path"] == "." and payload["stats"]["proposed"] == len(WIDE_CORPUS)
        assert estimated_tokens_of_payload(payload) <= 256


# ------------------------------------------------------------------- HTTP


class TestHttpIngestionRoutes:
    def _stack(self, tmp_path: Path) -> Any:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        return build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")

    def test_ingest_file_end_to_end(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/ingest/file", {"path": "corpus/alpha.py"})
            assert status == 200, out
            assert out["status"] == "proposal"
            assert out["input"]["path"] == "corpus/alpha.py"
            assert out["stats"]["proposed"] == 1
            # The pipeline keys the v3 tables by its derived root (the file's
            # parent): "corpus/alpha.py" lands as "alpha.py".
            assert stack.store.get_file("alpha.py") is not None
        finally:
            stack.close()
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_ingest_directory_end_to_end(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(
                app, "/v1/knowledge/ingest/directory", {"path": "corpus", "recursive": True, "max_files": 16}
            )
            assert status == 200, out
            assert out["status"] == "proposal"
            assert out["input"]["path"] == "corpus"
            assert out["stats"]["proposed"] == len(CORPUS)
        finally:
            stack.close()
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_directory"]

    def test_boundary_auth_gates_the_routes(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack, api_key="secret-key")
            status, out = _post(app, "/v1/knowledge/ingest/file", {"path": "corpus/alpha.py"})
            assert status == 401, out
            status, out = _post(
                app,
                "/v1/knowledge/ingest/file",
                {"path": "corpus/alpha.py"},
                headers=[(b"x-api-key", b"wrong")],
            )
            assert status == 401, out
            status, out = _post(
                app,
                "/v1/knowledge/ingest/file",
                {"path": "corpus/alpha.py"},
                headers=[(b"x-api-key", b"secret-key")],
            )
            assert status == 200, out
        finally:
            stack.close()

    def test_json_content_type_is_required(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(
                app,
                "/v1/knowledge/ingest/file",
                {"path": "corpus/alpha.py"},
                headers=[(b"content-type", b"text/plain")],
            )
            assert status == 415, out
        finally:
            stack.close()

    def test_path_escape_is_typed_and_audited_once(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/ingest/file", {"path": "../outside.py"})
            assert status == 400, out
            assert out["error"]["code"] == "PATH_OUTSIDE_ROOT"
            assert out["error"]["audit_event_id"]
        finally:
            stack.close()
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_missing_path_is_typed(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/ingest/file", {"path": "corpus/ghost.py"})
            assert status == 404, out
            assert out["error"]["code"] == "PATH_NOT_FOUND"
        finally:
            stack.close()

    def test_unknown_property_is_rejected(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/ingest/file", {"path": "corpus/alpha.py", "graph_root": "/etc"})
            assert status == 400, out
            assert out["error"]["code"] == "VALIDATION_ERROR"
        finally:
            stack.close()

    def test_budget_floor_and_ceiling_are_typed(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/ingest/file", {"path": "corpus/alpha.py", "token_budget": 64})
            assert status == 400 and out["error"]["code"] == "BUDGET_TOO_SMALL", out
            status, out = _post(app, "/v1/knowledge/ingest/file", {"path": "corpus/alpha.py", "token_budget": 10**9})
            assert status == 400 and out["error"]["code"] == "BUDGET_EXCEEDS_MAX", out
        finally:
            stack.close()
        # Two adapter rejections: exactly two audited invocations.
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file", "knowledge.ingest_file"]

    def test_max_files_range_is_validated(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/ingest/directory", {"path": "corpus", "max_files": 0})
            assert status == 400 and out["error"]["code"] == "VALIDATION_ERROR", out
            status, out = _post(app, "/v1/knowledge/ingest/directory", {"path": "corpus", "max_files": 65537})
            assert status == 400 and out["error"]["code"] == "VALIDATION_ERROR", out
        finally:
            stack.close()

    def test_file_payload_is_atomic_over_budget(self, tmp_path: Path) -> None:
        stack = self._stack(tmp_path)
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/ingest/file", {"path": "corpus/alpha.py", "token_budget": 128})
            assert status == 400, out
            assert out["error"]["code"] == "METADATA_EXCEEDS_BUDGET"
        finally:
            stack.close()
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_directory_truncates_tail_first_under_budget(self, tmp_path: Path) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus, WIDE_CORPUS)
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            app = create_app(stack)
            status, out = _post(app, "/v1/knowledge/ingest/directory", {"path": "corpus", "token_budget": 384})
            assert status == 200, out
            assert out["truncated"] is True
            assert 0 < len(out["files"]) < len(WIDE_CORPUS)
            assert [entry["relpath"] for entry in out["files"]] == sorted(WIDE_CORPUS)[: len(out["files"])]
            assert estimated_tokens_of_payload(out) <= 384
        finally:
            stack.close()


# -------------------------------------------------------------------- CLI


class TestCliIngestionAliases:
    def test_aliases_are_registered(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["ingest-file", "some/path.py"])
        assert args.command == "ingest-file"
        assert args.path == "some/path.py"
        assert args.token_budget == 1024
        args = parser.parse_args(["ingest-directory", "some/dir", "--no-recursive", "--max-files", "8"])
        assert args.command == "ingest-directory"
        assert args.recursive is False and args.max_files == 8
        # The existing ops command is untouched.
        assert build_parser().parse_args(["ingest", "p"]).command == "ingest"

    def test_ingest_file_end_to_end(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        monkeypatch.chdir(tmp_path)
        code = main(["--store", str(tmp_path / "soul.sqlite3"), "ingest-file", "corpus/alpha.py"])
        assert code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["status"] == "proposal"
        assert payload["input"]["path"] == "corpus/alpha.py"
        assert payload["stats"]["proposed"] == 1
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_ingest_directory_end_to_end(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        monkeypatch.chdir(tmp_path)
        code = main(["--store", str(tmp_path / "soul.sqlite3"), "ingest-directory", "corpus", "--max-files", "16"])
        assert code == 0
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert payload["status"] == "proposal"
        assert payload["stats"]["proposed"] == len(CORPUS)
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_directory"]

    def test_missing_path_is_a_typed_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _write_corpus(tmp_path / "corpus")
        monkeypatch.chdir(tmp_path)
        code = main(["--store", str(tmp_path / "soul.sqlite3"), "ingest-file", "corpus/ghost.py"])
        assert code == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err)["error"]["code"] == "PATH_NOT_FOUND"
        rows = _audit_rows(tmp_path / "soul.sqlite3")
        assert [str(row["event_type"]) for row in rows] == ["knowledge.ingest_file"]

    def test_escape_above_the_root_is_typed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
        corpus = tmp_path / "corpus"
        _write_corpus(corpus)
        monkeypatch.chdir(corpus)
        code = main(["--store", str(tmp_path / "soul.sqlite3"), "ingest-file", "../outside.py"])
        assert code == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err)["error"]["code"] == "PATH_OUTSIDE_ROOT"

    def test_budget_below_the_floor_is_typed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        _write_corpus(tmp_path / "corpus")
        monkeypatch.chdir(tmp_path)
        code = main(
            ["--store", str(tmp_path / "soul.sqlite3"), "ingest-file", "corpus/alpha.py", "--token-budget", "64"]
        )
        assert code == 1
        captured = capsys.readouterr()
        assert json.loads(captured.err)["error"]["code"] == "BUDGET_TOO_SMALL"
