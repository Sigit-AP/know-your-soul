"""Integration tests for the mega-wave 2 wiring (SUB-80).

The wave-2 modules (SUB-74 analytics algorithms/paths, SUB-75 export
formats, SUB-76 query tools, SUB-77 ops config/metrics/diagnose) shipped as
unwired modules; this file pins their integration into the production
paths: the built-in registry, the ``export``/``analytics`` CLI commands,
and the new ``config``/``metrics``/``diagnose`` OPS subcommands.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from know_your_soul.cli import main
from know_your_soul.exporters.builtin import (
    ANALYTICS_ALIASES,
    FORMAT_ALIASES,
    build_registry,
    resolve_selection,
)
from know_your_soul.exporters.orchestrator import produce_exports
from test_builtin_integration import _invoke, _read_model, _rebuilt

NEW_FORMAT_ARTIFACTS = {
    "exports/graph.gexf",
    "exports/cytoscape.json",
    "exports/d3-force.json",
    "exports/adjacency.csv",
    "exports/edges.txt",
    "exports/graph.tgf",
}

NEW_ANALYTICS_ARTIFACTS = {
    "analytics/graph-summary.json",
    "analytics/hits.json",
    "analytics/eigenvector.json",
    "analytics/katz.json",
    "analytics/clustering.json",
    "analytics/louvain.json",
    "analytics/reachability.json",
    "analytics/motifs.json",
    "analytics/assortativity.json",
    "analytics/paths.json",
    "analytics/aggregates.json",
    "analytics/near-duplicates.json",
}


@pytest.fixture()
def produced(tmp_path: Path) -> tuple[dict[str, bytes], dict]:
    """All artifacts produced from one real rebuilt generation."""
    db_path, out_path = _rebuilt(tmp_path)
    registry = build_registry()
    artifacts = produce_exports(registry, _read_model(out_path))
    return artifacts, _read_model(out_path)


# ------------------------------------------------------------------- formats


def test_new_format_aliases_resolve() -> None:
    registry = build_registry()
    assert resolve_selection("gexf,cytoscape,d3force,adjacency,tgf", kind="format", registry=registry) == tuple(
        sorted(NEW_FORMAT_ARTIFACTS)
    )
    assert set(FORMAT_ALIASES) >= {"gexf", "cytoscape", "d3force", "adjacency", "tgf"}
    assert set(ANALYTICS_ALIASES) >= {
        "graph-summary",
        "hits",
        "eigenvector",
        "katz",
        "clustering",
        "louvain",
        "reachability",
        "motifs",
        "assortativity",
        "paths",
        "aggregates",
        "near-duplicates",
    }


def test_gexf_artifact_is_valid_gexf(produced: tuple[dict[str, bytes], dict]) -> None:
    artifacts, _ = produced
    root = ET.fromstring(artifacts["exports/graph.gexf"].decode("utf-8"))
    assert root.tag == "{http://www.gexf.net/1.2draft}gexf"
    graph = root.find("{http://www.gexf.net/1.2draft}graph")
    assert graph is not None and graph.get("defaultedgetype") == "directed"


def test_cytoscape_and_d3force_artifacts(produced: tuple[dict[str, bytes], dict]) -> None:
    artifacts, read_model = produced
    cytoscape = json.loads(artifacts["exports/cytoscape.json"])
    assert cytoscape["artifact"] == "cytoscape"
    assert cytoscape["schema_version"] == 1
    assert len(cytoscape["elements"]["nodes"]) == len(read_model["nodes"])
    d3force = json.loads(artifacts["exports/d3-force.json"])
    assert [node["index"] for node in d3force["nodes"]] == list(range(len(d3force["nodes"])))


def test_adjacency_and_tgf_artifacts(produced: tuple[dict[str, bytes], dict]) -> None:
    artifacts, read_model = produced
    node_count = len(read_model["nodes"])
    matrix_rows = artifacts["exports/adjacency.csv"].decode("utf-8").split("\r\n")
    assert len(matrix_rows[0].split(",")) == node_count + 1  # header: id + one column per node
    edge_list = artifacts["exports/edges.txt"].decode("utf-8")
    assert "N-P1\tN-P2" in edge_list  # the one seeded relation
    tgf = artifacts["exports/graph.tgf"].decode("utf-8")
    node_section, edge_section = tgf.split("#\n")
    assert len(node_section.strip().splitlines()) == node_count
    assert edge_section.strip(), "the seeded relation must appear in the TGF edge section"


# ----------------------------------------------------------------- analytics


def test_new_analytics_aliases_resolve() -> None:
    registry = build_registry()
    resolved = resolve_selection(
        "graph-summary,hits,eigenvector,katz,clustering,louvain,reachability,motifs,assortativity,"
        "paths,aggregates,near-duplicates",
        kind="analytics",
        registry=registry,
    )
    assert resolved == tuple(sorted(NEW_ANALYTICS_ARTIFACTS))


def test_algorithm_artifacts_are_real(produced: tuple[dict[str, bytes], dict]) -> None:
    artifacts, read_model = produced
    node_count = len(read_model["nodes"])

    hits = json.loads(artifacts["analytics/hits.json"])
    assert hits["kind"] == "analytics.algorithms.hits"
    assert "domains" in hits  # P1..P7 induced-subgraph block, like centrality

    eigenvector = json.loads(artifacts["analytics/eigenvector.json"])
    assert eigenvector["kind"] == "analytics.algorithms.eigenvector"
    assert "converged" in eigenvector  # honest convergence flag, never faked

    katz = json.loads(artifacts["analytics/katz.json"])
    assert katz["kind"] == "analytics.algorithms.katz"

    clustering = json.loads(artifacts["analytics/clustering.json"])
    assert clustering["kind"] == "analytics.algorithms.clustering"

    louvain = json.loads(artifacts["analytics/louvain.json"])
    assert louvain["kind"] == "analytics.algorithms.louvain"
    members = [member for community in louvain["communities"] for member in community["members"]]
    assert sorted(members) == sorted(node["id"] for node in read_model["nodes"])  # exact cover

    reachability = json.loads(artifacts["analytics/reachability.json"])
    assert reachability["kind"] == "analytics.algorithms.reachability"
    summary = reachability["summary"]
    assert summary["pairs"] + summary["unreachable_pairs"] == node_count * (node_count - 1)

    motifs = json.loads(artifacts["analytics/motifs.json"])
    assert motifs["kind"] == "analytics.algorithms.motifs"

    assortativity = json.loads(artifacts["analytics/assortativity.json"])
    assert assortativity["kind"] == "analytics.algorithms.assortativity"


def test_paths_probe_artifact(produced: tuple[dict[str, bytes], dict]) -> None:
    artifacts, read_model = produced
    probe = json.loads(artifacts["analytics/paths.json"])
    assert probe["kind"] == "analytics.paths.k_shortest"
    node_ids = sorted(node["id"] for node in read_model["nodes"])
    assert probe["parameters"]["source"] == node_ids[0]
    assert probe["parameters"]["target"] == node_ids[-1]
    # The seeded graph is KROOT, N-P1, N-P2, N-P5 with one link N-P1->N-P2:
    # source KROOT cannot reach target N-P5, so zero paths is the honest result.
    assert probe["path_count"] == 0
    for path in probe["paths"]:
        assert len(path["nodes"]) == path["cost"] + 1


def test_paths_probe_with_insufficient_nodes() -> None:
    registry = build_registry()
    artifacts = produce_exports(registry, {"nodes": [], "links": []}, select=("analytics/paths.json",))
    probe = json.loads(artifacts["analytics/paths.json"])
    assert probe["status"] == "insufficient_nodes"
    assert probe["path_count"] == 0


def test_query_tools_artifacts(produced: tuple[dict[str, bytes], dict]) -> None:
    artifacts, read_model = produced
    aggregates = json.loads(artifacts["analytics/aggregates.json"])
    assert aggregates["group_by"] == "domain"
    assert aggregates["total"] == len(read_model["nodes"])
    assert sum(group["count"] for group in aggregates["groups"]) == aggregates["total"]

    near_duplicates = json.loads(artifacts["analytics/near-duplicates.json"])
    assert near_duplicates["disposition"] == "proposal_only"
    assert near_duplicates["auto_merge"] is False

    graph_summary = json.loads(artifacts["analytics/graph-summary.json"])
    histogram = graph_summary["degree"]["histogram"]
    assert sum(histogram.values()) == len(read_model["nodes"])
    assert graph_summary["counts"]["nodes"] == len(read_model["nodes"])


# ------------------------------------------------------------------ CLI: ops


def test_cli_config_describe_effective_dump(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    base = ["--store", str(db_path), "--output-root", str(out_path)]

    code, out, err = _invoke(base + ["config", "effective"], capsys)
    assert code == 0, err
    assert out is not None
    assert out["store.path"] == str(db_path)  # the CLI flag joined the resolution as the cli layer
    assert out["projection.output_root"] == str(out_path)

    code, out, err = _invoke(base + ["config", "dump"], capsys)
    assert code == 0, err
    assert out is not None
    assert out["store.path"]["source"] == "cli"
    assert out["store.path"]["value"] == str(db_path)
    assert out["serve.api_key"]["value"] == "<redacted>" or out["serve.api_key"]["value"] is None

    code = main(base + ["config", "describe"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("# Resolved Configuration")
    assert str(db_path) in captured.out


def test_cli_config_env_layer_wins_over_defaults(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KNOW_YOUR_SOUL_STALE_AFTER_SECONDS", "42")
    code, out, err = _invoke(["config", "dump"], capsys)
    assert code == 0, err
    assert out is not None
    assert out["ops.stale_after_seconds"]["value"] == 42.0
    assert out["ops.stale_after_seconds"]["source"] == "env"


def test_cli_config_rejects_a_broken_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("not [valid", encoding="utf-8")
    code, out, err = _invoke(["config", "--config", str(bad), "effective"], capsys)
    assert code == 2
    assert err is not None and err["error"]["code"] == "CLI_USAGE"


def test_cli_metrics_json_and_markdown(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    base = ["--store", str(db_path), "--output-root", str(out_path)]

    code, out, err = _invoke(base + ["metrics"], capsys)
    assert code == 0, err
    assert out is not None
    assert out["schema_version"] == 1
    assert out["counts"]["domain_events"] == 7  # 4 upserts + 1 relation + 2 evidence
    assert out["health"]["score"] >= 0
    assert out["projection"]["present"] is True

    code = main(base + ["metrics", "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("# Operational Metrics")


def test_cli_diagnose_healthy_store_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    base = ["--store", str(db_path), "--output-root", str(out_path)]

    code, out, err = _invoke(base + ["diagnose"], capsys)
    assert err is None
    assert out is not None
    assert code == (0 if out["ok"] else 1)  # exit code tracks the report, by contract
    assert out["error_count"] == 0, out["findings"]
    assert code == 0
    assert "hash_chain" in out["checked"]

    code = main(base + ["diagnose", "--format", "markdown"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.startswith("# Diagnostics Report")


def test_cli_diagnose_tampered_artifact_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    generation = out_path / "generations" / (out_path / "CURRENT").read_text(encoding="utf-8").strip()
    graph_json = generation / "graph.json"
    graph_json.write_text(graph_json.read_text(encoding="utf-8") + " ", encoding="utf-8")  # tamper: hash mismatch

    code, out, err = _invoke(["--store", str(db_path), "--output-root", str(out_path), "diagnose"], capsys)
    assert code == 1
    assert out is not None
    assert out["ok"] is False
    assert any(finding["severity"] == "error" for finding in out["findings"])


def test_cli_ops_commands_never_touch_the_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """config/metrics/diagnose are read-only: the store file is byte-identical afterwards."""
    db_path, out_path = _rebuilt(tmp_path)
    before = db_path.read_bytes()
    base = ["--store", str(db_path), "--output-root", str(out_path)]
    for argv in (
        base + ["config", "effective"],
        base + ["metrics"],
        base + ["diagnose"],
    ):
        code, _out, err = _invoke(argv, capsys)
        assert code == 0, err
    assert db_path.read_bytes() == before
