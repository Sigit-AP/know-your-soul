"""Contract v2 tests: the four G1 retrieval verbs (path/explain/affected/god_nodes).

Operator ruling 2026-10-26 (``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/progress.md``):
the frozen ten spec operations keep their semantics; the Graphify-parity G1
verbs extend the knowledge surface as contract v2 — addendum
``docs/contracts/retrieval-verbs-v2.md``. Matrix row 37 (path/explain
retrieval parity).

Every verb is a read: it binds the active projection snapshot, returns the
shared metadata envelope, honors the token budget (truncation for list
payloads; a typed ``MetadataExceedsBudget`` for the non-truncatable path
payload), and produces exactly one final audit event per invocation. All
algorithms are deterministic (BFS over the ``graph.json`` links list with
sorted ``(relation, peer)`` tie-breaks); no model, no ranking.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from soul_knowledge.contracts import (
    MINIMUM_TOKEN_BUDGET,
    BudgetTooSmall,
    MetadataExceedsBudget,
    NodeNotFound,
)
from soul_knowledge.domain import canonical_json

# New contract-v2 symbols are imported lazily inside the tests so a missing
# implementation fails as test failures (RED), not as a collection error.
from soul_knowledge.retrieval import GraphLink
from test_service import _child, _relate, _roots, _stack, _upsert

# ------------------------------------------------------------------ fixtures


def _verb_seeded(tmp_path: Path) -> tuple[Any, Any, Any]:
    """Roots plus a deterministic relation web over seven child nodes.

    Directed links (plus the parent_id-synthesized ``parent_of`` rows):

        n-a -supports-> n-x, n-y      n-x, n-y -supports-> n-b
        n-b -depends_on-> n-c         n-d -supports-> n-c
        n-c -supports-> n-a           (cycle back to n-a)

    n-z hangs off P1 only (``parent_of``), so nothing reaches it from the web.
    """
    store, projector, service = _stack(tmp_path)
    _roots(store)
    for node_id in ("n-a", "n-b", "n-c", "n-d", "n-x", "n-y", "n-z"):
        _upsert(store, _child(node_id, summary=f"verb node {node_id}"))
    for source, target, relation in (
        ("n-a", "n-x", "supports"),
        ("n-a", "n-y", "supports"),
        ("n-x", "n-b", "supports"),
        ("n-y", "n-b", "supports"),
        ("n-b", "n-c", "depends_on"),
        ("n-d", "n-c", "supports"),
        ("n-c", "n-a", "supports"),
    ):
        _relate(store, source, target, relation)
    projector.rebuild()
    return store, projector, service


def _links() -> list[GraphLink]:
    """The same web as ``_verb_seeded`` without the store, for unit probes."""
    return [
        GraphLink(source=source, target=target, relation=relation)
        for source, target, relation in (
            ("n-a", "n-x", "supports"),
            ("n-a", "n-y", "supports"),
            ("n-x", "n-b", "supports"),
            ("n-y", "n-b", "supports"),
            ("n-b", "n-c", "depends_on"),
            ("n-d", "n-c", "supports"),
            ("n-c", "n-a", "supports"),
        )
    ]


# ------------------------------------------------- core: RelationalGraph.path


def test_shortest_path_directed_with_sorted_tie_break() -> None:
    from soul_knowledge.retrieval import RelationalGraph

    graph = RelationalGraph(_links())
    outcome = graph.shortest_path("n-a", "n-c")
    assert outcome.status == "found"
    # Two equal-length paths exist (via n-x or n-y); the sorted (relation,
    # target) adjacency picks n-x deterministically.
    assert outcome.nodes == ("n-a", "n-x", "n-b", "n-c")
    assert [(edge.source, edge.target, edge.relation) for edge in outcome.edges] == [
        ("n-a", "n-x", "supports"),
        ("n-x", "n-b", "supports"),
        ("n-b", "n-c", "depends_on"),
    ]
    # Direction matters: the cycle edge gives the one-hop return path.
    back = graph.shortest_path("n-c", "n-a")
    assert back.status == "found"
    assert back.nodes == ("n-c", "n-a")
    assert len(back.edges) == 1


def test_shortest_path_self_no_path_and_cycle_safety() -> None:
    from soul_knowledge.retrieval import RelationalGraph

    graph = RelationalGraph(_links())
    self_path = graph.shortest_path("n-a", "n-a")
    assert self_path.status == "found"
    assert self_path.nodes == ("n-a",)
    assert self_path.edges == ()
    # No directed route reaches n-d from n-a (n-d only points outward to n-c).
    missing = graph.shortest_path("n-a", "n-d")
    assert missing.status == "no_path"
    assert missing.nodes == () and missing.edges == ()
    # Cycle: n-a -> n-c -> n-a must terminate and never re-enter the seed.
    outcome = graph.shortest_path("n-a", "n-c")
    assert outcome.nodes.count("n-a") == 1
    # Empty graph: everything is a typed no-path, never an exception.
    empty = RelationalGraph(())
    assert empty.shortest_path("a", "b").status == "no_path"
    assert empty.shortest_path("a", "a").status == "found"


# ---------------------------------------------- core: explain / affected / god


def test_explain_neighborhood_sorted_with_counts() -> None:
    from soul_knowledge.retrieval import RelationalGraph

    graph = RelationalGraph(_links())
    explanation = graph.explain_neighborhood("n-c")
    assert [(edge.relation, edge.target) for edge in explanation.outgoing] == [("supports", "n-a")]
    assert [(edge.relation, edge.source) for edge in explanation.incoming] == [
        ("depends_on", "n-b"),
        ("supports", "n-d"),
    ]
    assert explanation.relation_counts == (("depends_on", 1), ("supports", 2))
    isolated = graph.explain_neighborhood("n-lonely")
    assert isolated.outgoing == () and isolated.incoming == ()
    assert isolated.relation_counts == ()


def test_affected_reverse_bfs_depth_bound_cycle_safe() -> None:
    from soul_knowledge.retrieval import RelationalGraph

    graph = RelationalGraph(_links())
    report = graph.affected("n-c", depth=2, max_nodes=256)
    assert [(hit.node_id, hit.depth, hit.relation, hit.via) for hit in report.hits] == [
        ("n-b", 1, "depends_on", "n-c"),
        ("n-d", 1, "supports", "n-c"),
        ("n-x", 2, "supports", "n-b"),
        ("n-y", 2, "supports", "n-b"),
    ]
    # Depth 1 stops before the grandparents.
    shallow = graph.affected("n-c", depth=1, max_nodes=256)
    assert {hit.node_id for hit in shallow.hits} == {"n-b", "n-d"}
    # Cycle safety: affected("n-a") walks n-c -> ... and never re-reports n-a.
    deep = graph.affected("n-a", depth=6, max_nodes=256)
    assert "n-a" not in {hit.node_id for hit in deep.hits}
    assert {hit.node_id for hit in deep.hits} == {"n-b", "n-c", "n-d", "n-x", "n-y"}
    # The max_nodes guard is typed, not silent.
    capped = graph.affected("n-c", depth=2, max_nodes=2)
    assert len(capped.hits) == 2
    assert "max_nodes" in capped.guards_hit
    with pytest.raises(ValueError):
        graph.affected("n-c", depth=0, max_nodes=256)


def test_god_nodes_degree_ranking_deterministic_bounded() -> None:
    from soul_knowledge.retrieval import RelationalGraph

    links = _links() + [GraphLink(source="P1", target=f"n-{item}", relation="parent_of") for item in "abcdxyz"]
    graph = RelationalGraph(links)
    # Measured degree = link rows touching the node (parallel typed relations
    # count; the links list is never collapsed). Ordering: (-degree, node_id).
    ranked = graph.god_nodes(limit=10)
    assert ranked[0] == ("P1", 7)
    degrees = dict(ranked)
    assert degrees["n-c"] == 4  # parent_of + depends_on in, supports in/out
    assert [node for node, _ in ranked[:4]] == ["P1", "n-a", "n-b", "n-c"]
    bounded = graph.god_nodes(limit=2)
    assert bounded == ranked[:2]
    assert RelationalGraph(()).god_nodes(limit=5) == ()
    with pytest.raises(ValueError):
        graph.god_nodes(limit=0)


# --------------------------------------------------------- service: path verb


def test_service_path_found_deterministic_across_runs(tmp_path: Path) -> None:
    _, _, service = _verb_seeded(tmp_path)
    first = service.path("n-a", "n-c")
    second = service.path("n-a", "n-c")
    assert first.status == "found"
    assert first.hops == 3
    assert first.nodes == ("n-a", "n-x", "n-b", "n-c")
    assert len(first.edges) == 3
    assert first.audit_event_id and second.audit_event_id
    assert first.audit_event_id != second.audit_event_id  # one audit per invocation
    # Determinism: identical canonical payload modulo the per-invocation audit id.
    left = {**first.canonical_payload(), "audit_event_id": ""}
    right = {**second.canonical_payload(), "audit_event_id": ""}
    assert canonical_json(left) == canonical_json(right)
    assert first.estimated_tokens <= 1024


def test_service_path_typed_outcomes(tmp_path: Path) -> None:
    store, _, service = _verb_seeded(tmp_path)
    # Known node, no directed route: typed result, never an exception.
    outcome = service.path("n-a", "n-z")
    assert outcome.status == "no_path"
    assert outcome.nodes == () and outcome.edges == () and outcome.hops == 0
    assert outcome.audit_event_id
    # Self path: zero hops, one node, no edges.
    reflexive = service.path("n-b", "n-b")
    assert reflexive.status == "found" and reflexive.nodes == ("n-b",)
    # Unknown endpoint: typed NODE_NOT_FOUND, exactly one audit event.
    audits_before = store.audit_event_count()
    with pytest.raises(NodeNotFound) as excinfo:
        service.path("n-missing", "n-c")
    assert excinfo.value.code == "NODE_NOT_FOUND"
    assert excinfo.value.audit_event_id
    assert store.audit_event_count() == audits_before + 1


def test_service_path_budget_is_honored_without_dishonest_truncation(tmp_path: Path) -> None:
    _, _, service = _verb_seeded(tmp_path)
    full = service.path("n-a", "n-c", token_budget=1024)
    assert full.estimated_tokens <= 1024
    # A path is atomic: it never truncates, so an undersized budget is a
    # typed MetadataExceedsBudget, not a partial (dishonest) path.
    tight = max(MINIMUM_TOKEN_BUDGET, full.estimated_tokens - 1)
    if tight < full.estimated_tokens:
        with pytest.raises(MetadataExceedsBudget):
            service.path("n-a", "n-c", token_budget=tight)
    with pytest.raises(BudgetTooSmall):
        service.path("n-a", "n-c", token_budget=MINIMUM_TOKEN_BUDGET - 1)


# ------------------------------------------------------- service: explain verb


def test_service_explain_relational_not_subtree(tmp_path: Path) -> None:
    _, _, service = _verb_seeded(tmp_path)
    response = service.explain("n-c")
    assert response.node.node_id == "n-c"
    # Outgoing/incoming are the typed incident links, deterministically sorted
    # by (relation, peer); this is a relational explanation, not a subtree.
    assert [(edge.relation, edge.target) for edge in response.outgoing] == [("supports", "n-a")]
    assert [(edge.relation, edge.source) for edge in response.incoming] == [
        ("depends_on", "n-b"),
        ("parent_of", "P1"),
        ("supports", "n-d"),
    ]
    assert response.relation_counts == {"depends_on": 1, "parent_of": 1, "supports": 2}
    assert response.truncated is False
    assert response.audit_event_id
    assert response.estimated_tokens <= 1024
    with pytest.raises(NodeNotFound):
        service.explain("n-missing")


def test_service_explain_truncates_to_budget(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    _roots(store)
    _upsert(store, _child("n-hub", summary="hub node"))
    for index in range(12):
        _upsert(store, _child(f"n-dep-{index:02d}", summary=f"dependent {index}"))
        _relate(store, f"n-dep-{index:02d}", "n-hub", "depends_on")
    projector.rebuild()
    full = service.explain("n-hub", token_budget=8192)
    assert len(full.incoming) == 13  # 12 dependents + parent_of P1
    budget = max(MINIMUM_TOKEN_BUDGET, full.estimated_tokens - 1)
    tight = service.explain("n-hub", token_budget=budget)
    assert tight.truncated is True
    assert len(tight.incoming) < 13
    assert tight.estimated_tokens <= budget
    assert tight.node.node_id == "n-hub"  # the node view never truncates away


# ------------------------------------------------------ service: affected verb


def test_service_affected_reverse_dependencies(tmp_path: Path) -> None:
    _, _, service = _verb_seeded(tmp_path)
    response = service.affected("n-c", depth=2)
    got = [(hit.node_id, hit.depth, hit.relation, hit.via) for hit in response.affected]
    assert got == [
        ("n-b", 1, "depends_on", "n-c"),
        ("P1", 1, "parent_of", "n-c"),
        ("n-d", 1, "supports", "n-c"),
        # BFS order at depth 2: n-b's referrers first, then P1's.
        ("n-x", 2, "supports", "n-b"),
        ("n-y", 2, "supports", "n-b"),
        ("KROOT", 2, "parent_of", "P1"),
    ]
    assert response.node_id == "n-c"
    assert response.audit_event_id
    shallow = service.affected("n-c", depth=1)
    assert {hit.node_id for hit in shallow.affected} == {"P1", "n-b", "n-d"}
    with pytest.raises(NodeNotFound):
        service.affected("n-missing")


def test_service_affected_budget_truncation(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    _roots(store)
    _upsert(store, _child("n-base", summary="base node"))
    for index in range(10):
        _upsert(store, _child(f"n-user-{index:02d}", summary=f"user {index}"))
        _relate(store, f"n-user-{index:02d}", "n-base", "depends_on")
    projector.rebuild()
    full = service.affected("n-base", depth=2, token_budget=8192)
    # 10 users + P1 (parent_of) at depth 1, KROOT (parent_of P1) at depth 2.
    assert len(full.affected) == 12
    budget = max(MINIMUM_TOKEN_BUDGET, full.estimated_tokens - 1)
    tight = service.affected("n-base", depth=2, token_budget=budget)
    assert tight.truncated is True
    assert len(tight.affected) < 12
    assert tight.estimated_tokens <= budget


# ----------------------------------------------------- service: god_nodes verb


def test_service_god_nodes_measured_degree(tmp_path: Path) -> None:
    _, _, service = _verb_seeded(tmp_path)
    response = service.god_nodes(limit=5)
    assert response.limit == 5
    assert len(response.nodes) == 5
    degrees = [node.degree for node in response.nodes]
    assert degrees == sorted(degrees, reverse=True)
    # P1 carries the seven parent_of rows plus the KROOT inbound row.
    assert response.nodes[0].node_id == "P1"
    assert response.nodes[0].degree == 8
    assert response.nodes[0].summary
    # Deterministic across runs (modulo the audit id).
    again = service.god_nodes(limit=5)
    assert [node.node_id for node in again.nodes] == [node.node_id for node in response.nodes]
    with pytest.raises(BudgetTooSmall):
        service.god_nodes(limit=5, token_budget=MINIMUM_TOKEN_BUDGET - 1)


def test_god_nodes_and_path_on_an_empty_graph(tmp_path: Path) -> None:
    store, projector, service = _stack(tmp_path)
    projector.rebuild()  # empty store -> empty (but active) generation
    gods = service.god_nodes(limit=10)
    assert gods.nodes == ()
    assert gods.audit_event_id
    with pytest.raises(NodeNotFound):
        service.path("n-a", "n-b")
    with pytest.raises(NodeNotFound):
        service.explain("n-a")
    with pytest.raises(NodeNotFound):
        service.affected("n-a")


# ------------------------------------------------------------ audit parity


def test_every_verb_invocation_is_audited_exactly_once(tmp_path: Path) -> None:
    store, _, service = _verb_seeded(tmp_path)
    watermark = store.knowledge_high_watermark()
    audits_before = store.audit_event_count()
    receipts = [
        service.path("n-a", "n-c").audit_event_id,
        service.explain("n-c").audit_event_id,
        service.affected("n-c", depth=2).audit_event_id,
        service.god_nodes(limit=5).audit_event_id,
    ]
    assert all(receipts)
    assert len(set(receipts)) == 4
    assert store.audit_event_count() == audits_before + 4
    # Read audits never move the knowledge watermark (no self-staleness).
    assert store.knowledge_high_watermark() == watermark
    assert store.verify_chain().ok


# -------------------------------------------------------------- adapter probes


def test_http_dispatch_exposes_all_four_verbs(tmp_path: Path) -> None:
    from soul_knowledge.http_api import KNOWLEDGE_OPERATIONS, build_stack, dispatch_text, to_jsonable

    for operation in ("path", "explain", "affected", "god_nodes"):
        assert operation in KNOWLEDGE_OPERATIONS
    _verb_seeded(tmp_path)
    stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    try:
        pathed = to_jsonable(stack.service.path("n-a", "n-c"))  # service-level sanity
        assert pathed["status"] == "found"
        dispatched = to_jsonable(
            dispatch_text(stack, "path", json.dumps({"from_id": "n-a", "to_id": "n-c", "token_budget": 1024}))
        )
        assert dispatched["status"] == "found"
        assert dispatched["nodes"] == ["n-a", "n-x", "n-b", "n-c"]
        explained = to_jsonable(dispatch_text(stack, "explain", json.dumps({"node_id": "n-c", "token_budget": 1024})))
        assert explained["node"]["node_id"] == "n-c"
        affected = to_jsonable(dispatch_text(stack, "affected", json.dumps({"node_id": "n-c", "depth": 1})))
        assert {hit["node_id"] for hit in affected["affected"]} == {"P1", "n-b", "n-d"}
        gods = to_jsonable(dispatch_text(stack, "god_nodes", json.dumps({"limit": 2})))
        assert [node["node_id"] for node in gods["nodes"]] == ["P1", "KROOT"]
    finally:
        stack.close()


def test_cli_exposes_all_four_verbs(tmp_path: Path, capsys: Any) -> None:
    from soul_knowledge.cli import main

    _verb_seeded(tmp_path)
    base = ["--store", str(tmp_path / "soul.sqlite3"), "--output-root", str(tmp_path / "out")]
    assert main([*base, "path", '{"from_id": "n-a", "to_id": "n-c", "token_budget": 1024}']) == 0
    pathed = json.loads(capsys.readouterr().out)
    assert pathed["status"] == "found"
    assert pathed["nodes"] == ["n-a", "n-x", "n-b", "n-c"]
    assert main([*base, "explain", '{"node_id": "n-c", "token_budget": 1024}']) == 0
    explained = json.loads(capsys.readouterr().out)
    assert explained["node"]["node_id"] == "n-c"
    assert main([*base, "affected", '{"node_id": "n-c", "depth": 1}']) == 0
    affected = json.loads(capsys.readouterr().out)
    assert {hit["node_id"] for hit in affected["affected"]} == {"P1", "n-b", "n-d"}
    # CLI renders the underscore as a dash (god-nodes), like every operation.
    assert main([*base, "god-nodes", '{"limit": 2}']) == 0
    gods = json.loads(capsys.readouterr().out)
    assert [node["node_id"] for node in gods["nodes"]] == ["P1", "KROOT"]


def test_mcp_exposes_all_four_verbs_with_strict_schemas(tmp_path: Path) -> None:
    import anyio

    from soul_knowledge.mcp_server import EXPECTED_TOOLS, build_server
    from test_mcp_server import _call_tool, _error_payload, _result_text, _session

    _, _, service = _verb_seeded(tmp_path)
    for tool in ("knowledge_path", "knowledge_explain", "knowledge_affected", "knowledge_god_nodes"):
        assert tool in EXPECTED_TOOLS
    assert len(EXPECTED_TOOLS) == 14
    server = build_server(service, graph_root=tmp_path / "out")

    async def scenario() -> None:
        async with _session(server) as session:
            listed = await session.list_tools()
            by_name = {tool.name: tool for tool in listed.tools}
            path_schema = by_name["knowledge_path"].input_schema
            assert path_schema.get("additionalProperties") is False
            assert set(path_schema["required"]) == {"from_id", "to_id"}
            god_schema = by_name["knowledge_god_nodes"].input_schema
            assert god_schema["properties"]["limit"]["minimum"] == 1
            assert god_schema["properties"]["limit"]["maximum"] == 64

            pathed = await _call_tool(session, "knowledge_path", {"from_id": "n-a", "to_id": "n-c"})
            assert not pathed.is_error
            payload = json.loads(_result_text(pathed))
            assert payload["status"] == "found"
            assert payload["nodes"] == ["n-a", "n-x", "n-b", "n-c"]

            explained = await _call_tool(session, "knowledge_explain", {"node_id": "n-c"})
            assert json.loads(_result_text(explained))["node"]["node_id"] == "n-c"

            affected = await _call_tool(session, "knowledge_affected", {"node_id": "n-c", "depth": 1})
            hits = json.loads(_result_text(affected))["affected"]
            assert {hit["node_id"] for hit in hits} == {"P1", "n-b", "n-d"}

            gods = await _call_tool(session, "knowledge_god_nodes", {"limit": 2})
            assert [n["node_id"] for n in json.loads(_result_text(gods))["nodes"]] == ["P1", "KROOT"]

            # Strict validation parity: unknown properties are typed rejections.
            rejected = await _call_tool(session, "knowledge_path", {"from_id": "n-a", "to_id": "n-c", "bogus": 1})
            assert _error_payload(rejected)["code"] == "INVALID_ARGUMENTS"
            missing = await _call_tool(session, "knowledge_god_nodes", {"limit": 0})
            assert _error_payload(missing)["code"] == "INVALID_ARGUMENTS"

    anyio.run(scenario)
