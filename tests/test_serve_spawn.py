"""Detached-spawn tests (SUB-100).

Real process supervision: double-fork/setsid detachment, the atomic O_EXCL
lockfile as the spawn arbiter, stale-lock recovery after SIGKILL, idle
reaping of a quiet daemon, and the double-spawn race where exactly one
daemon may survive.
"""

from __future__ import annotations

import json
import os
import signal
import threading
from pathlib import Path

import pytest

from _serve_helpers import SocketClient, make_config, wait_for
from know_your_soul.serve.daemon_config import DaemonConfig
from know_your_soul.serve.superpower import (
    DaemonUnavailable,
    daemon_status,
    spawn_daemon,
    stop_daemon,
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(pid, 0); it is not alive for our purposes.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        if stat.rsplit(")", 1)[1].split()[1] == "Z":
            return False
    except OSError, IndexError:
        pass
    return True


@pytest.fixture()
def config(tmp_path: Path) -> DaemonConfig:
    return make_config(tmp_path, idle_timeout_seconds=300.0)


@pytest.fixture(autouse=True)
def _cleanup(tmp_path: Path):
    yield
    # Best-effort: stop any daemon rooted under this test's tmp_path.
    for runtime in tmp_path.glob("**/superpower"):
        pidfile = runtime / "daemon.pid"
        try:
            pid = int(pidfile.read_text().split()[0])
        except OSError, ValueError, IndexError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class TestDetachedSpawn:
    def test_spawn_returns_live_detached_daemon(self, config: DaemonConfig) -> None:
        result = spawn_daemon(config)
        assert not result.already_running
        assert _pid_alive(result.pid)
        # Detached: its own session, not our process group.
        assert os.getpgid(result.pid) != os.getpgrp()
        status = daemon_status(config)
        assert status["running"] is True
        assert status["pid"] == result.pid
        stop_daemon(config)

    def test_second_spawn_returns_same_daemon(self, config: DaemonConfig) -> None:
        first = spawn_daemon(config)
        second = spawn_daemon(config)
        assert second.already_running is True
        assert second.pid == first.pid
        stop_daemon(config)

    def test_double_spawn_race_single_survivor(self, config: DaemonConfig) -> None:
        """Eight concurrent spawns: exactly one daemon pid survives."""
        results: list = []
        errors: list[BaseException] = []

        def spawn() -> None:
            try:
                results.append(spawn_daemon(config))
            except BaseException as error:  # noqa: BLE001 - asserted below
                errors.append(error)

        threads = [threading.Thread(target=spawn) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert not errors, f"spawn errors: {errors!r}"
        pids = {result.pid for result in results}
        assert len(pids) == 1, f"expected one daemon, got pids {pids}"
        assert daemon_status(config)["running"] is True
        stop_daemon(config)

    def test_sigkilled_daemon_leaves_recoverable_state(self, config: DaemonConfig) -> None:
        first = spawn_daemon(config)
        os.kill(first.pid, signal.SIGKILL)
        assert wait_for(lambda: not _pid_alive(first.pid), timeout=10)
        # Stale lock + pidfile remain; the next spawn must recover.
        second = spawn_daemon(config)
        assert second.pid != first.pid
        assert daemon_status(config)["running"] is True
        with SocketClient(config.paths.socket) as client:
            assert client.request("ping")["result"] == {}
        stop_daemon(config)

    def test_idle_daemon_exits_cleanly(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, idle_timeout_seconds=1.0)
        result = spawn_daemon(config)
        assert _pid_alive(result.pid)
        assert wait_for(lambda: not _pid_alive(result.pid), timeout=30), "idle daemon never exited"
        assert not config.paths.lock.exists()
        assert not config.paths.socket.exists()
        exit_report = json.loads(config.paths.exit.read_text(encoding="utf-8"))
        assert exit_report["reason"] == "idle"

    def test_stop_daemon_graceful(self, config: DaemonConfig) -> None:
        result = spawn_daemon(config)
        report = stop_daemon(config, timeout_seconds=15)
        assert report["stopped"] is True
        assert wait_for(lambda: not _pid_alive(result.pid), timeout=15)
        exit_report = json.loads(config.paths.exit.read_text(encoding="utf-8"))
        assert exit_report["reason"] == "operator"
        status = daemon_status(config)
        assert status["running"] is False
        assert status["last_exit"]["reason"] == "operator"

    def test_stop_when_not_running_is_typed_not_error(self, config: DaemonConfig) -> None:
        report = stop_daemon(config)
        assert report["stopped"] is False
        assert report["reason"] == "not running"

    def test_spawn_failure_is_typed_not_hanging(self, tmp_path: Path) -> None:
        """A daemon that cannot start (db path is a directory) fails typed."""
        config = make_config(tmp_path, spawn_timeout_seconds=3.0)
        broken = DaemonConfig(
            db_path=tmp_path,  # a directory: sqlite3.connect fails in the child
            graph_root=config.graph_root,
            runtime_dir=config.runtime_dir,
            spawn_timeout_seconds=3.0,
        )
        with pytest.raises(DaemonUnavailable):
            spawn_daemon(broken)

    def test_daemon_answers_real_query(self, config: DaemonConfig) -> None:
        spawn_daemon(config)
        try:
            with SocketClient(config.paths.socket) as client:
                response = client.call_tool(
                    "knowledge_query",
                    {"purpose": "run the gates", "domain": "P1", "token_budget": 512},
                    actor="spawn-test",
                )
            assert "result" in response
            assert not response["result"].get("isError"), response
        finally:
            stop_daemon(config)

    def test_recursive_spawn_refused(self, config: DaemonConfig) -> None:
        """KNOW_YOUR_SOUL_DAEMON_INTERNAL blocks daemon-in-daemon spawning."""
        import subprocess
        import sys

        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "know_your_soul.serve",
                "--daemon",
                "--db",
                str(config.db_path),
                "--graph-root",
                str(config.graph_root),
            ],
            env={**os.environ, "KNOW_YOUR_SOUL_DAEMON_INTERNAL": "1"},
            capture_output=True,
            timeout=30,
        )
        assert completed.returncode == 2
        assert b"recursive" in completed.stderr
