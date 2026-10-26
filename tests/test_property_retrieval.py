"""Property tests for retrieval: lexical ranking, routing, traversal, tokens.

Pins the invariants the retrieval contract claims but example tests cannot
cover exhaustively:

* the lexical index is a pure function of its content: permuting the input
  entries never changes a ranking (ties break on node id, never graph order);
* every ranked listing is strictly ordered by ``(-score, -field_rank, node_id)``
  and carries only positive scores;
* adversarial queries (unicode soup, empty, control characters) never raise;
* ``estimated_tokens`` is the frozen ``ceil(utf8_bytes/4)`` estimator: it is
  key-order invariant and monotone under payload growth;
* the budgeted traversal honours its node/edge/depth/allowlist guards on
  arbitrary graphs and is deterministic;
* routing outcomes are always one of the five typed statuses, with the
  structural consistency each status implies.

All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.contracts import AmbiguousDomain, UnknownDomain
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

_word = st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=2, max_size=8)
_node_id = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_"), min_size=1, max_size=10
)


def _entries(max_size: int = 12) -> st.SearchStrategy[list[dict[str, Any]]]:
    return st.lists(
        st.fixed_dictionaries(
            {
                "node_id": _node_id,
                "fields": st.fixed_dictionaries(
                    {
                        "node_id": st.frozensets(_word, max_size=3),
                        "aliases": st.frozensets(_word, max_size=2),
                        "summary": st.frozensets(_word, max_size=4),
                        "context_tags": st.frozensets(_word, max_size=3),
                    }
                ),
            }
        ),
        max_size=max_size,
        unique_by=lambda entry: str(entry["node_id"]),
    )


def _index(entries: list[dict[str, Any]]) -> LexicalIndex:
    return LexicalIndex.from_entries(entries)


_terms = st.lists(_word, max_size=5)


# ------------------------------------------------------------- lexical index


@given(entries=_entries(), terms=_terms, seed=st.integers(min_value=0, max_value=2**31))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_rank_is_invariant_under_input_permutation(entries: list[dict[str, Any]], terms: list[str], seed: int) -> None:
    import random

    shuffled = list(entries)
    random.Random(seed).shuffle(shuffled)
    assert _index(entries).rank(terms) == _index(shuffled).rank(terms)


@given(entries=_entries(), terms=_terms)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_rank_output_is_strictly_ordered_and_positive(entries: list[dict[str, Any]], terms: list[str]) -> None:
    hits = _index(entries).rank(terms)
    assert all(hit.score > 0 for hit in hits)
    node_ids = [hit.node_id for hit in hits]
    assert len(node_ids) == len(set(node_ids))
    keys = [(-hit.score, -hit.field_rank, hit.node_id) for hit in hits]
    assert keys == sorted(keys)


@given(entries=_entries(), terms=_terms)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_rank_is_deterministic_across_repeated_calls(entries: list[dict[str, Any]], terms: list[str]) -> None:
    index = _index(entries)
    assert index.rank(terms) == index.rank(terms)


@given(entries=_entries(), terms=_terms)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_rank_only_returns_indexed_nodes(entries: list[dict[str, Any]], terms: list[str]) -> None:
    indexed = {entry["node_id"] for entry in entries}
    assert {hit.node_id for hit in _index(entries).rank(terms)} <= indexed


@given(entries=_entries(), term=_word)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_idf_stays_within_closed_bounds(entries: list[dict[str, Any]], term: str) -> None:
    index = _index(entries)
    value = index.idf(term)
    # df = 0 maximizes idf at ln(N+1)+1; computed in Decimal like the index.
    upper = Decimal(index.size + 1).ln() + 1
    assert Decimal(1) <= value <= upper + Decimal("1e-20")
    assert index.document_frequency(term) <= index.size


@given(entries=_entries(), terms=_terms)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_field_boost_stays_sublinear(entries: list[dict[str, Any]], terms: list[str]) -> None:
    # The strongest field boost is 1 + (4 - 1) * 0.05 = 1.15 (spec 13.1).
    for entry in entries:
        boost = LexicalIndex._term_boost(entry["fields"], terms[0]) if terms else Decimal(0)
        assert Decimal(0) <= boost <= Decimal("1.15")


# ------------------------------------------------------------- query hygiene


@given(text=st.text(max_size=300))
@settings(derandomize=True, max_examples=50, deadline=None)
def test_tokenize_never_raises_and_yields_no_control_characters(text: str) -> None:
    for token in tokenize(text):
        assert token == token.casefold()
        assert all(char.isprintable() or not char.isspace() for char in token)


@given(text=st.text(max_size=300))
@settings(derandomize=True, max_examples=50, deadline=None)
def test_query_terms_are_unique_normalized_and_stopword_free(text: str) -> None:
    terms = query_terms(text)
    assert len(terms) == len(set(terms))
    assert all(term and term not in STOPWORDS for term in terms)


@given(term=st.text(max_size=64))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_normalize_term_never_lengthens_and_is_idempotent(term: str) -> None:
    normalized = normalize_term(term)
    assert len(normalized) <= len(term)
    assert normalize_term(normalized) == normalized


@given(value=st.text(max_size=700), max_length=st.integers(min_value=1, max_value=512))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_retrieval_sanitize_truncates_and_strips(value: str, max_length: int) -> None:
    import unicodedata

    cleaned = sanitize_text(value, max_length=max_length)
    assert len(cleaned) <= max_length
    assert all(unicodedata.category(char) not in {"Cc", "Cf"} for char in cleaned)


@given(value=st.text(max_size=600))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_retrieval_sanitize_never_raises_within_default_cap(value: str) -> None:
    cleaned = sanitize_text(value)
    assert len(cleaned) <= 512


# ------------------------------------------------------------- domain routing


def test_resolve_domain_input_resolves_every_declared_alias() -> None:
    for domain in PrimaryDomain:
        candidates = {domain.value, domain.title, *DOMAIN_ALIASES[domain]}
        for candidate in candidates:
            assert resolve_domain_input(candidate) is domain


@given(text=st.text(max_size=40))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_resolve_domain_input_has_only_typed_outcomes(text: str) -> None:
    try:
        resolved = resolve_domain_input(text)
    except UnknownDomain, AmbiguousDomain:
        return
    assert isinstance(resolved, PrimaryDomain)


@given(text=st.text(alphabet=st.characters(whitelist_categories=("Cc", "Cf", "Zs")), max_size=12))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_resolve_domain_input_never_guesses_from_control_only_text(text: str) -> None:
    with pytest.raises((UnknownDomain, AmbiguousDomain)):
        resolve_domain_input(text)


def test_resolve_domain_input_tie_is_typed_ambiguous_never_first() -> None:
    # "pem" is a prefix of both P1 (pembelajaran) and P5 (pemikiran) aliases.
    with pytest.raises(AmbiguousDomain) as excinfo:
        resolve_domain_input("pem")
    assert len(excinfo.value.candidates) >= 2


# ------------------------------------------------------------- token estimator


def _jsonable(depth: int = 0) -> st.SearchStrategy[Any]:
    scalars = st.one_of(st.none(), st.booleans(), st.integers(-1000, 1000), st.text(max_size=24))
    if depth >= 3:
        return scalars
    return st.one_of(
        scalars,
        st.lists(_jsonable(depth + 1), max_size=4),
        st.dictionaries(st.text(max_size=10), _jsonable(depth + 1), max_size=4),
    )


@given(payload=_jsonable())
@settings(derandomize=True, max_examples=50, deadline=None)
def test_estimated_tokens_matches_the_frozen_estimator(payload: Any) -> None:
    from know_your_soul.domain import canonical_json

    expected = -(-len(canonical_json(payload).encode("utf-8")) // 4)
    assert estimated_tokens(payload) == expected
    assert estimated_tokens(payload) >= 0


@given(
    pairs=st.integers(min_value=1, max_value=6).flatmap(
        lambda n: st.tuples(
            st.lists(st.text(alphabet="abcde", min_size=1, max_size=4), min_size=n, max_size=n, unique=True),
            st.lists(st.integers(0, 100), min_size=n, max_size=n),
        )
    )
)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_estimated_tokens_is_invariant_under_key_order(pairs: tuple[list[str], list[int]]) -> None:
    keys, values = pairs
    payload = dict(zip(keys, values, strict=True))
    reversed_payload = dict(reversed(list(payload.items())))
    assert estimated_tokens(payload) == estimated_tokens(reversed_payload)


@given(payload=st.dictionaries(st.text(max_size=8), st.integers(0, 100), max_size=5))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_estimated_tokens_is_monotone_under_key_addition(payload: dict[str, Any]) -> None:
    grown = {**payload, "zz-extra-key": "x" * 32}
    assert estimated_tokens(grown) >= estimated_tokens(payload)


@given(text=st.text(max_size=64), extra=st.text(min_size=1, max_size=64))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_estimated_tokens_is_monotone_under_value_extension(text: str, extra: str) -> None:
    assert estimated_tokens({"v": text + extra}) >= estimated_tokens({"v": text})


# ------------------------------------------------------------- traversal guards


_link = st.builds(
    GraphLink,
    source=_node_id,
    target=_node_id,
    relation=st.sampled_from(["parent_of", "supports", "derived_from", "depends_on"]),
)


def _reports(
    links: list[GraphLink], start: str, depth: int, mode: str, max_nodes: int, max_edges: int
) -> tuple[Any, Any]:
    traversal = BudgetedTraversal(links)
    kwargs = {"depth": depth, "mode": mode, "relation_allowlist": None, "max_nodes": max_nodes, "max_edges": max_edges}
    return traversal.traverse(start, **kwargs), traversal.traverse(start, **kwargs)


@given(
    links=st.lists(_link, max_size=20),
    start=_node_id,
    depth=st.integers(min_value=1, max_value=6),
    mode=st.sampled_from(["bfs", "dfs"]),
    max_nodes=st.integers(min_value=1, max_value=10),
    max_edges=st.integers(min_value=1, max_value=15),
)
@settings(derandomize=True, max_examples=50, deadline=None)
def test_traversal_respects_node_and_edge_guards(
    links: list[GraphLink], start: str, depth: int, mode: str, max_nodes: int, max_edges: int
) -> None:
    report, _ = _reports(links, start, depth, mode, max_nodes, max_edges)
    assert len(report.nodes) <= max_nodes
    assert len(report.edges) <= max_edges
    assert set(report.guards_hit) <= {"max_nodes", "max_edges", "depth"}


@given(
    links=st.lists(_link, max_size=20),
    start=_node_id,
    depth=st.integers(min_value=1, max_value=6),
    mode=st.sampled_from(["bfs", "dfs"]),
)
@settings(derandomize=True, max_examples=50, deadline=None)
def test_traversal_is_deterministic_and_visits_unique_nodes(
    links: list[GraphLink], start: str, depth: int, mode: str
) -> None:
    first, second = _reports(links, start, depth, mode, 64, 256)
    assert first == second
    assert first.nodes[0] == start
    assert len(first.nodes) == len(set(first.nodes))


@given(
    links=st.lists(_link, max_size=20),
    start=_node_id,
    depth=st.integers(min_value=1, max_value=6),
    mode=st.sampled_from(["bfs", "dfs"]),
)
@settings(derandomize=True, max_examples=50, deadline=None)
def test_traversal_edges_come_from_the_input_links(links: list[GraphLink], start: str, depth: int, mode: str) -> None:
    report, _ = _reports(links, start, depth, mode, 64, 256)
    link_set = {(link.source, link.target, link.relation) for link in links}
    assert {(link.source, link.target, link.relation) for link in report.edges} <= link_set
    visited = set(report.nodes)
    assert all(link.source in visited and link.target in visited for link in report.edges)
    for link in report.cross_edges:
        assert link.source in visited and link.target in visited
        assert (link.source, link.target, link.relation) not in {
            (edge.source, edge.target, edge.relation) for edge in report.edges
        }


@given(
    links=st.lists(_link, max_size=20),
    start=_node_id,
    allowlist=st.frozensets(st.sampled_from(["parent_of", "supports"]), min_size=1),
)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_traversal_allowlist_filters_every_traversed_edge(
    links: list[GraphLink], start: str, allowlist: frozenset[str]
) -> None:
    report = BudgetedTraversal(links).traverse(
        start, depth=6, mode="bfs", relation_allowlist=allowlist, max_nodes=64, max_edges=256
    )
    assert all(link.relation in allowlist for link in report.edges)


@pytest.mark.parametrize("depth", [0, 7, -1])
def test_traversal_rejects_out_of_range_depth(depth: int) -> None:
    with pytest.raises(ValueError):
        BudgetedTraversal([]).traverse("a", depth=depth, mode="bfs", relation_allowlist=None, max_nodes=4, max_edges=4)


def test_traversal_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        BudgetedTraversal([]).traverse("a", depth=2, mode="random", relation_allowlist=None, max_nodes=4, max_edges=4)


# ------------------------------------------------------------- router outcomes


def _graph_nodes() -> st.SearchStrategy[list[dict[str, Any]]]:
    primary = [
        {"id": domain.value, "kind": "primary", "domain": domain.value, "label": domain.title, "context_tags": []}
        for domain in PrimaryDomain
    ]
    children = st.lists(
        st.fixed_dictionaries(
            {
                "id": _node_id,
                "kind": st.just("child"),
                "domain": st.sampled_from([domain.value for domain in PrimaryDomain]),
                "label": st.text(alphabet=st.characters(whitelist_categories=("Ll",)), min_size=2, max_size=16),
                "context_tags": st.lists(_word, max_size=2),
            }
        ),
        max_size=8,
        unique_by=lambda node: str(node["id"]),
    )
    return children.map(lambda extra: [*primary, *extra])


@given(nodes=_graph_nodes(), purpose=st.text(max_size=80), tags=st.lists(_word, max_size=3))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_route_outcome_is_always_typed_and_consistent(
    nodes: list[dict[str, Any]], purpose: str, tags: list[str]
) -> None:
    outcome = PrimaryRouter(nodes).route(domain=None, purpose=purpose, context_tags=tags)
    assert outcome.status in {"direct", "ranked", "ambiguous", "no_match", "missing_root"}
    node_ids = {str(node["id"]) for node in nodes}
    if outcome.status == "ambiguous":
        assert len(outcome.candidates) >= 2
        assert outcome.root_node_id is None
    elif outcome.status == "ranked":
        assert outcome.root_node_id in node_ids
        assert outcome.candidates == ()
    else:
        assert outcome.candidates == ()
        assert outcome.root_node_id is None


@given(purpose=st.text(max_size=60))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_route_with_supplied_domain_is_direct_when_root_present(purpose: str) -> None:
    nodes = [
        {"id": domain.value, "kind": "primary", "domain": domain.value, "label": domain.title, "context_tags": []}
        for domain in PrimaryDomain
    ]
    outcome = PrimaryRouter(nodes).route(domain="P3", purpose=purpose, context_tags=())
    assert outcome.status == "direct"
    assert outcome.domain is PrimaryDomain.P3
    assert outcome.root_node_id == "P3"


@given(purpose=st.text(max_size=60))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_route_missing_root_never_falls_back_to_keywords(purpose: str) -> None:
    nodes = [
        {"id": domain.value, "kind": "primary", "domain": domain.value, "label": domain.title, "context_tags": []}
        for domain in PrimaryDomain
        if domain is not PrimaryDomain.P4
    ]
    outcome = PrimaryRouter(nodes).route(domain="keputusan", purpose=purpose, context_tags=())
    assert outcome.status == "missing_root"
    assert outcome.domain is PrimaryDomain.P4


@given(nodes=_graph_nodes(), purpose=_word, tags=st.lists(_word, max_size=2))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_route_is_deterministic_across_repeated_calls(
    nodes: list[dict[str, Any]], purpose: str, tags: list[str]
) -> None:
    router = PrimaryRouter(nodes)
    assert router.route(domain=None, purpose=purpose, context_tags=tags) == router.route(
        domain=None, purpose=purpose, context_tags=tags
    )
