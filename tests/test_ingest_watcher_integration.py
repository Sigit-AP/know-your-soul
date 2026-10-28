"""Integration tests: watcher -> real incremental pipeline -> store v3 (SUB-92).

File writes/edits/deletes under the watched root must move the v3 graph
state (``files`` / ``symbols`` / ``graph_edges`` / ``unresolved_refs``) to
exactly the state a fresh ingest of the same tree produces, deterministically.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from _ingest_fakes import ToyExtractor, ToyResolver
from know_your_soul.ingest.pipeline import IngestionBudget, IngestionPipeline, canonical_ingestion_dump
from know_your_soul.ingest.watcher import (
    ChangeKind,
    IngestionWatcher,
    WatcherConfig,
)
from know_your_soul.store import EventStore

# ------------------------------------------------------------------ helpers

FIXED_EPOCH_NS = 1_700_000_000_000_000_000


def write_file(root: Path, rel: str, text: str, *, tick: int = 0) -> None:
    """Write a file with a deterministic mtime (wall clock never leaks in)."""
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    os.utime(target, ns=(FIXED_EPOCH_NS + tick * 1_000_000, FIXED_EPOCH_NS + tick * 1_000_000))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class FakeClock:
    def __init__(self, step: float = 0.001) -> None:
        self.t = 0.0
        self.step = step

    def __call__(self) -> float:
        self.t += self.step
        return self.t


def no_sleep(seconds: float) -> None:
    raise AssertionError(f"watcher slept {seconds}s in a zero-debounce integration test")


def make_watcher(root, store, *, config=None, sleep=no_sleep):
    pipeline = IngestionPipeline(store, extractor=ToyExtractor(), resolver=ToyResolver(), budget=IngestionBudget())
    return IngestionWatcher(
        root,
        pipeline,
        config=config
        if config is not None
        else WatcherConfig(interval_seconds=0.05, debounce_seconds=0.0, quick_quiet_seconds=0.0),
        clock=FakeClock(),
        sleep=sleep,
        logger=None,
    )


def symbol_names(store, relpath) -> list[str]:
    return sorted(row.name for row in store.symbols_in_path(relpath))


A_V1 = "def alpha():\n    pass\n\ndef beta():\n    pass\n"
A_V2 = "def alpha():\n    pass\n\ndef beta():\n    pass\n\ndef gamma():\n    pass\n"
B_V1 = "class Delta:\n    pass\n"


# ------------------------------------------------------- state transitions


class TestStateGraphTransitions:
    def test_initial_cycle_populates_v3_tables(self, tmp_path):
        write_file(tmp_path, "a.py", A_V1)
        write_file(tmp_path, "b.py", B_V1)
        with EventStore() as store:
            report = make_watcher(tmp_path, store).run(once=True)
            cycle = report.cycles[0]
            assert cycle.mode == "quick"
            assert {row.path for row in store.list_files()} == {"a.py", "b.py"}
            assert symbol_names(store, "a.py") == ["alpha", "beta"]
            assert symbol_names(store, "b.py") == ["Delta"]
            assert cycle.files_proposed == 2
            assert cycle.nodes == 3 + 2  # a.py: file+alpha+beta; b.py: file+Delta
            assert cycle.edges == 2 + 1
            tracked_a = store.get_file("a.py")
            assert tracked_a is not None
            assert tracked_a.sha256 == sha(A_V1)

    def test_modify_updates_symbols_and_hash(self, tmp_path):
        write_file(tmp_path, "a.py", A_V1)
        with EventStore() as store:
            watcher = make_watcher(tmp_path, store)
            watcher.poll_once()
            write_file(tmp_path, "a.py", A_V2, tick=1)
            cycle = watcher.poll_once()
            assert cycle.mode == "quick"
            assert [(c.relpath, c.kind) for c in cycle.changes] == [("a.py", ChangeKind.CHANGED)]
            assert symbol_names(store, "a.py") == ["alpha", "beta", "gamma"]
            tracked = store.get_file("a.py")
            assert tracked is not None
            assert tracked.sha256 == sha(A_V2)
            assert tracked.size == len(A_V2.encode())

    def test_delete_cascades_file_symbols_and_edges(self, tmp_path):
        write_file(tmp_path, "a.py", A_V1)
        write_file(tmp_path, "b.py", B_V1)
        with EventStore() as store:
            watcher = make_watcher(tmp_path, store)
            watcher.poll_once()
            with store.read() as conn:
                edges_before = conn.execute("SELECT COUNT(*) AS n FROM graph_edges").fetchone()["n"]
            assert edges_before == 3  # contains: file->alpha, file->beta, file->Delta
            os.unlink(tmp_path / "b.py")
            cycle = watcher.poll_once()
            assert cycle.mode == "full-scan"
            assert [(c.relpath, c.kind) for c in cycle.changes] == [("b.py", ChangeKind.REMOVED)]
            assert cycle.files_deleted == 1
            assert store.get_file("b.py") is None
            assert store.symbols_in_path("b.py") == []
            with store.read() as conn:
                edges_after = conn.execute("SELECT COUNT(*) AS n FROM graph_edges").fetchone()["n"]
            assert edges_after == 2  # only a.py's contains edges survive

    def test_add_modify_delete_mixed_cycle(self, tmp_path):
        write_file(tmp_path, "a.py", A_V1)
        write_file(tmp_path, "b.py", B_V1)
        with EventStore() as store:
            watcher = make_watcher(tmp_path, store)
            watcher.poll_once()
            write_file(tmp_path, "a.py", A_V2, tick=1)
            write_file(tmp_path, "c.py", "def epsilon():\n    pass\n", tick=2)
            os.unlink(tmp_path / "b.py")
            cycle = watcher.poll_once()
            assert cycle.mode == "full-scan"  # removal present -> scan-diff lane
            by_rel = {c.relpath: c.kind for c in cycle.changes}
            assert by_rel == {"a.py": ChangeKind.CHANGED, "b.py": ChangeKind.REMOVED, "c.py": ChangeKind.ADDED}
            assert symbol_names(store, "a.py") == ["alpha", "beta", "gamma"]
            assert store.get_file("b.py") is None
            assert symbol_names(store, "c.py") == ["epsilon"]

    def test_rename_is_delete_plus_add(self, tmp_path):
        write_file(tmp_path, "old.py", A_V1)
        with EventStore() as store:
            watcher = make_watcher(tmp_path, store)
            watcher.poll_once()
            os.rename(tmp_path / "old.py", tmp_path / "new.py")
            cycle = watcher.poll_once()
            by_rel = {c.relpath: c.kind for c in cycle.changes}
            assert by_rel == {"new.py": ChangeKind.ADDED, "old.py": ChangeKind.REMOVED}
            assert store.get_file("old.py") is None
            assert symbol_names(store, "new.py") == ["alpha", "beta"]

    def test_unsupported_file_reported_not_crashing(self, tmp_path):
        write_file(tmp_path, "a.py", A_V1)
        write_file(tmp_path, "data.bin", "binary")
        with EventStore() as store:
            cycle = make_watcher(tmp_path, store).run(once=True).cycles[0]
            assert cycle.files_proposed == 1
            assert cycle.files_failed == 1  # data.bin: UNSUPPORTED_MEDIA_TYPE
            assert {row.path for row in store.list_files()} == {"a.py"}

    def test_unresolved_ref_resolves_across_cycles(self, tmp_path):
        write_file(tmp_path, "a.py", "def caller():\n    -> helper\n")
        with EventStore() as store:
            watcher = make_watcher(tmp_path, store)
            watcher.poll_once()
            assert len(store.pending_unresolved_refs()) == 0
            assert len(store.failed_unresolved_refs(name_tail="helper")) == 1
            write_file(tmp_path, "b.py", "def helper():\n    pass\n", tick=1)
            watcher.poll_once()
            assert store.failed_unresolved_refs(name_tail="helper") == []
            with store.read() as conn:
                edges = conn.execute("SELECT kind FROM graph_edges WHERE kind = 'calls'").fetchall()
            assert len(edges) == 1


# ------------------------------------------------------------- convergence


class TestIncrementalConvergence:
    def _scenario(self, root: Path, store, *, via_watcher: bool) -> None:
        write_file(root, "a.py", A_V1)
        write_file(root, "b.py", B_V1)
        pipeline = IngestionPipeline(store, extractor=ToyExtractor(), resolver=ToyResolver())
        if via_watcher:
            watcher = IngestionWatcher(
                root,
                pipeline,
                config=WatcherConfig(interval_seconds=0.05, debounce_seconds=0.0, quick_quiet_seconds=0.0),
                clock=FakeClock(),
                sleep=no_sleep,
            )
            watcher.poll_once()
            write_file(root, "a.py", A_V2, tick=1)
            write_file(root, "c.py", "def epsilon():\n    pass\n", tick=2)
            os.unlink(root / "b.py")
            watcher.poll_once()
        else:
            # Fresh ingest of the final tree only.
            write_file(root, "a.py", A_V2, tick=1)
            write_file(root, "c.py", "def epsilon():\n    pass\n", tick=2)
            os.unlink(root / "b.py")
            pipeline.ingest_path(root)

    def test_watcher_history_matches_fresh_ingest(self, tmp_path):
        root_a, root_b = tmp_path / "ra", tmp_path / "rb"
        root_a.mkdir()
        root_b.mkdir()
        with EventStore() as store_a, EventStore() as store_b:
            self._scenario(root_a, store_a, via_watcher=True)
            self._scenario(root_b, store_b, via_watcher=False)
            assert canonical_ingestion_dump(store_a, include_ids=False) == canonical_ingestion_dump(
                store_b, include_ids=False
            )

    def test_byte_identical_two_watcher_runs(self, tmp_path):
        dumps = []
        reports = []
        for name in ("r1", "r2"):
            root = tmp_path / name
            root.mkdir()
            write_file(root, "a.py", A_V1)
            write_file(root, "b.py", B_V1)
            with EventStore() as store:
                watcher = make_watcher(root, store)
                first = watcher.poll_once()
                write_file(root, "a.py", A_V2, tick=1)
                os.unlink(root / "b.py")
                second = watcher.poll_once()
                dumps.append(canonical_ingestion_dump(store))
                reports.append((first.to_dict(), second.to_dict()))
        assert dumps[0] == dumps[1]
        assert reports[0] == reports[1]

    def test_deterministic_under_hashseed(self, tmp_path):
        import subprocess
        import sys

        repo = Path(__file__).resolve().parents[1]
        child = tmp_path / "child.py"
        child.write_text(
            "import os, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(repo / 'src')!r})\n"
            f"sys.path.insert(0, {str(repo / 'tests')!r})\n"
            "from _ingest_fakes import ToyExtractor, ToyResolver\n"
            "from know_your_soul.ingest.pipeline import IngestionPipeline, canonical_ingestion_dump\n"
            "from know_your_soul.ingest.watcher import IngestionWatcher, WatcherConfig\n"
            "from know_your_soul.store import EventStore\n"
            f"root = Path(sys.argv[1])\n"
            "root.mkdir(exist_ok=True)\n"
            "(root / 'a.py').write_text('def alpha():\\n    pass\\n')\n"
            "os.utime(root / 'a.py', ns=(1700000000000000000, 1700000000000000000))\n"
            "with EventStore() as store:\n"
            "    w = IngestionWatcher(root, IngestionPipeline(store, extractor=ToyExtractor(), resolver=ToyResolver()),"
            " config=WatcherConfig(interval_seconds=0.05, debounce_seconds=0.0, quick_quiet_seconds=0.0),"
            " clock=lambda: 0.0, sleep=lambda s: None)\n"
            "    w.poll_once()\n"
            "    sys.stdout.write(canonical_ingestion_dump(store))\n",
            encoding="utf-8",
        )
        outputs = []
        for seed in ("0", "1", "42"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            corpus = tmp_path / f"corpus_{seed}"
            result = subprocess.run(
                [sys.executable, str(child), str(corpus)], capture_output=True, text=True, env=env, check=True
            )
            outputs.append(result.stdout)
        assert outputs[0] == outputs[1] == outputs[2]


# -------------------------------------------------------- real sibling lane


class TestRealSiblingIntegration:
    def test_real_extractor_through_watcher(self, tmp_path):
        """No fakes: the real SUB-87 extractor + SUB-88 resolver drive the cycle."""
        write_file(tmp_path, "mod.py", "def real_function():\n    return 1\n")
        with EventStore() as store:
            pipeline = IngestionPipeline(store)  # real extractor + resolver
            watcher = IngestionWatcher(
                tmp_path,
                pipeline,
                config=WatcherConfig(interval_seconds=0.05, debounce_seconds=0.0, quick_quiet_seconds=0.0),
                clock=FakeClock(),
                sleep=no_sleep,
            )
            cycle = watcher.run(once=True).cycles[0]
            assert cycle.mode == "quick"
            assert cycle.files_proposed == 1
            assert symbol_names(store, "mod.py") == ["real_function"]
