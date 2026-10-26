"""Tests for ``exporters.graphml_svg`` (ATM wave B, plan F-B4).

Pins the two interchange artifacts: GraphML (XML 1.0 legal — C0 controls
stripped, correct namespace, parse-back round trip) and a static SVG that
renders the graph through the deterministic ``viz.layout`` engine (domain
colors, bounded labels with a marked elision, legend). Both producers are
pure functions of the read model with explicit fail-closed caps.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Any

import pytest

from know_your_soul.exporters.graphml_svg import (
    GRAPHML_MAX_BYTES,
    GRAPHML_NAMESPACE,
    SVG_MAX_BYTES,
    GraphExportCapExceededError,
    GraphExportError,
    GraphExportRoundTripError,
    to_graphml,
    to_svg,
    validate_graphml_bytes,
    validate_svg_bytes,
)


def _node(node_id: str, domain: str = "P1", kind: str = "leaf", label: str | None = None) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "domain": domain, "kind": kind}
    node["label"] = label if label is not None else node_id
    return node


def _link(source: str, target: str, relation: str = "parent_of") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"directed": True, "nodes": list(nodes), "links": list(links)}


def _sample_graph() -> dict[str, Any]:
    nodes = [_node("KROOT", "P1", "root", "Knowledge Root")]
    links: list[dict[str, Any]] = []
    for d in range(1, 4):
        hub = f"P{d}"
        nodes.append(_node(hub, hub, "domain", f"Domain {hub}"))
        links.append(_link("KROOT", hub))
        for f in range(3):
            leaf = f"{hub}-file-{f}"
            nodes.append(_node(leaf, hub, "leaf", f"Leaf {hub} number {f}"))
            links.append(_link(hub, leaf))
    return _graph(nodes, links)


# ------------------------------------------------------------------ GraphML


def test_graphml_three_runs_are_byte_identical() -> None:
    graph = _sample_graph()
    blobs = [to_graphml(graph) for _ in range(3)]
    assert blobs[0] == blobs[1] == blobs[2]


def test_graphml_input_permutation_is_byte_identical() -> None:
    graph = _sample_graph()
    shuffled = _graph(list(reversed(graph["nodes"])), list(reversed(graph["links"])))
    assert to_graphml(graph) == to_graphml(shuffled)


def test_graphml_parses_with_correct_namespace() -> None:
    data = to_graphml(_sample_graph())
    assert data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    root = ET.fromstring(data.decode("utf-8"))
    assert root.tag == f"{{{GRAPHML_NAMESPACE}}}graphml"
    graph_el = root.find(f"{{{GRAPHML_NAMESPACE}}}graph")
    assert graph_el is not None
    nodes = graph_el.findall(f"{{{GRAPHML_NAMESPACE}}}node")
    edges = graph_el.findall(f"{{{GRAPHML_NAMESPACE}}}edge")
    assert len(nodes) == 13
    assert len(edges) == 12
    assert graph_el.get("edgedefault") == "directed"


def test_graphml_node_data_carries_domain_kind_label_version() -> None:
    graph = _graph([_node("a", "P3", "leaf", "Alpha & <Beta>"), _node("b", "P3")], [_link("a", "b", "supports")])
    root = ET.fromstring(to_graphml(graph).decode("utf-8"))
    ns = {"g": GRAPHML_NAMESPACE}
    node_a = root.find("g:graph/g:node[@id='a']", ns)
    assert node_a is not None
    data = {d.get("key"): d.text for d in node_a.findall("g:data", ns)}
    assert data["d_domain"] == "P3"
    assert data["d_kind"] == "leaf"
    assert data["d_label"] == "Alpha & <Beta>"
    assert data["d_version"] == "0"
    edge = root.find("g:graph/g:edge", ns)
    assert edge is not None
    assert edge.find("g:data", ns).text == "supports"  # type: ignore[union-attr]


def test_graphml_strips_c0_controls_from_labels() -> None:
    # A single C0 control in one label must not abort the whole export.
    nasty = "bad\x00label\x07with\x0bbells"
    graph = _graph([_node("a", "P1", "leaf", nasty), _node("b")], [_link("a", "b")])
    data = to_graphml(graph)
    assert b"\x00" not in data and b"\x07" not in data and b"\x0b" not in data
    root = ET.fromstring(data.decode("utf-8"))
    ns = {"g": GRAPHML_NAMESPACE}
    node_a = root.find("g:graph/g:node[@id='a']/g:data[@key='d_label']", ns)
    assert node_a is not None
    assert node_a.text == "badlabelwithbells"


def test_graphml_drops_links_with_unknown_endpoints_deterministically() -> None:
    graph = _graph([_node("a"), _node("b")], [_link("a", "ghost"), _link("a", "b")])
    root = ET.fromstring(to_graphml(graph).decode("utf-8"))
    edges = root.find(f"{{{GRAPHML_NAMESPACE}}}graph").findall(f"{{{GRAPHML_NAMESPACE}}}edge")  # type: ignore[union-attr]
    assert len(edges) == 1
    assert edges[0].get("source") == "a" and edges[0].get("target") == "b"


def test_graphml_roundtrip_validator_accepts_own_output() -> None:
    validate_graphml_bytes(to_graphml(_sample_graph()))


def test_graphml_roundtrip_validator_rejects_defects() -> None:
    good = to_graphml(_sample_graph()).decode("utf-8")
    with pytest.raises(GraphExportRoundTripError):
        validate_graphml_bytes(b"<not-xml")
    with pytest.raises(GraphExportRoundTripError):
        validate_graphml_bytes(b'<?xml version="1.0"?><graphml xmlns="http://wrong.example/ns"/>')
    dangling = good.replace('source="KROOT"', 'source="GHOST"', 1)
    with pytest.raises(GraphExportRoundTripError):
        validate_graphml_bytes(dangling.encode("utf-8"))
    # A C0 byte smuggled past the producer is a defect, not valid XML.
    poisoned = good.replace("Knowledge Root", "Knowledge\x01Root", 1)
    with pytest.raises(GraphExportRoundTripError):
        validate_graphml_bytes(poisoned.encode("utf-8"))


def test_graphml_cap_is_fail_closed_typed() -> None:
    nodes = [_node(f"n{i:05d}") for i in range(400)]
    links = [_link(f"n{i:05d}", f"n{i + 1:05d}") for i in range(399)]
    with pytest.raises(GraphExportCapExceededError):
        to_graphml(_graph(nodes, links), max_bytes=1024)
    with pytest.raises(GraphExportError):
        to_graphml(_sample_graph(), max_bytes=0)


def test_graphml_node_cap_is_fail_closed_typed() -> None:
    nodes = [_node(f"n{i:05d}") for i in range(5000)]
    with pytest.raises(GraphExportCapExceededError):
        to_graphml(_graph(nodes, []), max_nodes=64)


# ---------------------------------------------------------------------- SVG


def test_svg_three_runs_are_byte_identical() -> None:
    graph = _sample_graph()
    blobs = [to_svg(graph, seed=7, width=800, height=600) for _ in range(3)]
    assert blobs[0] == blobs[1] == blobs[2]


def test_svg_input_permutation_is_byte_identical() -> None:
    graph = _sample_graph()
    shuffled = _graph(list(reversed(graph["nodes"])), list(reversed(graph["links"])))
    assert to_svg(graph, seed=7) == to_svg(shuffled, seed=7)


def test_svg_is_valid_xml_with_svg_namespace() -> None:
    data = to_svg(_sample_graph(), width=900, height=700)
    assert data.startswith(b'<?xml version="1.0" encoding="UTF-8"?>')
    root = ET.fromstring(data.decode("utf-8"))
    assert root.tag == "{http://www.w3.org/2000/svg}svg"
    assert root.get("width") == "900"
    assert root.get("height") == "700"
    circles = root.findall(".//{http://www.w3.org/2000/svg}circle")
    lines = root.findall(".//{http://www.w3.org/2000/svg}line")
    assert len(circles) >= 13  # one per node, plus legend swatches
    assert len(lines) == 12  # one per edge


def test_svg_circles_stay_inside_the_frame() -> None:
    width, height = 800, 600
    root = ET.fromstring(to_svg(_sample_graph(), width=width, height=height).decode("utf-8"))
    ns = "{http://www.w3.org/2000/svg}"
    for circle in root.findall(f".//{ns}circle"):
        cx, cy = float(circle.get("cx")), float(circle.get("cy"))  # type: ignore[arg-type]
        r = float(circle.get("r"))  # type: ignore[arg-type]
        assert 0.0 <= cx - r and cx + r <= width
        assert 0.0 <= cy - r and cy + r <= height


def test_svg_labels_are_bounded_with_marked_elision() -> None:
    long_label = "A" * 200
    graph = _graph([_node("a", "P1", "leaf", long_label), _node("b")], [_link("a", "b")])
    root = ET.fromstring(to_svg(graph, width=800, height=600).decode("utf-8"))
    ns = "{http://www.w3.org/2000/svg}"
    texts = [t for t in root.findall(f".//{ns}text") if (t.text or "").startswith("A")]
    assert texts, "expected the long label to be rendered"
    rendered = texts[0].text or ""
    assert rendered.endswith("…")
    assert len(rendered) < 100
    # The full label survives in the hover title.
    title = texts[0].find(f"{ns}title")
    assert title is not None and title.text == long_label


def test_svg_legend_lists_domains_sorted() -> None:
    root = ET.fromstring(to_svg(_sample_graph(), width=900, height=700).decode("utf-8"))
    ns = "{http://www.w3.org/2000/svg}"
    texts = [t.text for t in root.findall(f".//{ns}text")]
    legend_rows = [t for t in texts if t and t.startswith("P") and "·" in t]
    assert legend_rows[:3] == ["P1 · 4", "P2 · 4", "P3 · 4"] or legend_rows[:3] == sorted(legend_rows[:3])
    assert legend_rows == sorted(legend_rows)


def test_svg_escapes_label_text() -> None:
    graph = _graph([_node("a", "P1", "leaf", 'Fish & "Chips" <hot>'), _node("b")], [_link("a", "b")])
    data = to_svg(graph, width=800, height=600)
    assert b"Fish &amp;" in data
    assert b"<hot>" not in data
    ET.fromstring(data.decode("utf-8"))  # parses


def test_svg_roundtrip_validator_accepts_own_output() -> None:
    validate_svg_bytes(to_svg(_sample_graph()))


def test_svg_roundtrip_validator_rejects_defects() -> None:
    with pytest.raises(GraphExportRoundTripError):
        validate_svg_bytes(b"<html><body/></html>")
    with pytest.raises(GraphExportRoundTripError):
        validate_svg_bytes(b"not xml at all")


def test_svg_caps_are_fail_closed_typed() -> None:
    nodes = [_node(f"n{i:05d}") for i in range(200)]
    with pytest.raises(GraphExportCapExceededError):
        to_svg(_graph(nodes, []), max_nodes=64)
    with pytest.raises(GraphExportCapExceededError):
        to_svg(_sample_graph(), max_bytes=128)
    with pytest.raises(GraphExportError):
        to_svg(_sample_graph(), width=0, height=100)


def test_export_constants_are_explicit() -> None:
    assert GRAPHML_MAX_BYTES > 0
    assert SVG_MAX_BYTES > 0
    assert GRAPHML_NAMESPACE == "http://graphml.graphdrawing.org/xmlns"
