"""CLI tests for ``know-your-soul watch`` (SUB-92).

OPS surface only — like ``ingest``/``export`` it never joins the frozen
fourteen knowledge operations. Exit codes: 0 completed, 1 typed failure
(including auto-degraded and policy-disabled), 2 usage error.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from know_your_soul.cli import main
from know_your_soul.ingest.watcher import ENV_NO_WATCH

FIXED_NS = 1_700_000_000_000_000_000


def write_py(root: Path, name: str = "mod.py", text: str = "def watched():\n    return 1\n") -> Path:
    target = root / name
    target.write_text(text, encoding="utf-8")
    os.utime(target, ns=(FIXED_NS, FIXED_NS))
    return target


def run_cli(argv):
    return main(argv)


class TestWatchCliOnce:
    def test_once_json_report(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        write_py(root)
        store = tmp_path / "s.sqlite3"
        code = run_cli(["--store", str(store), "watch", str(root), "--once", "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "completed"
        assert len(payload["cycles"]) == 1
        cycle = payload["cycles"][0]
        assert cycle["mode"] == "quick"
        assert [c["relpath"] for c in cycle["changes"]] == ["mod.py"]
        assert cycle["files_proposed"] == 1

    def test_once_summary_line(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        write_py(root)
        code = run_cli(["--store", str(tmp_path / "s.sqlite3"), "watch", str(root), "--once"])
        assert code == 0
        out = capsys.readouterr().out
        assert "watch: cycle 1 quick" in out
        assert "status completed" in out

    def test_once_empty_root_idle(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        code = run_cli(["--store", str(tmp_path / "s.sqlite3"), "watch", str(root), "--once", "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["cycles"][0]["mode"] == "idle"

    def test_once_persists_graph_state(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        write_py(root)
        store = tmp_path / "s.sqlite3"
        assert run_cli(["--store", str(store), "watch", str(root), "--once"]) == 0
        capsys.readouterr()
        from know_your_soul.store import EventStore

        with EventStore(str(store)) as reopened:
            assert sorted(row.path for row in reopened.list_files()) == ["mod.py"]
            assert sorted(row.name for row in reopened.symbols_in_path("mod.py")) == ["watched"]

    def test_second_invocation_skips_unchanged(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        write_py(root)
        store = tmp_path / "s.sqlite3"
        assert run_cli(["--store", str(store), "watch", str(root), "--once", "--json"]) == 0
        capsys.readouterr()
        assert run_cli(["--store", str(store), "watch", str(root), "--once", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        cycle = payload["cycles"][0]
        assert cycle["mode"] == "quick"  # watcher baseline is per-process
        assert cycle["files_proposed"] == 0
        assert cycle["files_skipped"] == 1  # pipeline incremental pre-filter

    def test_store_inside_watched_root_not_ingested(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        write_py(root)
        store = root / "soul.sqlite3"  # store lives inside the watched root
        code = run_cli(["--store", str(store), "watch", str(root), "--once", "--json"])
        assert code == 0
        payload = json.loads(capsys.readouterr().out)
        relpaths = [c["relpath"] for c in payload["cycles"][0]["changes"]]
        assert relpaths == ["mod.py"]


class TestWatchCliPolicy:
    def test_no_watch_env_disables(self, tmp_path, capsys, monkeypatch):
        root = tmp_path / "corpus"
        root.mkdir()
        monkeypatch.setenv(ENV_NO_WATCH, "1")
        code = run_cli(["--store", str(tmp_path / "s.sqlite3"), "watch", str(root), "--once"])
        assert code == 1
        err = json.loads(capsys.readouterr().err)
        assert err["error"]["code"] == "WATCH_DISABLED"
        assert ENV_NO_WATCH in err["error"]["message"]

    def test_force_does_not_override_no_watch(self, tmp_path, capsys, monkeypatch):
        root = tmp_path / "corpus"
        root.mkdir()
        monkeypatch.setenv(ENV_NO_WATCH, "1")
        code = run_cli(["--store", str(tmp_path / "s.sqlite3"), "watch", str(root), "--once", "--force"])
        assert code == 1
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "WATCH_DISABLED"


class TestWatchCliUsage:
    def test_missing_path_usage_error(self, tmp_path, capsys):
        code = run_cli(["--store", str(tmp_path / "s.sqlite3"), "watch", str(tmp_path / "nope"), "--once"])
        assert code == 2
        err = json.loads(capsys.readouterr().err)
        assert err["error"]["code"] == "CLI_USAGE"

    def test_file_path_usage_error(self, tmp_path, capsys):
        target = write_py(tmp_path)
        code = run_cli(["--store", str(tmp_path / "s.sqlite3"), "watch", str(target), "--once"])
        assert code == 2

    def test_interval_zero_usage_error(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        code = run_cli(["--store", str(tmp_path / "s.sqlite3"), "watch", str(root), "--once", "--interval", "0"])
        assert code == 2
        assert "interval" in json.loads(capsys.readouterr().err)["error"]["message"]

    def test_interval_below_floor_usage_error(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        code = run_cli(["--store", str(tmp_path / "s.sqlite3"), "watch", str(root), "--once", "--interval", "0.001"])
        assert code == 2

    def test_negative_ceiling_usage_error(self, tmp_path, capsys):
        root = tmp_path / "corpus"
        root.mkdir()
        code = run_cli(
            ["--store", str(tmp_path / "s.sqlite3"), "watch", str(root), "--once", "--full-scan-ceiling", "0"]
        )
        assert code == 2
        assert "full_scan_ceiling" in json.loads(capsys.readouterr().err)["error"]["message"]

    def test_watch_not_a_knowledge_operation(self):
        from know_your_soul.http_api import KNOWLEDGE_OPERATIONS, OPS_OPERATIONS

        assert "watch" not in KNOWLEDGE_OPERATIONS
        assert "watch" not in OPS_OPERATIONS
