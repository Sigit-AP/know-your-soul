"""Tests for ``know_your_soul.ops.subgraph_export`` (ATM F-D3).

Pins the bounded subgraph contract: per-domain and per-node (ego-net)
selection from the generation read model, deterministic BFS with a hard node
cap and explicit truncation markers, canonical JSON + Mermaid artifacts that
round-trip, sha256 manifest entries, and typed errors — never a silent
elision, never a partial export.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from know_your_soul.domain import canonical_json
from know_your_soul.exporters.mermaid import roundtrip_mermaid
from know_your_soul.ops.subgraph_export import (
    SubgraphCapExceededError,
    SubgraphExportError,
    SubgraphRequest,
    SubgraphSelectionError,
    UnknownSubgraphNodeError,
    build_subgraph,
    export_subgraph,
    subgraph_manifest_entries,
)

# ------------------------------------------------------------------ helpers


def _node(node_id: str, domain: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "domain": domain,
        "kind": "primary",
        "lifecycle_status": "active",
        "version": 1,
        "label": f"Node {node_id}",
    }


def _link(source: str, target: str, relation: str = "related") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _model() -> dict[str, Any]:
    return {
        "nodes": [
            _node("a1", "P1"),
            _node("a2", "P1"),
            _node("b1", "P2"),
            _node("b2", "P2"),
            _node("b3", "P2"),
            _node("c1", "P3"),
        ],
        "links": [
            _link("a1", "a2", "supports"),
            _link("a2", "b1"),
            _link("b1", "b2"),
            _link("b2", "b3"),
            _link("b3", "c1"),
        ],
    }


# ----------------------------------------------------------- domain selection


def test_domain_selection_keeps_internal_edges_only() -> None:
    result = build_subgraph(_model(), SubgraphRequest(domain="P2"))
    ids = [node["id"] for node in result.read_model["nodes"]]
    assert ids == ["b1", "b2", "b3"]
    edges = {(link["source"], link["target"]) for link in result.read_model["links"]}
    assert edges == {("b1", "b2"), ("b2", "b3")}
    assert result.nodes_omitted == 0
    assert result.edges_omitted == 0


def test_unknown_domain_is_a_typed_error() -> None:
    with pytest.raises(SubgraphSelectionError, match="P9"):
        build_subgraph(_model(), SubgraphRequest(domain="P9"))


def test_domain_selection_respects_max_nodes_with_marked_truncation() -> None:
    result = build_subgraph(_model(), SubgraphRequest(domain="P2", max_nodes=2))
    ids = [node["id"] for node in result.read_model["nodes"]]
    assert ids == ["b1", "b2"]  # canonical id order, never arbitrary
    assert result.nodes_omitted == 1
    assert result.edges_omitted == 1  # b2->b3 lost its far endpoint


# --------------------------------------------------------------- ego-net


def test_ego_net_depth_zero_is_the_seed_alone() -> None:
    result = build_subgraph(_model(), SubgraphRequest(node_id="b1", depth=0))
    assert [node["id"] for node in result.read_model["nodes"]] == ["b1"]
    assert result.read_model["links"] == []


def test_ego_net_depth_one_is_undirected() -> None:
    result = build_subgraph(_model(), SubgraphRequest(node_id="b1", depth=1))
    ids = {node["id"] for node in result.read_model["nodes"]}
    assert ids == {"b1", "a2", "b2"}  # incoming a2->b1 and outgoing b1->b2
    edges = {(link["source"], link["target"]) for link in result.read_model["links"]}
    assert edges == {("a2", "b1"), ("b1", "b2")}


def test_ego_net_depth_two_expands_one_more_layer() -> None:
    result = build_subgraph(_model(), SubgraphRequest(node_id="b1", depth=2))
    ids = {node["id"] for node in result.read_model["nodes"]}
    assert ids == {"b1", "a2", "b2", "a1", "b3"}


def test_unknown_seed_node_is_a_typed_error() -> None:
    with pytest.raises(UnknownSubgraphNodeError, match="nope"):
        build_subgraph(_model(), SubgraphRequest(node_id="nope"))


def test_ego_net_budget_prefers_nearest_then_canonical_id() -> None:
    result = build_subgraph(_model(), SubgraphRequest(node_id="b1", depth=2, max_nodes=2))
    ids = [node["id"] for node in result.read_model["nodes"]]
    assert ids == ["a2", "b1"]  # seed (dist 0) + nearest neighbour (dist 1), canonical order in output
    assert result.nodes_omitted == 3


# ------------------------------------------------------------ validation


def test_exactly_one_selector_is_required() -> None:
    with pytest.raises(SubgraphSelectionError):
        SubgraphRequest()
    with pytest.raises(SubgraphSelectionError):
        SubgraphRequest(domain="P1", node_id="a1")


def test_depth_and_max_nodes_must_be_valid() -> None:
    with pytest.raises(SubgraphSelectionError, match="depth"):
        SubgraphRequest(node_id="a1", depth=-1)
    with pytest.raises(SubgraphSelectionError, match="max_nodes"):
        SubgraphRequest(node_id="a1", max_nodes=0)


# ------------------------------------------------------------- determinism


def test_permuted_input_yields_identical_artifacts() -> None:
    model = _model()
    shuffled = {
        "nodes": list(reversed(model["nodes"])),
        "links": list(reversed(model["links"])),
    }
    request = SubgraphRequest(node_id="b1", depth=2)
    first = export_subgraph(model, request)
    second = export_subgraph(shuffled, request)
    assert first.artifacts == second.artifacts


# --------------------------------------------------------------- artifacts


def test_export_produces_canonical_json_and_mermaid() -> None:
    export = export_subgraph(_model(), SubgraphRequest(domain="P2"))
    assert set(export.artifacts) == {"subgraphs/domain-P2.json", "subgraphs/domain-P2.mmd"}
    payload = export.artifacts["subgraphs/domain-P2.json"]
    parsed = json.loads(payload.decode("utf-8"))
    assert payload == (canonical_json(parsed) + "\n").encode("utf-8")  # canonical form
    assert parsed["selector"] == {"domain": "P2"}
    assert parsed["truncated"] == {"nodes_omitted": 0, "edges_omitted": 0}
    roundtrip_mermaid(export.artifacts["subgraphs/domain-P2.mmd"])


def test_truncation_is_marked_in_the_json_artifact() -> None:
    export = export_subgraph(_model(), SubgraphRequest(domain="P2", max_nodes=2))
    parsed = json.loads(export.artifacts["subgraphs/domain-P2.json"].decode("utf-8"))
    assert parsed["truncated"]["nodes_omitted"] == 1


def test_manifest_entries_are_sha256_ready() -> None:
    export = export_subgraph(_model(), SubgraphRequest(domain="P2"))
    entries = subgraph_manifest_entries(export)
    assert set(entries) == set(export.artifacts)
    for name, entry in entries.items():
        data = export.artifacts[name]
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["size"] == len(data)
        assert entry["version"] == 1
        assert entry["kind"] == "format"


def test_byte_cap_fails_closed() -> None:
    with pytest.raises(SubgraphCapExceededError):
        export_subgraph(_model(), SubgraphRequest(domain="P2"), max_bytes=16)


def test_ego_artifact_slug_is_safe() -> None:
    model = {"nodes": [_node("weird/id with spaces", "P1")], "links": []}
    export = export_subgraph(model, SubgraphRequest(node_id="weird/id with spaces"))
    (json_name,) = [name for name in export.artifacts if name.endswith(".json")]
    assert json_name.startswith("subgraphs/node-")
    assert "/" not in json_name.removeprefix("subgraphs/").removesuffix(".json")
    assert " " not in json_name


def test_export_error_is_the_base_class() -> None:
    assert issubclass(SubgraphSelectionError, SubgraphExportError)
    assert issubclass(UnknownSubgraphNodeError, SubgraphSelectionError)
    assert issubclass(SubgraphCapExceededError, SubgraphExportError)
