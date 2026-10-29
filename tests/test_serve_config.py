"""Configuration validation tests for the superpower server (SUB-100).

DaemonConfig is the single fail-closed gate for every tunable: capacities,
watchdog intervals and the backpressure hooks. Invalid values are rejected
at construction, never at first use.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from know_your_soul.serve.daemon_config import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_MAX_AGENTS,
    DEFAULT_READ_POOL_SIZE,
    DEFAULT_READ_QUEUE_SIZE,
    DEFAULT_WRITE_QUEUE_SIZE,
    ConfigError,
    DaemonConfig,
)


def _config(tmp_path: Path, **overrides: object) -> DaemonConfig:
    kwargs: dict[str, object] = {"db_path": tmp_path / "soul.sqlite3", "graph_root": tmp_path / "graph"}
    kwargs.update(overrides)
    return DaemonConfig(**kwargs)  # type: ignore[arg-type]


class TestDefaults:
    def test_capacity_defaults(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        assert config.max_agents == DEFAULT_MAX_AGENTS == 50
        assert config.read_pool_size == DEFAULT_READ_POOL_SIZE == 8
        assert config.write_queue_size == DEFAULT_WRITE_QUEUE_SIZE > 0
        assert config.read_queue_size == DEFAULT_READ_QUEUE_SIZE > 0

    def test_idle_default_is_five_minutes(self, tmp_path: Path) -> None:
        assert _config(tmp_path).idle_timeout_seconds == DEFAULT_IDLE_TIMEOUT_SECONDS == 300.0

    def test_mutation_delay_defaults_off(self, tmp_path: Path) -> None:
        assert _config(tmp_path).mutation_delay_seconds == 0.0


class TestPaths:
    def test_runtime_dir_defaults_next_to_store(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        assert config.paths.root == (tmp_path / "superpower").resolve()

    def test_runtime_dir_override(self, tmp_path: Path) -> None:
        config = _config(tmp_path, runtime_dir=tmp_path / "rt")
        assert config.paths.root == (tmp_path / "rt").resolve()

    def test_artifact_paths(self, tmp_path: Path) -> None:
        paths = _config(tmp_path).paths
        assert paths.lock.name == "daemon.lock"
        assert paths.pid.name == "daemon.pid"
        assert paths.socket.name == "daemon.sock"
        assert paths.ready.name == "daemon.ready"
        assert paths.exit.name == "daemon.exit.json"
        assert paths.lock.parent == paths.ready.parent == paths.root

    def test_paths_do_not_escape_runtime_root(self, tmp_path: Path) -> None:
        paths = _config(tmp_path).paths
        for artifact in (paths.lock, paths.pid, paths.ready, paths.exit):
            assert artifact.resolve().is_relative_to(paths.root.resolve())
        # The socket stays under root when it fits the AF_UNIX cap; longer
        # roots redirect deterministically into $TMPDIR/kys-<hash>/.
        socket_text = str(paths.socket)
        assert paths.socket.resolve().is_relative_to(paths.root.resolve()) or "/kys-" in socket_text

    def test_long_root_gets_short_deterministic_socket(self) -> None:
        from know_your_soul.serve.daemon_config import DaemonPaths

        long_root = Path("/tmp") / ("x" * 120)
        paths = DaemonPaths(long_root)
        assert len(str(paths.socket).encode()) <= 100
        assert paths.socket == DaemonPaths(long_root).socket  # deterministic
        assert paths.socket != DaemonPaths(Path("/tmp") / ("y" * 120)).socket


class TestValidation:
    def test_zero_max_agents_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, max_agents=0)

    def test_max_agents_hard_cap(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, max_agents=257)

    def test_read_pool_zero_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, read_pool_size=0)

    def test_read_pool_hard_cap(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, read_pool_size=65)

    def test_zero_write_queue_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, write_queue_size=0)

    def test_zero_read_queue_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, read_queue_size=0)

    def test_non_positive_idle_timeout_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, idle_timeout_seconds=0.0)

    def test_negative_mutation_delay_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, mutation_delay_seconds=-0.5)

    def test_memory_store_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, db_path=":memory:")

    def test_tiny_frame_cap_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, max_frame_bytes=16)

    def test_stall_must_exceed_heartbeat(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError):
            _config(tmp_path, liveness_heartbeat_seconds=5.0, liveness_stall_seconds=1.0)

    def test_config_is_frozen(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        with pytest.raises(Exception):  # noqa: B017 - frozen dataclass raises FrozenInstanceError
            config.max_agents = 3  # type: ignore[misc]

    def test_per_actor_caps_default_and_bounds(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        assert config.per_actor_write_cap == 64
        assert config.per_actor_read_cap == 128
        with pytest.raises(ConfigError):
            _config(tmp_path, per_actor_write_cap=0)
        with pytest.raises(ConfigError):
            _config(tmp_path, per_actor_write_cap=config.write_queue_size + 1)
        with pytest.raises(ConfigError):
            _config(tmp_path, per_actor_read_cap=0)
        with pytest.raises(ConfigError):
            _config(tmp_path, per_actor_read_cap=config.read_queue_size + 1)
