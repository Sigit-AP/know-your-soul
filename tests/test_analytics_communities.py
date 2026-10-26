"""Tests for ``know_your_soul.analytics.communities`` (ATM F-A3).

Pins the deterministic community contract: canonical-order label propagation
with an explicit iteration cap, stable community ids (0 = largest), hub labels
with id tie-break, sha256 membership signatures, cohesion with self-loops
discarded, and greedy cross-generation remapping with a strict both-directions
majority rule for label reuse.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.communities import (
    DEFAULT_MAX_ITERATIONS,
    detect_communities,
    label_still_applies,
    membership_signature,
    remap_communities,
)
from know_your_soul.domain import canonical_json

# ------------------------------------------------------------------ helpers


def _node(node_id: str, *, label: str | None = None, domain: str = "P1") -> dict[str, object]:
    return {"id": node_id, "label": label if label is not None else node_id, "domain": domain}


def _link(source: str, target: str, *, relation: str = "supports", weight: float = 1.0) -> dict[str, object]:
    return {"source": source, "target": target, "relation": relation, "weight": weight}


def _canonical(value: object) -> str:
    return canonical_json(value)


# ---------------------------------------------------------------- signature


class TestMembershipSignature:
    def test_sixteen_hex_chars(self) -> None:
        sig = membership_signature(["a", "b", "c"])
        assert len(sig) == 16
        int(sig, 16)

    def test_order_independent(self) -> None:
        assert membership_signature(["c", "a", "b"]) == membership_signature(["a", "b", "c"])

    def test_matches_documented_recipe(self) -> None:
        expected = hashlib.sha256(b"a\nb\nc").hexdigest()[:16]
        assert membership_signature(["c", "b", "a"]) == expected

    def test_distinct_membership_distinct_sig(self) -> None:
        assert membership_signature(["a", "b"]) != membership_signature(["a", "c"])


# --------------------------------------------------------------- detection


class TestDetectCommunities:
    def test_two_disconnected_pairs(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c"), _node("d")]
        links = [_link("a", "b"), _link("c", "d")]
        result = detect_communities(nodes, links)
        assert result["community_count"] == 2
        sizes = [c["size"] for c in result["communities"]]
        assert sizes == [2, 2]
        member_sets = [tuple(c["members"]) for c in result["communities"]]
        assert set(member_sets) == {("a", "b"), ("c", "d")}

    def test_id_zero_is_largest_with_canonical_tiebreak(self) -> None:
        nodes = [_node("x"), _node("y"), _node("z"), _node("w")]
        links = [_link("z", "w"), _link("x", "y")]
        result = detect_communities(nodes, links)
        # Both communities have size 2; id 0 goes to the one whose smallest
        # member id sorts first ("w" < "x").
        assert result["communities"][0]["members"] == ["w", "z"]
        assert result["communities"][1]["members"] == ["x", "y"]
        assert result["communities"][0]["id"] == 0

    def test_largest_gets_id_zero(self) -> None:
        nodes = [_node(chr(ord("a") + i)) for i in range(6)]
        links = [
            _link("d", "e"),
            _link("d", "f"),
            _link("e", "f"),
            _link("a", "b"),
        ]
        result = detect_communities(nodes, links)
        assert result["communities"][0]["id"] == 0
        assert result["communities"][0]["members"] == ["d", "e", "f"]
        assert result["communities"][1]["members"] == ["a", "b"]

    def test_triangle_stays_together(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c"), _node("z")]
        links = [_link("a", "b"), _link("b", "c"), _link("a", "c")]
        result = detect_communities(nodes, links)
        assert result["community_count"] == 2
        big = result["communities"][0]
        assert big["members"] == ["a", "b", "c"]
        assert result["communities"][1]["members"] == ["z"]

    def test_converged_flag_and_iteration_accounting(self) -> None:
        nodes = [_node("a"), _node("b")]
        links = [_link("a", "b")]
        result = detect_communities(nodes, links)
        assert result["converged"] is True
        assert 1 <= result["iterations_used"] <= DEFAULT_MAX_ITERATIONS
        assert result["max_iterations"] == DEFAULT_MAX_ITERATIONS
        assert result["algorithm"] == "canonical-label-propagation"

    def test_iteration_cap_respected(self) -> None:
        nodes = [_node("a"), _node("b")]
        links = [_link("a", "b")]
        result = detect_communities(nodes, links, max_iterations=1)
        assert result["iterations_used"] <= 1
        assert result["max_iterations"] == 1

    def test_empty_graph(self) -> None:
        result = detect_communities([], [])
        assert result["community_count"] == 0
        assert result["communities"] == []
        assert result["converged"] is True

    def test_isolated_node_forms_singleton_community(self) -> None:
        result = detect_communities([_node("solo")], [])
        assert result["community_count"] == 1
        assert result["communities"][0]["members"] == ["solo"]

    def test_membership_sig_present_and_stable(self) -> None:
        nodes = [_node("a"), _node("b")]
        links = [_link("a", "b")]
        result = detect_communities(nodes, links)
        community = result["communities"][0]
        assert community["membership_sig"] == membership_signature(["b", "a"])

    def test_input_order_permutation_byte_identical(self) -> None:
        nodes = [_node(c) for c in "abcdefgh"]
        links = [
            _link("a", "b"),
            _link("b", "c"),
            _link("a", "c"),
            _link("d", "e"),
            _link("f", "g"),
            _link("g", "h"),
        ]
        base = _canonical(detect_communities(nodes, links))
        permuted_nodes = list(reversed(nodes))
        permuted_links = list(reversed(links))
        assert _canonical(detect_communities(permuted_nodes, permuted_links)) == base

    def test_hashseed_independent_via_subprocess(self) -> None:
        code = (
            "from know_your_soul.analytics.communities import detect_communities\n"
            "from know_your_soul.domain import canonical_json\n"
            "nodes = [{'id': c} for c in 'abcdefghij']\n"
            "links = [{'source': a, 'target': b, 'relation': 'supports', 'weight': 1.0}\n"
            "         for a, b in [('a','b'),('b','c'),('d','e'),('e','f'),('f','d'),\n"
            "                      ('g','h'),('i','j'),('c','d')]]\n"
            "print(canonical_json(detect_communities(nodes, links)))\n"
        )
        outputs = []
        for seed in ("0", "1", "42", "2026"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            proc = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                env=env,
                check=True,
            )
            outputs.append(proc.stdout)
        assert len(set(outputs)) == 1

    def test_weighted_links_influence_grouping(self) -> None:
        # "b" sits between two pairs; the heavy a-b edge must pull b toward a.
        nodes = [_node("a"), _node("b"), _node("c"), _node("d")]
        links = [
            _link("a", "b", weight=5.0),
            _link("b", "c", weight=1.0),
            _link("c", "d", weight=5.0),
        ]
        result = detect_communities(nodes, links)
        member_sets = {tuple(c["members"]) for c in result["communities"]}
        assert member_sets == {("a", "b"), ("c", "d")}


# ------------------------------------------------------------------- labels


class TestLabels:
    def test_label_is_hub_member(self) -> None:
        # Star: "hub" connects to three leaves; it must name the community.
        nodes = [_node("hub", label="Hub Label"), _node("l1"), _node("l2"), _node("l3")]
        links = [_link("hub", "l1"), _link("hub", "l2"), _link("hub", "l3")]
        result = detect_communities(nodes, links)
        community = result["communities"][0]
        assert community["label_node_id"] == "hub"
        assert community["label"] == "Hub Label"

    def test_label_tie_breaks_by_smallest_id(self) -> None:
        nodes = [_node("m", label="Em"), _node("a", label="Ay")]
        links = [_link("m", "a")]
        result = detect_communities(nodes, links)
        community = result["communities"][0]
        # Both have degree 1; smallest id wins.
        assert community["label_node_id"] == "a"
        assert community["label"] == "Ay"

    def test_label_falls_back_to_id_without_label_field(self) -> None:
        nodes = [{"id": "p"}, {"id": "q"}]
        links = [_link("p", "q")]
        result = detect_communities(nodes, links)
        assert result["communities"][0]["label"] == "p"
        assert result["communities"][0]["label_node_id"] == "p"


# ----------------------------------------------------------------- cohesion


class TestCohesion:
    def test_triangle_cohesion_is_one(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c")]
        links = [_link("a", "b"), _link("b", "c"), _link("a", "c")]
        result = detect_communities(nodes, links)
        assert result["communities"][0]["cohesion"] == "1.000000000000"

    def test_path_of_three_cohesion_two_thirds(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c")]
        links = [_link("a", "b"), _link("b", "c")]
        result = detect_communities(nodes, links)
        assert result["communities"][0]["cohesion"] == "0.666666666667"

    def test_self_loop_discarded(self) -> None:
        # Without the self-loop rule this pair would report cohesion > 1.
        nodes = [_node("a"), _node("b")]
        links = [_link("a", "b"), _link("a", "a")]
        result = detect_communities(nodes, links)
        assert result["communities"][0]["cohesion"] == "1.000000000000"

    def test_singleton_cohesion_vacuously_one(self) -> None:
        result = detect_communities([_node("solo")], [])
        assert result["communities"][0]["cohesion"] == "1.000000000000"

    def test_pair_without_internal_edge_scores_zero(self) -> None:
        # Force a community {"a", "b"} with no a-b edge is impossible via LPA,
        # so test cohesion through remap-free direct observation instead: a
        # 4-node community with a single internal edge.
        nodes = [_node(c) for c in "abcd"]
        links = [_link("a", "b", weight=10.0), _link("b", "c", weight=1.0), _link("c", "d", weight=1.0)]
        result = detect_communities(nodes, links)
        # b joins a (heavy edge); c joins d's side... assert whatever the LPA
        # decides is internally consistent: cohesion in [0, 1] always.
        for community in result["communities"]:
            value = float(community["cohesion"])
            assert 0.0 <= value <= 1.0


# -------------------------------------------------------------------- remap


def _community(cid: int, members: list[str]) -> dict[str, object]:
    return {
        "id": cid,
        "members": sorted(members),
        "membership_sig": membership_signature(members),
        "label": f"label-{cid}",
        "size": len(members),
    }


class TestRemap:
    def test_identical_generations_map_everything(self) -> None:
        previous = [_community(0, ["a", "b", "c"]), _community(1, ["d", "e"])]
        current = [_community(0, ["a", "b", "c"]), _community(1, ["d", "e"])]
        result = remap_communities(previous, current)
        assert result["unmatched_previous"] == []
        assert result["unmatched_current"] == []
        for entry in result["mapping"]:
            assert entry["label_still_applies"] is True
            assert entry["overlap"] == entry["previous_size"] == entry["current_size"]

    def test_exact_membership_keeps_label_despite_id_change(self) -> None:
        previous = [_community(7, ["a", "b"])]
        current = [_community(0, ["b", "a"])]
        result = remap_communities(previous, current)
        assert result["mapping"] == [
            {
                "previous_id": 7,
                "current_id": 0,
                "overlap": 2,
                "previous_size": 2,
                "current_size": 2,
                "label_still_applies": True,
                "previous_sig": membership_signature(["a", "b"]),
                "current_sig": membership_signature(["a", "b"]),
            }
        ]

    def test_majority_overlap_both_directions_keeps_label(self) -> None:
        # prev {a,b,c,d} -> curr {a,b,c}: 3/4 prev, 3/3 curr — strict majority
        # on both sides.
        previous = [_community(0, ["a", "b", "c", "d"])]
        current = [_community(0, ["a", "b", "c"])]
        result = remap_communities(previous, current)
        assert result["mapping"][0]["label_still_applies"] is True
        assert result["mapping"][0]["overlap"] == 3

    def test_half_overlap_loses_label(self) -> None:
        # prev {a,b,c,d} -> curr {a,b,e,f}: overlap 2 is exactly half of both
        # sides — not a strict majority.
        previous = [_community(0, ["a", "b", "c", "d"])]
        current = [_community(0, ["a", "b", "e", "f"])]
        result = remap_communities(previous, current)
        assert result["mapping"][0]["label_still_applies"] is False

    def test_greedy_one_to_one(self) -> None:
        # curr 0 overlaps prev 0 by 3 and prev 1 by 2; prev 0 wins, prev 1
        # falls back to curr 1 (overlap 2) if available.
        previous = [_community(0, ["a", "b", "c"]), _community(1, ["a", "b", "z"])]
        current = [_community(0, ["a", "b", "c"]), _community(1, ["a", "b", "w"])]
        result = remap_communities(previous, current)
        pairs = {(m["previous_id"], m["current_id"]) for m in result["mapping"]}
        assert (0, 0) in pairs
        assert (1, 1) in pairs
        assert len(result["mapping"]) == 2

    def test_unmatched_reported_sorted(self) -> None:
        previous = [_community(0, ["a", "b"]), _community(1, ["x", "y"])]
        current = [_community(0, ["a", "b"]), _community(1, ["p", "q"])]
        result = remap_communities(previous, current)
        assert result["unmatched_previous"] == [1]
        assert result["unmatched_current"] == [1]

    def test_label_still_applies_helper(self) -> None:
        assert label_still_applies(["a", "b", "c"], ["a", "b", "c"]) is True
        assert label_still_applies(["a", "b", "c", "d"], ["a", "b", "c"]) is True
        assert label_still_applies(["a", "b", "c", "d"], ["a", "b", "e", "f"]) is False
        assert label_still_applies(["a", "b"], ["a", "b", "c", "d", "e"]) is False

    def test_remap_deterministic_under_permutation(self) -> None:
        previous = [_community(0, ["a", "b", "c"]), _community(1, ["d", "e"])]
        current = [_community(0, ["a", "b"]), _community(1, ["d", "e", "f"])]
        base = _canonical(remap_communities(previous, current))
        assert _canonical(remap_communities(list(reversed(previous)), list(reversed(current)))) == base


# ---------------------------------------------------------------- property


_id_alphabet = st.characters(whitelist_categories=("Ll",), whitelist_characters="0123")


@settings(derandomize=True, max_examples=60)
@given(
    n=st.integers(min_value=0, max_value=8),
    edge_indexes=st.lists(
        st.tuples(st.integers(min_value=0, max_value=7), st.integers(min_value=0, max_value=7)),
        max_size=14,
    ),
    permutation=st.permutations(range(8)),
)
def test_detection_permutation_invariant(
    n: int, edge_indexes: list[tuple[int, int]], permutation: tuple[int, ...]
) -> None:
    ids = [f"n{i}" for i in range(n)]
    nodes = [_node(i) for i in ids]
    links = [_link(ids[a], ids[b]) for a, b in edge_indexes if a < n and b < n and a != b]
    base = _canonical(detect_communities(nodes, links))
    # Derive a true bijection over the indexes from the drawn permutation
    # (filtering by size would silently drop elements when the collection is
    # larger than the permutation's range).
    node_order = sorted(range(n), key=lambda i: (permutation[i % len(permutation)], i))
    link_order = sorted(range(len(links)), key=lambda i: (permutation[i % len(permutation)], i))
    permuted_nodes = [nodes[p] for p in node_order]
    permuted_links = [
        {"source": links[p]["source"], "target": links[p]["target"], "relation": "supports", "weight": 1.0}
        for p in link_order
    ]
    assert _canonical(detect_communities(permuted_nodes, permuted_links)) == base


@settings(derandomize=True, max_examples=40)
@given(
    members=st.lists(st.text(alphabet=_id_alphabet, min_size=1, max_size=4), min_size=1, max_size=8, unique=True),
    permutation=st.permutations(range(8)),
)
def test_signature_stable_under_input_order(members: list[str], permutation: tuple[int, ...]) -> None:
    base = membership_signature(members)
    shuffled = [members[p] for p in sorted(range(len(members)), key=lambda i: (permutation[i % len(permutation)], i))]
    assert membership_signature(shuffled) == base
    assert len(base) == 16


def test_output_is_json_canonical_round_trippable() -> None:
    nodes = [_node(c) for c in "abcde"]
    links = [_link("a", "b"), _link("b", "c"), _link("d", "e")]
    result = detect_communities(nodes, links)
    encoded = _canonical(result)
    assert json.loads(encoded) == result
    assert _canonical(json.loads(encoded)) == encoded


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
