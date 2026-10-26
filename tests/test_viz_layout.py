"""Tests for ``viz.layout`` (ATM wave B, plan F-B7).

Pins the deterministic pure-Python layout engine: explicit-seed RNG, fixed
iteration counts in canonical (sorted) order, quantized coordinates, an
automatic radial-tree/force mode decision derived purely from the graph
shape, and input-permutation invariance — the same graph content must render
byte-identical no matter how the read model rows are ordered.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.domain import canonical_json
from know_your_soul.viz.layout import (
    DEFAULT_ITERATIONS,
    DEFAULT_SEED,
    MAX_LAYOUT_NODES,
    QUANTUM,
    LayoutError,
    layout,
)


def _node(node_id: str, domain: str = "P1", kind: str = "leaf") -> dict[str, Any]:
    return {"id": node_id, "domain": domain, "kind": kind}


def _link(source: str, target: str, relation: str = "parent_of") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"directed": True, "nodes": list(nodes), "links": list(links)}


def _star_of_stars() -> dict[str, Any]:
    """The corpus topology: one root, domain hubs, file leaves (a tree).

    The root has the highest degree (5 hubs; each hub has root + 3 leaves),
    matching the real corpus where KROOT out-degrees every domain hub.
    """
    nodes = [_node("KROOT", "P1", "root")]
    links: list[dict[str, Any]] = []
    for d in range(1, 6):
        hub = f"P{d}"
        nodes.append(_node(hub, hub, "domain"))
        links.append(_link("KROOT", hub))
        for f in range(3):
            leaf = f"{hub}-file-{f}"
            nodes.append(_node(leaf, hub))
            links.append(_link(hub, leaf))
    return _graph(nodes, links)


def _dense_graph() -> dict[str, Any]:
    """A graph with more undirected pairs than a tree (m > n - 1)."""
    ids = ["a", "b", "c", "d", "e"]
    links = [_link("a", "b"), _link("b", "c"), _link("c", "d"), _link("d", "e"), _link("e", "a"), _link("a", "c")]
    return _graph([_node(i) for i in ids], links)


# ------------------------------------------------------------------ basics


def test_empty_graph_layouts_empty() -> None:
    result = layout(_graph([], []))
    assert result.positions == {}
    assert result.metadata["node_count"] == 0
    assert result.metadata["edge_count"] == 0


def test_single_node_sits_at_frame_center() -> None:
    result = layout(_graph([_node("solo")], []), width=400.0, height=200.0)
    assert result.positions == {"solo": (200.0, 100.0)}


def test_positions_cover_every_node() -> None:
    graph = _star_of_stars()
    result = layout(graph, width=900.0, height=600.0)
    assert set(result.positions) == {n["id"] for n in graph["nodes"]}


def test_links_with_unknown_endpoints_are_ignored() -> None:
    graph = _graph(
        [_node("a"), _node("b")],
        [_link("a", "b"), _link("a", "ghost"), _link("ghost", "b"), _link("a", "a")],
    )
    result = layout(graph, width=400.0, height=300.0)
    assert set(result.positions) == {"a", "b"}
    assert result.metadata["edge_count"] == 1


# ------------------------------------------------------- determinism proofs


def test_three_runs_are_byte_identical() -> None:
    graph = _star_of_stars()
    runs = [layout(graph, seed=7, iterations=48, width=800.0, height=600.0) for _ in range(3)]
    blobs = [canonical_json({"positions": r.positions, "metadata": r.metadata}) for r in runs]
    assert blobs[0] == blobs[1] == blobs[2]


def test_force_mode_three_runs_are_byte_identical() -> None:
    graph = _dense_graph()
    runs = [layout(graph, seed=11, iterations=32, width=640.0, height=480.0, mode="force") for _ in range(3)]
    blobs = [canonical_json(r.positions) for r in runs]
    assert blobs[0] == blobs[1] == blobs[2]


def test_input_permutation_does_not_change_output() -> None:
    graph = _star_of_stars()
    forward = layout(graph, seed=3, iterations=24, width=700.0, height=500.0)
    shuffled = _graph(list(reversed(graph["nodes"])), list(reversed(graph["links"])))
    backward = layout(shuffled, seed=3, iterations=24, width=700.0, height=500.0)
    assert forward.positions == backward.positions
    assert forward.metadata == backward.metadata


def test_seed_is_part_of_the_contract() -> None:
    # Different seeds are allowed to differ; identical seeds must not.
    graph = _dense_graph()
    again = layout(graph, seed=99, iterations=16, width=500.0, height=500.0)
    same = layout(graph, seed=99, iterations=16, width=500.0, height=500.0)
    assert again.positions == same.positions


# -------------------------------------------------------- frame + quantum


def test_coordinates_stay_inside_the_frame() -> None:
    graph = _star_of_stars()
    width, height = 800.0, 600.0
    result = layout(graph, seed=5, iterations=64, width=width, height=height)
    for x, y in result.positions.values():
        assert 0.0 <= x <= width
        assert 0.0 <= y <= height


def test_force_mode_coordinates_stay_inside_the_frame() -> None:
    graph = _dense_graph()
    width, height = 500.0, 400.0
    result = layout(graph, seed=5, iterations=64, width=width, height=height, mode="force")
    for x, y in result.positions.values():
        assert 0.0 <= x <= width
        assert 0.0 <= y <= height


def test_coordinates_are_quantized() -> None:
    graph = _star_of_stars()
    result = layout(graph, seed=5, width=777.7, height=555.5)
    for x, y in result.positions.values():
        assert x == round(round(x / QUANTUM) * QUANTUM, 6)
        assert y == round(round(y / QUANTUM) * QUANTUM, 6)


# ---------------------------------------------------------------- mode pick


def test_auto_mode_picks_radial_for_a_tree() -> None:
    result = layout(_star_of_stars(), width=800.0, height=600.0)
    assert result.metadata["mode"] == "radial"


def test_auto_mode_picks_force_for_dense_graphs() -> None:
    result = layout(_dense_graph(), width=800.0, height=600.0)
    assert result.metadata["mode"] == "force"


def test_explicit_mode_is_respected() -> None:
    graph = _dense_graph()
    assert layout(graph, width=800.0, height=600.0, mode="radial").metadata["mode"] == "radial"
    assert layout(_star_of_stars(), width=800.0, height=600.0, mode="force").metadata["mode"] == "force"


def test_radial_root_sits_at_component_center() -> None:
    result = layout(_star_of_stars(), width=800.0, height=600.0, mode="radial")
    kroot = result.positions["KROOT"]
    hubs = [result.positions[f"P{d}"] for d in range(1, 6)]
    mean_x = sum(x for x, _ in hubs) / len(hubs)
    mean_y = sum(y for _, y in hubs) / len(hubs)
    # The hub ring is centered on the root (star-of-stars invariant).
    assert abs(kroot[0] - mean_x) <= 2.0
    assert abs(kroot[1] - mean_y) <= 2.0


def test_metadata_is_complete_and_deterministic() -> None:
    result = layout(_star_of_stars(), seed=42, iterations=10, width=800.0, height=600.0)
    metadata = result.metadata
    assert metadata["seed"] == 42
    assert metadata["iterations"] == 10
    assert metadata["width"] == 800.0
    assert metadata["height"] == 600.0
    assert metadata["node_count"] == 21
    assert metadata["edge_count"] == 20
    assert metadata["mode"] == "radial"
    assert metadata["components"] == 1
    assert metadata["quantum"] == QUANTUM


# ---------------------------------------------------------------- fail-closed


def test_invalid_frame_is_a_typed_error() -> None:
    graph = _graph([_node("a")], [])
    with pytest.raises(LayoutError):
        layout(graph, width=0.0, height=100.0)
    with pytest.raises(LayoutError):
        layout(graph, width=100.0, height=-5.0)
    with pytest.raises(LayoutError):
        layout(graph, width=float("nan"), height=100.0)


def test_invalid_iterations_or_mode_is_a_typed_error() -> None:
    graph = _graph([_node("a")], [])
    with pytest.raises(LayoutError):
        layout(graph, iterations=-1)
    with pytest.raises(LayoutError):
        layout(graph, mode="sideways")


def test_node_cap_is_a_typed_error() -> None:
    graph = _graph([_node(f"n{i:05d}") for i in range(MAX_LAYOUT_NODES + 1)], [])
    with pytest.raises(LayoutError):
        layout(graph)


def test_malformed_read_model_rows_are_skipped() -> None:
    graph = _graph([_node("a"), {"nope": True}, {"id": ""}, _node("b")], [_link("a", "b"), {"bad": 1}])
    result = layout(graph)
    assert set(result.positions) == {"a", "b"}


# ------------------------------------------------------------- property tests


@given(
    seed=st.integers(min_value=0, max_value=2**31 - 1),
    node_count=st.integers(min_value=0, max_value=24),
    edge_flags=st.lists(st.booleans(), min_size=24 * 24, max_size=24 * 24),
)
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_permutation_invariant_and_in_frame(seed: int, node_count: int, edge_flags: list[bool]) -> None:
    ids = [f"n{i:02d}" for i in range(node_count)]
    nodes = [_node(i) for i in ids]
    links: list[dict[str, Any]] = []
    cursor = 0
    for a in ids:
        for b in ids:
            if a != b and edge_flags[cursor]:
                links.append(_link(a, b))
            cursor += 1
    graph = _graph(nodes, links)
    reference = layout(graph, seed=seed, iterations=8, width=640.0, height=480.0)
    permuted_graph = _graph(list(reversed(nodes)), list(reversed(links)))
    permuted = layout(permuted_graph, seed=seed, iterations=8, width=640.0, height=480.0)
    assert reference.positions == permuted.positions
    assert set(reference.positions) == set(ids)
    for x, y in reference.positions.values():
        assert 0.0 <= x <= 640.0
        assert 0.0 <= y <= 480.0


def test_defaults_are_exported_constants() -> None:
    assert DEFAULT_SEED > 0
    assert DEFAULT_ITERATIONS > 0
    assert MAX_LAYOUT_NODES > 0
