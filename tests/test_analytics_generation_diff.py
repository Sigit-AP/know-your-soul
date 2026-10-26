"""Tests for ``know_your_soul.analytics.generation_diff`` (ATM F-C1).

Pins the cross-generation diff contract: canonical edge keys
``(source, target, relation)``, reversed edges detected explicitly and never
double counted as add+remove, per-node P/M/E score deltas in 12-decimal
fixed-point with node versions, community remap (via
``analytics.communities.remap_communities`` semantics) and label changes, a
deterministic summary, and byte-stable ``DIFF.md`` rendering.
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.generation_diff import (
    DIFF_SCHEMA_VERSION,
    GenerationDiffError,
    diff_generations,
    render_diff_markdown,
)
from know_your_soul.domain import canonical_json

# ------------------------------------------------------------------ helpers


def _node(
    node_id: str,
    *,
    label: str | None = None,
    version: int = 1,
    p: str = "0.000000000000",
    m: str = "0.000000000000",
    e: str = "0.000000000000",
    sig: str | None = None,
) -> dict[str, object]:
    node: dict[str, object] = {
        "id": node_id,
        "label": label if label is not None else node_id,
        "version": version,
        "scores": {"p": p, "m": m, "e": e, "uncertainty": "1.000000000000", "confidence": "0.000000000000"},
    }
    if sig is not None:
        node["community_sig"] = sig
    return node


def _link(source: str, target: str, relation: str = "supports") -> dict[str, object]:
    return {"source": source, "target": target, "relation": relation}


def _graph(nodes: list[dict[str, object]], links: list[dict[str, object]]) -> dict[str, object]:
    return {"nodes": nodes, "links": links}


def _edge_keys(diff: dict[str, object], field: str) -> set[tuple[str, str, str]]:
    return {(str(e["source"]), str(e["target"]), str(e["relation"])) for e in diff[field]}  # type: ignore[index]


# ---------------------------------------------------------------- node diff


class TestNodeDiff:
    def test_identical_graphs_are_an_empty_diff(self) -> None:
        graph = _graph([_node("a"), _node("b")], [_link("a", "b")])
        diff = diff_generations(graph, graph)
        assert diff["schema_version"] == DIFF_SCHEMA_VERSION
        assert diff["nodes_added"] == []
        assert diff["nodes_removed"] == []
        assert diff["nodes_changed"] == []
        assert diff["edges_added"] == []
        assert diff["edges_removed"] == []
        assert diff["edges_reversed"] == []

    def test_added_and_removed_nodes_are_sorted(self) -> None:
        previous = _graph([_node("b"), _node("a"), _node("c")], [])
        current = _graph([_node("c"), _node("d")], [])
        diff = diff_generations(previous, current)
        assert diff["nodes_added"] == ["d"]
        assert diff["nodes_removed"] == ["a", "b"]

    def test_score_change_reports_deltas_and_versions(self) -> None:
        previous = _graph([_node("a", version=3, p="0.500000000000")], [])
        current = _graph([_node("a", version=4, p="0.750000000000", e="0.100000000000")], [])
        diff = diff_generations(previous, current)
        changed = diff["nodes_changed"]
        assert len(changed) == 1
        entry = changed[0]
        assert entry["node_id"] == "a"
        assert entry["version_from"] == 3
        assert entry["version_to"] == 4
        assert entry["scores"]["p"] == {
            "from": "0.500000000000",
            "to": "0.750000000000",
            "delta": "0.250000000000",
        }
        assert entry["scores"]["e"]["delta"] == "0.100000000000"
        assert "m" not in entry["scores"]
        assert "scores" in entry["changes"]

    def test_negative_delta_renders_with_sign(self) -> None:
        previous = _graph([_node("a", m="0.500000000000")], [])
        current = _graph([_node("a", m="0.250000000000")], [])
        entry = diff_generations(previous, current)["nodes_changed"][0]
        assert entry["scores"]["m"]["delta"] == "-0.250000000000"

    def test_label_change_is_reported(self) -> None:
        previous = _graph([_node("a", label="old")], [])
        current = _graph([_node("a", label="new")], [])
        entry = diff_generations(previous, current)["nodes_changed"][0]
        assert entry["label_from"] == "old"
        assert entry["label_to"] == "new"
        assert "label" in entry["changes"]

    def test_version_only_change_is_reported(self) -> None:
        previous = _graph([_node("a", version=1)], [])
        current = _graph([_node("a", version=2)], [])
        entry = diff_generations(previous, current)["nodes_changed"][0]
        assert "version" in entry["changes"]
        assert "scores" not in entry["changes"]

    def test_unchanged_node_is_not_listed(self) -> None:
        previous = _graph([_node("a"), _node("b")], [])
        current = _graph([_node("a"), _node("b")], [])
        assert diff_generations(previous, current)["nodes_changed"] == []

    def test_input_ordering_does_not_change_the_diff(self) -> None:
        previous = _graph([_node("b"), _node("a")], [_link("b", "a"), _link("a", "b", "causes")])
        current = _graph([_node("c"), _node("a", p="0.100000000000")], [_link("c", "a")])
        forward = diff_generations(previous, current)
        shuffled_previous = _graph(list(reversed(previous["nodes"])), list(reversed(previous["links"])))  # type: ignore[arg-type]
        shuffled_current = _graph(list(reversed(current["nodes"])), list(reversed(current["links"])))  # type: ignore[arg-type]
        assert canonical_json(diff_generations(shuffled_previous, shuffled_current)) == canonical_json(forward)


# ---------------------------------------------------------------- edge diff


class TestEdgeDiff:
    def test_added_and_removed_edges(self) -> None:
        previous = _graph([], [_link("a", "b"), _link("c", "d")])
        current = _graph([], [_link("a", "b"), _link("d", "e", "derived_from")])
        diff = diff_generations(previous, current)
        assert _edge_keys(diff, "edges_added") == {("d", "e", "derived_from")}
        assert _edge_keys(diff, "edges_removed") == {("c", "d", "supports")}

    def test_reversed_edge_is_explicit_and_not_double_counted(self) -> None:
        previous = _graph([], [_link("a", "b")])
        current = _graph([], [_link("b", "a")])
        diff = diff_generations(previous, current)
        assert diff["edges_added"] == []
        assert diff["edges_removed"] == []
        reversed_edges = diff["edges_reversed"]
        assert len(reversed_edges) == 1
        entry = reversed_edges[0]
        assert entry["from"] == {"source": "a", "target": "b", "relation": "supports"}
        assert entry["to"] == {"source": "b", "target": "a", "relation": "supports"}

    def test_reversal_requires_the_same_relation(self) -> None:
        previous = _graph([], [_link("a", "b", "supports")])
        current = _graph([], [_link("b", "a", "contradicts")])
        diff = diff_generations(previous, current)
        assert diff["edges_reversed"] == []
        assert _edge_keys(diff, "edges_added") == {("b", "a", "contradicts")}
        assert _edge_keys(diff, "edges_removed") == {("a", "b", "supports")}

    def test_reversal_matching_is_one_to_one(self) -> None:
        # Two parallel-look edges collapse to one key per generation, but a
        # one-sided reversal must leave the unmatched direction visible.
        previous = _graph([], [_link("a", "b"), _link("x", "y")])
        current = _graph([], [_link("b", "a")])
        diff = diff_generations(previous, current)
        assert len(diff["edges_reversed"]) == 1
        assert _edge_keys(diff, "edges_removed") == {("x", "y", "supports")}
        assert diff["edges_added"] == []

    def test_edge_lists_are_sorted_canonically(self) -> None:
        previous = _graph([], [_link("m", "n"), _link("a", "z", "causes")])
        current = _graph([], [_link("z", "a"), _link("a", "m", "causes")])
        diff = diff_generations(previous, current)
        added = [(e["source"], e["target"], e["relation"]) for e in diff["edges_added"]]  # type: ignore[index]
        removed = [(e["source"], e["target"], e["relation"]) for e in diff["edges_removed"]]  # type: ignore[index]
        assert added == sorted(added)
        assert removed == sorted(removed)


# ------------------------------------------------------------- communities


class TestCommunityDiff:
    def test_moves_are_derived_from_community_sig(self) -> None:
        previous = _graph([_node("a", sig="sig1"), _node("b", sig="sig1"), _node("c", sig="sig2")], [])
        current = _graph([_node("a", sig="sig1"), _node("b", sig="sig2"), _node("c", sig="sig2")], [])
        diff = diff_generations(previous, current)
        assert diff["community_moves"] == [{"node_id": "b", "from": "sig1", "to": "sig2"}]

    def test_remap_uses_member_overlap(self) -> None:
        previous = _graph([_node(n, sig="sigA") for n in ("a", "b", "c")] + [_node("d", sig="sigB")], [])
        current = _graph([_node(n, sig="sigY") for n in ("a", "b", "c", "d")], [])
        diff = diff_generations(previous, current)
        communities = diff["communities"]
        assert communities["source"] == "derived"
        mapping = communities["mapping"]
        # sigA's members survive inside sigY; sigB is absorbed.
        assert {entry["previous_sig"] for entry in mapping} == {"sigA"}
        assert mapping[0]["current_sig"] == "sigY"
        assert mapping[0]["overlap"] == 3
        assert communities["unmatched_previous"] == ["sigB"]
        assert communities["unmatched_current"] == []

    def test_provided_communities_take_precedence(self) -> None:
        previous = _graph([_node("a"), _node("b")], [])
        current = _graph([_node("a"), _node("b")], [])
        provided_previous = [{"id": 0, "members": ["a", "b"]}]
        provided_current = [{"id": 0, "members": ["a"]}, {"id": 1, "members": ["b"]}]
        diff = diff_generations(
            previous,
            current,
            previous_communities=provided_previous,
            current_communities=provided_current,
        )
        communities = diff["communities"]
        assert communities["source"] == "provided"
        assert communities["mapping"][0]["overlap"] == 1
        assert communities["unmatched_current"] == [1]

    def test_no_community_information_is_honest(self) -> None:
        diff = diff_generations(_graph([_node("a")], []), _graph([_node("a")], []))
        assert diff["communities"]["source"] == "none"
        assert diff["communities"]["mapping"] == []

    def test_node_without_sig_is_not_a_move(self) -> None:
        previous = _graph([_node("a")], [])
        current = _graph([_node("a", sig="sig1")], [])
        diff = diff_generations(previous, current)
        assert diff["community_moves"] == [{"node_id": "a", "from": None, "to": "sig1"}]


# ------------------------------------------------------------------ summary


class TestSummary:
    def test_counts_match_the_sections(self) -> None:
        previous = _graph(
            [_node("a", sig="s1"), _node("b", p="0.100000000000"), _node("gone")],
            [_link("a", "b"), _link("gone", "a")],
        )
        current = _graph(
            [_node("a", sig="s2"), _node("b", p="0.200000000000"), _node("new")],
            [_link("b", "a"), _link("a", "new")],
        )
        diff = diff_generations(previous, current)
        summary = diff["summary"]
        assert summary["nodes_added"] == 1
        assert summary["nodes_removed"] == 1
        # "a" only moved communities (tracked separately); "b" changed scores.
        assert summary["nodes_changed"] == 1
        assert summary["edges_added"] == 1
        assert summary["edges_removed"] == 1
        assert summary["edges_reversed"] == 1
        assert summary["community_moves"] == 1
        assert summary["nodes_before"] == 3
        assert summary["nodes_after"] == 3
        assert summary["edges_before"] == 2
        assert summary["edges_after"] == 2

    def test_full_accounting_invariant(self) -> None:
        previous = _graph([], [_link("a", "b"), _link("c", "d"), _link("e", "f")])
        current = _graph([], [_link("b", "a"), _link("c", "d"), _link("f", "g")])
        diff = diff_generations(previous, current)
        prev_only = {("e", "f", "supports"), ("a", "b", "supports")}
        curr_only = {("f", "g", "supports"), ("b", "a", "supports")}
        assert len(diff["edges_removed"]) + len(diff["edges_reversed"]) == len(prev_only)
        assert len(diff["edges_added"]) + len(diff["edges_reversed"]) == len(curr_only)


# ------------------------------------------------------------------ render


class TestRenderMarkdown:
    def _sample_diff(self) -> dict[str, object]:
        previous = _graph(
            [_node("a", label="alpha", sig="s1"), _node("b", p="0.100000000000"), _node("gone")],
            [_link("a", "b"), _link("gone", "a")],
        )
        current = _graph(
            [_node("a", label="alpha-new", sig="s2"), _node("b", p="0.200000000000"), _node("new")],
            [_link("b", "a"), _link("a", "new")],
        )
        return diff_generations(previous, current)

    def test_render_is_deterministic_and_covers_sections(self) -> None:
        diff = self._sample_diff()
        first = render_diff_markdown(diff)
        second = render_diff_markdown(diff)
        assert first == second
        assert first.startswith("# DIFF")
        assert "a" in first and "gone" in first and "new" in first
        assert "alpha" in first and "alpha-new" in first
        assert "supports" in first

    def test_render_rejects_a_foreign_schema(self) -> None:
        with pytest.raises(GenerationDiffError):
            render_diff_markdown({"schema_version": 999})

    def test_render_rejects_absolute_path_shapes(self) -> None:
        previous = _graph([_node("a", label="/etc/passwd/secret")], [])
        current = _graph([_node("a", label="clean")], [])
        # Fail-closed at build time: the path shape can never reach an artifact.
        with pytest.raises(GenerationDiffError):
            diff_generations(previous, current)


# ------------------------------------------------------- property-based tests

_ID_POOL = ["a", "b", "c", "d", "e"]
_REL_POOL = ["supports", "causes", "parent_of"]


def _graph_strategy() -> st.SearchStrategy[dict[str, object]]:
    ids = st.lists(st.sampled_from(_ID_POOL), min_size=0, max_size=5, unique=True)
    links = st.lists(
        st.tuples(st.sampled_from(_ID_POOL), st.sampled_from(_ID_POOL), st.sampled_from(_REL_POOL)),
        min_size=0,
        max_size=6,
        unique=True,
    )

    @st.composite
    def build(draw: st.DrawFn) -> dict[str, object]:
        node_ids = draw(ids)
        nodes = [
            _node(node_id, p=draw(st.sampled_from(["0.000000000000", "0.500000000000", "1.000000000000"])))
            for node_id in node_ids
        ]
        edges = [_link(source, target, relation) for source, target, relation in draw(links)]
        return _graph(nodes, edges)

    return build()


@given(pair=st.tuples(_graph_strategy(), _graph_strategy()))
@settings(max_examples=60, derandomize=True)
def test_diff_is_antisymmetric(pair: tuple[dict[str, object], dict[str, object]]) -> None:
    """Swapping the generations swaps added/removed and flips reversals."""
    previous, current = pair
    forward = diff_generations(previous, current)
    backward = diff_generations(current, previous)
    assert forward["nodes_added"] == backward["nodes_removed"]
    assert forward["nodes_removed"] == backward["nodes_added"]
    assert _edge_keys(forward, "edges_added") == _edge_keys(backward, "edges_removed")
    assert _edge_keys(forward, "edges_removed") == _edge_keys(backward, "edges_added")
    assert len(forward["edges_reversed"]) == len(backward["edges_reversed"])
    for f_entry, b_entry in zip(forward["edges_reversed"], backward["edges_reversed"], strict=True):
        assert f_entry["from"] == b_entry["to"]  # type: ignore[index]
        assert f_entry["to"] == b_entry["from"]  # type: ignore[index]


@given(pair=st.tuples(_graph_strategy(), _graph_strategy()))
@settings(max_examples=60, derandomize=True)
def test_diff_is_deterministic_under_input_shuffle(
    pair: tuple[dict[str, object], dict[str, object]],
) -> None:
    previous, current = pair
    baseline = canonical_json(diff_generations(previous, current))
    shuffled_previous = _graph(list(reversed(previous["nodes"])), list(reversed(previous["links"])))  # type: ignore[arg-type]
    shuffled_current = _graph(list(reversed(current["nodes"])), list(reversed(current["links"])))  # type: ignore[arg-type]
    assert canonical_json(diff_generations(shuffled_previous, shuffled_current)) == baseline


@given(pair=st.tuples(_graph_strategy(), _graph_strategy()))
@settings(max_examples=60, derandomize=True)
def test_edges_are_fully_accounted_without_double_counting(
    pair: tuple[dict[str, object], dict[str, object]],
) -> None:
    previous, current = pair

    def keys(graph: dict[str, object]) -> set[tuple[str, str, str]]:
        return {
            (str(link["source"]), str(link["target"]), str(link.get("relation", "supports")))
            for link in graph["links"]  # type: ignore[union-attr]
        }

    prev_only = keys(previous) - keys(current)
    curr_only = keys(current) - keys(previous)
    diff = diff_generations(previous, current)
    assert len(diff["edges_removed"]) + len(diff["edges_reversed"]) == len(prev_only)
    assert len(diff["edges_added"]) + len(diff["edges_reversed"]) == len(curr_only)
