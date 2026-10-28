"""Unit tests for the Wave-4 polling ingestion watcher (SUB-92).

Pure-stdlib polling watcher: ``os.stat`` scans (no inotify), configurable
debounce, quick-path for small change sets, full scan-diff above the
ceiling, auto-degrade after K consecutive failures, and WSL/remote-mount
detection. Clock and sleep are injectable so every test is deterministic
and no test ever sleeps for real.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from know_your_soul.ingest.watcher import (
    DEFAULT_DEBOUNCE_SECONDS,
    DEFAULT_FULL_SCAN_CEILING,
    DEFAULT_INTERVAL_SECONDS,
    DEFAULT_MAX_CONSECUTIVE_FAILURES,
    DEFAULT_MAX_QUIET_ROUNDS,
    DEFAULT_QUICK_MAX_CHANGES,
    DEFAULT_QUICK_QUIET_SECONDS,
    ENV_FORCE_WATCH,
    ENV_NO_WATCH,
    MIN_INTERVAL_SECONDS,
    REMOTE_FS_TYPES,
    ChangeKind,
    FileChange,
    IngestionWatcher,
    WatcherConfig,
    WatcherRootError,
    WatchPolicyDecision,
    diff_snapshots,
    evaluate_watch_policy,
    scan_stat_snapshot,
)

# ------------------------------------------------------------------ helpers


class FakeClock:
    """Monotonic clock advancing a fixed step per read."""

    def __init__(self, step: float = 0.001) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


class FakeSleep:
    """Records sleep durations; optionally runs hooks between sleeps."""

    def __init__(self, hooks: list | None = None) -> None:
        self.calls: list[float] = []
        self._hooks = list(hooks or [])

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        if self._hooks:
            hook = self._hooks.pop(0)
            if hook is not None:
                hook()  # type: ignore[operator]


class ListLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, str]] = []

    def info(self, msg: str) -> None:
        self.records.append(("info", msg))

    def warning(self, msg: str) -> None:
        self.records.append(("warning", msg))

    def error(self, msg: str) -> None:
        self.records.append(("error", msg))


def fake_report(
    scanned=1, proposed=1, skipped=0, failed=0, deleted=0, nodes=2, edges=1, refs=0, resolved=0, refs_failed=0
):
    return SimpleNamespace(
        stats={"scanned": scanned, "proposed": proposed, "skipped": skipped, "failed": failed, "deleted": deleted},
        totals={"nodes": nodes, "edges": edges, "refs_parked": refs},
        resolution={"resolved": resolved, "failed": refs_failed, "pending": 0},
        resolution_status="ok",
    )


class SpyPipeline:
    """Records ingest_path calls and replays scripted outcomes."""

    def __init__(self, outcomes: list | None = None) -> None:
        self.calls: list[str] = []
        self._outcomes = list(outcomes or [])

    def ingest_path(self, path, *, incremental=True, recursive=True, resolve=True):
        self.calls.append(str(path))
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        return fake_report()


def fast_config(**overrides) -> WatcherConfig:
    """Zero-debounce config: no quiet sleeps unless a test opts in."""
    base: dict[str, Any] = dict(
        interval_seconds=0.05,
        debounce_seconds=0.0,
        quick_quiet_seconds=0.0,
    )
    base.update(overrides)
    return WatcherConfig(**base)


def make_watcher(root, pipeline, *, config=None, sleep=None, logger=None, clock=None) -> IngestionWatcher:
    return IngestionWatcher(
        root,
        pipeline,
        config=config if config is not None else fast_config(),
        clock=clock if clock is not None else FakeClock(),
        sleep=sleep if sleep is not None else FakeSleep(),
        logger=logger if logger is not None else ListLogger(),
    )


# ------------------------------------------------------------------- config


class TestWatcherConfig:
    def test_defaults_match_codegraph_knobs(self):
        config = WatcherConfig()
        assert config.interval_seconds == DEFAULT_INTERVAL_SECONDS
        assert config.debounce_seconds == DEFAULT_DEBOUNCE_SECONDS
        assert config.quick_quiet_seconds == DEFAULT_QUICK_QUIET_SECONDS
        assert config.quick_max_changes == DEFAULT_QUICK_MAX_CHANGES
        assert config.full_scan_ceiling == DEFAULT_FULL_SCAN_CEILING
        assert config.max_consecutive_failures == DEFAULT_MAX_CONSECUTIVE_FAILURES
        assert config.max_quiet_rounds == DEFAULT_MAX_QUIET_ROUNDS
        assert DEFAULT_DEBOUNCE_SECONDS == 2.0
        assert DEFAULT_QUICK_QUIET_SECONDS == 0.3
        assert DEFAULT_QUICK_MAX_CHANGES == 2
        assert DEFAULT_FULL_SCAN_CEILING == 500
        assert DEFAULT_MAX_CONSECUTIVE_FAILURES == 5

    def test_interval_floor_blocks_busy_loop(self):
        with pytest.raises(ValueError, match="interval_seconds"):
            WatcherConfig(interval_seconds=MIN_INTERVAL_SECONDS / 2)
        with pytest.raises(ValueError, match="interval_seconds"):
            WatcherConfig(interval_seconds=0.0)
        with pytest.raises(ValueError, match="interval_seconds"):
            WatcherConfig(interval_seconds=-1.0)

    def test_interval_floor_boundary_ok(self):
        assert WatcherConfig(interval_seconds=MIN_INTERVAL_SECONDS).interval_seconds == MIN_INTERVAL_SECONDS

    @pytest.mark.parametrize("field", ["debounce_seconds", "quick_quiet_seconds"])
    def test_quiet_fields_allow_zero_but_not_negative(self, field):
        zero: dict[str, Any] = {field: 0.0}
        negative: dict[str, Any] = {field: -0.1}
        assert WatcherConfig(**zero) is not None
        with pytest.raises(ValueError, match=field):
            WatcherConfig(**negative)

    @pytest.mark.parametrize(
        "field",
        ["quick_max_changes", "full_scan_ceiling", "max_consecutive_failures", "max_quiet_rounds"],
    )
    def test_int_fields_must_be_positive(self, field):
        zero: dict[str, Any] = {field: 0}
        negative: dict[str, Any] = {field: -3}
        with pytest.raises(ValueError, match=field):
            WatcherConfig(**zero)
        with pytest.raises(ValueError, match=field):
            WatcherConfig(**negative)

    @pytest.mark.parametrize("field", ["quick_max_changes", "max_consecutive_failures"])
    def test_int_fields_reject_bool(self, field):
        invalid: dict[str, Any] = {field: True}
        with pytest.raises(ValueError, match=field):
            WatcherConfig(**invalid)

    def test_frozen(self):
        import dataclasses

        config = WatcherConfig()
        with pytest.raises(dataclasses.FrozenInstanceError):
            config.interval_seconds = 9.0  # type: ignore[misc]


# ------------------------------------------------------------------ scanning


class TestScanStatSnapshot:
    def test_empty_root(self, tmp_path):
        assert scan_stat_snapshot(tmp_path) == {}

    def test_files_recorded_with_size_and_mtime_ns(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes(b"def a():\n    pass\n")
        snap = scan_stat_snapshot(tmp_path)
        assert set(snap) == {"a.py"}
        size, mtime_ns = snap["a.py"]
        stat = target.stat()
        assert size == stat.st_size
        assert mtime_ns == stat.st_mtime_ns

    def test_nested_relpaths_posix(self, tmp_path):
        (tmp_path / "sub" / "deep").mkdir(parents=True)
        (tmp_path / "sub" / "deep" / "x.py").write_bytes(b"x")
        assert set(scan_stat_snapshot(tmp_path)) == {"sub/deep/x.py"}

    @pytest.mark.parametrize("skip", [".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build"])
    def test_skip_dirs(self, tmp_path, skip):
        skipped = tmp_path / skip
        skipped.mkdir()
        (skipped / "junk.py").write_bytes(b"x")
        (tmp_path / "real.py").write_bytes(b"x")
        assert set(scan_stat_snapshot(tmp_path)) == {"real.py"}

    def test_excluded_realpaths_never_scanned(self, tmp_path):
        db = tmp_path / "soul.sqlite3"
        db.write_bytes(b"db")
        (tmp_path / "a.py").write_bytes(b"x")
        excluded = frozenset({os.path.realpath(db), os.path.realpath(db) + "-wal", os.path.realpath(db) + "-shm"})
        assert set(scan_stat_snapshot(tmp_path, exclude_realpaths=excluded)) == {"a.py"}

    def test_unreadable_stat_skipped(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_bytes(b"x")
        (tmp_path / "b.py").write_bytes(b"x")
        original = os.stat

        def guarded(path, *args, **kwargs):
            if str(path).endswith("b.py") and "dir_fd" not in kwargs:
                raise OSError("denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", guarded)
        assert set(scan_stat_snapshot(tmp_path)) == {"a.py"}

    def test_deterministic_across_repeats(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        (tmp_path / "b.py").write_bytes(b"yy")
        assert scan_stat_snapshot(tmp_path) == scan_stat_snapshot(tmp_path)


class TestDiffSnapshots:
    def test_added(self):
        changes = diff_snapshots({}, {"a.py": (1, 100)})
        assert changes == (FileChange(relpath="a.py", kind=ChangeKind.ADDED, size=1),)

    def test_removed(self):
        changes = diff_snapshots({"a.py": (1, 100)}, {})
        assert changes == (FileChange(relpath="a.py", kind=ChangeKind.REMOVED, size=None),)

    def test_changed_on_size(self):
        changes = diff_snapshots({"a.py": (1, 100)}, {"a.py": (2, 100)})
        assert changes == (FileChange(relpath="a.py", kind=ChangeKind.CHANGED, size=2),)

    def test_changed_on_mtime_only(self):
        changes = diff_snapshots({"a.py": (1, 100)}, {"a.py": (1, 200)})
        assert changes == (FileChange(relpath="a.py", kind=ChangeKind.CHANGED, size=1),)

    def test_unchanged(self):
        assert diff_snapshots({"a.py": (1, 100)}, {"a.py": (1, 100)}) == ()

    def test_bytewise_order(self):
        old = {"b.py": (1, 1), "A.py": (1, 1), "a.py": (1, 1)}
        new = {"b.py": (2, 1), "A.py": (2, 1), "c.py": (1, 1)}
        order = [c.relpath for c in diff_snapshots(old, new)]
        assert order == sorted({"A.py", "a.py", "b.py", "c.py"}, key=str.encode)

    def test_mixed_kinds(self):
        changes = diff_snapshots(
            {"gone.py": (1, 1), "same.py": (1, 1), "mod.py": (1, 1)},
            {"same.py": (1, 1), "mod.py": (9, 1), "new.py": (3, 3)},
        )
        by_rel = {c.relpath: c for c in changes}
        assert by_rel["gone.py"].kind is ChangeKind.REMOVED
        assert by_rel["mod.py"].kind is ChangeKind.CHANGED
        assert by_rel["new.py"].kind is ChangeKind.ADDED
        assert "same.py" not in by_rel


# ------------------------------------------------------------------- policy


MOUNTS = (
    "sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0\n"
    "/dev/sda1 / ext4 rw,relatime 0 0\n"
    "server:/share /mnt/data nfs4 rw,relatime 0 0\n"
    "//server/share /mnt/win cifs rw 0 0\n"
    "C:\\134 /mnt/c drvfs rw,noatime 0 0\n"
    "gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse rw 0 0\n"
    "host /home/user/My\\040Share sshfs rw 0 0\n"
)


class TestWatchPolicy:
    def test_default_enabled_local(self, tmp_path):
        decision = evaluate_watch_policy(tmp_path, env={}, platform_release="6.1.0-generic", mounts_text=MOUNTS)
        assert decision.enabled is True
        assert decision.source == "default"

    def test_no_watch_env_disables(self, tmp_path):
        decision = evaluate_watch_policy(
            tmp_path, env={ENV_NO_WATCH: "1"}, platform_release="6.1.0-generic", mounts_text=MOUNTS
        )
        assert decision.enabled is False
        assert decision.source == "env-no-watch"
        assert ENV_NO_WATCH in decision.reason

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
    def test_no_watch_truthy_values(self, tmp_path, value):
        assert (
            evaluate_watch_policy(tmp_path, env={ENV_NO_WATCH: value}, platform_release="x", mounts_text="").enabled
            is False
        )

    def test_no_watch_empty_value_ignored(self, tmp_path):
        assert (
            evaluate_watch_policy(tmp_path, env={ENV_NO_WATCH: ""}, platform_release="x", mounts_text="").enabled
            is True
        )

    def test_no_watch_beats_force(self, tmp_path):
        decision = evaluate_watch_policy(
            tmp_path, env={ENV_NO_WATCH: "1", ENV_FORCE_WATCH: "1"}, platform_release="x", mounts_text=""
        )
        assert decision.enabled is False
        assert decision.source == "env-no-watch"

    def test_force_overrides_wsl(self):
        decision = evaluate_watch_policy(
            "/mnt/c/project",
            env={ENV_FORCE_WATCH: "1"},
            platform_release="5.15.90.1-microsoft-standard-WSL2",
            mounts_text=MOUNTS,
        )
        assert decision.enabled is True
        assert decision.source == "env-force"

    @pytest.mark.parametrize(
        "release", ["5.15.90.1-microsoft-standard-WSL2", "6.6.87.2-microsoft-custom", "5.10.16.3-WSL2"]
    )
    def test_wsl_windows_mount_off(self, release):
        decision = evaluate_watch_policy("/mnt/c/Users/dev/repo", env={}, platform_release=release, mounts_text=MOUNTS)
        assert decision.enabled is False
        assert decision.source == "wsl"
        assert "/mnt/c" in decision.reason

    def test_wsl_native_fs_stays_on(self):
        decision = evaluate_watch_policy(
            "/home/dev/repo", env={}, platform_release="5.15.90.1-microsoft-standard-WSL2", mounts_text=MOUNTS
        )
        assert decision.enabled is True

    def test_non_wsl_mnt_path_stays_on(self, tmp_path):
        decision = evaluate_watch_policy(
            "/mnt/c/looks-like-but-not-wsl", env={}, platform_release="6.1.0-generic", mounts_text=""
        )
        assert decision.source != "wsl"

    def test_remote_mount_nfs_off(self):
        decision = evaluate_watch_policy(
            "/mnt/data/project", env={}, platform_release="6.1.0-generic", mounts_text=MOUNTS
        )
        assert decision.enabled is False
        assert decision.source == "remote-mount"
        assert "nfs4" in decision.reason

    def test_remote_mount_cifs_off(self):
        decision = evaluate_watch_policy("/mnt/win/docs", env={}, platform_release="6.1.0-generic", mounts_text=MOUNTS)
        assert decision.enabled is False
        assert decision.source == "remote-mount"

    def test_remote_mount_exact_mount_point(self):
        decision = evaluate_watch_policy("/mnt/data", env={}, platform_release="6.1.0-generic", mounts_text=MOUNTS)
        assert decision.enabled is False

    def test_longest_prefix_wins(self):
        mounts = "/dev/sda1 /mnt/data ext4 rw 0 0\nserver:/s /mnt/data/sub nfs rw 0 0\n"
        decision = evaluate_watch_policy(
            "/mnt/data/sub/deep", env={}, platform_release="6.1.0-generic", mounts_text=mounts
        )
        assert decision.enabled is False
        assert "nfs" in decision.reason

    def test_escaped_space_mount_point(self):
        decision = evaluate_watch_policy(
            "/home/user/My Share/docs", env={}, platform_release="6.1.0-generic", mounts_text=MOUNTS
        )
        assert decision.enabled is False
        assert "sshfs" in decision.reason

    def test_unrelated_remote_mount_not_blocking(self):
        decision = evaluate_watch_policy("/home/dev/repo", env={}, platform_release="6.1.0-generic", mounts_text=MOUNTS)
        assert decision.enabled is True

    def test_remote_fstypes_table(self):
        for fstype in ("nfs", "nfs4", "cifs", "smbfs", "sshfs", "9p", "drvfs"):
            assert fstype in REMOTE_FS_TYPES
        assert "ext4" not in REMOTE_FS_TYPES

    def test_decision_shape(self, tmp_path):
        decision = evaluate_watch_policy(tmp_path, env={}, platform_release="x", mounts_text="")
        assert isinstance(decision, WatchPolicyDecision)
        payload = decision.to_dict()
        assert payload == {"enabled": True, "reason": payload["reason"], "source": "default"}


# -------------------------------------------------------------- one cycle


class TestPollOnce:
    def test_first_cycle_ingests_existing_files(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy)
        report = watcher.poll_once()
        assert report.mode == "quick"
        assert [c.relpath for c in report.changes] == ["a.py"]
        assert report.changes[0].kind is ChangeKind.ADDED
        assert spy.calls == [str(tmp_path / "a.py")]
        assert report.files_proposed == 1

    def test_empty_root_first_cycle_idle(self, tmp_path):
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy)
        report = watcher.poll_once()
        assert report.mode == "idle"
        assert report.changes == ()
        assert spy.calls == []

    def test_no_change_second_cycle_idle(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy)
        watcher.poll_once()
        spy.calls.clear()
        assert watcher.poll_once().mode == "idle"
        assert spy.calls == []

    def test_modified_file_quick_path(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy)
        watcher.poll_once()
        spy.calls.clear()
        target.write_bytes(b"xxxxx")
        report = watcher.poll_once()
        assert report.mode == "quick"
        assert report.changes[0].kind is ChangeKind.CHANGED
        assert spy.calls == [str(tmp_path / "a.py")]

    def test_removed_file_forces_full_scan(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy)
        watcher.poll_once()
        spy.calls.clear()
        target.unlink()
        report = watcher.poll_once()
        assert report.mode == "full-scan"
        assert report.changes[0].kind is ChangeKind.REMOVED
        assert spy.calls == [str(tmp_path)]

    def test_removal_within_quick_limit_still_full_scan(self, tmp_path):
        """Even a single removal takes the scan-diff lane (deletes are cascade work)."""
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy, config=fast_config(quick_max_changes=10, full_scan_ceiling=100))
        watcher.poll_once()
        (tmp_path / "a.py").unlink()
        assert watcher.poll_once().mode == "full-scan"

    def test_ceiling_forces_full_scan(self, tmp_path):
        for index in range(5):
            (tmp_path / f"f{index}.py").write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy, config=fast_config(full_scan_ceiling=3))
        report = watcher.poll_once()
        assert report.mode == "full-scan"
        assert len(report.changes) == 5
        assert spy.calls == [str(tmp_path)]

    def test_at_ceiling_boundary_stays_quick(self, tmp_path):
        for index in range(3):
            (tmp_path / f"f{index}.py").write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy, config=fast_config(full_scan_ceiling=3))
        report = watcher.poll_once()
        assert report.mode == "quick"
        assert len(spy.calls) == 3

    def test_quick_path_ingests_in_bytewise_order(self, tmp_path):
        for name in ("b.py", "A.py", "a.py"):
            (tmp_path / name).write_bytes(b"x")
        spy = SpyPipeline()
        make_watcher(tmp_path, spy).poll_once()
        expected = sorted(("A.py", "a.py", "b.py"), key=str.encode)
        assert spy.calls == [str(tmp_path / name) for name in expected]

    def test_cycle_report_counts_aggregated(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        (tmp_path / "b.py").write_bytes(b"y")
        spy = SpyPipeline(
            outcomes=[fake_report(nodes=3, edges=2, resolved=1), fake_report(nodes=5, edges=4, refs_failed=1)]
        )
        report = make_watcher(tmp_path, spy).poll_once()
        assert report.files_proposed == 2
        assert report.nodes == 8
        assert report.edges == 6
        assert report.refs_resolved == 1
        assert report.refs_failed == 1

    def test_full_scan_counts_from_report(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy)
        watcher.poll_once()
        (tmp_path / "a.py").unlink()
        spy.calls.clear()
        spy._outcomes.append(fake_report(scanned=0, proposed=0, deleted=1, nodes=0, edges=0))
        report = watcher.poll_once()
        assert report.mode == "full-scan"
        assert report.files_deleted == 1

    def test_cycle_numbers_increase(self, tmp_path):
        watcher = make_watcher(tmp_path, SpyPipeline())
        assert watcher.poll_once().cycle == 1
        assert watcher.poll_once().cycle == 2

    def test_duration_uses_injected_clock(self, tmp_path):
        watcher = make_watcher(tmp_path, SpyPipeline(), clock=FakeClock(step=0.01))
        report = watcher.poll_once()
        assert report.duration_ms == 10  # exactly two clock reads * 10 ms

    def test_cycle_report_to_dict_deterministic(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")

        def run_once():
            spy = SpyPipeline()
            watcher = make_watcher(tmp_path, spy)
            return watcher.poll_once().to_dict()

        first, second = run_once(), run_once()
        assert first == second
        assert list(first["changes"][0]) == ["relpath", "kind", "size"]


# ----------------------------------------------------------------- debounce


class TestDebounce:
    def test_burst_coalesces_into_one_cycle(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        sleep = FakeSleep(hooks=[lambda: (tmp_path / "b.py").write_bytes(b"y")])
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy, config=fast_config(quick_quiet_seconds=0.3), sleep=sleep)
        report = watcher.poll_once()
        assert sleep.calls[:1] == [0.3]  # quick quiet window
        assert sorted(c.relpath for c in report.changes) == ["a.py", "b.py"]
        assert sorted(Path(c).name for c in spy.calls) == ["a.py", "b.py"]
        assert report.mode == "quick"

    def test_quiet_choice_quick_vs_full(self, tmp_path):
        """≤ quick_max_changes pending -> quick quiet; more -> full debounce."""
        for index in range(4):
            (tmp_path / f"f{index}.py").write_bytes(b"x")
        sleep = FakeSleep()
        watcher = make_watcher(
            tmp_path,
            SpyPipeline(),
            config=fast_config(
                debounce_seconds=2.0, quick_quiet_seconds=0.3, quick_max_changes=2, full_scan_ceiling=50
            ),
            sleep=sleep,
        )
        watcher.poll_once()
        assert sleep.calls[:1] == [2.0]  # 4 pending > 2 -> full debounce, not quick quiet

    def test_escalation_during_quiet_window(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")

        def add_more():
            for index in range(3):
                (tmp_path / f"n{index}.py").write_bytes(b"x")

        sleep = FakeSleep(hooks=[add_more])
        watcher = make_watcher(
            tmp_path,
            SpyPipeline(),
            config=fast_config(
                debounce_seconds=2.0, quick_quiet_seconds=0.3, quick_max_changes=2, full_scan_ceiling=50
            ),
            sleep=sleep,
        )
        report = watcher.poll_once()
        # First quiet is the quick window; after the burst grows past the
        # quick limit the next quiet is the full debounce.
        assert sleep.calls[:2] == [0.3, 2.0]
        assert len(report.changes) == 4

    def test_stable_after_first_quiet(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        sleep = FakeSleep()
        make_watcher(tmp_path, SpyPipeline(), config=fast_config(quick_quiet_seconds=0.3), sleep=sleep).poll_once()
        assert sleep.calls == [0.3]  # one quiet probe, no churn -> done

    def test_constant_churn_bounded_by_max_quiet_rounds(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        counter = {"n": 0}

        def churn():
            counter["n"] += 1
            (tmp_path / f"churn{counter['n']}.py").write_bytes(b"x")

        hooks = [churn] * 10
        sleep = FakeSleep(hooks=hooks)
        watcher = make_watcher(
            tmp_path,
            SpyPipeline(),
            config=fast_config(
                quick_quiet_seconds=0.3,
                debounce_seconds=2.0,
                quick_max_changes=100,
                full_scan_ceiling=1000,
                max_quiet_rounds=3,
            ),
            sleep=sleep,
        )
        report = watcher.poll_once()
        assert sleep.calls == [0.3] * 3  # bounded re-probes, then proceeds
        assert report.mode == "quick"
        assert len(report.changes) == 1 + 3  # a.py + 3 churn files


# --------------------------------------------------------------- degrade


class TestAutoDegrade:
    def test_failures_then_degrade_stops_without_raising(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline(outcomes=[RuntimeError("boom")] * 10)
        logger = ListLogger()
        sleep = FakeSleep()
        watcher = make_watcher(
            tmp_path, spy, config=fast_config(max_consecutive_failures=3), sleep=sleep, logger=logger
        )
        report = watcher.run(max_cycles=10)
        assert report.status == "degraded"
        assert watcher.degraded is True
        assert [c.mode for c in report.cycles] == ["failed", "failed", "degraded"]
        assert report.cycles[-1].error is not None and "boom" in report.cycles[-1].error
        warnings = [msg for level, msg in logger.records if level == "warning"]
        errors = [msg for level, msg in logger.records if level == "error"]
        assert len(warnings) == 2  # one per recoverable failed cycle
        assert len(errors) == 1
        assert "degraded" in errors[0]

    def test_failure_sleeps_interval_no_spin(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline(outcomes=[RuntimeError("boom")] * 5)
        sleep = FakeSleep()
        watcher = make_watcher(tmp_path, spy, config=fast_config(max_consecutive_failures=3), sleep=sleep)
        watcher.run(max_cycles=10)
        assert sleep.calls == [0.05, 0.05]  # interval sleep between the 3 cycles

    def test_success_resets_failure_counter(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        outcomes: list = [RuntimeError("x"), RuntimeError("y")] + [fake_report()] * 10
        spy = SpyPipeline(outcomes=outcomes)
        watcher = make_watcher(tmp_path, spy, config=fast_config(max_consecutive_failures=3))
        report = watcher.run(max_cycles=4)
        assert report.status == "completed"
        assert watcher.degraded is False
        assert [c.mode for c in report.cycles] == ["failed", "failed", "quick", "idle"]

    def test_failed_cycle_keeps_baseline_for_retry(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline(outcomes=[RuntimeError("boom"), fake_report()])
        watcher = make_watcher(tmp_path, spy, config=fast_config(max_consecutive_failures=5))
        first = watcher.poll_once()
        second = watcher.poll_once()
        assert first.mode == "failed"
        assert second.mode == "quick"
        assert [c.relpath for c in second.changes] == ["a.py"]

    def test_scan_failure_counts_toward_degrade(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline()
        watcher = make_watcher(tmp_path, spy, config=fast_config(max_consecutive_failures=2))
        monkeypatch.setattr(watcher, "scan", lambda: (_ for _ in ()).throw(OSError("fs gone")))
        report = watcher.run(max_cycles=5)
        assert report.status == "degraded"
        assert spy.calls == []
        assert [c.mode for c in report.cycles] == ["failed", "degraded"]

    def test_run_on_degraded_watcher_returns_immediately(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        spy = SpyPipeline(outcomes=[RuntimeError("boom")] * 5)
        watcher = make_watcher(tmp_path, spy, config=fast_config(max_consecutive_failures=1))
        first = watcher.run(max_cycles=5)
        assert first.status == "degraded"
        second = watcher.run(max_cycles=5)
        assert second.status == "degraded"
        assert second.cycles == ()


# -------------------------------------------------------------------- run


class TestRunLoop:
    def test_once_runs_exactly_one_cycle(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        sleep = FakeSleep()
        report = make_watcher(tmp_path, SpyPipeline(), sleep=sleep).run(once=True)
        assert report.status == "completed"
        assert len(report.cycles) == 1
        assert sleep.calls == []  # no trailing sleep in --once

    def test_max_cycles_bounds_loop(self, tmp_path):
        sleep = FakeSleep()
        report = make_watcher(tmp_path, SpyPipeline(), sleep=sleep).run(max_cycles=3)
        assert len(report.cycles) == 3
        assert sleep.calls == [0.05, 0.05]  # one interval sleep between cycles

    def test_interval_sleep_value(self, tmp_path):
        sleep = FakeSleep()
        make_watcher(tmp_path, SpyPipeline(), config=fast_config(interval_seconds=1.5), sleep=sleep).run(max_cycles=2)
        assert sleep.calls == [1.5]

    def test_run_once_alias(self, tmp_path):
        assert make_watcher(tmp_path, SpyPipeline()).run_once().cycles[0].cycle == 1

    def test_watch_report_shape(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")
        report = make_watcher(tmp_path, SpyPipeline()).run(once=True)
        payload = report.to_dict()
        assert payload["status"] == "completed"
        assert payload["root"] == str(Path(tmp_path).resolve())
        assert len(payload["cycles"]) == 1
        assert payload["total_changes"] == 1
        assert payload["consecutive_failures"] == 0

    def test_run_report_deterministic_two_runs(self, tmp_path):
        (tmp_path / "a.py").write_bytes(b"x")

        def run():
            return make_watcher(tmp_path, SpyPipeline()).run(max_cycles=3).to_dict()

        assert run() == run()


# ---------------------------------------------------------------- root guard


class TestRootGuard:
    def test_missing_root_rejected(self, tmp_path):
        with pytest.raises(WatcherRootError):
            IngestionWatcher(tmp_path / "nope", SpyPipeline())

    def test_file_root_rejected(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_bytes(b"x")
        with pytest.raises(WatcherRootError):
            IngestionWatcher(target, SpyPipeline())

    def test_degraded_property_starts_false(self, tmp_path):
        assert make_watcher(tmp_path, SpyPipeline()).degraded is False
