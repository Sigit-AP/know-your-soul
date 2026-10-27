"""Tests for the institutional benchmark harness (bench/institutional/run.py).

Covers the harness contract, not the analytics kernels themselves:

* datasets are pure functions of code (two builds are byte-identical);
* two benchmark runs produce identical correctness verdicts and error metrics
  (timings are excluded — wall time is not deterministic by definition);
* kernels whose implementation has not landed are reported as honest skips,
  never as passes or crashes;
* auto-discovery picks up the mega-wave-2 ``analytics.algorithms`` module
  when it lands (simulated via a stub module in ``sys.modules``);
* the CLI writes JSON + markdown and exits 0 (and 1 under --strict on FAIL).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

BENCH_RUN = Path(__file__).resolve().parents[1] / "bench" / "institutional" / "run.py"


def _load_harness() -> Any:
    spec = importlib.util.spec_from_file_location("institutional_bench_run", BENCH_RUN)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve cls.__module__ via sys.modules; register before exec.
    sys.modules.setdefault("institutional_bench_run", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness() -> Any:
    return _load_harness()


def _correctness_projection(report: dict[str, Any]) -> Any:
    """Strip everything timing-related; keep verdicts, errors, determinism flags."""
    return [
        (
            kernel["kernel"],
            kernel["status"],
            kernel["reason"],
            [
                (
                    entry["dataset"],
                    entry["correctness"],
                    entry["deterministic"],
                    json.dumps(entry["detail"], sort_keys=True, default=str),
                )
                for entry in kernel["datasets"]
            ],
        )
        for kernel in report["kernels"]
    ]


# ------------------------------------------------------------------ datasets


def test_datasets_are_pure_and_deterministic(harness: Any) -> None:
    first = harness.build_datasets()
    second = harness.build_datasets()
    assert [d.name for d in first] == [d.name for d in second]
    for left, right in zip(first, second, strict=True):
        assert json.dumps(left.graph, sort_keys=True) == json.dumps(right.graph, sort_keys=True)


def test_datasets_cover_known_and_seeded(harness: Any) -> None:
    datasets = {d.name: d for d in harness.build_datasets()}
    assert {"path3", "diamond", "two_triangles_bridge", "cliques_4x5"} <= set(datasets)
    seeded = [d for d in datasets.values() if "seeded" in d.tags]
    assert any(d.n == 512 for d in seeded), "scaling dataset n=512 must exist"
    for dataset in datasets.values():
        links = dataset.graph["links"]
        pairs = [(link["source"], link["target"]) for link in links]
        assert len(pairs) == len(set(pairs)), f"{dataset.name}: duplicate edges"
        assert all(source != target for source, target in pairs), f"{dataset.name}: self-loop"


# ------------------------------------------------------------------ determinism of verdicts


def test_benchmark_verdicts_deterministic_across_runs(harness: Any) -> None:
    first = harness.run_benchmark(repeats=1, quick=True)
    second = harness.run_benchmark(repeats=1, quick=True)
    assert _correctness_projection(first) == _correctness_projection(second)
    assert first["summary"] == second["summary"]


def test_current_kernel_scorecard(harness: Any) -> None:
    report = harness.run_benchmark(repeats=1, quick=True)
    by_kernel = {kernel["kernel"]: kernel for kernel in report["kernels"]}
    # Landed kernels must pass correctness.
    for key in ("pagerank", "wcc", "bc", "cycles"):
        assert by_kernel[key]["status"] == "pass", f"{key}: {by_kernel[key]['reason']}"
    # CDLP is landed; its known two-triangles-bridge divergence is allowed to be
    # pass or fail, but never skipped — the harness must judge it honestly.
    assert by_kernel["cdlp"]["status"] in {"pass", "fail"}
    # Kernels without a public implementation must be honest skips.
    assert by_kernel["bfs"]["status"] == "skipped"
    assert by_kernel["sssp"]["status"] == "skipped"
    assert "belum tersedia" in by_kernel["bfs"]["reason"]
    summary = report["summary"]
    assert summary["kernels_passed"] + summary["kernels_failed"] == summary["kernels_available"]
    assert summary["kernels_total"] == summary["kernels_available"] + summary["kernels_skipped"]


def test_every_measured_dataset_is_deterministic(harness: Any) -> None:
    report = harness.run_benchmark(repeats=1, quick=True)
    for kernel in report["kernels"]:
        for entry in kernel["datasets"]:
            assert entry["deterministic"], f"{kernel['kernel']} on {entry['dataset']} is not deterministic"


# ------------------------------------------------------------------ honest skip + auto-discovery


def test_missing_kernel_is_honest_skip(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "_load", lambda module, attr: None)
    report = harness._kernel_cdlp(harness.build_datasets(), repeats=1, quick=True, max_n=64)
    assert report.status == "skipped"
    assert "belum tersedia" in report.reason
    assert report.datasets == []


def test_algorithms_module_autodiscovered_when_landed(harness: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate mega-wave-2 landing: a stub analytics.algorithms with a correct
    compute_clustering must be picked up by the lcc/tc kernels without harness edits."""

    def stub_clustering(graph: dict[str, Any]) -> dict[str, Any]:
        node_ids = sorted(node["id"] for node in graph["nodes"])
        neighbours: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
        for link in graph["links"]:
            neighbours[link["source"]].add(link["target"])
            neighbours[link["target"]].add(link["source"])
        per_node = []
        triangles_total = 0
        for node_id in node_ids:
            peers = sorted(neighbours[node_id])
            linked = sum(1 for i, left in enumerate(peers) for right in peers[i + 1 :] if right in neighbours[left])
            degree = len(peers)
            coefficient = (2.0 * linked / (degree * (degree - 1))) if degree >= 2 else 0.0
            triangles_total += linked
            per_node.append({"node_id": node_id, "coefficient": f"{coefficient:.12f}", "triangles": linked})
        return {
            "kind": "analytics.algorithms.clustering",
            "per_node": per_node,
            "global": {
                "triangles": triangles_total // 3,
                "average_coefficient": "0.000000000000",
                "transitivity": "0.000000000000",
            },
        }

    stub = types.ModuleType("know_your_soul.analytics.algorithms")
    stub.compute_clustering = stub_clustering  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "know_your_soul.analytics.algorithms", stub)

    datasets = harness.build_datasets()
    lcc = harness._kernel_lcc(datasets, repeats=1, quick=True, max_n=64)
    assert lcc.status == "pass"
    assert any(entry.dataset == "diamond" and entry.correctness == "pass" for entry in lcc.datasets)
    diamond = next(entry for entry in lcc.datasets if entry.dataset == "diamond")
    assert diamond.detail["max_abs_err"] < 1e-9  # stub computes exactly 7/12-avg semantics

    tc = harness._kernel_tc(datasets, repeats=1, quick=True, max_n=64)
    assert tc.status == "pass"
    diamond_tc = next(entry for entry in tc.datasets if entry.dataset == "diamond")
    assert diamond_tc.detail == {"ours_triangles": 1, "reference_triangles": 1}


# ------------------------------------------------------------------ references


def test_pagerank_reference_is_self_validated(harness: Any) -> None:
    datasets = {d.name: d for d in harness.build_datasets()}
    scores = harness.ref_pagerank(datasets["diamond"])
    assert abs(sum(scores.values()) - 1.0) < 1e-9
    assert all(value > 0 for value in scores.values())
    # Bidirectional star: all leaves are structurally identical, must tie exactly.
    star = harness.ref_pagerank(datasets["star9_bidir"])
    leaves = [value for node, value in star.items() if node != "hub"]
    assert max(leaves) - min(leaves) < 1e-15
    assert star["hub"] > max(leaves)


def test_bc_reference_known_star(harness: Any) -> None:
    datasets = {d.name: d for d in harness.build_datasets()}
    ours = harness.ours_bc_kernel(datasets["star9_bidir"].graph)
    ref = harness.ref_bc(datasets["star9_bidir"])
    assert ref["hub"] == pytest.approx(1.0, abs=1e-12)
    assert ours["hub"] == pytest.approx(1.0, abs=1e-12)
    assert all(value == pytest.approx(0.0, abs=1e-12) for key, value in ref.items() if key != "hub")


def test_cycles_reference_known_values(harness: Any) -> None:
    datasets = {d.name: d for d in harness.build_datasets()}
    assert len(harness.ref_cycles(datasets["directed_cycle6"])) == 1
    assert len(harness.ref_cycles(datasets["two_cycles_shared_node"])) == 2
    assert len(harness.ours_cycles_public(datasets["directed_cycle6"].graph)) == 1


# ------------------------------------------------------------------ CLI


def test_cli_writes_reports_and_exits_zero(harness: Any, tmp_path: Path) -> None:
    json_path = tmp_path / "report.json"
    md_path = tmp_path / "report.md"
    exit_code = harness.main(["--quick", "--repeats", "1", "--json", str(json_path), "--md", str(md_path)])
    assert exit_code == 0
    report = json.loads(json_path.read_text(encoding="utf-8"))
    assert report["kind"] == "bench.institutional"
    assert report["summary"]["score"].endswith("available kernels PASS correctness")
    markdown = md_path.read_text(encoding="utf-8")
    assert "| Kernel | Suite | Status | Detail |" in markdown
    assert "pagerank" in markdown


def test_cli_strict_exit_codes(harness: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _stub(failed: int) -> dict[str, Any]:
        return {
            "summary": {
                "kernels_total": 1,
                "kernels_available": 1,
                "kernels_passed": 1 - failed,
                "kernels_failed": failed,
                "kernels_skipped": 0,
                "score": f"{1 - failed}/1 available kernels PASS correctness",
            },
            "kernels": [],
            "config": {"repeats": 1, "quick": True},
            "reference": "stub",
            "datasets": [],
        }

    passing = _stub(0)
    failing = _stub(1)
    args = ["--quick", "--repeats", "1", "--json", str(tmp_path / "a.json"), "--md", str(tmp_path / "a.md"), "--strict"]
    monkeypatch.setattr(harness, "run_benchmark", lambda **kwargs: passing)
    assert harness.main(args) == 0
    monkeypatch.setattr(harness, "run_benchmark", lambda **kwargs: failing)
    assert harness.main(args) == 1
    # Without --strict a failed kernel still exits 0 (report-first policy).
    monkeypatch.setattr(harness, "run_benchmark", lambda **kwargs: failing)
    assert harness.main([a for a in args if a != "--strict"]) == 0
