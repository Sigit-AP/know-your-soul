"""Tests for ``know_your_soul.report.graph_report`` (W8 SUB-107).

Pins the composite graph-analysis report that measurably beats Graphify's
``analyze.py`` mechanisms:

* god nodes ranked by a *composite* of degree + PageRank + bridge
  (betweenness) centrality — Graphify ranks by degree alone;
* surprising connections carry a structured explanation (stable reason
  codes) plus a confidence bucket — Graphify emits a flat ``why`` string;
* suggested questions are generated from graph-structure templates, each
  carrying evidence node ids and an executable query spec that
  :func:`answer_question` resolves deterministically against the read
  model (provably answerable) — Graphify questions carry no evidence ids
  and no query;
* a token benchmark is computed at build time (Graphify prints one after
  ``build``); ours is embedded in the canonical JSON and the markdown.
"""

from __future__ import annotations

import json
import random
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.cli import main
from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    canonical_json,
)
from know_your_soul.http_api import KNOWLEDGE_OPERATIONS, OPS_OPERATIONS
from know_your_soul.projection import GraphProjector
from know_your_soul.report.graph_report import (
    DEFAULT_TOP_GOD,
    DEFAULT_TOP_QUESTIONS,
    DEFAULT_TOP_SURPRISING,
    GRAPH_REPORT_KIND,
    GRAPH_REPORT_SCHEMA_VERSION,
    GraphReportError,
    QuestionError,
    answer_question,
    build_graph_analysis,
    render_graph_analysis_markdown,
)
from know_your_soul.retrieval import estimated_tokens
from know_your_soul.store import EventStore

# ------------------------------------------------------------------ helpers


def _node(
    node_id: str,
    *,
    label: str | None = None,
    domain: str = "P1",
    version: int = 1,
    sig: str | None = None,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "label": label if label is not None else node_id,
        "domain": domain,
        "version": version,
        "confidence": 0.9,
    }
    if sig is not None:
        node["community_sig"] = sig
    return node


def _link(source: str, target: str, *, relation: str = "supports", confidence: str = "EXTRACTED") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation, "confidence": confidence}


def _graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "directed": True,
        "multigraph": False,
        "graph": {"schema_version": 1, "generator": "know-your-soul"},
        "nodes": nodes,
        "links": links,
        "hyperedges": [],
    }


def _star_in(hub: str = "hub", leaves: tuple[str, ...] = ("l1", "l2", "l3", "l4")) -> dict[str, Any]:
    nodes = [_node(hub)] + [_node(leaf) for leaf in leaves]
    links = [_link(leaf, hub) for leaf in leaves]
    return _graph(nodes, links)


def _path_graph() -> dict[str, Any]:
    ids = ["a", "b", "c", "d", "e"]
    return _graph([_node(i) for i in ids], [_link(ids[i], ids[i + 1]) for i in range(4)])


def _clique_links(members: tuple[str, ...]) -> list[dict[str, Any]]:
    links: list[dict[str, Any]] = []
    for i, source in enumerate(members):
        for target in members[i + 1 :]:
            links.append(_link(source, target))
            links.append(_link(target, source))
    return links


def _barbell_with_source_hub() -> dict[str, Any]:
    """Degree trap `x` (six out-leaves, zero inflow) vs the b/e bridge pair.

    Degree-only ranking (Graphify) puts `x` first; the composite must
    promote the bridge nodes `b`/`e` above it.
    """
    leaves = tuple(f"l{i}" for i in range(1, 7))
    c1 = ("a", "b", "c", "d")
    c2 = ("e", "f", "g", "h")
    nodes = [_node("x")] + [_node(leaf) for leaf in leaves] + [_node(m) for m in (*c1, *c2)]
    links = [_link("x", leaf) for leaf in leaves]
    links += _clique_links(c1)
    links += _clique_links(c2)
    links.append(_link("b", "e"))
    return _graph(nodes, links)


def _two_communities_sole_link() -> dict[str, Any]:
    nodes = [
        _node("a", sig="s1"),
        _node("b", sig="s1"),
        _node("c", sig="s2"),
        _node("d", sig="s2"),
    ]
    return _graph(nodes, [_link("a", "b"), _link("c", "d"), _link("b", "c")])


def _redundant_bridge() -> dict[str, Any]:
    nodes = [
        _node("a", sig="s1"),
        _node("b", sig="s1"),
        _node("c", sig="s2"),
        _node("d", sig="s2"),
    ]
    links = [
        _link("a", "b"),
        _link("b", "a"),
        _link("c", "d"),
        _link("d", "c"),
        _link("b", "c"),
        _link("a", "d"),
    ]
    return _graph(nodes, links)


def _disconnected_components() -> dict[str, Any]:
    nodes = [
        _node("a", sig="s1"),
        _node("b", sig="s1"),
        _node("c", sig="s2"),
        _node("d", sig="s2"),
        _node("e", sig="s2"),
    ]
    return _graph(nodes, [_link("a", "b"), _link("c", "d"), _link("d", "e")])


def _rich_graph() -> dict[str, Any]:
    """One graph carrying every signal family at once."""
    graph = _barbell_with_source_hub()
    for node in graph["nodes"]:
        node["community_sig"] = "s1" if node["id"] in {"a", "b", "c", "d", "x"} or node["id"].startswith("l") else "s2"
    graph["nodes"].append(_node("z1", sig="s3"))
    graph["nodes"].append(_node("z2", sig="s3"))
    graph["links"].append(_link("z1", "z2"))
    graph["nodes"].append(_node("iso"))
    graph["links"].append(_link("a", "c", confidence="AMBIGUOUS", relation="mentions"))
    return graph


def _entry(report: dict[str, Any], section: str, key: str, value: str) -> dict[str, Any]:
    for item in report[section]:
        if item.get(key) == value or item.get("node_id") == value:
            return item
    raise AssertionError(f"{value} not found in {section}")


# ================================================================= god nodes


class TestGodNodeCompositeShape:
    def test_schema_and_kind(self) -> None:
        report = build_graph_analysis(_star_in())
        assert report["schema_version"] == GRAPH_REPORT_SCHEMA_VERSION
        assert report["kind"] == GRAPH_REPORT_KIND

    def test_parameters_echoed(self) -> None:
        report = build_graph_analysis(_star_in(), top_god=3, top_surprising=2, top_questions=4)
        assert report["parameters"] == {"top_god": 3, "top_surprising": 2, "top_questions": 4}

    def test_default_parameters(self) -> None:
        assert DEFAULT_TOP_GOD == 10
        assert DEFAULT_TOP_SURPRISING == 10
        assert DEFAULT_TOP_QUESTIONS == 8

    def test_entry_shape(self) -> None:
        report = build_graph_analysis(_star_in())
        entry = report["god_nodes"][0]
        assert set(entry) == {
            "rank",
            "node_id",
            "label",
            "domain",
            "degree",
            "incoming",
            "outgoing",
            "scores",
            "rank_by_degree_only",
        }
        assert set(entry["scores"]) == {"degree", "pagerank", "betweenness", "composite"}

    def test_scores_are_12dp_strings(self) -> None:
        report = build_graph_analysis(_star_in())
        pattern = re.compile(r"^-?\d+\.\d{12}$")
        for entry in report["god_nodes"]:
            for value in entry["scores"].values():
                assert pattern.match(value), value

    def test_scores_within_unit_interval(self) -> None:
        report = build_graph_analysis(_barbell_with_source_hub())
        for entry in report["god_nodes"]:
            for value in entry["scores"].values():
                assert Decimal(0) <= Decimal(value) <= Decimal(1)

    def test_ranks_are_dense_from_one(self) -> None:
        report = build_graph_analysis(_barbell_with_source_hub())
        assert [entry["rank"] for entry in report["god_nodes"]] == list(range(1, len(report["god_nodes"]) + 1))

    def test_top_god_caps_the_ranking(self) -> None:
        report = build_graph_analysis(_barbell_with_source_hub(), top_god=3)
        assert len(report["god_nodes"]) == 3

    def test_ties_break_by_node_id(self) -> None:
        # Perfect symmetry: l1..l4 tie on every metric -> id order.
        report = build_graph_analysis(_star_in())
        tail = [entry["node_id"] for entry in report["god_nodes"][1:]]
        assert tail == sorted(tail)

    def test_empty_graph_yields_empty_ranking(self) -> None:
        report = build_graph_analysis(_graph([], []))
        assert report["god_nodes"] == []
        assert report["counts"] == {"nodes": 0, "links": 0, "communities": 0, "dropped_links": 0}

    def test_single_node_graph(self) -> None:
        report = build_graph_analysis(_graph([_node("solo")], []))
        assert report["god_nodes"][0]["node_id"] == "solo"
        assert report["god_nodes"][0]["scores"]["composite"] == "0.000000000000"

    def test_label_and_domain_carried(self) -> None:
        graph = _graph([_node("n1", label="Core Abstraction", domain="P3")], [])
        entry = build_graph_analysis(graph)["god_nodes"][0]
        assert entry["label"] == "Core Abstraction"
        assert entry["domain"] == "P3"

    def test_incoming_outgoing_counts(self) -> None:
        report = build_graph_analysis(_star_in())
        hub = _entry(report, "god_nodes", "node_id", "hub")
        assert hub["incoming"] == 4
        assert hub["outgoing"] == 0
        leaf = _entry(report, "god_nodes", "node_id", "l1")
        assert leaf["incoming"] == 0
        assert leaf["outgoing"] == 1

    def test_dropped_links_counted(self) -> None:
        graph = _graph(
            [_node("a"), _node("b")],
            [
                _link("a", "b"),
                _link("a", "b"),  # duplicate
                _link("a", "a"),  # self-loop
                _link("a", "ghost"),  # unknown endpoint
                {"source": "a"},  # malformed
            ],
        )
        report = build_graph_analysis(graph)
        assert report["counts"]["dropped_links"] == 4
        assert report["counts"]["links"] == 1


class TestGodNodeKnownGraphs:
    def test_star_center_ranks_first(self) -> None:
        report = build_graph_analysis(_star_in())
        assert report["god_nodes"][0]["node_id"] == "hub"

    def test_path_middle_ranks_first(self) -> None:
        # a->b->c->d->e: c has the highest betweenness and ties degree;
        # the composite must put c first.
        report = build_graph_analysis(_path_graph())
        assert report["god_nodes"][0]["node_id"] == "c"

    def test_composite_beats_degree_only_on_the_degree_trap(self) -> None:
        """The load-bearing proof vs Graphify's degree-only ranking.

        `x` has the raw highest degree (six out-leaves) but zero inflow and
        zero bridging value; `b`/`e` bridge two cliques. The composite must
        demote `x` below the bridge pair.
        """
        report = build_graph_analysis(_barbell_with_source_hub())
        order = [entry["node_id"] for entry in report["god_nodes"]]
        degree_only = {entry["node_id"]: entry["rank_by_degree_only"] for entry in report["god_nodes"]}
        assert degree_only["x"] == 1  # Graphify would put x first
        assert order[0] in {"b", "e"}
        assert order[1] in {"b", "e"}
        assert order.index("x") > 1  # composite demotes the degree trap

    def test_rank_by_degree_only_present_for_every_entry(self) -> None:
        report = build_graph_analysis(_barbell_with_source_hub())
        ranks = sorted(entry["rank_by_degree_only"] for entry in report["god_nodes"])
        assert ranks == list(range(1, len(report["god_nodes"]) + 1))

    def test_betweenness_component_decides_degree_ties(self) -> None:
        # b, c, d of the path all have degree 2; c wins via betweenness.
        report = build_graph_analysis(_path_graph())
        scores = {entry["node_id"]: entry["scores"] for entry in report["god_nodes"]}
        assert Decimal(scores["c"]["betweenness"]) > Decimal(scores["b"]["betweenness"])
        assert Decimal(scores["c"]["composite"]) > Decimal(scores["b"]["composite"])

    def test_pagerank_component_rewards_endorsement(self) -> None:
        # Star into hub: hub's PageRank is the maximum.
        report = build_graph_analysis(_star_in())
        scores = {entry["node_id"]: entry["scores"] for entry in report["god_nodes"]}
        assert Decimal(scores["hub"]["pagerank"]) > Decimal(scores["l1"]["pagerank"])


# ==================================================== surprising connections


class TestSurprisingConnections:
    def test_empty_when_no_bridges(self) -> None:
        report = build_graph_analysis(_star_in())
        assert report["surprising_connections"] == []

    def test_entry_shape(self) -> None:
        report = build_graph_analysis(_two_communities_sole_link())
        entry = report["surprising_connections"][0]
        assert set(entry) == {
            "rank",
            "source",
            "target",
            "relation",
            "kind",
            "surprise_score",
            "confidence",
            "components",
            "explanations",
            "evidence_node_ids",
            "alternative_path_hops",
        }

    def test_evidence_node_ids_are_the_endpoints(self) -> None:
        report = build_graph_analysis(_two_communities_sole_link())
        entry = report["surprising_connections"][0]
        assert entry["evidence_node_ids"] == ["b", "c"]

    def test_sole_link_explanation_codes(self) -> None:
        report = build_graph_analysis(_two_communities_sole_link())
        codes = [e["code"] for e in report["surprising_connections"][0]["explanations"]]
        assert "CROSS_COMMUNITY" in codes
        assert "SOLE_COMMUNITY_LINK" in codes
        assert "NO_ALTERNATIVE_PATH" in codes

    def test_alternative_path_reported_when_present(self) -> None:
        report = build_graph_analysis(_redundant_bridge())
        entry = report["surprising_connections"][0]
        codes = [e["code"] for e in entry["explanations"]]
        assert "UNEXPECTED_PATH" in codes
        assert entry["alternative_path_hops"] >= 3
        assert "SOLE_COMMUNITY_LINK" not in codes

    def test_unexpected_path_detail_carries_hops(self) -> None:
        report = build_graph_analysis(_redundant_bridge())
        details = [e["detail"] for e in report["surprising_connections"][0]["explanations"]]
        assert any("3 hops" in detail for detail in details)

    def test_sole_link_outranks_redundant_bridge(self) -> None:
        nodes = [
            _node("a", sig="s1"),
            _node("b", sig="s1"),
            _node("c", sig="s2"),
            _node("d", sig="s2"),
            _node("e", sig="s3"),
            _node("f", sig="s3"),
        ]
        links = [
            _link("a", "b"),
            _link("c", "d"),
            _link("e", "f"),
            _link("b", "c"),  # sole link s1<->s2
            _link("a", "e"),
            _link("b", "e"),  # redundant pair s1<->s3
        ]
        report = build_graph_analysis(_graph(nodes, links))
        pairs = [(b["source"], b["target"]) for b in report["surprising_connections"]]
        assert pairs[0] == ("b", "c")

    def test_cross_domain_explanation(self) -> None:
        graph = _graph([_node("a", domain="P1"), _node("b", domain="P7")], [_link("a", "b")])
        entry = build_graph_analysis(graph)["surprising_connections"][0]
        codes = [e["code"] for e in entry["explanations"]]
        assert "CROSS_DOMAIN" in codes
        assert entry["kind"] == "cross_domain"

    def test_degree_anomaly_explanation(self) -> None:
        # Leaf `p` (degree 1) bridges to hub (degree 6) across domains.
        nodes = [_node("hub", domain="P2"), _node("p", domain="P5")]
        nodes += [_node(f"m{i}", domain="P2") for i in range(6)]
        links = [_link(f"m{i}", "hub") for i in range(6)]
        links.append(_link("p", "hub"))
        report = build_graph_analysis(_graph(nodes, links))
        entry = _entry(report, "surprising_connections", "source", "p")
        codes = [e["code"] for e in entry["explanations"]]
        assert "DEGREE_ANOMALY" in codes

    def test_no_degree_anomaly_between_two_hubs(self) -> None:
        nodes = [_node("h1", domain="P1"), _node("h2", domain="P2")]
        nodes += [_node(f"a{i}", domain="P1") for i in range(3)]
        nodes += [_node(f"b{i}", domain="P2") for i in range(3)]
        links = [_link(f"a{i}", "h1") for i in range(3)]
        links += [_link(f"b{i}", "h2") for i in range(3)]
        links.append(_link("h1", "h2"))
        report = build_graph_analysis(_graph(nodes, links))
        codes = [e["code"] for e in report["surprising_connections"][0]["explanations"]]
        assert "DEGREE_ANOMALY" not in codes

    def test_rare_relation_explanation(self) -> None:
        nodes = [_node(f"n{i}", domain="P1") for i in range(10)] + [_node("x", domain="P7")]
        links = [_link("n0", f"n{i}") for i in range(1, 10)]  # 9 x supports
        links.append(_link("n0", "x", relation="mentions"))  # the rare one
        report = build_graph_analysis(_graph(nodes, links))
        entry = _entry(report, "surprising_connections", "target", "x")
        codes = [e["code"] for e in entry["explanations"]]
        assert "RARE_RELATION" in codes

    def test_common_relation_has_no_rarity_explanation(self) -> None:
        graph = _graph([_node("a", domain="P1"), _node("b", domain="P7")], [_link("a", "b")])
        entry = build_graph_analysis(graph)["surprising_connections"][0]
        codes = [e["code"] for e in entry["explanations"]]
        assert "RARE_RELATION" not in codes

    @pytest.mark.parametrize("confidence", ["AMBIGUOUS", "INFERRED"])
    def test_low_confidence_explanation(self, confidence: str) -> None:
        graph = _graph(
            [_node("a", domain="P1"), _node("b", domain="P7")],
            [_link("a", "b", confidence=confidence)],
        )
        entry = build_graph_analysis(graph)["surprising_connections"][0]
        codes = [e["code"] for e in entry["explanations"]]
        assert "LOW_CONFIDENCE" in codes

    def test_extracted_confidence_has_no_low_confidence_explanation(self) -> None:
        graph = _graph([_node("a", domain="P1"), _node("b", domain="P7")], [_link("a", "b")])
        entry = build_graph_analysis(graph)["surprising_connections"][0]
        codes = [e["code"] for e in entry["explanations"]]
        assert "LOW_CONFIDENCE" not in codes

    def test_confidence_buckets(self) -> None:
        # Sole community link: (3*1 + 2*0 + 1*1)/6 = 0.6667 -> medium.
        report = build_graph_analysis(_two_communities_sole_link())
        assert report["surprising_connections"][0]["confidence"] == "medium"

    def test_confidence_bucket_high(self) -> None:
        # distance 1.0, rarity >= 0.9, novelty 1 -> (3 + 1.8 + 1)/6 = 0.9667 -> high.
        nodes = [_node("a", sig="s1"), _node("b", sig="s1")]
        nodes += [_node("c", sig="s2"), _node("d", sig="s2")]
        nodes += [_node(f"f{i}", sig="s1") for i in range(8)]
        links = [_link("a", "b"), _link("c", "d")]
        links += [_link("a", f"f{i}") for i in range(8)]
        links.append(_link("b", "c", relation="mentions"))
        report = build_graph_analysis(_graph(nodes, links))
        entry = _entry(report, "surprising_connections", "source", "b")
        assert entry["confidence"] == "high"

    def test_confidence_bucket_low(self) -> None:
        # Redundant bridge, common relation, version 5 nodes: low surprise.
        graph = _redundant_bridge()
        for node in graph["nodes"]:
            node["version"] = 5
        report = build_graph_analysis(graph)
        assert report["surprising_connections"][0]["confidence"] == "low"

    def test_top_surprising_caps(self) -> None:
        nodes = [_node("hub", domain="P1")]
        links: list[dict[str, Any]] = []
        for i in range(6):
            nodes.append(_node(f"x{i}", domain="P7"))
            links.append(_link("hub", f"x{i}"))
        report = build_graph_analysis(_graph(nodes, links), top_surprising=2)
        assert len(report["surprising_connections"]) == 2

    def test_explanation_details_are_nonempty_strings(self) -> None:
        report = build_graph_analysis(_rich_graph())
        for entry in report["surprising_connections"]:
            assert entry["explanations"], entry
            for explanation in entry["explanations"]:
                assert set(explanation) == {"code", "detail"}
                assert explanation["detail"].strip()

    def test_explanations_sorted_by_stable_code_order(self) -> None:
        report = build_graph_analysis(_two_communities_sole_link())
        codes = [e["code"] for e in report["surprising_connections"][0]["explanations"]]
        order = [
            "CROSS_DOMAIN",
            "CROSS_COMMUNITY",
            "SOLE_COMMUNITY_LINK",
            "NO_ALTERNATIVE_PATH",
            "UNEXPECTED_PATH",
            "DEGREE_ANOMALY",
            "RARE_RELATION",
            "LOW_CONFIDENCE",
        ]
        assert codes == sorted(codes, key=order.index)

    def test_ranks_dense(self) -> None:
        report = build_graph_analysis(_rich_graph())
        ranks = [entry["rank"] for entry in report["surprising_connections"]]
        assert ranks == list(range(1, len(ranks) + 1))


# ======================================================== suggested questions


class TestSuggestedQuestions:
    def test_hub_dependency_question(self) -> None:
        report = build_graph_analysis(_star_in())
        hub_q = [q for q in report["suggested_questions"] if q["type"] == "hub_dependency"]
        assert len(hub_q) == 1
        question = hub_q[0]
        assert question["question"] == "What depends on `hub`?"
        assert question["evidence_node_ids"] == ["hub"]
        assert question["query"] == {"kind": "dependents", "node": "hub"}

    def test_hub_question_skipped_without_incoming(self) -> None:
        # x in the barbell fixture has only outgoing links -> no question.
        report = build_graph_analysis(_barbell_with_source_hub())
        for question in report["suggested_questions"]:
            assert question["query"] != {"kind": "dependents", "node": "x"}

    def test_bridge_path_question(self) -> None:
        report = build_graph_analysis(_two_communities_sole_link())
        bridge_q = [q for q in report["suggested_questions"] if q["type"] == "bridge_path"]
        assert len(bridge_q) == 1
        question = bridge_q[0]
        assert question["question"] == "How is `b` connected to `c`?"
        assert question["query"] == {"kind": "path", "source": "b", "target": "c"}
        assert question["evidence_node_ids"] == ["b", "c"]

    def test_isolated_community_question(self) -> None:
        report = build_graph_analysis(_disconnected_components())
        community_q = [q for q in report["suggested_questions"] if q["type"] == "isolated_community"]
        assert community_q, report["suggested_questions"]
        question = community_q[0]
        assert "separate from the rest of the graph" in question["question"]
        assert question["query"]["kind"] == "community_boundary"
        assert sorted(question["query"]["members"]) == sorted(question["evidence_node_ids"])

    def test_whole_graph_community_is_not_isolated(self) -> None:
        nodes = [_node("a", sig="only"), _node("b", sig="only")]
        report = build_graph_analysis(_graph(nodes, [_link("a", "b")]))
        assert [q for q in report["suggested_questions"] if q["type"] == "isolated_community"] == []

    def test_isolated_node_question(self) -> None:
        # a, b, c form a triangle (degree 2 each); z is degree 0.
        graph = _graph(
            [_node("a"), _node("b"), _node("c"), _node("z")],
            [_link("a", "b"), _link("b", "c"), _link("c", "a")],
        )
        report = build_graph_analysis(graph)
        iso_q = [q for q in report["suggested_questions"] if q["type"] == "isolated_node"]
        assert len(iso_q) == 1
        assert iso_q[0]["question"] == "What connects `z` to the rest of the system?"
        assert iso_q[0]["query"] == {"kind": "neighborhood", "node": "z"}

    def test_degree_one_node_is_isolated_candidate(self) -> None:
        graph = _graph([_node("a"), _node("b"), _node("c")], [_link("a", "b"), _link("b", "c")])
        report = build_graph_analysis(graph)
        iso = {q["query"]["node"] for q in report["suggested_questions"] if q["type"] == "isolated_node"}
        assert iso == {"a", "c"}

    def test_ambiguous_relationship_question(self) -> None:
        graph = _graph([_node("a"), _node("b")], [_link("a", "b", confidence="AMBIGUOUS", relation="mentions")])
        report = build_graph_analysis(graph)
        amb_q = [q for q in report["suggested_questions"] if q["type"] == "ambiguous_relationship"]
        assert len(amb_q) == 1
        assert amb_q[0]["question"] == "What is the exact relationship between `a` and `b`?"
        assert amb_q[0]["query"] == {"kind": "edge_evidence", "source": "a", "target": "b"}

    def test_question_entry_shape(self) -> None:
        report = build_graph_analysis(_rich_graph())
        for question in report["suggested_questions"]:
            assert set(question) == {"rank", "type", "question", "why", "evidence_node_ids", "query"}

    def test_ranks_dense_after_interleave(self) -> None:
        report = build_graph_analysis(_rich_graph())
        ranks = [q["rank"] for q in report["suggested_questions"]]
        assert ranks == list(range(1, len(ranks) + 1))

    def test_types_are_diversified_by_interleave(self) -> None:
        # With several god nodes AND bridges, the first two questions must
        # not share a type (round-robin interleave, Graphify's zip_longest).
        report = build_graph_analysis(_rich_graph())
        types = [q["type"] for q in report["suggested_questions"]]
        assert len(types) >= 2
        assert types[0] != types[1]

    def test_top_questions_caps(self) -> None:
        report = build_graph_analysis(_rich_graph(), top_questions=3)
        assert len(report["suggested_questions"]) == 3

    def test_no_signal_fallback(self) -> None:
        report = build_graph_analysis(_graph([], []))
        assert len(report["suggested_questions"]) == 1
        fallback = report["suggested_questions"][0]
        assert fallback["type"] == "no_signal"
        assert fallback["question"] is None
        assert fallback["query"] is None

    def test_every_question_carries_evidence(self) -> None:
        report = build_graph_analysis(_rich_graph())
        for question in report["suggested_questions"]:
            if question["type"] == "no_signal":
                continue
            assert question["evidence_node_ids"], question
            assert question["query"] is not None

    def test_every_generated_question_is_answerable(self) -> None:
        """The answerability proof: every question the generator emits can be
        resolved against the very read model that produced it, and the answer
        carries type-appropriate content."""
        graph = _rich_graph()
        report = build_graph_analysis(graph)
        assert report["suggested_questions"]
        for question in report["suggested_questions"]:
            if question["type"] == "no_signal":
                continue
            answer = answer_question(graph, question)
            assert answer["kind"] == question["query"]["kind"]
            assert answer["answerable"] is True
            if answer["kind"] == "dependents":
                assert answer["count"] >= 1
            elif answer["kind"] == "path":
                assert answer["reachable"] is True
                assert answer["path"][0] == question["query"]["source"]
                assert answer["path"][-1] == question["query"]["target"]
            elif answer["kind"] == "community_boundary":
                assert "isolated" in answer
            elif answer["kind"] == "neighborhood":
                assert answer["degree"] == len(answer["neighbors"])
            elif answer["kind"] == "edge_evidence":
                assert answer["count"] >= 1
            else:  # pragma: no cover - guards future kinds
                raise AssertionError(f"unhandled answer kind {answer['kind']}")

    def test_question_text_uses_labels(self) -> None:
        graph = _graph(
            [_node("n1", label="Event Store"), _node("n2", label="Projection")],
            [_link("n2", "n1")],
        )
        report = build_graph_analysis(graph)
        hub_q = [q for q in report["suggested_questions"] if q["type"] == "hub_dependency"]
        assert hub_q[0]["question"] == "What depends on `Event Store`?"
        # ...but evidence stays node ids, ready to query.
        assert hub_q[0]["evidence_node_ids"] == ["n1"]


# ============================================================= answer engine


class TestAnswerQuestion:
    def test_dependents_sorted(self) -> None:
        answer = answer_question(_star_in(), {"query": {"kind": "dependents", "node": "hub"}})
        assert answer == {
            "kind": "dependents",
            "node": "hub",
            "dependents": ["l1", "l2", "l3", "l4"],
            "count": 4,
            "answerable": True,
        }

    def test_dependents_empty(self) -> None:
        answer = answer_question(_star_in(), {"query": {"kind": "dependents", "node": "l1"}})
        assert answer["dependents"] == []
        assert answer["count"] == 0

    def test_dependents_unknown_node_raises(self) -> None:
        with pytest.raises(QuestionError):
            answer_question(_star_in(), {"query": {"kind": "dependents", "node": "ghost"}})

    def test_path_direct(self) -> None:
        answer = answer_question(_path_graph(), {"query": {"kind": "path", "source": "a", "target": "e"}})
        assert answer["reachable"] is True
        assert answer["path"] == ["a", "b", "c", "d", "e"]
        assert answer["cost"] == 4

    def test_path_unreachable(self) -> None:
        answer = answer_question(_path_graph(), {"query": {"kind": "path", "source": "e", "target": "a"}})
        assert answer["reachable"] is False
        assert answer["path"] is None

    def test_path_unknown_endpoint_raises(self) -> None:
        with pytest.raises(QuestionError):
            answer_question(_path_graph(), {"query": {"kind": "path", "source": "a", "target": "ghost"}})

    def test_path_same_endpoint_raises(self) -> None:
        with pytest.raises(QuestionError):
            answer_question(_path_graph(), {"query": {"kind": "path", "source": "a", "target": "a"}})

    def test_community_boundary_isolated(self) -> None:
        answer = answer_question(
            _disconnected_components(), {"query": {"kind": "community_boundary", "members": ["c", "d", "e"]}}
        )
        assert answer["isolated"] is True
        assert answer["boundary_links"] == []
        assert answer["count"] == 0

    def test_community_boundary_with_links(self) -> None:
        answer = answer_question(
            _two_communities_sole_link(), {"query": {"kind": "community_boundary", "members": ["a", "b"]}}
        )
        assert answer["isolated"] is False
        assert answer["count"] == 1
        assert answer["boundary_links"][0]["source"] == "b"
        assert answer["boundary_links"][0]["direction"] == "outgoing"

    def test_community_boundary_incoming_direction(self) -> None:
        answer = answer_question(
            _two_communities_sole_link(), {"query": {"kind": "community_boundary", "members": ["c", "d"]}}
        )
        assert answer["boundary_links"][0]["direction"] == "incoming"

    def test_neighborhood(self) -> None:
        answer = answer_question(_path_graph(), {"query": {"kind": "neighborhood", "node": "c"}})
        assert answer["neighbors"] == ["b", "d"]
        assert answer["degree"] == 2

    def test_neighborhood_isolated_node(self) -> None:
        graph = _graph([_node("a"), _node("z")], [])
        answer = answer_question(graph, {"query": {"kind": "neighborhood", "node": "z"}})
        assert answer["neighbors"] == []
        assert answer["degree"] == 0

    def test_edge_evidence_both_directions(self) -> None:
        graph = _graph(
            [_node("a"), _node("b")],
            [_link("a", "b"), _link("b", "a", relation="contradicts")],
        )
        answer = answer_question(graph, {"query": {"kind": "edge_evidence", "source": "a", "target": "b"}})
        assert answer["count"] == 2
        directions = {link["direction"] for link in answer["links"]}
        assert directions == {"forward", "reverse"}
        relations = {link["relation"] for link in answer["links"]}
        assert relations == {"supports", "contradicts"}

    def test_edge_evidence_empty(self) -> None:
        answer = answer_question(_path_graph(), {"query": {"kind": "edge_evidence", "source": "a", "target": "e"}})
        assert answer["count"] == 0
        assert answer["links"] == []

    def test_unknown_kind_raises(self) -> None:
        with pytest.raises(QuestionError):
            answer_question(_path_graph(), {"query": {"kind": "teleport", "node": "a"}})

    def test_missing_query_raises(self) -> None:
        with pytest.raises(QuestionError):
            answer_question(_path_graph(), {"query": None})
        with pytest.raises(QuestionError):
            answer_question(_path_graph(), {})

    def test_answer_is_canonical_json_safe(self) -> None:
        graph = _rich_graph()
        report = build_graph_analysis(graph)
        for question in report["suggested_questions"]:
            if question["query"] is None:
                continue
            json.loads(canonical_json(answer_question(graph, question)))


# ============================================================ token benchmark


class TestTokenBenchmark:
    def test_benchmark_shape(self) -> None:
        report = build_graph_analysis(_star_in())
        benchmark = report["token_benchmark"]
        assert set(benchmark) == {
            "estimator",
            "graph_tokens",
            "report_json_tokens",
            "report_markdown_tokens",
            "savings_ratio",
            "compression_ratio",
        }

    def test_estimator_matches_retrieval(self) -> None:
        graph = _star_in()
        report = build_graph_analysis(graph)
        assert report["token_benchmark"]["graph_tokens"] == estimated_tokens(graph)

    def test_report_json_tokens_exclude_the_benchmark_itself(self) -> None:
        report = build_graph_analysis(_star_in())
        stripped = {key: value for key, value in report.items() if key != "token_benchmark"}
        assert report["token_benchmark"]["report_json_tokens"] == estimated_tokens(stripped)

    def test_markdown_tokens_match_rendering(self) -> None:
        report = build_graph_analysis(_star_in())
        markdown = render_graph_analysis_markdown(report)
        expected = -(-len(markdown.encode("utf-8")) // 4)
        assert report["token_benchmark"]["report_markdown_tokens"] == expected

    def test_report_is_smaller_than_raw_graph(self) -> None:
        nodes = [_node(f"n{i}", label=f"Node number {i} with a verbose label") for i in range(60)]
        links = [_link(f"n{i}", f"n{(i + 1) % 60}") for i in range(60)]
        links += [_link(f"n{i}", f"n{(i + 7) % 60}", relation="mentions") for i in range(60)]
        report = build_graph_analysis(_graph(nodes, links))
        benchmark = report["token_benchmark"]
        assert benchmark["report_json_tokens"] < benchmark["graph_tokens"]
        assert benchmark["report_markdown_tokens"] < benchmark["graph_tokens"]

    def test_savings_ratio_unit_interval(self) -> None:
        report = build_graph_analysis(_rich_graph())
        savings = Decimal(report["token_benchmark"]["savings_ratio"])
        assert Decimal(0) < savings < Decimal(1)

    def test_compression_ratio_above_one(self) -> None:
        report = build_graph_analysis(_rich_graph())
        assert Decimal(report["token_benchmark"]["compression_ratio"]) > Decimal(1)

    def test_ratios_are_12dp_strings(self) -> None:
        report = build_graph_analysis(_star_in())
        pattern = re.compile(r"^-?\d+\.\d{12}$")
        assert pattern.match(report["token_benchmark"]["savings_ratio"])
        assert pattern.match(report["token_benchmark"]["compression_ratio"])

    def test_empty_graph_benchmark_is_safe(self) -> None:
        report = build_graph_analysis(_graph([], []))
        benchmark = report["token_benchmark"]
        assert benchmark["graph_tokens"] > 0  # canonical json of an empty read model
        # Tiny graphs are honestly reported: the report can be bigger than the
        # raw read model, so savings may be <= 0 — never fabricated positive.
        Decimal(benchmark["savings_ratio"])
        assert benchmark["compression_ratio"] == "1.000000000000" or Decimal(benchmark["compression_ratio"]) > 0


# ================================================================== markdown


class TestMarkdown:
    def test_header(self) -> None:
        markdown = render_graph_analysis_markdown(build_graph_analysis(_star_in()))
        assert markdown.startswith("# SOUL Graph Analysis Report")

    def test_sections_in_order(self) -> None:
        markdown = render_graph_analysis_markdown(build_graph_analysis(_rich_graph()))
        god = markdown.index("## God nodes")
        surprising = markdown.index("## Surprising connections")
        questions = markdown.index("## Suggested questions")
        benchmark = markdown.index("## Token benchmark")
        assert god < surprising < questions < benchmark

    def test_god_node_table_row(self) -> None:
        markdown = render_graph_analysis_markdown(build_graph_analysis(_star_in()))
        assert "| 1 | `hub` |" in markdown
        assert "degree-only #" in markdown

    def test_surprising_connection_lines(self) -> None:
        markdown = render_graph_analysis_markdown(build_graph_analysis(_two_communities_sole_link()))
        assert "`b`" in markdown and "`c`" in markdown
        assert "SOLE_COMMUNITY_LINK" in markdown

    def test_question_lines_carry_evidence(self) -> None:
        markdown = render_graph_analysis_markdown(build_graph_analysis(_star_in()))
        assert "What depends on `hub`?" in markdown
        assert "evidence: hub" in markdown
        assert "query: dependents" in markdown

    def test_benchmark_section_values(self) -> None:
        report = build_graph_analysis(_star_in())
        markdown = render_graph_analysis_markdown(report)
        assert f"~{report['token_benchmark']['graph_tokens']} tokens" in markdown
        assert "savings" in markdown

    def test_render_is_deterministic(self) -> None:
        report = build_graph_analysis(_rich_graph())
        assert render_graph_analysis_markdown(report) == render_graph_analysis_markdown(report)

    def test_render_rejects_foreign_schema(self) -> None:
        with pytest.raises(GraphReportError):
            render_graph_analysis_markdown({"schema_version": 999})

    def test_empty_sections_render_honestly(self) -> None:
        markdown = render_graph_analysis_markdown(build_graph_analysis(_graph([], [])))
        assert "none" in markdown

    def test_markdown_is_pure_output(self) -> None:
        report = build_graph_analysis(_rich_graph())
        before = canonical_json(report)
        render_graph_analysis_markdown(report)
        assert canonical_json(report) == before


# ============================================================== determinism


class TestDeterminism:
    @pytest.mark.parametrize("seed", [0, 1, 2, 7, 42])
    def test_input_permutation_invariance(self, seed: int) -> None:
        graph = _rich_graph()
        shuffled = _graph(list(graph["nodes"]), list(graph["links"]))
        rng = random.Random(seed)
        rng.shuffle(shuffled["nodes"])
        rng.shuffle(shuffled["links"])
        assert canonical_json(build_graph_analysis(shuffled)) == canonical_json(build_graph_analysis(graph))

    def test_repeated_builds_identical(self) -> None:
        graph = _rich_graph()
        assert canonical_json(build_graph_analysis(graph)) == canonical_json(build_graph_analysis(graph))

    @given(seed=st.integers(min_value=0, max_value=10_000))
    @settings(max_examples=15, derandomize=True)
    def test_property_permutation_invariance(self, seed: int) -> None:
        graph = _rich_graph()
        shuffled = _graph(list(graph["nodes"]), list(graph["links"]))
        rng = random.Random(seed)
        rng.shuffle(shuffled["nodes"])
        rng.shuffle(shuffled["links"])
        assert canonical_json(build_graph_analysis(shuffled)) == canonical_json(build_graph_analysis(graph))

    def test_output_is_canonical_json_safe(self) -> None:
        report = build_graph_analysis(_rich_graph())
        text = canonical_json(report)
        assert json.loads(text)["kind"] == GRAPH_REPORT_KIND

    def test_no_absolute_path_shapes_leak(self) -> None:
        nodes = [_node("n1", label="docs/guide.md"), _node("n2")]
        report = build_graph_analysis(_graph(nodes, [_link("n2", "n1")]))
        assert "/home/" not in canonical_json(report)
        assert "/Users/" not in canonical_json(report)

    def test_absolute_path_labels_fail_closed(self) -> None:
        nodes = [_node("n1", label="/home/user/secret/thing"), _node("n2")]
        with pytest.raises(GraphReportError):
            build_graph_analysis(_graph(nodes, [_link("n2", "n1")]))


# ====================================================================== CLI


def _seeded_paths(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "soul.sqlite3"
    out_path = tmp_path / "out"
    store = EventStore(db_path)
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="KROOT",
            domain=PrimaryDomain.P1,
            node=Node(
                node_id="KROOT",
                kind=NodeKind.ROOT,
                domain=PrimaryDomain.P1,
                summary="Knowledge Root",
                provenance=("soul://roots",),
                source_version_id="sv-test",
            ),
            source_version_id="sv-test",
        ),
        idempotency_key="test:upsert:KROOT",
    )
    GraphProjector(store, output_root=out_path).rebuild()
    store.close()
    return db_path, out_path


def _base(db_path: Path, out_path: Path) -> list[str]:
    return ["--store", str(db_path), "--output-root", str(out_path)]


class TestReportCli:
    def test_happy_path_markdown_on_stdout(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        code = main(_base(db_path, out_path) + ["report"])
        captured = capsys.readouterr()
        assert code == 0
        assert captured.out.startswith("# SOUL Graph Analysis Report")
        assert "## God nodes" in captured.out

    def test_benchmark_printed_to_stderr(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        code = main(_base(db_path, out_path) + ["report"])
        captured = capsys.readouterr()
        assert code == 0
        assert "token benchmark" in captured.err
        assert "tokens" in captured.err

    def test_json_format(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        code = main(_base(db_path, out_path) + ["report", "--format", "json"])
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["kind"] == GRAPH_REPORT_KIND
        assert payload["schema_version"] == GRAPH_REPORT_SCHEMA_VERSION

    def test_out_writes_both_artifacts(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        code = main(_base(db_path, out_path) + ["report", "--out", "reports"])
        capsys.readouterr()
        assert code == 0
        target = out_path / "reports"
        assert (target / "graph-report.json").is_file()
        assert (target / "graph-report.md").is_file()
        payload = json.loads((target / "graph-report.json").read_text(encoding="utf-8"))
        assert payload["kind"] == GRAPH_REPORT_KIND
        assert (target / "graph-report.md").read_text(encoding="utf-8").startswith("# SOUL Graph Analysis Report")

    def test_out_is_idempotent(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        assert main(_base(db_path, out_path) + ["report", "--out", "reports"]) == 0
        capsys.readouterr()
        first = (out_path / "reports" / "graph-report.json").read_bytes()
        assert main(_base(db_path, out_path) + ["report", "--out", "reports"]) == 0
        capsys.readouterr()
        assert (out_path / "reports" / "graph-report.json").read_bytes() == first

    def test_without_active_generation_fails_closed(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path = tmp_path / "soul.sqlite3"
        EventStore(db_path).close()
        out_path = tmp_path / "out"
        code = main(_base(db_path, out_path) + ["report"])
        captured = capsys.readouterr()
        assert code == 1
        envelope = json.loads(captured.err)
        assert envelope["error"]["code"] == "NO_ACTIVE_GENERATION"

    def test_out_escape_is_usage_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        code = main(_base(db_path, out_path) + ["report", "--out", "../../outside"])
        captured = capsys.readouterr()
        assert code == 2
        envelope = json.loads(captured.err)
        assert envelope["error"]["code"] == "CLI_USAGE"

    @pytest.mark.parametrize("flag", ["--top-god", "--top-surprising", "--top-questions"])
    def test_non_positive_tops_are_usage_errors(
        self, flag: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        code = main(_base(db_path, out_path) + ["report", flag, "0"])
        captured = capsys.readouterr()
        assert code == 2
        envelope = json.loads(captured.err)
        assert envelope["error"]["code"] == "CLI_USAGE"

    def test_top_flags_shape_output(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        code = main(_base(db_path, out_path) + ["report", "--format", "json", "--top-god", "1"])
        captured = capsys.readouterr()
        assert code == 0
        payload = json.loads(captured.out)
        assert payload["parameters"]["top_god"] == 1
        assert len(payload["god_nodes"]) <= 1

    def test_report_is_not_a_knowledge_or_http_op(self) -> None:
        assert "report" not in KNOWLEDGE_OPERATIONS
        assert "report" not in OPS_OPERATIONS
        assert len(KNOWLEDGE_OPERATIONS) == 14

    def test_report_does_not_touch_the_store(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        db_path, out_path = _seeded_paths(tmp_path)
        before = EventStore(db_path)
        try:
            count = int(before.execute_raw("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"])
        finally:
            before.close()
        assert main(_base(db_path, out_path) + ["report"]) == 0
        capsys.readouterr()
        after = EventStore(db_path)
        try:
            assert int(after.execute_raw("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"]) == count
        finally:
            after.close()
