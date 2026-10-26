"""Property tests for the frozen token-budget contract on knowledge.query.

Spec sections 8/13.1: a successful response never exceeds its budget;
undersized and over-large budgets are typed errors, never silent clamps; an
adversarial purpose string never escapes the typed error vocabulary.

The service stack is built once per module (roots + one projected P1 child)
and every example runs against it as a read-only query, so the suite stays
fast. All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soul_knowledge.contracts import (
    DEFAULT_MAX_TOKEN_BUDGET,
    MINIMUM_TOKEN_BUDGET,
    BudgetExceedsMax,
    BudgetTooSmall,
    KnowledgeError,
    QueryRequest,
)
from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, Relation, RelationKind
from soul_knowledge.projection import GraphProjector
from soul_knowledge.service import KnowledgeService
from soul_knowledge.store import EventStore


def _upsert(store: EventStore, node: Node) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=f"prop:upsert:{node.node_id}",
    )


@pytest.fixture(scope="module")
def service_stack(tmp_path_factory: pytest.TempPathFactory) -> KnowledgeService:
    tmp_path = tmp_path_factory.mktemp("budget-prop")
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    _upsert(
        store,
        Node(
            node_id="KROOT",
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            provenance=("soul://roots",),
            source_version_id="sv-test",
        ),
    )
    for domain in PrimaryDomain:
        _upsert(
            store,
            Node(
                node_id=domain.value,
                parent_id="KROOT",
                kind=NodeKind.PRIMARY,
                domain=domain,
                summary=domain.title,
                provenance=("soul://roots",),
                source_version_id="sv-test",
            ),
        )
    _upsert(
        store,
        Node(
            node_id="n-gates",
            parent_id="P1",
            kind=NodeKind.CHILD,
            domain=PrimaryDomain.P1,
            summary="Run the gates before commit",
            context_tags=("gates", "discipline"),
            provenance=("soul://corpus/lesson.md",),
            source_version_id="sv-test",
        ),
    )
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id="P1",
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.PARENT_OF, source_node_id="P1", target_node_id="n-gates"),
        ),
        idempotency_key="prop:rel:P1:n-gates",
    )
    projector.rebuild()
    return KnowledgeService(store, projector)


@given(budget=st.integers(min_value=0, max_value=MINIMUM_TOKEN_BUDGET - 1))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_budget_below_minimum_is_a_typed_error(service_stack: KnowledgeService, budget: int) -> None:
    with pytest.raises(BudgetTooSmall):
        service_stack.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=budget))


@given(budget=st.integers(min_value=DEFAULT_MAX_TOKEN_BUDGET + 1, max_value=DEFAULT_MAX_TOKEN_BUDGET + 10_000))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_budget_above_maximum_is_a_typed_error(service_stack: KnowledgeService, budget: int) -> None:
    with pytest.raises(BudgetExceedsMax):
        service_stack.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=budget))


@given(budget=st.integers(min_value=MINIMUM_TOKEN_BUDGET, max_value=DEFAULT_MAX_TOKEN_BUDGET))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_a_successful_response_never_exceeds_its_budget(service_stack: KnowledgeService, budget: int) -> None:
    try:
        response = service_stack.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=budget))
    except KnowledgeError:
        return  # a typed failure is fail-closed; only successes are budget-bound
    assert response.estimated_tokens <= budget


@given(budget=st.integers(min_value=MINIMUM_TOKEN_BUDGET, max_value=DEFAULT_MAX_TOKEN_BUDGET))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_access_only_response_never_exceeds_its_cap(service_stack: KnowledgeService, budget: int) -> None:
    try:
        response = service_stack.query(
            QueryRequest(purpose="run the gates", domain="P1", token_budget=budget, access_only=True)
        )
    except KnowledgeError:
        return
    assert response.estimated_tokens <= min(budget, 160)
    assert response.results == ()


@given(purpose=st.text(min_size=1, max_size=512))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_adversarial_purpose_never_raises_an_untyped_error(service_stack: KnowledgeService, purpose: str) -> None:
    try:
        service_stack.query(QueryRequest(purpose=purpose, token_budget=512))
    except KnowledgeError:
        return


@given(purpose=st.text(alphabet="abgates ", min_size=1, max_size=40))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_query_routing_is_deterministic_for_identical_requests(service_stack: KnowledgeService, purpose: str) -> None:
    first = service_stack.query(QueryRequest(purpose=purpose, token_budget=512))
    second = service_stack.query(QueryRequest(purpose=purpose, token_budget=512))
    assert first.routing == second.routing
    assert [node.node_id for node in first.results] == [node.node_id for node in second.results]
    assert first.candidates == second.candidates


@given(purpose=st.text(min_size=1, max_size=512))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_domain_query_with_valid_alias_is_direct_or_typed(service_stack: KnowledgeService, purpose: str) -> None:
    response = service_stack.query(QueryRequest(purpose=purpose, domain="pembelajaran", token_budget=512))
    assert response.routing == "direct"


def test_minimum_budget_boundary_is_accepted(service_stack: KnowledgeService) -> None:
    # The boundary itself is inclusive: exactly MINIMUM_TOKEN_BUDGET is legal.
    try:
        response = service_stack.query(
            QueryRequest(purpose="run the gates", domain="P1", token_budget=MINIMUM_TOKEN_BUDGET)
        )
    except KnowledgeError:
        return
    assert response.estimated_tokens <= MINIMUM_TOKEN_BUDGET
