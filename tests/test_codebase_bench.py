"""Tests for the codebase benchmark harness (SUB-94, wave 5).

The harness lives in ``bench/codebase/run.py`` (a script, loaded here as a
module). The dataset is vendored under ``bench/codebase/data/`` (pinned by
commit + sha256 manifest); tests that need it **skip honestly** when the
archives are absent — they never hit the network.

Gates pinned here:

* dataset integrity vs ``manifest.json`` (per-file sha256);
* ground-truth correctness: lookups hit (fixture top-1, repos hit@10),
  hierarchy sets match the manually verified ``ground_truth.json`` exactly,
  the negative query returns nothing;
* determinism: two ingestions of every target are byte-identical
  (``canonical_ingestion_dump``);
* regression vs ``baseline.json``: counts and the failed-file list are
  STRICT (truth), timings LOOSE (10x + 250ms slack).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CODEBENCH_PATH = REPO_ROOT / "bench" / "codebase" / "run.py"


def _load_codebench() -> Any:
    spec = importlib.util.spec_from_file_location("kys_codebench_run", CODEBENCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass needs the module registered
    spec.loader.exec_module(module)
    return module


cb = _load_codebench()

DATASET_MISSING = not cb.dataset_present()
requires_dataset = pytest.mark.skipif(
    DATASET_MISSING,
    reason="codebase bench dataset not vendored (run: uv run python bench/codebase/run.py fetch)",
)


# --------------------------------------------------------------- no dataset


class TestSelfChecks:
    def test_check_exits_zero(self):
        assert cb.cmd_check() == 0

    @requires_dataset
    def test_manifest_pins_match_repos(self):
        manifest = cb.load_manifest()
        by_name = {entry["name"]: entry for entry in manifest["repos"]}
        assert set(by_name) == {spec["name"] for spec in cb.REPOS}
        for spec in cb.REPOS:
            entry = by_name[spec["name"]]
            assert entry["commit"] == spec["commit"]
            assert entry["url"] == cb.archive_url(spec)
            assert len(entry["commit"]) == 40
            assert entry["file_count"] == len(entry["files"]) > 0

    @requires_dataset
    def test_ground_truth_paths_exist_in_manifest(self):
        """Every real-repo ground-truth path must be a file in the pinned tree."""
        manifest = cb.load_manifest()
        truth = cb.load_ground_truth()
        files_by_repo = {entry["name"]: {f["path"] for f in entry["files"]} for entry in manifest["repos"]}
        for name in files_by_repo:
            for query in truth["targets"][name].get("lookups", []):
                expect = query.get("expect")
                if expect is not None:
                    assert expect["path"] in files_by_repo[name], (name, expect)
            for query in truth["targets"][name].get("hierarchy", []):
                assert query["file"] in files_by_repo[name]
                for entry in query["expect"]:
                    assert entry["path"] in files_by_repo[name]

    def test_ground_truth_fixture_negative_query(self):
        truth = cb.load_ground_truth()
        negatives = [q for t in truth["targets"].values() for q in t.get("lookups", []) if q.get("expect") is None]
        assert negatives, "ground truth must pin at least one negative lookup"

    def test_baseline_matches_ground_truth_targets(self):
        baseline = cb.load_baseline()
        assert baseline is not None, "baseline.json missing (run with --update-baseline)"
        truth = cb.load_ground_truth()
        assert set(baseline["targets"]) == set(truth["targets"])


@requires_dataset
class TestDatasetIntegrity:
    def test_archives_match_manifest(self):
        manifest = cb.load_manifest()
        for entry in manifest["repos"]:
            assert cb.verify_repo(entry) == [], entry["name"]

    def test_verify_command_exits_zero(self):
        assert cb.cmd_verify() == 0

    def test_vendored_size_budget(self):
        """The vendored dataset must stay small (< 5 MiB total, per the brief)."""
        total = sum(p.stat().st_size for p in cb.DATA_DIR.glob("*.tar.gz"))
        assert total < 5 * 1024 * 1024, total


# ------------------------------------------------------------ full measure


@pytest.fixture(scope="module")
def measured() -> list[Any]:
    if DATASET_MISSING:
        pytest.skip("codebase bench dataset not vendored")
    return cb.measure(repeats=3)


@requires_dataset
class TestCorrectnessGate:
    def test_all_ground_truth_holds(self, measured):
        failures: list[str] = []
        for result in measured:
            failures.extend(cb.correctness_failures(result.name, result.queries, result.determinism))
        assert failures == []

    def test_summary_counts(self, measured):
        summary = cb.correctness_summary(measured)
        assert summary["lookup_total"] > 0
        assert summary["lookup_hit_at_k"] == summary["lookup_total"]
        assert summary["hierarchy_total"] > 0
        assert summary["hierarchy_exact"] == summary["hierarchy_total"]
        assert summary["negative_ok"]
        assert summary["determinism_ok"]

    def test_every_target_ingested_files(self, measured):
        names = {result.name for result in measured}
        assert names == {"fixture", *(spec["name"] for spec in cb.REPOS)}
        for result in measured:
            assert result.ingest["stats"]["proposed"] > 0, result.name
            assert result.ingest["nodes"] > 0, result.name

    def test_query_latency_recorded(self, measured):
        for result in measured:
            for lookup in result.queries.get("lookups", {}).get("results", []):
                assert 0.0 <= lookup["p50_ms"] <= lookup["p95_ms"], (result.name, lookup["query"])


@requires_dataset
class TestRegressionGate:
    def test_baseline_gate(self, measured):
        """Counts + failed-file list are STRICT; timings LOOSE (10x + slack)."""
        baseline = cb.load_baseline()
        assert baseline is not None
        failures = cb.regression_failures(baseline, measured)
        assert failures == []

    def test_known_extraction_failures_fixed(self, measured):
        """F-SUB94-1 is fixed (wave W5, SUB-95): `from . import x` no longer
        crashes the extractor, so the requests files it killed (api.py,
        __init__.py, help.py, utils.py, test_requests.py) must be ingested —
        no EXTRACTION_FAILED entries may remain in any target's failed-file
        list, in the refreshed baseline and in the live measurement."""
        baseline = cb.load_baseline()
        assert baseline is not None
        killed_by_f_sub94_1 = {
            "src/requests/api.py",
            "src/requests/__init__.py",
            "src/requests/help.py",
            "src/requests/utils.py",
            "tests/test_requests.py",
        }
        for target, entry in baseline["targets"].items():
            extraction_failures = [f for f in entry["failed_files"] if f.endswith("[EXTRACTION_FAILED]")]
            assert extraction_failures == [], (target, extraction_failures)
            failed_paths = {f.rsplit(" [", 1)[0] for f in entry["failed_files"]}
            assert not (failed_paths & killed_by_f_sub94_1), (target, failed_paths & killed_by_f_sub94_1)
        for result in measured:
            extraction_failures = [f for f in result.ingest["failed_files"] if f.endswith("[EXTRACTION_FAILED]")]
            assert extraction_failures == [], (result.name, extraction_failures)
