"""The token-bounded knowledge service: five audited operations (Task 4).

Task 4 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7 (invocation/audit flow), 8 (retrieval and direct tools) and
13.1/13.2.

* Every operation — reads included — is wrapped in one durable invocation
  with a pre-allocated ``audit_event_id`` and exactly one final audit event.
  Read audits are ``knowledge_affecting=False``, so a self-auditing read never
  moves the knowledge watermark and never makes itself stale (spec 7.5/8).
* Reads linearize at invocation start (``required_knowledge_watermark``) and
  bind to the active projection manifest. A stale read triggers one
  synchronous repair and one retry; still-stale is a typed retryable error
  (spec 7.6). Read content comes from the published generation directory and
  the store only — never from the raw corpus.
* Graph snapshots and lexical indexes cache on ``(graph_content_hash,
  projection_version)`` — never ``(mtime_ns, size)`` (spec 13.1).
* Successful responses satisfy the frozen ``ceil(utf8_bytes/4)`` budget over
  the complete canonical payload; undersized budgets are typed errors and
  metadata overflow is an error, never an over-budget success (token-budget
  contract 1.0.0).

``evaluate`` is a fixed contract with an audited typed not-implemented until
Task 5 lands the scoring engine.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from soul_knowledge.audit import AuditLog, success_outcome
from soul_knowledge.contracts import (
    ACCESS_ONLY_TOKEN_CAP,
    DEFAULT_MAX_TOKEN_BUDGET,
    MINIMUM_TOKEN_BUDGET,
    ApplyImprovementRequest,
    ApprovalRequiredError,
    BudgetExceedsMax,
    BudgetTooSmall,
    DueReviewView,
    EvaluateRequest,
    EvaluateResponse,
    ImprovementNotFound,
    ImprovementResponse,
    InvalidProposal,
    KnowledgeError,
    LifecycleError,
    MetadataExceedsBudget,
    MixedSourceVersionsError,
    NodeNotFound,
    NodeResponse,
    NodeView,
    OperatorDecisionRequiredError,
    PrimaryRootMissing,
    ProposalResponse,
    ProposeImprovementRequest,
    QueryRequest,
    QueryResponse,
    RecommendationBlock,
    RecommendationItem,
    RegressionGateError,
    ResultNode,
    ReviewDecisionView,
    ReviewRequest,
    ReviewResponse,
    RollbackRequest,
    RollbackResponse,
    StaleProjectionUnavailable,
    SubtreeEdge,
    SubtreeResponse,
    UnknownRelearnError,
    estimated_tokens_of_payload,
    score_text,
)
from soul_knowledge.domain import Event, EventKind, Evidence, Node, canonical_json, quantize_score
from soul_knowledge.events import Invocation, InvocationOutcome, WriteReceipt
from soul_knowledge.improvement import (
    ApprovalRequired,
    ProposalValidationError,
    RegressionGateFailure,
    UnknownImprovement,
    apply_improvement,
    get_proposal,
    propose,
    rollback_improvement,
)
from soul_knowledge.improvement import UnknownNode as ImprovementUnknownNode
from soul_knowledge.projection import GraphProjector, ProjectorBusy
from soul_knowledge.read_model_validator import ReadModelValidator
from soul_knowledge.relearn import (
    BackupUnavailable,
    MixedSourceVersions,
    OperatorDecisionRequired,
    RelearnEngine,
    UnknownRelearn,
)
from soul_knowledge.retrieval import (
    BudgetedTraversal,
    LexicalIndex,
    PrimaryRouter,
    estimated_tokens,
    query_terms,
    sanitize_text,
)
from soul_knowledge.review import ReviewEngine
from soul_knowledge.review import UnknownNode as ReviewUnknownNode
from soul_knowledge.scheduler import DueReviewScheduler
from soul_knowledge.scoring import ScoreEngine, UnknownNodeError
from soul_knowledge.security import redact_text
from soul_knowledge.store import EventStore, StoreError

__all__ = ["KnowledgeService"]

_MAX_SNAPSHOT_CACHE = 8
_MAX_PROVENANCE_ENTRIES = 32
_MAX_PROVENANCE_LENGTH = 256
_MAX_SUMMARY_LENGTH = 256
_RECOMMENDATION_LIST_CAP = 3
_RECOMMENDATION_EXPIRY_DAYS = 30


def _redacted_content(text: str, *, max_length: int) -> str:
    """Sanitize then redact one content-derived string leaving the service.

    Defense-in-depth (residual audit wave-1, R1): the publish gate already
    fails closed on secret shapes, but every read surface additionally passes
    content text through :func:`soul_knowledge.security.redact_text`. Clean
    input is byte-identical (``redact_text`` only rewrites pattern matches),
    so the frozen token-budget contract is untouched for clean corpora; only
    the string payload changes, never the response structure.
    """

    return redact_text(sanitize_text(text, max_length=max_length))


class _Snapshot:
    """One validated, cached view of an active generation."""

    def __init__(
        self, manifest: dict[str, Any], nodes: dict[str, dict[str, Any]], traversal: BudgetedTraversal
    ) -> None:
        self.manifest = manifest
        self.nodes = nodes
        self.traversal = traversal
        self._router: PrimaryRouter | None = None
        self._index: LexicalIndex | None = None

    @property
    def router(self) -> PrimaryRouter:
        if self._router is None:
            self._router = PrimaryRouter(self.nodes.values(), self.traversal.links)
        return self._router

    @property
    def index(self) -> LexicalIndex:
        if self._index is None:
            self._index = LexicalIndex.from_graph_nodes(self.nodes.values())
        return self._index

    @property
    def watermark(self) -> int:
        return int(self.manifest["source_knowledge_high_watermark"])

    @property
    def projection_version(self) -> str:
        return str(self.manifest["projection_version"])


class KnowledgeService:
    """The application service behind the CLI, local API and MCP surfaces."""

    def __init__(
        self,
        store: EventStore,
        projector: GraphProjector,
        *,
        audit_log: AuditLog | None = None,
        validator: ReadModelValidator | None = None,
        max_token_budget: int = DEFAULT_MAX_TOKEN_BUDGET,
        default_actor: str = "operator",
    ) -> None:
        self._store = store
        self._projector = projector
        self._audit = audit_log if audit_log is not None else AuditLog(store)
        self._validator = validator if validator is not None else ReadModelValidator()
        self._max_token_budget = max_token_budget
        self._default_actor = default_actor
        self._snapshot_cache: dict[tuple[str, str], _Snapshot] = {}
        self._cache_lock = threading.Lock()
        # Lifecycle engines (Tasks 5/6) share one evidence-scope hook so the
        # relearn quarantine excludes the same evidence from scoring and from
        # review counts (Task 6 concern #2). The production engine is wired to
        # the projector's generation root so its verified backup covers the
        # whole protected set (SQLite + CURRENT pointer + generations/), not
        # just the database file (relearn contract "Backup and rollback").
        self._relearn_engine = RelearnEngine(
            store,
            generation_dir=projector._output_root,  # noqa: SLF001 - same-package wiring (mcp_server precedent)
        )
        self._evidence_scope = self._relearn_scope
        self._score_engine = ScoreEngine(store, evidence_scope=self._evidence_scope)
        self._review_engine = ReviewEngine(
            store,
            registry=self._relearn_engine.registry,
            evidence_scope=self._evidence_scope,
        )
        self._scheduler = DueReviewScheduler(store, self._review_engine)

    # ------------------------------------------------------------- introspection

    def _relearn_scope(self) -> tuple[frozenset[str], frozenset[str]]:
        """The shared ``(compatible, quarantined)`` evidence scope.

        Both scoring (Task 5) and review (Task 6) consume this hook, so a
        relearn quarantine excludes the same evidence everywhere instead of
        only in the review count (Task 6 concern #2).

        The source-version filter is only meaningful once a source manifest
        exists. A store that never recorded a source version has nothing to be
        incompatible with, so the compatible set is "every recorded evidence
        source version" rather than the empty fail-closed set — otherwise a
        freshly built store could never score anything. Once the registry holds
        rows, ``active() is None`` (all CURRENT pending an operator decision)
        fails closed exactly as the relearn contract requires.
        """
        quarantined = self._relearn_engine.quarantined_evidence_ids()
        compatible = self._relearn_engine.compatible_source_version_ids()
        if not compatible:
            with self._store.read() as conn:
                registry_rows = conn.execute("SELECT COUNT(*) AS n FROM source_version").fetchone()
            if registry_rows is not None and int(registry_rows["n"]) == 0:
                compatible = self._evidence_source_versions()
        return compatible, quarantined

    def _evidence_source_versions(self) -> frozenset[str]:
        with self._store.read() as conn:
            rows = conn.execute("SELECT DISTINCT source_version_id FROM evidence").fetchall()
        return frozenset(str(row["source_version_id"]) for row in rows if row["source_version_id"] is not None)

    def snapshot_cache_keys(self) -> list[tuple[str, str]]:
        """The warm cache keys: ``(graph_content_hash, projection_version)`` only."""
        with self._cache_lock:
            return sorted(self._snapshot_cache)

    # ------------------------------------------------------------------ plumbing

    @staticmethod
    def _request_hash(payload: Any) -> str:
        return hashlib.sha256(canonical_json(json.loads(canonical_json(payload))).encode("utf-8")).hexdigest()

    def _begin(
        self, operation: str, request_payload: Any, actor: str | None, idempotency_key: str | None
    ) -> Invocation:
        invocation = Invocation.create(
            operation,
            request_hash=self._request_hash(request_payload),
            actor=actor or self._default_actor,
            idempotency_key=idempotency_key,
        )
        self._audit.begin(invocation)
        return invocation

    def _finish_success(self, invocation: Invocation, started: float, result_references: dict[str, Any]) -> None:
        # Spec 7.7 read path: the same shared outcome shape as the mutation
        # paths (audit.success_outcome, c5792f9). A read never moves the
        # projection, so its projection outcome is "unchanged" — distinct from
        # a mutation's "appended" and a failure's "skipped".
        self._audit.complete(
            invocation.invocation_id,
            success_outcome(
                invocation,
                started,
                result_references=result_references,
                projection_outcome="unchanged",
            ),
        )

    def _finish_failure(self, invocation: Invocation, started: float, error: BaseException) -> None:
        code = error.code if isinstance(error, KnowledgeError) else f"{type(error).__name__}: {error}"
        self._audit.complete(
            invocation.invocation_id,
            InvocationOutcome(
                status="failure",
                error=code,
                projection_outcome="skipped",
                duration_ms=int((time.monotonic() - started) * 1000),
                correlation_id=invocation.invocation_id,
            ),
        )

    # ------------------------------------------------------------------ snapshot

    def _try_snapshot(self, required_watermark: int) -> _Snapshot | None:
        """A validated active snapshot covering *required_watermark*, or None."""
        manifest = self._projector.active_manifest()
        generation = self._projector.active_generation_dir()
        if manifest is None or generation is None:
            return None
        if int(manifest.get("source_knowledge_high_watermark", -1)) < required_watermark:
            return None
        if manifest.get("projection_version") != self._store.projection_version():
            return None
        key = (str(manifest["graph_content_hash"]), str(manifest["projection_version"]))
        with self._cache_lock:
            cached = self._snapshot_cache.get(key)
        if cached is not None:
            return cached
        graph_bytes = (generation / "graph.json").read_bytes()
        if hashlib.sha256(graph_bytes).hexdigest() != manifest["graph_content_hash"]:
            return None
        if not self._validator.validate(generation, manifest).ok:
            return None
        graph = json.loads(graph_bytes.decode("utf-8"))
        nodes = {str(node["id"]): dict(node) for node in graph.get("nodes", []) if isinstance(node, dict)}
        snapshot = _Snapshot(manifest, nodes, BudgetedTraversal.from_graph_json(graph))
        with self._cache_lock:
            if len(self._snapshot_cache) >= _MAX_SNAPSHOT_CACHE:
                del self._snapshot_cache[sorted(self._snapshot_cache)[0]]
            self._snapshot_cache[key] = snapshot
        return snapshot

    def _snapshot(self, required_watermark: int) -> _Snapshot:
        snapshot = self._try_snapshot(required_watermark)
        if snapshot is not None:
            return snapshot
        # A stale read triggers synchronous repair and exactly one retry (7.6).
        try:
            self._projector.repair_if_stale()
        except ProjectorBusy:
            pass  # a concurrent writer is repairing; the re-check decides
        snapshot = self._try_snapshot(required_watermark)
        if snapshot is None:
            raise StaleProjectionUnavailable(
                f"the active projection does not cover knowledge watermark {required_watermark}"
            )
        return snapshot

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _scores_of(node: dict[str, Any]) -> dict[str, Any]:
        """The numeric score block; a node without one scores as zero.

        ``node["confidence"]`` in the read model is the Graphify edge label
        (``EXTRACTED``/``INFERRED``/``AMBIGUOUS``), never a number — the numeric
        confidence lives in ``node["scores"]["confidence"]`` (projection.py).
        """
        raw = node.get("scores")
        return raw if isinstance(raw, dict) else {}

    @classmethod
    def _result_node(cls, node: dict[str, Any]) -> ResultNode:
        scores = cls._scores_of(node)
        return ResultNode(
            node_id=str(node["id"]),
            summary=_redacted_content(str(node.get("label") or node["id"]), max_length=_MAX_SUMMARY_LENGTH),
            domain=str(node.get("domain", "")),
            kind=str(node.get("kind", "")),
            version=int(node.get("version", 1)),
            confidence=score_text(scores.get("confidence", "0")),
            p=score_text(scores.get("p", "0")),
        )

    @classmethod
    def _node_view(cls, node: dict[str, Any]) -> NodeView:
        scores = cls._scores_of(node)
        provenance = tuple(
            _redacted_content(str(item), max_length=_MAX_PROVENANCE_LENGTH)
            for item in node.get("provenance", [])
            if isinstance(item, str)
        )
        return NodeView(
            node_id=str(node["id"]),
            summary=_redacted_content(str(node.get("label") or node["id"]), max_length=_MAX_SUMMARY_LENGTH),
            domain=str(node.get("domain", "")),
            kind=str(node.get("kind", "")),
            version=int(node.get("version", 1)),
            lifecycle_status=str(node.get("lifecycle_status", "")),
            source_version_id=str(node.get("source_version_id", "")),
            provenance=provenance,
            context_tags=tuple(
                _redacted_content(str(tag), max_length=_MAX_SUMMARY_LENGTH)
                for tag in node.get("context_tags", [])
                if isinstance(tag, str)
            ),
            confidence=score_text(scores.get("confidence", "0")),
            p=score_text(scores.get("p", "0")),
        )

    def _evidence_ids_by_node(self, node_ids: list[str], upto_knowledge_seq: int) -> dict[str, tuple[str, ...]]:
        """Snapshot-consistent evidence IDs: nothing beyond the read watermark."""
        if not node_ids:
            return {}
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT e.node_id AS node_id, e.evidence_id AS evidence_id FROM evidence e "
                "JOIN event_log ev ON e.event_seq = ev.event_seq "
                "WHERE ev.knowledge_seq IS NOT NULL AND ev.knowledge_seq <= ? "
                "ORDER BY e.evidence_id",
                (upto_knowledge_seq,),
            ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            node_id = str(row["node_id"])
            if node_id in set(node_ids):
                result.setdefault(node_id, []).append(str(row["evidence_id"]))
        return {node_id: tuple(ids) for node_id, ids in result.items()}

    def _recommendations(
        self,
        results: list[ResultNode],
        *,
        required_watermark: int,
        at: datetime,
    ) -> RecommendationBlock:
        """Deterministic three-list block; structured references, never raw text."""
        evidence_map = self._evidence_ids_by_node([result.node_id for result in results], required_watermark)
        expiry = at + timedelta(days=_RECOMMENDATION_EXPIRY_DAYS)
        best: list[RecommendationItem] = []
        recommended: list[RecommendationItem] = []
        unnecessary: list[RecommendationItem] = []
        for result in results:
            evidence_ids = evidence_map.get(result.node_id, ())
            p_score = Decimal(result.p)
            if len(evidence_ids) >= 2 and p_score >= Decimal("0.7"):
                learned_class, reasons, target, promoted = (
                    "validated_practice",
                    ("HIGH_P", "RECENT_EVIDENCE"),
                    best,
                    True,
                )
            elif len(evidence_ids) >= 2 and p_score < Decimal("0.3"):
                learned_class, reasons, target, promoted = "anti_pattern", ("LOW_P",), unnecessary, False
            elif len(evidence_ids) == 1:
                # One observation can never promote: candidates land in
                # `recommendations` with promoted=False (contract 1.0.0).
                learned_class, reasons, target, promoted = "candidate", ("INSUFFICIENT_EVIDENCE",), recommended, False
            else:
                reasons = ("MEDIUM_P",) if p_score >= Decimal("0.3") else ("UNSCORED",)
                learned_class, reasons, target, promoted = "operating_procedure", reasons, recommended, False
            item = RecommendationItem(
                action_id=f"act-{result.node_id}",
                label=f"{learned_class} node {result.node_id}",
                source_node_ids=(result.node_id,),
                source_node_versions=(result.version,),
                evidence_ids=evidence_ids,
                learned_class=learned_class,
                reason_codes=reasons,
                expiry=expiry,
                promoted=promoted,
            )
            target.append(item)
        return RecommendationBlock(
            best_actions=tuple(best[:_RECOMMENDATION_LIST_CAP]),
            recommendations=tuple(recommended[:_RECOMMENDATION_LIST_CAP]),
            unnecessary_actions=tuple(unnecessary[:_RECOMMENDATION_LIST_CAP]),
        )

    @staticmethod
    def _knowledge_payload_tokens(
        results: list[ResultNode],
        *,
        confidence: str,
        node_versions: dict[str, int],
        provenance: tuple[str, ...],
        recommendations: RecommendationBlock,
    ) -> int:
        if not results:
            return 0
        return estimated_tokens_of_payload(
            {
                "confidence": confidence,
                "node_versions": node_versions,
                "provenance": list(provenance),
                "recommendations": json.loads(canonical_json(recommendations.model_dump(mode="json"))),
                "results": [result.model_dump(mode="json") for result in results],
            }
        )

    @staticmethod
    def _finalize_query(base: dict[str, Any]) -> QueryResponse:
        """Fixed-point the self-referential ``estimated_tokens`` field."""
        estimate = 0
        response = QueryResponse(**base, estimated_tokens=estimate)
        for _ in range(12):
            response = QueryResponse(**base, estimated_tokens=estimate)
            new_estimate = estimated_tokens(response.canonical_payload())
            if new_estimate == estimate:
                return response
            estimate = new_estimate
        return response

    @staticmethod
    def _aggregate_confidence(results: list[ResultNode]) -> str:
        if not results:
            return "0.000000000000"
        total = sum((Decimal(result.confidence) for result in results), Decimal(0))
        return score_text(total / Decimal(len(results)))

    # --------------------------------------------------------------------- query

    def query(self, request: QueryRequest, *, actor: str | None = None) -> QueryResponse:
        started = time.monotonic()
        invocation = self._begin("knowledge.query", request.model_dump(mode="json"), actor, None)
        try:
            response = self._query(request, invocation)
        except KnowledgeError as error:
            error.audit_event_id = invocation.audit_event_id
            self._finish_failure(invocation, started, error)
            raise
        except Exception as error:
            self._finish_failure(invocation, started, error)
            raise
        self._finish_success(
            invocation,
            started,
            {
                "routing": response.routing,
                "results": len(response.results),
                "truncated": response.truncated,
                "estimated_tokens": response.estimated_tokens,
            },
        )
        return response

    def _query(self, request: QueryRequest, invocation: Invocation) -> QueryResponse:
        if request.token_budget < MINIMUM_TOKEN_BUDGET:
            raise BudgetTooSmall(
                f"token_budget {request.token_budget} is below the {MINIMUM_TOKEN_BUDGET}-token minimum"
            )
        if request.token_budget > self._max_token_budget:
            raise BudgetExceedsMax(f"token_budget {request.token_budget} exceeds the configured maximum")
        # The read linearization point (spec 7.5).
        required_watermark = self._store.knowledge_high_watermark()
        snapshot = self._snapshot(required_watermark)

        meta = {
            "audit_event_id": invocation.audit_event_id,
            "projection_version": snapshot.projection_version,
            "required_knowledge_watermark": required_watermark,
            "source_knowledge_high_watermark": snapshot.watermark,
            "stale": False,
        }

        if request.access_only:
            # Ambiguous surface: zero knowledge payload within the 160 cap.
            response = self._finalize_query(
                {
                    **meta,
                    "truncated": False,
                    "routing": "access_only",
                    "candidates": (),
                    "confidence": "0.000000000000",
                    "node_versions": {},
                    "provenance": (),
                    "results": (),
                    "recommendations": RecommendationBlock.empty(),
                    "access_only": True,
                    "knowledge_payload_tokens": 0,
                }
            )
            cap = min(request.token_budget, ACCESS_ONLY_TOKEN_CAP)
            if response.estimated_tokens > cap:
                raise MetadataExceedsBudget("the access-only zero-payload response exceeds its cap")
            return response

        outcome = snapshot.router.route(
            domain=request.domain,
            purpose=request.purpose,
            context_tags=request.context_tags,
        )
        if outcome.status == "missing_root":
            raise PrimaryRootMissing(
                f"primary root {outcome.domain.value if outcome.domain else request.domain!r} is absent from the "
                "active projection; there is no keyword fallback"
            )
        if outcome.status in {"ambiguous", "no_match"}:
            return self._finalize_query(
                {
                    **meta,
                    "truncated": False,
                    "routing": outcome.status,
                    "candidates": tuple(outcome.candidates),
                    "confidence": "0.000000000000",
                    "node_versions": {},
                    "provenance": (),
                    "results": (),
                    "recommendations": RecommendationBlock.empty(),
                    "access_only": False,
                    "knowledge_payload_tokens": 0,
                }
            )

        assert outcome.root_node_id is not None  # guaranteed by direct/ranked
        allowlist = (
            frozenset(relation.value for relation in request.relation_allowlist)
            if request.relation_allowlist is not None
            else None
        )
        report = snapshot.traversal.traverse(
            outcome.root_node_id,
            depth=request.depth,
            mode=request.mode.value,
            relation_allowlist=allowlist,
            max_nodes=request.max_nodes,
            max_edges=request.max_edges,
        )

        consulted = [snapshot.nodes[node_id] for node_id in report.nodes if node_id in snapshot.nodes]
        node_versions = {str(node["id"]): int(node.get("version", 1)) for node in consulted}
        provenance = tuple(
            sorted(
                {
                    _redacted_content(str(item), max_length=_MAX_PROVENANCE_LENGTH)
                    for node in consulted
                    for item in node.get("provenance", [])
                    if isinstance(item, str)
                }
            )
        )[:_MAX_PROVENANCE_ENTRIES]

        # Relevance order comes from the index itself, so the field-rank
        # tie-break survives; unknown ids sort last by node id.
        terms = query_terms(request.purpose)
        for tag in request.context_tags:
            terms.extend(query_terms(tag))
        rank_order = {hit.node_id: position for position, hit in enumerate(snapshot.index.rank(terms))}
        ordered_ids = sorted(
            (node_id for node_id in report.nodes if node_id in snapshot.nodes),
            key=lambda node_id: (rank_order.get(node_id, len(rank_order)), node_id),
        )
        ranked_results = [self._result_node(snapshot.nodes[node_id]) for node_id in ordered_ids]

        results = ranked_results
        truncated = False
        while True:
            block = self._recommendations(results, required_watermark=required_watermark, at=invocation.started_at)
            confidence = self._aggregate_confidence(results)
            response = self._finalize_query(
                {
                    **meta,
                    "truncated": truncated,
                    "routing": outcome.status,
                    "candidates": (),
                    "confidence": confidence,
                    "node_versions": node_versions,
                    "provenance": provenance,
                    "results": tuple(results),
                    "recommendations": block,
                    "access_only": False,
                    "knowledge_payload_tokens": self._knowledge_payload_tokens(
                        results,
                        confidence=confidence,
                        node_versions=node_versions,
                        provenance=provenance,
                        recommendations=block,
                    ),
                }
            )
            if response.estimated_tokens <= request.token_budget:
                return response
            if not results:
                raise MetadataExceedsBudget(
                    f"mandatory metadata alone needs {response.estimated_tokens} tokens "
                    f"over the {request.token_budget}-token budget"
                )
            results = results[:-1]
            truncated = True

    # ------------------------------------------------------------------ get_node

    def get_node(self, node_id: str, *, actor: str | None = None) -> NodeResponse:
        started = time.monotonic()
        invocation = self._begin("knowledge.get_node", {"node_id": node_id}, actor, None)
        try:
            required_watermark = self._store.knowledge_high_watermark()
            snapshot = self._snapshot(required_watermark)
            node = snapshot.nodes.get(node_id)
            if node is None:
                raise NodeNotFound(f"node {node_id!r} is absent from the active projection")
            view = self._node_view(node)
            estimate = 0
            response = NodeResponse(
                audit_event_id=invocation.audit_event_id,
                projection_version=snapshot.projection_version,
                required_knowledge_watermark=required_watermark,
                source_knowledge_high_watermark=snapshot.watermark,
                stale=False,
                node=view,
            )
            for _ in range(12):
                candidate = response.model_copy(update={"estimated_tokens": estimate})
                new_estimate = estimated_tokens(candidate.canonical_payload())
                response = candidate
                if new_estimate == estimate:
                    break
                estimate = new_estimate
        except KnowledgeError as error:
            error.audit_event_id = invocation.audit_event_id
            self._finish_failure(invocation, started, error)
            raise
        except Exception as error:
            self._finish_failure(invocation, started, error)
            raise
        self._finish_success(invocation, started, {"node_id": node_id})
        return response

    # --------------------------------------------------------------- get_subtree

    def get_subtree(
        self,
        node_id: str,
        *,
        depth: int = 2,
        mode: str = "bfs",
        relation_allowlist: frozenset[str] | None = None,
        max_nodes: int = 64,
        max_edges: int = 256,
        token_budget: int = 1024,
        actor: str | None = None,
    ) -> SubtreeResponse:
        started = time.monotonic()
        invocation = self._begin(
            "knowledge.get_subtree",
            {
                "node_id": node_id,
                "depth": depth,
                "mode": mode,
                "relation_allowlist": sorted(relation_allowlist) if relation_allowlist else None,
                "max_nodes": max_nodes,
                "max_edges": max_edges,
                "token_budget": token_budget,
            },
            actor,
            None,
        )
        try:
            if token_budget < MINIMUM_TOKEN_BUDGET:
                raise BudgetTooSmall(f"token_budget {token_budget} is below the {MINIMUM_TOKEN_BUDGET}-token minimum")
            required_watermark = self._store.knowledge_high_watermark()
            snapshot = self._snapshot(required_watermark)
            if node_id not in snapshot.nodes:
                raise NodeNotFound(f"node {node_id!r} is absent from the active projection")
            report = snapshot.traversal.traverse(
                node_id,
                depth=depth,
                mode=mode,
                relation_allowlist=relation_allowlist,
                max_nodes=max_nodes,
                max_edges=max_edges,
            )
            nodes = [self._result_node(snapshot.nodes[nid]) for nid in report.nodes if nid in snapshot.nodes]
            edges = tuple(
                SubtreeEdge(source=link.source, target=link.target, relation=link.relation) for link in report.edges
            )
            cross = tuple(
                SubtreeEdge(source=link.source, target=link.target, relation=link.relation)
                for link in report.cross_edges
            )
            meta = {
                "audit_event_id": invocation.audit_event_id,
                "projection_version": snapshot.projection_version,
                "required_knowledge_watermark": required_watermark,
                "source_knowledge_high_watermark": snapshot.watermark,
                "stale": False,
            }
            truncated = False
            while True:
                response = SubtreeResponse(
                    **meta,
                    node_id=node_id,
                    nodes=tuple(nodes),
                    edges=edges,
                    cross_edges=cross,
                    guards_hit=report.guards_hit,
                    truncated=truncated,
                    knowledge_payload_tokens=estimated_tokens_of_payload(
                        {"nodes": [node.model_dump(mode="json") for node in nodes]}
                    )
                    if nodes
                    else 0,
                )
                estimate = 0
                for _ in range(12):
                    candidate = response.model_copy(update={"estimated_tokens": estimate})
                    new_estimate = estimated_tokens(candidate.canonical_payload())
                    response = candidate
                    if new_estimate == estimate:
                        break
                    estimate = new_estimate
                if response.estimated_tokens <= token_budget:
                    break
                if not nodes:
                    raise MetadataExceedsBudget("mandatory metadata exceeds the subtree budget")
                nodes = nodes[:-1]
                truncated = True
        except KnowledgeError as error:
            error.audit_event_id = invocation.audit_event_id
            self._finish_failure(invocation, started, error)
            raise
        except Exception as error:
            self._finish_failure(invocation, started, error)
            raise
        self._finish_success(
            invocation, started, {"node_id": node_id, "nodes": len(response.nodes), "truncated": response.truncated}
        )
        return response

    # ----------------------------------------------------------------- mutations

    def log_event(
        self,
        node: Node,
        *,
        actor: str | None = None,
        idempotency_key: str | None = None,
        expected_version: int | None = None,
    ) -> WriteReceipt:
        """Record structured content as a node upsert through ``store.commit``."""
        event = Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
            actor=actor or self._default_actor,
        )
        invocation = Invocation.create(
            "knowledge.log_event",
            request_hash=self._request_hash(event.model_dump(mode="json")),
            actor=actor or self._default_actor,
            idempotency_key=idempotency_key,
        )
        return self._store.commit(event, invocation=invocation, expected_version=expected_version)

    def record_outcome(
        self,
        evidence: Evidence,
        *,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> WriteReceipt:
        """Append one outcome evidence item through ``store.commit``."""
        event = Event(
            kind=EventKind.EVIDENCE_APPEND,
            node_id=evidence.node_id,
            domain=self._evidence_domain(evidence),
            evidence=evidence,
            source_version_id=evidence.source_version_id,
            actor=actor or self._default_actor,
        )
        invocation = Invocation.create(
            "knowledge.record_outcome",
            request_hash=self._request_hash(event.model_dump(mode="json")),
            actor=actor or self._default_actor,
            idempotency_key=idempotency_key,
        )
        return self._store.commit(event, invocation=invocation)

    def _evidence_domain(self, evidence: Evidence):
        node = self._store.current_node(evidence.node_id)
        if node is None:
            raise NodeNotFound(f"evidence target {evidence.node_id!r} does not exist")
        return node.domain

    # ---------------------------------------------------------------- lifecycle

    def _meta_after(self, audit_event_id: str) -> dict[str, Any]:
        """Audit/projection metadata for a lifecycle response.

        A lifecycle call can move the knowledge watermark, so the metadata
        names the generation that actually covers the change: the stale-read
        repair path runs once. If even that repair cannot produce a readable
        generation the response is marked ``stale=True`` instead of failing a
        write that already committed.
        """
        required = self._store.knowledge_high_watermark()
        try:
            snapshot = self._snapshot(required)
        except StaleProjectionUnavailable:
            return {
                "audit_event_id": audit_event_id,
                "projection_version": self._store.projection_version(),
                "required_knowledge_watermark": required,
                "source_knowledge_high_watermark": required,
                "stale": True,
            }
        return {
            "audit_event_id": audit_event_id,
            "projection_version": snapshot.projection_version,
            "required_knowledge_watermark": required,
            "source_knowledge_high_watermark": snapshot.watermark,
            "stale": False,
        }

    @staticmethod
    def _map_lifecycle_error(error: BaseException) -> KnowledgeError:
        """Map the Task 5/6 store errors onto the knowledge error taxonomy."""
        if isinstance(error, KnowledgeError):
            return error
        if isinstance(error, UnknownNodeError):
            return NodeNotFound(str(error))
        if isinstance(error, (ReviewUnknownNode, ImprovementUnknownNode)):
            return NodeNotFound(str(error))
        if isinstance(error, ProposalValidationError):
            return InvalidProposal(str(error))
        if isinstance(error, ApprovalRequired):
            return ApprovalRequiredError(str(error))
        if isinstance(error, RegressionGateFailure):
            return RegressionGateError(str(error))
        if isinstance(error, UnknownImprovement):
            return ImprovementNotFound(str(error))
        if isinstance(error, UnknownRelearn):
            return UnknownRelearnError(str(error))
        if isinstance(error, OperatorDecisionRequired):
            return OperatorDecisionRequiredError(str(error))
        if isinstance(error, MixedSourceVersions):
            return MixedSourceVersionsError(str(error))
        if isinstance(error, BackupUnavailable):
            return LifecycleError(f"backup unavailable: {error}")
        return LifecycleError(f"{type(error).__name__}: {error}")

    def _audit_lifecycle_failure(
        self,
        operation: str,
        payload: Any,
        actor: str | None,
        error: BaseException,
        started: float,
    ) -> KnowledgeError:
        """Close a failed lifecycle invocation with exactly one final audit."""
        typed = self._map_lifecycle_error(error)
        invocation = self._begin(operation, payload, actor, None)
        typed.audit_event_id = invocation.audit_event_id
        self._finish_failure(invocation, started, typed)
        return typed

    def _result_node_from_node(self, node: Node) -> ResultNode:
        """A contract result row built from a domain node (review responses)."""
        return ResultNode(
            node_id=node.node_id,
            summary=_redacted_content(node.summary or node.node_id, max_length=_MAX_SUMMARY_LENGTH),
            domain=node.domain.value,
            kind=node.kind.value,
            version=node.version,
            confidence=score_text(node.scores.confidence),
            p=score_text(node.scores.p),
        )

    def _assessment_event(self, node_id: str, assessment: Any, *, at: datetime) -> tuple[Event | None, int | None]:
        """The node revision that materializes the assessment, or ``None``.

        ``evaluate`` is one of the three score-moving tools (relearn contract):
        the frozen P/M/E vector is written as a new immutable revision. An
        unchanged vector writes nothing, so repeated evaluations cannot inflate
        the revision history.
        """
        node = self._store.current_node(node_id)
        if node is None:
            raise UnknownNodeError(f"unknown node {node_id!r}")
        current = node.scores
        updated = current.model_copy(
            update={
                "p": assessment.p,
                "m": assessment.m,
                "e": assessment.e,
                "uncertainty": assessment.overall_uncertainty,
                "confidence": Decimal(1) - assessment.overall_uncertainty,
                "professional_level": assessment.professional_level,
                "selected_m_bin": assessment.selected_m_bin,
            }
        )
        unchanged = (
            quantize_score(updated.p),
            quantize_score(updated.m),
            quantize_score(updated.e),
            quantize_score(updated.uncertainty),
            quantize_score(updated.confidence),
            updated.professional_level,
            updated.selected_m_bin,
        ) == (
            current.p,
            current.m,
            current.e,
            current.uncertainty,
            current.confidence,
            current.professional_level,
            current.selected_m_bin,
        )
        if unchanged:
            return None, None
        event = Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node_id,
            domain=node.domain,
            node=node.model_copy(update={"scores": updated, "updated_at": at}),
            source_version_id=node.source_version_id,
            occurred_at=at,
            payload={"evaluate": assessment.to_contract_dict()},
        )
        return event, node.version

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, request: EvaluateRequest, *, actor: str | None = None) -> EvaluateResponse:
        """Score one node (or a primary plus descendants) and materialize P/M/E.

        Full precision is used for every comparison; only the response carries
        the 12-decimal serialization. Exactly one final audit event closes the
        invocation: the scoring write owns it when scores changed, otherwise
        the read-only audit does.
        """
        started = time.monotonic()
        payload = request.model_dump(mode="json")
        try:
            at = request.at or datetime.now(UTC)
            engine = self._score_engine
            assessment = (
                engine.aggregate_primary(request.node_id, context=request.context_tags, at=at)
                if request.include_descendants
                else engine.score_node(request.node_id, context=request.context_tags, at=at)
            )
            event, expected_version = self._assessment_event(request.node_id, assessment, at=at)
        except (UnknownNodeError, StoreError) as error:
            typed = self._audit_lifecycle_failure("knowledge.evaluate", payload, actor, error, started)
            raise typed from error

        invocation = Invocation.create(
            "knowledge.evaluate",
            request_hash=self._request_hash(payload),
            actor=actor or self._default_actor,
        )
        if event is None:
            self._audit.begin(invocation)
            audit_event_id = invocation.audit_event_id
            self._finish_success(
                invocation,
                started,
                {"node_id": request.node_id, "persisted": False, "level": assessment.professional_level},
            )
        else:
            receipt = self._store.commit(event, invocation=invocation, expected_version=expected_version)
            audit_event_id = receipt.audit_event_id

        data = assessment.to_contract_dict()
        return EvaluateResponse(
            **self._meta_after(audit_event_id),
            node_id=request.node_id,
            p=str(data["p"]),
            m=str(data["m"]),
            e=str(data["e"]),
            u_p=str(data["u_p"]),
            u_m=str(data["u_m"]),
            u_e=str(data["u_e"]),
            overall_uncertainty=str(data["overall_uncertainty"]),
            professional_level=int(data["professional_level"]),
            selected_m_bin=data["selected_m_bin"],
            m_bin_evidence_count=data["m_bin_evidence_count"],
            promotable=bool(data["promotable"]),
            g=str(data["g"]),
            explanations=dict(assessment.explanations),
        )

    # -------------------------------------------------------------------- review

    def review(
        self,
        request: ReviewRequest | str,
        *,
        at: datetime | None = None,
        include_due: bool = False,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> ReviewResponse:
        """Run the deterministic review policy and return its recommendation block.

        The canonical argument is a validated :class:`ReviewRequest`; an adapter
        may instead pass the node id plus keyword arguments, which is exactly
        the tool's argument surface.
        """
        if not isinstance(request, ReviewRequest):
            request = ReviewRequest(node_id=request, at=at, include_due=include_due)
        started = time.monotonic()
        payload = request.model_dump(mode="json")
        try:
            report = self._review_engine.review(
                request.node_id,
                at=request.at,
                actor=actor or self._default_actor,
                idempotency_key=idempotency_key,
            )
        except StoreError as error:
            typed = self._audit_lifecycle_failure("knowledge.review", payload, actor, error, started)
            raise typed from error

        try:
            at = request.at or datetime.now(UTC)
            node = self._store.current_node(request.node_id)
            if node is None:  # pragma: no cover - the engine just read it
                raise NodeNotFound(f"node {request.node_id!r} disappeared during review")
            required = self._store.knowledge_high_watermark()
            block = self._recommendations([self._result_node_from_node(node)], required_watermark=required, at=at)
            due = tuple(self._scheduler.scan_due(at=at)) if request.include_due else ()
            decision = report.decision
            response = ReviewResponse(
                **self._meta_after(report.audit_event_id),
                node_id=report.node_id,
                decision=ReviewDecisionView(
                    current_status=decision.current_status.value,
                    desired_status=decision.desired_status.value,
                    target_status=decision.target_status.value,
                    transitioned=decision.transitioned,
                    qualifying_for_promotion=decision.qualifying_for_promotion,
                    reasons=decision.reasons,
                    blocked_by=decision.blocked_by,
                    policy_version=decision.policy_version,
                ),
                persisted=report.persisted,
                from_version=report.from_version,
                to_version=report.to_version,
                relearn_pending=report.relearn_pending,
                due=tuple(
                    DueReviewView(
                        node_id=candidate.node_id,
                        version=candidate.version,
                        domain=candidate.domain.value,
                        reasons=candidate.reasons,
                        last_review_at=candidate.last_review_at,
                        target_status=(
                            candidate.decision.target_status.value if candidate.decision is not None else None
                        ),
                    )
                    for candidate in due
                ),
                recommendations=block,
            )
        except StoreError as error:
            typed = self._map_lifecycle_error(error)
            typed.audit_event_id = report.audit_event_id
            raise typed from error
        return response

    # ------------------------------------------------------ propose / apply / rollback

    def propose_improvement(
        self,
        request: ProposeImprovementRequest | None = None,
        *,
        node_id: str | None = None,
        diff: tuple[Any, ...] | list[Any] | None = None,
        rationale: str | None = None,
        base_version: int | None = None,
        at: datetime | None = None,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> ProposalResponse:
        """Persist one structured diff proposal (zero knowledge weight).

        Accepts a validated :class:`ProposeImprovementRequest` or the same
        fields as keyword arguments (CLI/API/MCP adapters).
        """
        if not isinstance(request, ProposeImprovementRequest):
            if node_id is None or diff is None or rationale is None:
                raise InvalidProposal("propose_improvement requires node_id, diff and rationale")
            request = ProposeImprovementRequest(
                node_id=node_id,
                diff=tuple(diff),
                rationale=rationale,
                base_version=base_version,
                at=at,
            )
        started = time.monotonic()
        payload = request.model_dump(mode="json")
        try:
            record = propose(
                self._store,
                node_id=request.node_id,
                diff=request.diff,
                rationale=request.rationale,
                base_version=request.base_version,
                actor=actor or self._default_actor,
                at=request.at,
                idempotency_key=idempotency_key,
            )
        except StoreError as error:
            typed = self._audit_lifecycle_failure("knowledge.propose_improvement", payload, actor, error, started)
            raise typed from error
        try:
            meta = self._meta_after(record.audit_event_id)
        except StoreError as error:  # pragma: no cover - projection repair failure
            typed = self._map_lifecycle_error(error)
            typed.audit_event_id = record.audit_event_id
            raise typed from error
        return ProposalResponse(
            **meta,
            proposal=record.proposal,
            domain_event_id=record.domain_event_id,
            audit_event_seq=record.audit_event_seq,
            node_version=record.node_version,
            knowledge_seq=record.knowledge_seq,
        )

    def apply_improvement(
        self,
        request: ApplyImprovementRequest | str | None = None,
        *,
        proposal_id: str | None = None,
        approval: Any = None,
        at: datetime | None = None,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> ImprovementResponse:
        """Apply a proposal: approval and regression gate run before any write.

        Accepts a validated :class:`ApplyImprovementRequest`, a bare proposal id,
        or the tool's keyword arguments.
        """
        if not isinstance(request, ApplyImprovementRequest):
            resolved = proposal_id if proposal_id is not None else request
            if resolved is None:
                raise InvalidProposal("apply_improvement requires a proposal_id")
            request = ApplyImprovementRequest(proposal_id=resolved, approval=approval, at=at)
        started = time.monotonic()
        payload = request.model_dump(mode="json")
        try:
            proposal = get_proposal(self._store, request.proposal_id)
            if proposal is None:
                raise UnknownImprovement(f"no proposal {request.proposal_id!r} in the event log")
            receipt = apply_improvement(
                self._store,
                proposal,
                approval=request.approval,
                at=request.at,
                actor=actor or self._default_actor,
                idempotency_key=idempotency_key,
            )
        except StoreError as error:
            typed = self._audit_lifecycle_failure("knowledge.apply_improvement", payload, actor, error, started)
            raise typed from error
        try:
            meta = self._meta_after(receipt.audit_event_id)
        except StoreError as error:  # pragma: no cover - projection repair failure
            typed = self._map_lifecycle_error(error)
            typed.audit_event_id = receipt.audit_event_id
            raise typed from error
        return ImprovementResponse(
            **meta,
            proposal_id=receipt.proposal_id,
            node_id=receipt.node_id,
            from_version=receipt.from_version,
            to_version=receipt.to_version,
            status=receipt.status,
            content_sha256=receipt.content_sha256,
            idempotent_replay=receipt.idempotent_replay,
            domain_event_id=receipt.domain_event_id,
            audit_event_seq=receipt.audit_event_seq,
            node_version=receipt.node_version,
            knowledge_seq=receipt.knowledge_seq,
        )

    def rollback(
        self,
        request: RollbackRequest | None = None,
        *,
        node_id: str | None = None,
        to_version: int | None = None,
        relearn_id: str | None = None,
        at: datetime | None = None,
        actor: str | None = None,
        idempotency_key: str | None = None,
    ) -> RollbackResponse:
        """Roll back one improvement revision *or* one relearn apply.

        Accepts a validated :class:`RollbackRequest` or the tool's keyword
        arguments; exactly one rollback target must be supplied.
        """
        if not isinstance(request, RollbackRequest):
            try:
                request = RollbackRequest(
                    node_id=node_id,
                    to_version=to_version,
                    relearn_id=relearn_id,
                    at=at,
                )
            except ValidationError as error:
                raise InvalidProposal(f"rollback request is invalid: {error}") from error
        started = time.monotonic()
        payload = request.model_dump(mode="json")
        operation = "knowledge.rollback"
        try:
            if request.relearn_id is not None:
                relearn_receipt = self._relearn_engine.rollback(request.relearn_id, actor=actor or self._default_actor)
                try:
                    meta = self._meta_after(relearn_receipt.audit_event_id)
                except StoreError as error:  # pragma: no cover - projection repair failure
                    typed = self._map_lifecycle_error(error)
                    typed.audit_event_id = relearn_receipt.audit_event_id
                    raise typed from error
                return RollbackResponse(
                    **meta,
                    kind="relearn",
                    relearn_id=relearn_receipt.relearn_id,
                    status="restored",
                    restored_db_sha256=relearn_receipt.restored_db_sha256,
                    verified=relearn_receipt.verified,
                )
            assert request.node_id is not None  # RollbackRequest guarantees one target
            receipt = rollback_improvement(
                self._store,
                node_id=request.node_id,
                to_version=request.to_version,
                actor=actor or self._default_actor,
                at=request.at,
                idempotency_key=idempotency_key,
            )
        except StoreError as error:
            typed = self._audit_lifecycle_failure(operation, payload, actor, error, started)
            raise typed from error
        try:
            meta = self._meta_after(receipt.audit_event_id)
        except StoreError as error:  # pragma: no cover - projection repair failure
            typed = self._map_lifecycle_error(error)
            typed.audit_event_id = receipt.audit_event_id
            raise typed from error
        return RollbackResponse(
            **meta,
            kind="improvement",
            node_id=receipt.node_id,
            from_version=receipt.from_version,
            to_version=receipt.to_version,
            status=receipt.status,
            content_sha256=receipt.content_sha256,
            idempotent_replay=receipt.idempotent_replay,
            domain_event_id=receipt.domain_event_id,
            audit_event_seq=receipt.audit_event_seq,
            node_version=receipt.node_version,
            knowledge_seq=receipt.knowledge_seq,
        )
