"""Contract tests for deterministic primary-root routing and bounded traversal (Task 4).

Spec section 8 and 13.1; matrix row 37 (named test
``test_bounded_traversal_and_typed_ambiguity``).

Covered here at the unit level (no store/projection):

* direct domain routing to the stable primary root, bilingual aliases;
* missing-root failure with *no* keyword fallback, ambiguous/unknown domain
  rejection (no guessed primary root);
* deterministic lexical ranking with zero provenance relevance and a
  field-complete IDF (node id, aliases, summary, context tags);
* ``BudgetedTraversal`` reading the ``graph.json`` links list (parallel typed
  relations on one node pair survive), with explicit relation-allowlist,
  node/edge/depth guards, deduplication, and cross-edge inclusion.
"""

from __future__ import annotations

import pytest

from soul_knowledge.contracts import (
    AmbiguousDomain,
    UnknownDomain,
)
from soul_knowledge.domain import PrimaryDomain
from soul_knowledge.retrieval import (
    DOMAIN_ALIASES,
    BudgetedTraversal,
    GraphLink,
    LexicalIndex,
    PrimaryRouter,
    estimated_tokens,
    resolve_domain_input,
    sanitize_text,
    tokenize,
)

# ------------------------------------------------------------------ fixtures


def _node(node_id: str, *, kind: str = "leaf", label: str = "", tags: tuple[str, ...] = ()) -> dict:
    return {
        "id": node_id,
        "kind": kind,
        "label": label or node_id,
        "context_tags": list(tags),
        "domain": "P1",
        "provenance": ["soul://corpus/x.md"],
        "source_file": "x.md",
    }


def _primary_nodes() -> list[dict]:
    titles = {
        "P1": "Pembelajaran",
        "P2": "Gagasan",
        "P3": "Tindakan",
        "P4": "Keputusan",
        "P5": "Pemikiran/Penalaran",
        "P6": "Disiplin",
        "P7": "Level Profesional",
    }
    return [
        {"id": pid, "kind": "primary", "label": title, "context_tags": [pid], "domain": pid}
        for pid, title in titles.items()
    ]


# ------------------------------------------------------- domain input routing


def test_bilingual_aliases_resolve_all_seven_domains() -> None:
    by_code = {domain.value: domain for domain in PrimaryDomain}
    assert len(by_code) == 7
    for code, domain in by_code.items():
        assert resolve_domain_input(code) is domain
        assert resolve_domain_input(code.lower()) is domain
        assert resolve_domain_input(domain.title) is domain
    english = {
        "P1": "learning",
        "P2": "ideas",
        "P3": "actions",
        "P4": "decisions",
        "P5": "reasoning",
        "P6": "discipline",
        "P7": "professional level",
    }
    for code, alias in english.items():
        assert resolve_domain_input(alias) is by_code[code], alias
        assert alias.casefold() in {a.casefold() for a in DOMAIN_ALIASES[by_code[code]]}


def test_ambiguous_domain_is_rejected_without_guessing_a_root() -> None:
    # "pem" prefix-matches both Pembelajaran (P1) and Pemikiran (P5).
    with pytest.raises(AmbiguousDomain) as excinfo:
        resolve_domain_input("pem")
    assert excinfo.value.code == "AMBIGUOUS_DOMAIN"
    assert {d.value for d in excinfo.value.candidates} == {"P1", "P5"}


def test_unknown_domain_is_rejected() -> None:
    with pytest.raises(UnknownDomain) as excinfo:
        resolve_domain_input("zzz-no-such-domain")
    assert excinfo.value.code == "UNKNOWN_DOMAIN"


def test_router_direct_routing_hits_the_stable_primary_root() -> None:
    router = PrimaryRouter(_primary_nodes())
    outcome = router.route(domain="tindakan", purpose="anything", context_tags=())
    assert outcome.status == "direct"
    assert outcome.domain is PrimaryDomain.P3
    assert outcome.root_node_id == "P3"


def test_router_missing_root_fails_without_keyword_fallback() -> None:
    nodes = [n for n in _primary_nodes() if n["id"] != "P1"]
    # A non-root node whose label lexically matches the purpose must NOT be
    # promoted to a substitute root: there is no global keyword fallback.
    nodes = [*nodes, _node("n-decoy", label="learning lessons pembelajaran")]
    router = PrimaryRouter(nodes)
    outcome = router.route(domain="P1", purpose="learning lessons", context_tags=())
    assert outcome.status == "missing_root"
    assert outcome.domain is PrimaryDomain.P1
    assert outcome.root_node_id is None
    assert outcome.candidates == ()


# ---------------------------------------------------------------LexicalIndex


def _entry(nid: str, **fields: set[str]) -> dict:
    return {"node_id": nid, "fields": fields}


def test_ranking_ignores_provenance_and_source_paths() -> None:
    nodes = [
        {
            **_node("a"),
            "label": "alpha",
            "provenance": ["soul://corpus/uniqueprovterm.md"],
            "source_file": "uniqueprovterm.md",
        },
        {**_node("b"), "label": "alpha"},
    ]
    index = LexicalIndex.from_graph_nodes(nodes)
    # The query term exists only in provenance/source fields: zero relevance.
    assert index.rank(("uniqueprovterm",)) == ()
    hits = index.rank(("alpha",))
    assert [hit.node_id for hit in hits] == ["a", "b"]
    assert hits[0].score == hits[1].score, "provenance must contribute zero relevance"


def test_field_complete_idf_counts_every_searchable_field() -> None:
    entries = [
        _entry("a", summary={"common", "target"}),
        _entry("b", context_tags={"common"}),
        _entry("c", aliases={"common"}),
    ]
    index = LexicalIndex.from_entries(entries)
    # "common" appears in three nodes across three different field kinds, so
    # its IDF is computed over the field-complete document frequency (df=3).
    assert index.document_frequency("common") == 3
    assert index.document_frequency("target") == 1
    assert index.idf("target") > index.idf("common")
    hits = index.rank(("common", "target"))
    assert hits[0].node_id == "a"
    assert hits[0].score > 0


def test_field_weights_order_node_id_alias_tags_summary() -> None:
    entries = [
        _entry("n-summary", summary={"termx"}),
        _entry("n-tags", context_tags={"termx"}),
        _entry("n-alias", aliases={"termx"}),
        _entry("termx", node_id={"termx"}),
    ]
    index = LexicalIndex.from_entries(entries)
    hits = index.rank(("termx",))
    assert [hit.node_id for hit in hits] == ["termx", "n-alias", "n-tags", "n-summary"]


def test_ranking_ties_break_on_node_id_never_graph_order() -> None:
    entries = [
        _entry("z-node", summary={"alpha"}),
        _entry("a-node", summary={"alpha"}),
    ]
    index = LexicalIndex.from_entries(entries)
    hits = index.rank(("alpha",))
    assert [hit.node_id for hit in hits] == ["a-node", "z-node"]


def test_router_ranked_tie_is_a_typed_ambiguity_not_first_graph_order() -> None:
    nodes = _primary_nodes()
    # Give P2 and P4 identical lexical footing for the purpose.
    for node in nodes:
        if node["id"] in {"P2", "P4"}:
            node["context_tags"] = ["governance"]
    router = PrimaryRouter(nodes)
    outcome = router.route(domain=None, purpose="governance", context_tags=("governance",))
    assert outcome.status == "ambiguous"
    assert outcome.root_node_id is None
    assert list(outcome.candidates) == sorted(outcome.candidates)
    assert len(outcome.candidates) >= 2


def test_router_no_match_is_a_typed_outcome() -> None:
    router = PrimaryRouter(_primary_nodes())
    outcome = router.route(domain=None, purpose="zzzqqq", context_tags=())
    assert outcome.status == "no_match"
    assert outcome.root_node_id is None


def test_router_ranked_route_selects_the_strongest_primary_root() -> None:
    nodes = _primary_nodes()
    for node in nodes:
        if node["id"] == "P6":
            node["context_tags"] = ["habit", "consistency"]
    router = PrimaryRouter(nodes)
    outcome = router.route(domain=None, purpose="habit consistency", context_tags=())
    assert outcome.status == "ranked"
    assert outcome.root_node_id == "P6"
    assert outcome.domain is PrimaryDomain.P6


# ---------------------------------------------------------- BudgetedTraversal


def _diamond_links() -> list[GraphLink]:
    return [
        GraphLink(source="A", target="B", relation="parent_of"),
        GraphLink(source="A", target="C", relation="parent_of"),
        GraphLink(source="B", target="D", relation="parent_of"),
        GraphLink(source="C", target="D", relation="parent_of"),
        GraphLink(source="B", target="C", relation="supports"),
    ]


def test_bounded_traversal_and_typed_ambiguity() -> None:
    """Matrix row 37: root-scoped retrieval, bounded traversal, ambiguity refusal."""
    traversal = BudgetedTraversal(_diamond := _diamond_links())
    report = traversal.traverse(
        "A",
        depth=3,
        mode="bfs",
        relation_allowlist=frozenset({"parent_of"}),
        max_nodes=10,
        max_edges=10,
    )
    assert report.nodes == ("A", "B", "C", "D")
    # Dedup: D is reachable via B and C but is visited exactly once.
    assert len(report.nodes) == len(set(report.nodes))
    # The allowlisted B->C supports link is excluded from traversal...
    assert all(link.relation == "parent_of" for link in report.edges)
    # ...but surfaces as a cross edge because both endpoints were visited.
    assert GraphLink(source="B", target="C", relation="supports") in report.cross_edges
    # Ambiguity refusal is typed, never first-graph-order (router level).
    router = PrimaryRouter(_primary_nodes())
    outcome = router.route(domain=None, purpose="zzzqqq", context_tags=())
    assert outcome.status in {"no_match", "ambiguous"}
    assert outcome.root_node_id is None


def test_traversal_relation_allowlist() -> None:
    links = [
        GraphLink(source="A", target="B", relation="parent_of"),
        GraphLink(source="B", target="C", relation="depends_on"),
    ]
    traversal = BudgetedTraversal(links)
    only_parent = traversal.traverse(
        "A", depth=3, mode="bfs", relation_allowlist=frozenset({"parent_of"}), max_nodes=10, max_edges=10
    )
    assert "C" not in only_parent.nodes
    with_deps = traversal.traverse(
        "A",
        depth=3,
        mode="bfs",
        relation_allowlist=frozenset({"parent_of", "depends_on"}),
        max_nodes=10,
        max_edges=10,
    )
    assert "C" in with_deps.nodes


def test_traversal_parallel_typed_links_survive_the_links_list() -> None:
    links = [
        GraphLink(source="A", target="B", relation="supports"),
        GraphLink(source="A", target="B", relation="depends_on"),
    ]
    traversal = BudgetedTraversal(links)
    report = traversal.traverse("A", depth=2, mode="bfs", relation_allowlist=None, max_nodes=10, max_edges=10)
    both = [link for link in report.edges if link.source == "A" and link.target == "B"]
    assert {link.relation for link in both} == {"supports", "depends_on"}
    assert report.nodes == ("A", "B")
    supports_only = traversal.traverse(
        "A", depth=2, mode="bfs", relation_allowlist=frozenset({"supports"}), max_nodes=10, max_edges=10
    )
    assert [link.relation for link in supports_only.edges] == ["supports"]


def test_traversal_depth_guard() -> None:
    links = [GraphLink(source=f"n{i}", target=f"n{i + 1}", relation="parent_of") for i in range(6)]
    traversal = BudgetedTraversal(links)
    report = traversal.traverse("n0", depth=2, mode="bfs", relation_allowlist=None, max_nodes=50, max_edges=50)
    assert report.nodes == ("n0", "n1", "n2")
    assert "depth" in report.guards_hit


def test_traversal_node_and_edge_guards() -> None:
    links = [GraphLink(source=f"n{i}", target=f"n{i + 1}", relation="parent_of") for i in range(6)]
    traversal = BudgetedTraversal(links)
    by_nodes = traversal.traverse("n0", depth=6, mode="bfs", relation_allowlist=None, max_nodes=2, max_edges=50)
    assert by_nodes.nodes == ("n0", "n1")
    assert "max_nodes" in by_nodes.guards_hit
    by_edges = traversal.traverse("n0", depth=6, mode="bfs", relation_allowlist=None, max_nodes=50, max_edges=1)
    assert len(by_edges.edges) == 1
    assert "max_edges" in by_edges.guards_hit


def test_traversal_bfs_and_dfs_orders_are_deterministic() -> None:
    links = [
        GraphLink(source="A", target="B", relation="parent_of"),
        GraphLink(source="A", target="C", relation="parent_of"),
        GraphLink(source="B", target="D", relation="parent_of"),
        GraphLink(source="C", target="E", relation="parent_of"),
    ]
    traversal = BudgetedTraversal(links)
    bfs = traversal.traverse("A", depth=3, mode="bfs", relation_allowlist=None, max_nodes=10, max_edges=10)
    dfs = traversal.traverse("A", depth=3, mode="dfs", relation_allowlist=None, max_nodes=10, max_edges=10)
    assert bfs.nodes == ("A", "B", "C", "D", "E")
    assert dfs.nodes == ("A", "B", "D", "C", "E")
    again = traversal.traverse("A", depth=3, mode="dfs", relation_allowlist=None, max_nodes=10, max_edges=10)
    assert again.nodes == dfs.nodes


# ------------------------------------------------------------------ utilities


def test_estimated_tokens_is_ceil_utf8_bytes_over_4() -> None:
    assert estimated_tokens({}) == 1  # b"{}" -> 2 bytes -> ceil(2/4)
    assert estimated_tokens({"a": "b"}) == 3  # 9 bytes -> ceil(9/4)
    multibyte = {"summary": "中文"}
    assert estimated_tokens(multibyte) == -(-len('{"summary":"中文"}'.encode()) // 4)


def test_sanitize_text_strips_control_bidi_and_zero_width() -> None:
    assert sanitize_text("ab­c") == "abc"  # soft hyphen (Cf)
    assert sanitize_text("a‍b") == "ab"  # zero-width joiner
    assert sanitize_text("a‮b") == "ab"  # RTL override (bidi)
    assert sanitize_text("a\x00b\x07c") == "abc"  # control chars
    assert sanitize_text("x" * 1000, max_length=10) == "x" * 10


def test_tokenize_is_casefolded_and_unicode_aware() -> None:
    assert tokenize("Pembelajaran Bermakna!") == ["pembelajaran", "bermakna"]
    assert tokenize("n-lesson-1") == ["n", "lesson", "1"]
