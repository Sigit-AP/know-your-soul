"""Polling ingestion watcher (Wave 4, SUB-92).

A pure-stdlib polling watcher — periodic ``os.stat`` scans, **not** inotify —
so it is portable (Linux/macOS/Windows, network filesystems) and fully
testable with injected clock/sleep. Adopted from the codegraph watcher
policy (``research-codegraph/RESEARCH-OPS.md`` §1):

* **Debounce, configurable**: a full debounce window (default 2.0 s) and a
  quick quiet window (default 0.3 s) used when at most
  ``quick_max_changes`` (default 2) files are pending, so a single save
  syncs almost instantly while bursts coalesce. The quiet window re-scans
  until the change set is stable, bounded by ``max_quiet_rounds`` so
  constant churn can never starve ingestion.
* **Quick path**: at most ``full_scan_ceiling`` (default 500) changed files
  and no removals → each changed file goes through
  :meth:`IngestionPipeline.ingest_path` individually, in bytewise order.
* **Full scan-diff**: above the ceiling, or whenever a removal is present
  (deletions are cascade work handled by the directory walk), the whole
  root is re-ingested incrementally — the pipeline's own
  ``(size, mtime) → sha256`` pre-filter keeps this cheap and self-heals any
  missed event.
* **Auto-degrade**: after ``max_consecutive_failures`` (default 5) failures
  in a row the watcher logs and **stops** — it never crashes the host
  process and never busy-loops (the interval sleep runs between cycles,
  failure cycles included; ``interval_seconds`` has a hard floor).
* **WSL / remote-mount detection**: watching is refused (with an actionable
  message) on WSL Windows mounts (``/mnt/<letter>``) and on remote/virtual
  filesystems (nfs, cifs, sshfs, …), unless forced.
  ``KNOW_YOUR_SOUL_NO_WATCH=1`` always wins; ``KNOW_YOUR_SOUL_FORCE_WATCH=1``
  overrides the WSL/remote heuristic only.

Determinism: every cycle produces a :class:`CycleReport` whose content is a
pure function of the filesystem state and the injected clock; iteration
order is bytewise everywhere. Proposal-only posture is inherited from the
pipeline (contract v3): the watcher writes nothing but the v3 ingestion
tables.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from know_your_soul.ingest.pipeline import SKIP_DIR_NAMES

__all__ = [
    "DEFAULT_DEBOUNCE_SECONDS",
    "DEFAULT_FULL_SCAN_CEILING",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "DEFAULT_MAX_QUIET_ROUNDS",
    "DEFAULT_QUICK_MAX_CHANGES",
    "DEFAULT_QUICK_QUIET_SECONDS",
    "ENV_FORCE_WATCH",
    "ENV_NO_WATCH",
    "MIN_INTERVAL_SECONDS",
    "REMOTE_FS_TYPES",
    "ChangeKind",
    "CycleReport",
    "FileChange",
    "IngestionWatcher",
    "WatchPolicyDecision",
    "WatchReport",
    "WatcherConfig",
    "WatcherRootError",
    "diff_snapshots",
    "evaluate_watch_policy",
    "scan_stat_snapshot",
]

ENV_NO_WATCH = "KNOW_YOUR_SOUL_NO_WATCH"
ENV_FORCE_WATCH = "KNOW_YOUR_SOUL_FORCE_WATCH"

#: Hard floor for the poll interval: below this the loop would busy-spin.
MIN_INTERVAL_SECONDS = 0.05

# codegraph knobs (RESEARCH-OPS §1): debounce 2000 ms, quick 300 ms for ≤2
# pending files, scoped-sync ceiling 500, 5 consecutive sync failures.
DEFAULT_INTERVAL_SECONDS = 2.0
DEFAULT_DEBOUNCE_SECONDS = 2.0
DEFAULT_QUICK_QUIET_SECONDS = 0.3
DEFAULT_QUICK_MAX_CHANGES = 2
DEFAULT_FULL_SCAN_CEILING = 500
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_MAX_QUIET_ROUNDS = 4

#: Filesystem types where polling is unreliable or needlessly expensive
#: (codegraph watch-policy: WSL drvfs; network mounts).
REMOTE_FS_TYPES: frozenset[str] = frozenset(
    {"9p", "cifs", "drvfs", "nfs", "nfs4", "prl_fs", "smb3", "smbfs", "sshfs", "fuse.sshfs", "vboxsf"}
)

_WSL_MARKERS = ("microsoft", "wsl")
_WSL_MNT_RE = re.compile(r"^/mnt/([a-z])(/|$)")

_LOGGER = logging.getLogger("know_your_soul.ingest.watcher")


class WatcherRootError(ValueError):
    """The watched root is missing or not a directory (CLI usage failure)."""

    code = "WATCHER_ROOT_INVALID"


class ChangeKind(StrEnum):
    ADDED = "added"
    CHANGED = "changed"
    REMOVED = "removed"


@dataclass(frozen=True)
class FileChange:
    """One stat-level change under the watched root.

    ``size`` is the size after the change; ``None`` for removals.
    """

    relpath: str
    kind: ChangeKind
    size: int | None

    def to_dict(self) -> dict[str, Any]:
        return {"relpath": self.relpath, "kind": str(self.kind), "size": self.size}


@dataclass(frozen=True)
class WatcherConfig:
    """Fail-closed watcher knobs; every value validated at construction."""

    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS
    quick_quiet_seconds: float = DEFAULT_QUICK_QUIET_SECONDS
    quick_max_changes: int = DEFAULT_QUICK_MAX_CHANGES
    full_scan_ceiling: int = DEFAULT_FULL_SCAN_CEILING
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES
    max_quiet_rounds: int = DEFAULT_MAX_QUIET_ROUNDS

    def __post_init__(self) -> None:
        interval = self.interval_seconds
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval < MIN_INTERVAL_SECONDS:
            raise ValueError(
                f"interval_seconds must be a number >= {MIN_INTERVAL_SECONDS} (busy-loop floor), got {interval!r}"
            )
        for name in ("debounce_seconds", "quick_quiet_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{name} must be a number >= 0, got {value!r}")
        for name in ("quick_max_changes", "full_scan_ceiling", "max_consecutive_failures", "max_quiet_rounds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an int >= 1, got {value!r}")


@dataclass(frozen=True)
class WatchPolicyDecision:
    """Whether watching is allowed for a path, with the deciding source."""

    enabled: bool
    reason: str
    source: str  # "default" | "env-no-watch" | "env-force" | "wsl" | "remote-mount"

    def to_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "reason": self.reason, "source": self.source}


@dataclass(frozen=True)
class CycleReport:
    """Deterministic per-cycle report; ``duration_ms`` comes from the
    injected clock so test runs reproduce it exactly."""

    cycle: int
    mode: str  # "idle" | "quick" | "full-scan" | "failed" | "degraded"
    changes: tuple[FileChange, ...] = ()
    files_proposed: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    files_deleted: int = 0
    nodes: int = 0
    edges: int = 0
    refs_parked: int = 0
    refs_resolved: int = 0
    refs_failed: int = 0
    duration_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "cycle": self.cycle,
            "mode": self.mode,
            "changes": [change.to_dict() for change in self.changes],
            "files_proposed": self.files_proposed,
            "files_skipped": self.files_skipped,
            "files_failed": self.files_failed,
            "files_deleted": self.files_deleted,
            "nodes": self.nodes,
            "edges": self.edges,
            "refs_parked": self.refs_parked,
            "refs_resolved": self.refs_resolved,
            "refs_failed": self.refs_failed,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass(frozen=True)
class WatchReport:
    """Aggregate of one :meth:`IngestionWatcher.run` invocation."""

    root: str
    status: str  # "completed" | "degraded"
    cycles: tuple[CycleReport, ...] = field(default_factory=tuple)
    consecutive_failures: int = 0
    total_changes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "status": self.status,
            "consecutive_failures": self.consecutive_failures,
            "total_changes": self.total_changes,
            "cycles": [cycle.to_dict() for cycle in self.cycles],
        }


# ------------------------------------------------------------------ snapshot


def scan_stat_snapshot(
    root: str | Path, *, exclude_realpaths: frozenset[str] = frozenset()
) -> dict[str, tuple[int, int]]:
    """Stat-only snapshot of the tree: ``relpath -> (size, mtime_ns)``.

    Pure ``os.walk`` + ``os.stat`` — no file bytes are read (the pipeline
    confirms real changes by sha256 itself). The same directory skips as the
    ingestion walk apply, plus explicit exclusions (the store database and
    its WAL/SHM sidecars, so a store inside the root never feeds back).
    Unreadable entries are skipped, never fatal.
    """
    base = Path(root)
    snapshot: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            abs_path = Path(dirpath) / name
            if os.path.realpath(abs_path) in exclude_realpaths:
                continue
            try:
                stat = os.stat(abs_path)
            except OSError:
                continue
            relpath = abs_path.relative_to(base).as_posix()
            snapshot[relpath] = (int(stat.st_size), int(stat.st_mtime_ns))
    return snapshot


def diff_snapshots(old: dict[str, tuple[int, int]], new: dict[str, tuple[int, int]]) -> tuple[FileChange, ...]:
    """Bytewise-ordered added/changed/removed diff of two stat snapshots."""
    changes: list[FileChange] = []
    for relpath in sorted(set(old) | set(new), key=str.encode):
        before, after = old.get(relpath), new.get(relpath)
        if before is None and after is not None:
            changes.append(FileChange(relpath=relpath, kind=ChangeKind.ADDED, size=after[0]))
        elif after is None and before is not None:
            changes.append(FileChange(relpath=relpath, kind=ChangeKind.REMOVED, size=None))
        elif before != after and after is not None:
            changes.append(FileChange(relpath=relpath, kind=ChangeKind.CHANGED, size=after[0]))
    return tuple(changes)


# -------------------------------------------------------------------- policy


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def _is_wsl_release(release: str) -> bool:
    lowered = release.lower()
    return any(marker in lowered for marker in _WSL_MARKERS)


def _remote_mount_for(resolved: Path, mounts_text: str) -> tuple[str, str] | None:
    """The (mount_point, fstype) of the longest remote mount covering the path."""
    best: tuple[str, str] | None = None
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        mount_point = fields[1].replace("\\040", " ")
        fstype = fields[2]
        if fstype not in REMOTE_FS_TYPES:
            continue
        posix = resolved.as_posix()
        if posix == mount_point or posix.startswith(mount_point.rstrip("/") + "/"):
            if best is None or len(mount_point) > len(best[0]):
                best = (mount_point, fstype)
    return best


def _read_proc_mounts() -> str | None:
    try:
        return Path("/proc/mounts").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def evaluate_watch_policy(
    path: str | Path,
    *,
    env: dict[str, str] | None = None,
    platform_release: str | None = None,
    mounts_text: str | None = None,
) -> WatchPolicyDecision:
    """Decide whether the watcher may run for *path* (codegraph watch-policy).

    Priority: ``KNOW_YOUR_SOUL_NO_WATCH`` > ``KNOW_YOUR_SOUL_FORCE_WATCH`` >
    WSL ``/mnt/<letter>`` heuristic > remote/virtual mount detection. All
    inputs injectable for deterministic tests.
    """
    environ = os.environ if env is None else env
    if _truthy(environ.get(ENV_NO_WATCH)):
        return WatchPolicyDecision(False, f"watching disabled by {ENV_NO_WATCH}", "env-no-watch")
    if _truthy(environ.get(ENV_FORCE_WATCH)):
        return WatchPolicyDecision(True, f"watching forced by {ENV_FORCE_WATCH}", "env-force")
    release = platform.release() if platform_release is None else platform_release
    resolved = Path(path).resolve()
    if _is_wsl_release(release):
        match = _WSL_MNT_RE.match(resolved.as_posix())
        if match is not None:
            return WatchPolicyDecision(
                False,
                f"watched path is on the WSL Windows mount /mnt/{match.group(1)}; polling there is too "
                f"slow — set {ENV_FORCE_WATCH}=1 to override or run `know-your-soul ingest` manually",
                "wsl",
            )
    text = _read_proc_mounts() if mounts_text is None else mounts_text
    if text:
        hit = _remote_mount_for(resolved, text)
        if hit is not None:
            mount_point, fstype = hit
            return WatchPolicyDecision(
                False,
                f"watched path is on remote/virtual filesystem {fstype} mounted at {mount_point}; "
                f"polling disabled — set {ENV_FORCE_WATCH}=1 to override",
                "remote-mount",
            )
    return WatchPolicyDecision(True, "local filesystem", "default")


# ------------------------------------------------------------------- watcher


class IngestionWatcher:
    """Polls a root on an interval and drives incremental ingestion.

    ``pipeline`` is anything implementing the ``IngestionPipeline``
    ``ingest_path(path, *, incremental, recursive, resolve)`` protocol.
    ``clock``/``sleep``/``logger`` are injectable; the defaults are
    :func:`time.monotonic`, :func:`time.sleep` and the module logger.
    """

    def __init__(
        self,
        root: str | Path,
        pipeline: Any,
        *,
        config: WatcherConfig | None = None,
        clock: Any = None,
        sleep: Any = None,
        logger: Any = None,
        exclude_realpaths: frozenset[str] | None = None,
    ) -> None:
        resolved = Path(root).resolve()
        if not resolved.is_dir():
            raise WatcherRootError(f"watched root {str(root)!r} is not an existing directory")
        self._root = resolved
        self._pipeline = pipeline
        self._config = config if config is not None else WatcherConfig()
        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._logger = logger if logger is not None else _LOGGER
        if exclude_realpaths is None:
            exclude_realpaths = frozenset()
            db_path = getattr(getattr(pipeline, "_store", None), "db_path", None)
            if db_path and db_path != ":memory:":
                real = os.path.realpath(db_path)
                exclude_realpaths = frozenset({real, real + "-wal", real + "-shm"})
        self._exclude = exclude_realpaths
        self._baseline: dict[str, tuple[int, int]] = {}
        self._cycles = 0
        self._consecutive_failures = 0
        self._degraded = False

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def config(self) -> WatcherConfig:
        return self._config

    def scan(self) -> dict[str, tuple[int, int]]:
        return scan_stat_snapshot(self._root, exclude_realpaths=self._exclude)

    # ------------------------------------------------------------- one cycle

    def poll_once(self) -> CycleReport:
        """One scan→diff→(debounce)→ingest cycle; failures never raise."""
        self._cycles += 1
        started = self._clock()
        try:
            current = self.scan()
            changes = diff_snapshots(self._baseline, current)
            if not changes:
                self._baseline = current
                self._consecutive_failures = 0
                return CycleReport(cycle=self._cycles, mode="idle", duration_ms=self._elapsed_ms(started))
            changes, current = self._settle(changes, current)
            report = self._apply(changes, current, started)
        except Exception as error:  # noqa: BLE001 - the watcher degrades, never crashes
            report = self._fail(started, error)
        return report

    def _settle(
        self, changes: tuple[FileChange, ...], current: dict[str, tuple[int, int]]
    ) -> tuple[tuple[FileChange, ...], dict[str, tuple[int, int]]]:
        """Quiet-window coalescing: re-scan until the change set is stable,
        bounded by ``max_quiet_rounds`` so churn can never starve ingestion."""
        rounds = 0
        while rounds < self._config.max_quiet_rounds:
            quiet = self._quiet_for(changes)
            if quiet <= 0:
                break
            self._sleep(quiet)
            current = self.scan()
            settled = diff_snapshots(self._baseline, current)
            rounds += 1
            if settled == changes:
                break
            changes = settled
        return changes, current

    def _quiet_for(self, changes: tuple[FileChange, ...]) -> float:
        if len(changes) <= self._config.quick_max_changes:
            return float(self._config.quick_quiet_seconds)
        return float(self._config.debounce_seconds)

    def _apply(
        self, changes: tuple[FileChange, ...], current: dict[str, tuple[int, int]], started: float
    ) -> CycleReport:
        has_removal = any(change.kind is ChangeKind.REMOVED for change in changes)
        if has_removal or len(changes) > self._config.full_scan_ceiling:
            mode = "full-scan"
            reports = [self._pipeline.ingest_path(self._root, incremental=True, recursive=True, resolve=True)]
        else:
            mode = "quick"
            reports = [
                self._pipeline.ingest_path(self._root / change.relpath, incremental=True, recursive=True, resolve=True)
                for change in changes
            ]
        counts = self._aggregate(reports)
        self._baseline = current
        self._consecutive_failures = 0
        self._logger.info(
            f"watch cycle {self._cycles} {mode}: {len(changes)} change(s), "
            f"proposed {counts['files_proposed']}, skipped {counts['files_skipped']}, "
            f"failed {counts['files_failed']}, deleted {counts['files_deleted']}"
        )
        return CycleReport(
            cycle=self._cycles,
            mode=mode,
            changes=changes,
            files_proposed=counts["files_proposed"],
            files_skipped=counts["files_skipped"],
            files_failed=counts["files_failed"],
            files_deleted=counts["files_deleted"],
            nodes=counts["nodes"],
            edges=counts["edges"],
            refs_parked=counts["refs_parked"],
            refs_resolved=counts["refs_resolved"],
            refs_failed=counts["refs_failed"],
            duration_ms=self._elapsed_ms(started),
        )

    @staticmethod
    def _aggregate(reports: list[Any]) -> dict[str, int]:
        counts = {
            "files_proposed": 0,
            "files_skipped": 0,
            "files_failed": 0,
            "files_deleted": 0,
            "nodes": 0,
            "edges": 0,
            "refs_parked": 0,
            "refs_resolved": 0,
            "refs_failed": 0,
        }
        for report in reports:
            counts["files_proposed"] += int(report.stats["proposed"])
            counts["files_skipped"] += int(report.stats["skipped"])
            counts["files_failed"] += int(report.stats["failed"])
            counts["files_deleted"] += int(report.stats["deleted"])
            counts["nodes"] += int(report.totals["nodes"])
            counts["edges"] += int(report.totals["edges"])
            counts["refs_parked"] += int(report.totals["refs_parked"])
            resolution = report.resolution
            if resolution is not None:
                counts["refs_resolved"] += int(resolution["resolved"])
                counts["refs_failed"] += int(resolution["failed"])
        return counts

    def _fail(self, started: float, error: Exception) -> CycleReport:
        self._consecutive_failures += 1
        exhausted = self._consecutive_failures >= self._config.max_consecutive_failures
        if exhausted:
            self._degraded = True
            self._logger.error(
                f"watcher auto-degraded after {self._consecutive_failures} consecutive failures "
                f"(last: {error}); stopped — investigate and restart `know-your-soul watch`"
            )
        else:
            self._logger.warning(
                f"watch cycle {self._cycles} failed ({self._consecutive_failures}/"
                f"{self._config.max_consecutive_failures}): {error}"
            )
        return CycleReport(
            cycle=self._cycles,
            mode="degraded" if exhausted else "failed",
            error=f"{type(error).__name__}: {error}",
            duration_ms=self._elapsed_ms(started),
        )

    def _elapsed_ms(self, started: float) -> int:
        return int((self._clock() - started) * 1000)

    # -------------------------------------------------------------- the loop

    def run(self, *, once: bool = False, max_cycles: int | None = None) -> WatchReport:
        """Run the poll loop. Never raises for scan/ingest failures: after
        ``max_consecutive_failures`` in a row it degrades and stops. The
        interval sleep runs between every two cycles (failure cycles
        included) — no busy-loop."""
        if self._degraded:
            return WatchReport(
                root=str(self._root),
                status="degraded",
                consecutive_failures=self._consecutive_failures,
            )
        cycles: list[CycleReport] = []
        while True:
            cycles.append(self.poll_once())
            if once or self._degraded or (max_cycles is not None and len(cycles) >= max_cycles):
                break
            self._sleep(float(self._config.interval_seconds))
        return WatchReport(
            root=str(self._root),
            status="degraded" if self._degraded else "completed",
            cycles=tuple(cycles),
            consecutive_failures=self._consecutive_failures,
            total_changes=sum(len(cycle.changes) for cycle in cycles),
        )

    def run_once(self) -> WatchReport:
        """A single cycle — the ``--once`` (test/CI) lane."""
        return self.run(once=True)
