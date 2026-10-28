"""CLI tests for the ``know-your-soul ingest`` ops subcommand (SUB-89)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from _ingest_fakes import ToyExtractor, ToyResolver, write_fixture
from know_your_soul.cli import build_parser, main

SIMPLE = {
    "a.py": "def alpha():\n    pass\n\ndef beta():\n    pass\n",
    "b.py": "class Gamma:\n    pass\n",
}


@pytest.fixture(autouse=True)
def fake_sibling_modules(monkeypatch):
    """SUB-87/SUB-88 have not landed: inject the deterministic fakes via the
    pipeline's documented fallback seam (tests skip honestly without it)."""
    import know_your_soul.ingest.pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "_default_extractor", lambda: ToyExtractor())
    monkeypatch.setattr(pipeline_mod, "_default_resolver", lambda: ToyResolver())


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class TestParserShape:
    def test_ingest_subcommand_registered(self):
        parser = build_parser()
        args = parser.parse_args(["--store", "x.sqlite3", "ingest", "some/path"])
        assert args.command == "ingest"
        assert args.path == "some/path"
        assert args.incremental is True  # default
        assert args.recursive is True

    def test_ingest_flags(self):
        args = build_parser().parse_args(
            ["ingest", "p", "--no-incremental", "--no-recursive", "--no-resolve", "--max-files", "10", "--json"]
        )
        assert args.incremental is False
        assert args.recursive is False
        assert args.no_resolve is True
        assert args.max_files == 10
        assert args.json is True

    def test_budget_flags(self):
        args = build_parser().parse_args(
            ["ingest", "p", "--max-nodes", "100", "--max-edges", "200", "--max-refs", "50", "--max-file-bytes", "999"]
        )
        assert (args.max_nodes, args.max_edges, args.max_refs, args.max_file_bytes) == (100, 200, 50, 999)

    def test_frozen_operations_untouched(self):
        """The 14 frozen ops are still dispatched via payload, not flags."""
        parser = build_parser()
        for op in (
            "query",
            "get-node",
            "get-subtree",
            "log-event",
            "record-outcome",
            "evaluate",
            "review",
            "propose-improvement",
            "apply-improvement",
            "rollback",
            "path",
            "explain",
            "affected",
            "god-nodes",
        ):
            args = parser.parse_args([op])
            assert args.command == op


class TestIngestCommand:
    def test_happy_path_human_output(self, tmp_path, capsys):
        write_fixture(tmp_path, SIMPLE)
        code, out, err = run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", str(tmp_path))
        assert code == 0, err
        assert "files 2" in out
        assert "nodes 5" in out
        assert "edges 3" in out
        assert "duration" in out
        assert "ms" in out

    def test_json_output(self, tmp_path, capsys):
        write_fixture(tmp_path, SIMPLE)
        code, out, _ = run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", "--json", str(tmp_path))
        assert code == 0
        payload = json.loads(out)
        assert payload["status"] == "proposal"
        assert payload["stats"]["proposed"] == 2
        assert payload["totals"]["nodes"] == 5
        assert payload["totals"]["edges"] == 3
        assert payload["resolution"] == {"resolved": 0, "failed": 0, "pending": 0}
        assert isinstance(payload["duration_ms"], int)
        assert len(payload["input"]["sha256"]) == 64

    def test_missing_path_exit_1_envelope(self, tmp_path, capsys):
        code, out, err = run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", str(tmp_path / "nope"))
        assert code == 1
        envelope = json.loads(err)
        assert envelope["error"]["code"] == "PATH_NOT_FOUND"

    def test_incremental_second_run_reports_skipped(self, tmp_path, capsys):
        write_fixture(tmp_path, SIMPLE)
        store = str(tmp_path / "s.sqlite3")
        code1, _, _ = run_cli(capsys, "--store", store, "ingest", str(tmp_path))
        code2, out2, _ = run_cli(capsys, "--store", store, "ingest", "--json", str(tmp_path))
        assert code1 == code2 == 0
        payload = json.loads(out2)
        assert payload["stats"]["skipped"] == 2
        assert payload["stats"]["proposed"] == 0

    def test_refs_summary_in_output(self, tmp_path, capsys):
        write_fixture(tmp_path, {"a.py": "def alpha():\n    pass\n-> beta\n", "b.py": "def beta():\n    pass\n"})
        code, out, _ = run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", "--json", str(tmp_path))
        assert code == 0
        payload = json.loads(out)
        assert payload["resolution"]["resolved"] == 1

    def test_unsupported_file_counted_not_fatal(self, tmp_path, capsys):
        write_fixture(tmp_path, {**SIMPLE, "x.bin": "\x00"})
        code, out, _ = run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", "--json", str(tmp_path))
        assert code == 0
        payload = json.loads(out)
        assert payload["stats"]["failed"] == 1

    def test_budget_failure_exit_1(self, tmp_path, capsys):
        write_fixture(tmp_path, SIMPLE)
        code, _, err = run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", "--max-nodes", "2", str(tmp_path)
        )
        assert code == 1
        envelope = json.loads(err)
        assert envelope["error"]["code"] == "INGESTION_BUDGET_EXCEEDED"

    def test_audit_written_once(self, tmp_path, capsys):
        write_fixture(tmp_path, SIMPLE)
        store_path = str(tmp_path / "s.sqlite3")
        code, _, _ = run_cli(capsys, "--store", store_path, "ingest", str(tmp_path))
        assert code == 0
        conn = sqlite3.connect(store_path)
        try:
            rows = conn.execute(
                "SELECT event_type FROM event_log WHERE kind = 'audit' AND event_type = 'knowledge.ingest_directory'"
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 1

    def test_no_resolve_flag(self, tmp_path, capsys):
        write_fixture(tmp_path, {"a.py": "def alpha():\n    pass\n-> beta\n"})
        code, out, _ = run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", "--json", "--no-resolve", str(tmp_path)
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["resolution_status"] == "skipped"

    def test_resolver_unavailable_reported_not_fatal(self, tmp_path, capsys, monkeypatch):
        """Without SUB-88 the CLI reports resolution as unavailable, exit 0."""
        import know_your_soul.ingest.pipeline as pipeline_mod

        monkeypatch.setattr(pipeline_mod, "_default_resolver", lambda: None)
        write_fixture(tmp_path, {"a.py": "def alpha():\n    pass\n-> beta\n"})
        code, out, _ = run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", "--json", str(tmp_path))
        assert code == 0
        payload = json.loads(out)
        assert payload["resolution_status"] == "unavailable"

    def test_extractor_unavailable_typed_failure(self, tmp_path, capsys, monkeypatch):
        """Without SUB-87 (and no injection) the CLI fails typed, exit 1."""
        import know_your_soul.ingest.pipeline as pipeline_mod

        def no_extractor():
            from know_your_soul.ingest.pipeline import ExtractorUnavailableError

            raise ExtractorUnavailableError("not landed")

        monkeypatch.setattr(pipeline_mod, "_default_extractor", no_extractor)
        write_fixture(tmp_path, SIMPLE)
        code, _, err = run_cli(capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", str(tmp_path))
        assert code == 1
        envelope = json.loads(err)
        assert envelope["error"]["code"] == "EXTRACTOR_UNAVAILABLE"

    def test_single_file_argument(self, tmp_path, capsys):
        write_fixture(tmp_path, SIMPLE)
        code, out, _ = run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", "--json", str(tmp_path / "a.py")
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["stats"]["proposed"] == 1

    def test_max_files_guard_visible(self, tmp_path, capsys):
        write_fixture(tmp_path, {f"f{i}.py": f"def f{i}():\n    pass\n" for i in range(4)})
        code, out, _ = run_cli(
            capsys, "--store", str(tmp_path / "s.sqlite3"), "ingest", "--json", "--max-files", "2", str(tmp_path)
        )
        assert code == 0
        payload = json.loads(out)
        assert payload["guards_hit"] == ["max_files"]
