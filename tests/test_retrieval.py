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

from know_your_soul.contracts import (
    AmbiguousDomain,
    UnknownDomain,
)
from know_your_soul.domain import PrimaryDomain
from know_your_soul.retrieval import (
    DOMAIN_ALIASES,
    STOPWORDS,
    BudgetedTraversal,
    GraphLink,
    LexicalIndex,
    PrimaryRouter,
    estimated_tokens,
    normalize_term,
    query_terms,
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


# ------------------------------------------------------- query-term hygiene


def test_query_terms_drop_stopwords_and_fold_plurals() -> None:
    """A natural-language question reduces to its content terms, deterministically."""
    terms = query_terms("What are the claims about biases, and which of them may never be stated as facts?")
    assert terms == ["claim", "bias", "stated", "fact"]
    assert all(term not in STOPWORDS for term in terms)
    # Stopword lists are closed and deterministic: no corpus statistics, no model.
    assert "the" in STOPWORDS and "yang" in STOPWORDS and "bagaimana" in STOPWORDS


def test_normalize_term_is_conservative() -> None:
    assert normalize_term("claims") == "claim"
    assert normalize_term("policies") == "policy"
    assert normalize_term("biases") == "bias"
    assert normalize_term("bias") == "bias"  # ambiguous -s, never stripped
    assert normalize_term("status") == "status"
    assert normalize_term("class") == "class"
    assert normalize_term("lens") == "lens"
    assert normalize_term("l1") == "l1"  # short ids survive untouched


def test_nl_question_routes_to_a_root_instead_of_no_match() -> None:
    """Before hygiene this question produced ``ambiguous``/``no_match`` (bench 2026-10-25)."""
    nodes = _primary_nodes()
    for node in nodes:
        if node["id"] == "P3":
            node["context_tags"] = ["kill criteria", "stop rule"]
    router = PrimaryRouter(nodes)
    outcome = router.route(
        domain=None,
        purpose="What are the kill criteria, and when should I stop the task instead of continuing?",
        context_tags=(),
    )
    assert outcome.status == "ranked"
    assert outcome.root_node_id == "P3"


def test_node_id_slug_segments_are_indexed_terms() -> None:
    """A snake_case node id contributes its *segments* to the node_id field.

    Bench 2026-10-25 (q05): ``tokenize`` keeps underscores inside ``\\w+``, so
    a slug like ``07_calibration_bayes`` indexed as a single token and the
    node_id field — the contract's strongest field — never fired for any real
    corpus id. Segments must be indexed separately.
    """
    nodes = [
        {"id": "07_calibration_bayes", "kind": "child", "domain": "P1", "label": "SOUL 07", "context_tags": []},
        {"id": "nx", "kind": "child", "domain": "P1", "label": "calibration notes", "context_tags": []},
    ]
    index = LexicalIndex.from_graph_nodes(nodes)
    assert index.document_frequency("calibration") == 2
    hits = index.rank(("calibration",))
    # Same term, same df: the node_id match (strongest field) beats the
    # summary-only match.
    assert [hit.node_id for hit in hits] == ["07_calibration_bayes", "nx"]
    assert hits[0].score > hits[1].score


def test_node_id_slug_segment_breaks_a_cross_root_tie_by_content() -> None:
    """A tie between roots breaks on slug-segment content, never on order.

    Bench 2026-10-25 (q05): two roots tied exactly — one child matched term X
    in its summary, the other matched a different term Y of identical IDF in
    its summary. The child whose stable id *names* term X carries a legitimate
    node_id-field signal; that content signal must break the tie (spec 13.1
    still forbids resolving ties to the first graph-order node).
    """
    nodes = [*_primary_nodes()]
    links = [
        GraphLink(source="P2", target="cat_heuristics_catalog", relation="parent_of"),
        GraphLink(source="P7", target="plain_notes", relation="parent_of"),
    ]
    nodes.append(
        {
            "id": "cat_heuristics_catalog",
            "kind": "child",
            "domain": "P2",
            "label": "Catalog — Heuristic digest",
            "context_tags": [],
        }
    )
    nodes.append(
        {
            "id": "plain_notes",
            "kind": "child",
            "domain": "P7",
            "label": "Notes — why this protocol exists",
            "context_tags": [],
        }
    )
    router = PrimaryRouter(nodes, links)
    # "heuristic" (df=1, child A) and "exist" (df=1, child B) have identical
    # IDF; only the slug segment separates them.
    outcome = router.route(domain=None, purpose="which heuristic explains why it exists", context_tags=())
    assert outcome.status == "ranked"
    assert outcome.root_node_id == "P2"


def test_root_aggregation_discounts_supporting_hits_by_reciprocal_rank() -> None:
    """One stronger hit beats a weaker hit padded by a weak secondary hit.

    Bench 2026-10-25 (q16): a plain top-three sum let a root whose top hit was
    *weaker* (14.17) win over a root with the single strongest hit (17.27)
    merely because a second, single-common-term hit (3.51) padded the total.
    Supporting hits now weigh 1/rank, so breadth only wins when it is strong.
    """
    nodes = [*_primary_nodes()]
    links = [
        GraphLink(source="P4", target="p4-child", relation="parent_of"),
        GraphLink(source="P6", target="p6-child-strong", relation="parent_of"),
        GraphLink(source="P6", target="p6-child-weak", relation="parent_of"),
    ]
    # All query terms share df=2, so every per-term score is identical t:
    # P4 scores 3t, P6 scores 2t (top) + t (secondary).
    nodes.append({"id": "p4-child", "kind": "child", "domain": "P4", "label": "alpha beta gamma", "context_tags": []})
    nodes.append({"id": "p6-child-strong", "kind": "child", "domain": "P6", "label": "alpha beta", "context_tags": []})
    nodes.append({"id": "p6-child-weak", "kind": "child", "domain": "P6", "label": "gamma", "context_tags": []})
    router = PrimaryRouter(nodes, links)
    outcome = router.route(domain=None, purpose="alpha beta gamma", context_tags=())
    # Plain sum: P6 ties P4 at 3t (typed ambiguous). Discounted: P6 = 2.5t < 3t.
    assert outcome.status == "ranked"
    assert outcome.root_node_id == "P4"


def test_root_aggregation_still_rewards_comparable_breadth() -> None:
    """The discount damps weak padding; genuinely broad coverage still wins."""
    nodes = [*_primary_nodes()]
    links = [
        GraphLink(source="P1", target="p1-child-a", relation="parent_of"),
        GraphLink(source="P1", target="p1-child-b", relation="parent_of"),
        GraphLink(source="P2", target="p2-child", relation="parent_of"),
    ]
    nodes.append({"id": "p1-child-a", "kind": "child", "domain": "P1", "label": "alpha beta gamma", "context_tags": []})
    nodes.append(
        {"id": "p1-child-b", "kind": "child", "domain": "P1", "label": "delta epsilon zeta", "context_tags": []}
    )
    nodes.append(
        {"id": "p2-child", "kind": "child", "domain": "P2", "label": "alpha beta gamma delta", "context_tags": []}
    )
    router = PrimaryRouter(nodes, links)
    outcome = router.route(domain=None, purpose="alpha beta gamma delta epsilon zeta", context_tags=())
    assert outcome.status == "ranked"
    assert outcome.root_node_id == "P1"


def test_root_aggregation_is_bounded_by_its_three_strongest_hits() -> None:
    """Many weak hits in one domain must not outvote one strong hit in another.

    A plain sum made a wide domain win on hit count; the top-three sum keeps
    breadth from beating relevance.
    """
    nodes = [*_primary_nodes()]
    links: list[GraphLink] = []
    for index in range(6):
        child = {
            "id": f"p1-child-{index}",
            "kind": "child",
            "domain": "P1",
            "label": f"weak mention number {index}",
            "context_tags": [],
        }
        nodes.append(child)
        links.append(GraphLink(source="P1", target=child["id"], relation="parent_of"))
    nodes.append(
        {
            "id": "p2-child-strong",
            "kind": "child",
            "domain": "P2",
            "label": "goodhart law metric target",
            "context_tags": [],
        }
    )
    links.append(GraphLink(source="P2", target="p2-child-strong", relation="parent_of"))
    router = PrimaryRouter(nodes, links)
    outcome = router.route(domain=None, purpose="goodhart law metric target", context_tags=())
    assert outcome.status == "ranked"
    assert outcome.root_node_id == "P2"


def test_enriched_summary_terms_route_to_the_right_root() -> None:
    """T10-1 (Opsi A): terms that exist only in a node's enriched summary —
    the defined-term digest lifted from the source body at projection — are
    searchable and route to that node's primary root. The digest text never
    appears in any heading, so this isolates the enrichment channel."""
    nodes = [*_primary_nodes()]
    links: list[GraphLink] = []
    nodes.append(
        {
            "id": "p2-catalog",
            "kind": "child",
            "domain": "P2",
            # Enriched label shape: heading + defined-term digest.
            "label": "Catalog — Overconfidence countermeasure; Calibration gate",
            "context_tags": [],
        }
    )
    links.append(GraphLink(source="P2", target="p2-catalog", relation="parent_of"))
    nodes.append(
        {
            "id": "p5-notes",
            "kind": "child",
            "domain": "P5",
            "label": "Notes — Reasoning chains",
            "context_tags": [],
        }
    )
    links.append(GraphLink(source="P5", target="p5-notes", relation="parent_of"))
    router = PrimaryRouter(nodes, links)
    outcome = router.route(domain=None, purpose="what is the overconfidence countermeasure?", context_tags=())
    assert outcome.status == "ranked"
    assert outcome.root_node_id == "P2"


def test_enriched_summary_first_sentence_is_indexed() -> None:
    """The capped first sentence after the digest is part of the indexable
    summary field (spec 13.1: summaries are searchable)."""
    index = LexicalIndex.from_graph_nodes(
        [
            {"id": "n-plain", "kind": "child", "domain": "P1", "label": "Plain", "context_tags": []},
            {
                "id": "n-enriched",
                "kind": "child",
                "domain": "P1",
                "label": "Guide — Intro sentence one with a zephyr marker.",
                "context_tags": [],
            },
        ]
    )
    hits = index.rank(["zephyr"])
    assert [hit.node_id for hit in hits] == ["n-enriched"]
