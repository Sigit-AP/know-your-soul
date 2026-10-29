"""Daemon configuration: one frozen, fail-closed value object.

Every tunable of the superpower server lives here. Validation happens at
construction (:class:`ConfigError`), so a daemon can never start with a
nonsensical capacity, a watchdog that cannot fire, or an in-memory store
(which would be invisible to every other process).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from know_your_soul.contracts import DEFAULT_MAX_TOKEN_BUDGET
from know_your_soul.serve.rpc import MAX_FRAME_BYTES

__all__ = [
    "DEFAULT_IDLE_TIMEOUT_SECONDS",
    "DEFAULT_LIVENESS_HEARTBEAT_SECONDS",
    "DEFAULT_LIVENESS_STALL_SECONDS",
    "DEFAULT_MAX_AGENTS",
    "DEFAULT_PPID_POLL_SECONDS",
    "DEFAULT_READ_POOL_SIZE",
    "DEFAULT_READ_PUBLISH_WAIT_SECONDS",
    "DEFAULT_READ_QUEUE_SIZE",
    "DEFAULT_RETRY_AFTER_MS",
    "DEFAULT_SPAWN_TIMEOUT_SECONDS",
    "DEFAULT_WRITE_QUEUE_SIZE",
    "HARD_MAX_AGENTS",
    "HARD_MAX_READ_POOL",
    "MIN_FRAME_BYTES",
    "ConfigError",
    "DaemonConfig",
    "DaemonPaths",
]

DEFAULT_MAX_AGENTS = 50
HARD_MAX_AGENTS = 256
DEFAULT_READ_POOL_SIZE = 8
HARD_MAX_READ_POOL = 64
DEFAULT_WRITE_QUEUE_SIZE = 256
DEFAULT_READ_QUEUE_SIZE = 512
DEFAULT_IDLE_TIMEOUT_SECONDS = 300.0
DEFAULT_SPAWN_TIMEOUT_SECONDS = 15.0
DEFAULT_LIVENESS_HEARTBEAT_SECONDS = 0.5
DEFAULT_LIVENESS_STALL_SECONDS = 10.0
DEFAULT_PPID_POLL_SECONDS = 0.5
DEFAULT_READ_PUBLISH_WAIT_SECONDS = 30.0
DEFAULT_RETRY_AFTER_MS = 100
MIN_FRAME_BYTES = 1024


# AF_UNIX sockaddr paths are capped (~108 bytes on Linux); beyond this the
# socket rendezvous moves to a deterministic short path derived from the
# runtime root hash (the lock/pid/ready/exit artifacts stay with the store).
_MAX_SOCKET_PATH_BYTES = 100


class ConfigError(ValueError):
    """A configuration value outside the fail-closed envelope."""


@dataclass(frozen=True)
class DaemonPaths:
    """The runtime artifacts of one daemon; all live under ``root``.

    Exception: when ``root/daemon.sock`` would exceed the AF_UNIX path cap,
    the socket (a transient rendezvous, never state) moves to
    ``$TMPDIR/kys-<sha256(root)[:16]>/daemon.sock`` — the same deterministic
    location for every process looking at this store.
    """

    root: Path

    @property
    def lock(self) -> Path:
        return self.root / "daemon.lock"

    @property
    def pid(self) -> Path:
        return self.root / "daemon.pid"

    @property
    def socket(self) -> Path:
        candidate = self.root / "daemon.sock"
        if len(str(candidate).encode("utf-8")) <= _MAX_SOCKET_PATH_BYTES:
            return candidate
        digest = hashlib.sha256(str(self.root).encode("utf-8")).hexdigest()[:16]
        return Path(tempfile.gettempdir()) / f"kys-{digest}" / "daemon.sock"

    @property
    def ready(self) -> Path:
        return self.root / "daemon.ready"

    @property
    def exit(self) -> Path:
        return self.root / "daemon.exit.json"

    def ensure(self) -> None:
        """Create the runtime root operator-only (0700), idempotently."""
        self.root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        socket_parent = self.socket.parent
        if socket_parent != self.root:
            socket_parent.mkdir(parents=True, exist_ok=True)
            os.chmod(socket_parent, 0o700)


@dataclass(frozen=True)
class DaemonConfig:
    """The complete, validated configuration of one daemon per store."""

    db_path: Path | str
    graph_root: Path | str
    runtime_dir: Path | str | None = None
    max_agents: int = DEFAULT_MAX_AGENTS
    read_pool_size: int = DEFAULT_READ_POOL_SIZE
    write_queue_size: int = DEFAULT_WRITE_QUEUE_SIZE
    read_queue_size: int = DEFAULT_READ_QUEUE_SIZE
    # Fairness (operator requirement W7): per-agent queue quotas — one
    # spamming agent fills only its own lane; round-robin drain keeps every
    # other agent scheduled.
    per_actor_write_cap: int = 64
    per_actor_read_cap: int = 128
    # Admission control (W7): per-agent token bucket; an agent exceeding its
    # sustained rate gets typed SERVER_BUSY with retry_after instead of an
    # unbounded queue.
    agent_rate_per_second: float = 100.0
    agent_burst: int = 200
    # Heavy lane (ingest v3): single preemptible worker, small queue.
    heavy_queue_size: int = 16
    per_actor_heavy_cap: int = 4
    # Session lease: a session with no heartbeat/request within this window is
    # closed and its slot released (the stdio proxy heartbeats transparently).
    session_lease_seconds: float = 30.0
    # Group commit (W7): the writer joins bursts of up to N mutations or a
    # W-millisecond window into ONE SQLite transaction (savepoint nesting),
    # amortizing fsync. Kill switch: KNOW_YOUR_SOUL_NO_GROUP_COMMIT=1.
    group_commit: bool = True
    group_commit_batch: int = 8
    group_commit_window_seconds: float = 0.002
    # Graceful drain: on shutdown, new work is refused typed while in-flight
    # items finish within this budget.
    drain_timeout_seconds: float = 10.0
    idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS
    spawn_timeout_seconds: float = DEFAULT_SPAWN_TIMEOUT_SECONDS
    liveness_heartbeat_seconds: float = DEFAULT_LIVENESS_HEARTBEAT_SECONDS
    liveness_stall_seconds: float = DEFAULT_LIVENESS_STALL_SECONDS
    ppid_poll_seconds: float = DEFAULT_PPID_POLL_SECONDS
    read_publish_wait_seconds: float = DEFAULT_READ_PUBLISH_WAIT_SECONDS
    max_frame_bytes: int = MAX_FRAME_BYTES
    max_token_budget: int = DEFAULT_MAX_TOKEN_BUDGET
    retry_after_ms: int = DEFAULT_RETRY_AFTER_MS
    # Deterministic backpressure test hook: the writer sleeps this long after
    # each mutation. Production configs keep it at zero; the concurrency tests
    # use it to fill the bounded write queue on demand.
    mutation_delay_seconds: float = 0.0
    # The actor the stdio proxy stamps onto host requests that carry no
    # ``_meta.actor`` of their own (audit requirement: every request has one).
    proxy_actor: str = "mcp-proxy"

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))
        object.__setattr__(self, "graph_root", Path(self.graph_root))
        if self.runtime_dir is None:
            object.__setattr__(self, "runtime_dir", Path(self.db_path).expanduser().resolve().parent / "superpower")
        else:
            object.__setattr__(self, "runtime_dir", Path(self.runtime_dir))
        self._validate()

    def _validate(self) -> None:
        if str(self.db_path) == ":memory:":
            raise ConfigError("the daemon requires a file-backed store; ':memory:' is unreachable across processes")
        if not 1 <= int(self.max_agents) <= HARD_MAX_AGENTS:
            raise ConfigError(f"max_agents must be in [1, {HARD_MAX_AGENTS}], got {self.max_agents}")
        if not 1 <= int(self.read_pool_size) <= HARD_MAX_READ_POOL:
            raise ConfigError(f"read_pool_size must be in [1, {HARD_MAX_READ_POOL}], got {self.read_pool_size}")
        if int(self.write_queue_size) < 1:
            raise ConfigError(f"write_queue_size must be >= 1, got {self.write_queue_size}")
        if int(self.read_queue_size) < 1:
            raise ConfigError(f"read_queue_size must be >= 1, got {self.read_queue_size}")
        if not 1 <= int(self.per_actor_write_cap) <= int(self.write_queue_size):
            raise ConfigError(
                f"per_actor_write_cap must be in [1, write_queue_size={self.write_queue_size}], "
                f"got {self.per_actor_write_cap}"
            )
        if not 1 <= int(self.per_actor_read_cap) <= int(self.read_queue_size):
            raise ConfigError(
                f"per_actor_read_cap must be in [1, read_queue_size={self.read_queue_size}], "
                f"got {self.per_actor_read_cap}"
            )
        if float(self.agent_rate_per_second) <= 0:
            raise ConfigError("agent_rate_per_second must be positive")
        if int(self.agent_burst) < 1:
            raise ConfigError("agent_burst must be >= 1")
        if int(self.heavy_queue_size) < 1 or not 1 <= int(self.per_actor_heavy_cap) <= int(self.heavy_queue_size):
            raise ConfigError("heavy queue caps out of range")
        if float(self.session_lease_seconds) <= 0:
            raise ConfigError("session_lease_seconds must be positive")
        if int(self.group_commit_batch) < 1:
            raise ConfigError("group_commit_batch must be >= 1")
        if float(self.group_commit_window_seconds) < 0:
            raise ConfigError("group_commit_window_seconds must be >= 0")
        if float(self.drain_timeout_seconds) <= 0:
            raise ConfigError("drain_timeout_seconds must be positive")
        if float(self.idle_timeout_seconds) <= 0:
            raise ConfigError(f"idle_timeout_seconds must be positive, got {self.idle_timeout_seconds}")
        if float(self.spawn_timeout_seconds) <= 0:
            raise ConfigError(f"spawn_timeout_seconds must be positive, got {self.spawn_timeout_seconds}")
        if float(self.liveness_heartbeat_seconds) <= 0:
            raise ConfigError("liveness_heartbeat_seconds must be positive")
        if float(self.liveness_stall_seconds) <= float(self.liveness_heartbeat_seconds):
            raise ConfigError(
                f"liveness_stall_seconds ({self.liveness_stall_seconds}) must exceed "
                f"liveness_heartbeat_seconds ({self.liveness_heartbeat_seconds})"
            )
        if float(self.ppid_poll_seconds) <= 0:
            raise ConfigError("ppid_poll_seconds must be positive")
        if float(self.read_publish_wait_seconds) <= 0:
            raise ConfigError("read_publish_wait_seconds must be positive")
        if int(self.max_frame_bytes) < MIN_FRAME_BYTES:
            raise ConfigError(f"max_frame_bytes must be >= {MIN_FRAME_BYTES}, got {self.max_frame_bytes}")
        if int(self.retry_after_ms) < 0:
            raise ConfigError("retry_after_ms must be >= 0")
        if float(self.mutation_delay_seconds) < 0:
            raise ConfigError("mutation_delay_seconds must be >= 0")
        if not str(self.proxy_actor).strip():
            raise ConfigError("proxy_actor must be a non-empty string")

    @property
    def paths(self) -> DaemonPaths:
        return DaemonPaths(Path(self.runtime_dir).expanduser().resolve())  # type: ignore[arg-type]
