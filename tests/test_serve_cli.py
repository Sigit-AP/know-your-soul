"""CLI surface tests for the superpower server (SUB-100).

Both entries are exercised as real subprocesses: the package module
(``python -m know_your_soul.serve``) and the operator CLI
(``know_your_soul.cli`` → ``serve --daemon|--proxy`` + ``daemon
status|stop``). The frozen fourteen-op command surface is untouched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _serve_helpers import seed_store, wait_for


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[1] == "Z":
            return False
    except OSError, IndexError:
        pass
    return True


@pytest.fixture()
def seeded(tmp_path: Path):
    graph_root, db = seed_store(tmp_path)
    yield graph_root, db
    # Best-effort cleanup of any daemon this test spawned.
    pidfile = tmp_path / "superpower" / "daemon.pid"
    try:
        pid = int(pidfile.read_text().split()[0])
        os.kill(pid, 9)
    except OSError, ValueError, IndexError, ProcessLookupError:
        pass


def _module_cli(db: Path, graph_root: Path, *args: str) -> list[str]:
    return [sys.executable, "-m", "know_your_soul.serve", *args, "--db", str(db), "--graph-root", str(graph_root)]


def _operator_cli(db: Path, graph_root: Path, *args: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "know_your_soul",
        "--store",
        str(db),
        "--output-root",
        str(graph_root),
        *args,
    ]


class TestModuleCli:
    def test_daemon_lifecycle_status_stop(self, seeded: tuple[Path, Path]) -> None:
        graph_root, db = seeded
        started = subprocess.run(_module_cli(db, graph_root, "--daemon"), capture_output=True, timeout=60)
        assert started.returncode == 0, started.stderr.decode()
        payload = json.loads(started.stdout)
        assert payload["status"] == "started"
        assert _pid_alive(payload["pid"])

        again = subprocess.run(_module_cli(db, graph_root, "--daemon"), capture_output=True, timeout=60)
        assert json.loads(again.stdout)["status"] == "already_running"
        assert json.loads(again.stdout)["pid"] == payload["pid"]

        status = subprocess.run(_module_cli(db, graph_root, "daemon", "status"), capture_output=True, timeout=30)
        assert status.returncode == 0
        report = json.loads(status.stdout)
        assert report["running"] is True
        assert report["pid"] == payload["pid"]

        stopped = subprocess.run(_module_cli(db, graph_root, "daemon", "stop"), capture_output=True, timeout=30)
        assert stopped.returncode == 0
        assert json.loads(stopped.stdout)["stopped"] is True
        assert wait_for(lambda: not _pid_alive(payload["pid"]), timeout=15)

    def test_proxy_smoke_over_cli(self, seeded: tuple[Path, Path]) -> None:
        graph_root, db = seeded
        proxy = subprocess.Popen(
            _module_cli(db, graph_root, "--proxy"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        assert proxy.stdin is not None and proxy.stdout is not None
        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
        proxy.stdin.write(json.dumps(frame).encode() + b"\n")
        proxy.stdin.flush()
        response = json.loads(proxy.stdout.readline())
        assert response["result"]["serverInfo"]["name"] == "know-your-soul"
        proxy.stdin.close()
        assert proxy.wait(timeout=20) == 0
        # Cleanup the auto-spawned daemon.
        subprocess.run(_module_cli(db, graph_root, "daemon", "stop"), capture_output=True, timeout=30)

    def test_recursive_spawn_refused(self, seeded: tuple[Path, Path]) -> None:
        graph_root, db = seeded
        completed = subprocess.run(
            _module_cli(db, graph_root, "--daemon"),
            env={**os.environ, "KNOW_YOUR_SOUL_DAEMON_INTERNAL": "1"},
            capture_output=True,
            timeout=30,
        )
        assert completed.returncode == 2
        assert b"recursive" in completed.stderr

    def test_missing_mode_is_usage_error(self, seeded: tuple[Path, Path]) -> None:
        graph_root, db = seeded
        completed = subprocess.run(_module_cli(db, graph_root), capture_output=True, timeout=30)
        assert completed.returncode == 2

    def test_status_when_down(self, seeded: tuple[Path, Path]) -> None:
        graph_root, db = seeded
        completed = subprocess.run(_module_cli(db, graph_root, "daemon", "status"), capture_output=True, timeout=30)
        assert completed.returncode == 0
        assert json.loads(completed.stdout)["running"] is False


class TestOperatorCli:
    def test_serve_daemon_status_stop(self, seeded: tuple[Path, Path]) -> None:
        graph_root, db = seeded
        started = subprocess.run(_operator_cli(db, graph_root, "serve", "--daemon"), capture_output=True, timeout=60)
        assert started.returncode == 0, started.stderr.decode()
        payload = json.loads(started.stdout)
        assert payload["status"] == "started"

        status = subprocess.run(_operator_cli(db, graph_root, "daemon", "status"), capture_output=True, timeout=30)
        assert json.loads(status.stdout)["running"] is True

        stopped = subprocess.run(_operator_cli(db, graph_root, "daemon", "stop"), capture_output=True, timeout=30)
        assert stopped.returncode == 0
        assert json.loads(stopped.stdout)["stopped"] is True
        assert wait_for(lambda: not _pid_alive(payload["pid"]), timeout=15)

    def test_serve_without_superpower_flags_still_requires_transport(self, seeded: tuple[Path, Path]) -> None:
        """The pre-existing HTTP/socket API surface is untouched: no flag = default socket serve."""
        graph_root, db = seeded
        # Start the classic socket server briefly and prove it binds.
        process = subprocess.Popen(
            _operator_cli(db, graph_root, "serve", "--socket", str(seeded[0].parent / "classic.sock")),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            assert wait_for(lambda: (seeded[0].parent / "classic.sock").exists(), timeout=20)
        finally:
            process.terminate()
            process.wait(timeout=10)
