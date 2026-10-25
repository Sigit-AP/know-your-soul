"""Adapter boundary between the product and the pinned Graphify library.

Task 3 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 13.5 and the mandatory adapters A1/A3 of the feature matrix.

Only deterministic, local Graphify primitives are imported here
(``graphify.build``, ``graphify.validate``, ``graphify.semantic_cleanup``,
``graphify.cluster``, ``graphify.analyze``, ``graphify.affected``,
``graphify.multigraph_compat``, ``graphify.ids``). The forbidden roots
(``graphify.llm``, ``graphify.serve``, ``graphify.prs``, ``graphify.ingest``,
``graphify.transcribe``, ``graphify.mcp_ingest``, ``graphify.exporters``) are
never touched; ``tests/test_no_ai_runtime.py`` statically enforces that.

Every fragment — generated locally or supplied by a caller — passes Graphify's
own ``validate_semantic_fragment`` and ``sanitize_semantic_fragment`` and then
``validate_extraction`` before it may become an :class:`ExtractionFragment`.
"""

from __future__ import annotations

import copy
import importlib.util
import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import networkx as nx
from graphify.affected import load_graph as _graphify_load_graph
from graphify.analyze import god_nodes as _graphify_god_nodes
from graphify.build import build_from_json
from graphify.cluster import cluster as _graphify_cluster
from graphify.cluster import community_member_sigs, score_all
from graphify.multigraph_compat import MultigraphCapabilityResult, probe_multigraph_capabilities
from graphify.semantic_cleanup import sanitize_semantic_fragment, validate_semantic_fragment
from graphify.validate import validate_extraction
from pydantic import BaseModel, ConfigDict, Field

from soul_knowledge.domain import RelationKind, canonical_json
from soul_knowledge.fragment_generator import FRAGMENT_GENERATOR_VERSION, FRAGMENT_PROMPT_FINGERPRINT, FragmentGenerator

if TYPE_CHECKING:
    from soul_knowledge.projection import FragmentCache

__all__ = [
    "ACCEPTED_SCHEMA_VERSIONS",
    "ADAPTER_VERSION",
    "CONFIDENCE_BY_RELATION",
    "CONFIDENCE_SCORE",
    "GRAPHIFY_PARTITIONER",
    "PINNED_HUB_POLICY",
    "ExtractionFragment",
    "FragmentValidationError",
    "GraphifyAdapter",
]

ADAPTER_VERSION = "1.0.0"

# graphify.export.GRAPH_SCHEMA_VERSION at the pinned 0.9.95; the read-model
# validator fails closed on anything outside this set (matrix row 22).
ACCEPTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

CONFIDENCE_BY_RELATION: dict[RelationKind, str] = {
    RelationKind.PARENT_OF: "EXTRACTED",
    RelationKind.DERIVED_FROM: "EXTRACTED",
    RelationKind.LEARNED_FROM: "INFERRED",
    RelationKind.APPLIED_IN: "INFERRED",
    RelationKind.SUPPORTS: "INFERRED",
    RelationKind.CAUSES: "INFERRED",
    RelationKind.DEPENDS_ON: "INFERRED",
    RelationKind.CONTRADICTS: "AMBIGUOUS",
    RelationKind.DUPLICATES: "AMBIGUOUS",
    RelationKind.SUPERSEDES: "AMBIGUOUS",
}
"""Deterministic provenance/confidence mapping for the typed relation allowlist."""

CONFIDENCE_SCORE: dict[str, float] = {"EXTRACTED": 1.0, "INFERRED": 0.55, "AMBIGUOUS": 0.2}
"""Matches ``graphify.export._CONFIDENCE_SCORE_DEFAULTS`` at the pin."""

PINNED_HUB_POLICY: dict[str, Any] = {"exclude_hubs_percentile": 99, "ambiguous_scale": 0.5}
"""The one documented hub/ambiguity policy per generation (matrix row 30)."""


def _detect_partitioner() -> dict[str, Any]:
    """Pin the partitioner for this environment (matrix row 26).

    ``graspologic`` extras stay out of the locked environment, so the pinned
    partitioner is NetworkX Louvain with Graphify's fixed seed. If a future
    environment review ever admits graspologic, this record changes and every
    generation manifest records the switch.
    """
    if importlib.util.find_spec("graspologic") is not None:  # pragma: no cover - locked out
        return {"name": "graspologic-leiden", "seed": 42, "resolution": 1.0}
    return {"name": "networkx-louvain", "seed": 42, "resolution": 1.0}


GRAPHIFY_PARTITIONER: dict[str, Any] = _detect_partitioner()


class FragmentValidationError(ValueError):
    """A fragment failed Graphify's validate/sanitize boundary."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("invalid extraction fragment: " + "; ".join(errors))
        self.errors = tuple(errors)


class ExtractionFragment(BaseModel):
    """A validated, sanitized Graphify-schema fragment with provenance."""

    model_config = ConfigDict(frozen=True)

    source_file: str
    partial: bool = False
    fingerprint: str = FRAGMENT_PROMPT_FINGERPRINT
    generator_version: str = FRAGMENT_GENERATOR_VERSION
    nodes: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    edges: tuple[dict[str, Any], ...] = Field(default_factory=tuple)
    hyperedges: tuple[dict[str, Any], ...] = Field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [dict(node) for node in self.nodes],
            "edges": [dict(edge) for edge in self.edges],
            "hyperedges": [dict(hyperedge) for hyperedge in self.hyperedges],
        }


class GraphifyAdapter:
    """The only door to Graphify primitives the product uses."""

    def __init__(self, *, generator: FragmentGenerator | None = None, cache: FragmentCache | None = None) -> None:
        self._generator = generator if generator is not None else FragmentGenerator()
        self._generator_version = generator.generator_version if generator is not None else FRAGMENT_GENERATOR_VERSION
        self._cache = cache

    # ------------------------------------------------------------ fragments

    def generate_fragment(
        self,
        nodes: Iterable[Mapping[str, Any]],
        edges: Iterable[Mapping[str, Any]],
        *,
        source_file: str = "",
        hyperedges: Iterable[Mapping[str, Any]] = (),
        partial: bool = False,
    ) -> ExtractionFragment:
        """Validate and sanitize caller-supplied nodes/edges into a fragment.

        This is the deterministic replacement for model-backed semantic
        extraction: Graphify's ``validate_semantic_fragment`` bounds ids and
        sizes, ``sanitize_semantic_fragment`` strips rationale/concept-tier
        nodes and their edges, and ``validate_extraction`` enforces the
        canonical schema. Dangling references edges (unresolved wikilinks) are
        tolerated exactly the way ``graphify.build`` tolerates them; the
        importer reports and resolves them against the store.
        """
        raw: dict[str, Any] = {
            "nodes": [dict(node) for node in nodes],
            "edges": [dict(edge) for edge in edges],
            "hyperedges": [dict(hyperedge) for hyperedge in hyperedges],
        }
        semantic_errors = validate_semantic_fragment(raw)
        if semantic_errors:
            raise FragmentValidationError(semantic_errors)
        cleaned = sanitize_semantic_fragment(raw)
        schema_errors = [error for error in validate_extraction(cleaned) if "does not match any node id" not in error]
        if schema_errors:
            raise FragmentValidationError(schema_errors)
        return ExtractionFragment(
            source_file=source_file,
            partial=partial,
            nodes=tuple(dict(node) for node in cleaned.get("nodes", [])),
            edges=tuple(dict(edge) for edge in cleaned.get("edges", [])),
            hyperedges=tuple(dict(hyperedge) for hyperedge in cleaned.get("hyperedges", []) or []),
        )

    def fragment_from_markdown(self, *, source_file: str, content: bytes) -> ExtractionFragment:
        """Generate a fragment from markdown bytes through the local generator.

        The content-hash cache is consulted only for complete sources; a
        truncated (``partial``) source is re-derived every time and never
        poisons the cache (matrix row 7). Cache keys are content + relative
        path, never stat data (matrix row 4).
        """
        key: str | None = None
        if self._cache is not None:
            key = self._cache.key_for(source_file, content)
            cached = self._cache.get(key)
            if cached is not None:
                return ExtractionFragment.model_validate(cached)
        generated = self._generator.generate(source_file=source_file, content=content)
        fragment = self.generate_fragment(
            generated.nodes,
            generated.edges,
            source_file=source_file,
            partial=generated.partial,
        )
        if self._cache is not None and key is not None and not fragment.partial:
            self._cache.put(key, fragment.model_dump(mode="json"))
        return fragment

    def merge_fragments(self, fragments: Iterable[ExtractionFragment]) -> dict[str, Any]:
        """Concatenate per-file fragments into one extraction dict."""
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        hyperedges: list[dict[str, Any]] = []
        for fragment in fragments:
            nodes.extend(dict(node) for node in fragment.nodes)
            edges.extend(dict(edge) for edge in fragment.edges)
            hyperedges.extend(dict(hyperedge) for hyperedge in fragment.hyperedges)
        return {"nodes": nodes, "edges": edges, "hyperedges": hyperedges}

    def replace_file_tier(self, extraction: Mapping[str, Any], fragment: ExtractionFragment) -> dict[str, Any]:
        """Replace exactly one file's tier inside a merged extraction.

        Explicit per-file provenance means re-extracting one source can never
        wipe another file's nodes (matrix row 18): only nodes whose
        ``source_file`` matches the new fragment are dropped, and edges are
        kept only while both endpoints survive.
        """
        replaced = fragment.source_file
        surviving_ids = {
            str(node["id"])
            for node in extraction.get("nodes", [])
            if isinstance(node, Mapping) and node.get("source_file") != replaced
        }
        nodes = [
            dict(node)
            for node in extraction.get("nodes", [])
            if isinstance(node, Mapping) and str(node.get("id")) in surviving_ids
        ]
        nodes.extend(dict(node) for node in fragment.nodes)
        surviving_ids |= {node["id"] for node in fragment.nodes}
        edges = [
            dict(edge)
            for edge in extraction.get("edges", [])
            if isinstance(edge, Mapping) and edge.get("source") in surviving_ids and edge.get("target") in surviving_ids
        ]
        edges.extend(dict(edge) for edge in fragment.edges)
        return {"nodes": nodes, "edges": edges, "hyperedges": list(extraction.get("hyperedges", []))}

    # ---------------------------------------------------------------- graph

    @property
    def generator_version(self) -> str:
        """The fragment-generator build this adapter runs (cache namespace pin)."""
        return self._generator_version

    @property
    def fragment_fingerprint(self) -> str:
        """The stable synthetic prompt fingerprint carried by every fragment."""
        return FRAGMENT_PROMPT_FINGERPRINT

    def validate_extraction_strict(self, extraction: Mapping[str, Any]) -> list[str]:
        """Fail-closed validation of an assembled extraction (build gate).

        Combines Graphify's semantic-fragment bounds (id charset, sizes) with
        the extraction schema validator. Unlike ``generate_fragment``, no
        dangling-endpoint tolerance applies here: the event store enforces
        relation integrity, so a dangling endpoint in an assembled extraction
        is a defect, not an unresolved wikilink.
        """
        raw = {
            "nodes": [dict(node) for node in extraction.get("nodes", [])],
            "edges": [dict(edge) for edge in extraction.get("edges", [])],
            "hyperedges": [dict(hyperedge) for hyperedge in extraction.get("hyperedges", [])],
        }
        errors = validate_semantic_fragment(raw)
        errors.extend(validate_extraction(raw))
        return errors

    def build_graph(self, extraction: Mapping[str, Any]) -> nx.DiGraph:
        """Assemble the NetworkX graph through Graphify's builder.

        ``directed=True`` preserves relation direction; ``dedup=False`` keeps
        the shrink guard meaningful and never coalesces distinct ids
        (matrix rows 19-20). Node/edge lists are sorted first so the build is
        canonical regardless of insertion order (matrix row 27).
        """
        prepared = copy.deepcopy(dict(extraction))
        prepared["nodes"] = sorted(prepared.get("nodes", []), key=canonical_json)
        prepared["edges"] = sorted(prepared.get("edges", []), key=canonical_json)
        graph = build_from_json(prepared, directed=True, dedup=False)
        if not isinstance(graph, nx.DiGraph):  # pragma: no cover - build contract
            raise TypeError(f"expected a directed graph, got {type(graph).__name__}")
        return graph

    def partition(self, graph: nx.Graph) -> dict[int, list[str]]:
        """Community detection with the pinned partitioner and hub policy.

        ``GRAPHIFY_AMBIGUOUS_SCALE`` is removed from the environment for the
        duration of the call so the recorded policy — not ambient state —
        decides (spec section 13.5: one pinned partitioner per generation).
        """
        saved = os.environ.pop("GRAPHIFY_AMBIGUOUS_SCALE", None)
        try:
            return _graphify_cluster(
                graph,
                resolution=float(GRAPHIFY_PARTITIONER["resolution"]),
                exclude_hubs_percentile=int(PINNED_HUB_POLICY["exclude_hubs_percentile"]),
                ambiguous_scale=float(PINNED_HUB_POLICY["ambiguous_scale"]),
            )
        finally:
            if saved is not None:
                os.environ["GRAPHIFY_AMBIGUOUS_SCALE"] = saved

    def community_signatures(self, communities: Mapping[int, list[str]]) -> dict[str, list[str]]:
        """Published membership signatures; internal community ids stay inside."""
        sigs = community_member_sigs(dict(communities))
        return {sig: sorted(str(member) for member in communities[cid]) for cid, sig in sigs.items()}

    def cohesion_by_signature(self, graph: nx.Graph, communities: Mapping[int, list[str]]) -> dict[str, float]:
        """Cohesion keyed by membership signature (structural diagnostic only)."""
        sigs = community_member_sigs(dict(communities))
        return {sigs[cid]: score for cid, score in score_all(graph, dict(communities)).items()}

    def god_nodes(self, graph: nx.Graph, *, top_n: int = 10) -> list[dict[str, Any]]:
        """Most-connected real entities (Graphify ``analyze.god_nodes``)."""
        return _graphify_god_nodes(graph, top_n=top_n)

    def load_graph(self, path: str | Path) -> nx.DiGraph:
        """Load ``graph.json`` through the pinned Graphify loader.

        The loader forces ``directed=True`` and normalizes the links key, so
        arc direction and confidence backfill survive the round trip
        (matrix row 34). The CLI is never invoked.
        """
        graph = _graphify_load_graph(Path(path))
        if not graph.is_directed():  # pragma: no cover - loader contract
            raise TypeError("the pinned loader must restore arc direction")
        return graph  # type: ignore[return-value]

    def multigraph_probe(self) -> MultigraphCapabilityResult:
        """Behavior-based NetworkX multigraph capability probe (matrix row 39).

        The knowledge graph is an explicit simple-graph decision; the probe is
        run, never version-guessed, so a runtime that lost keyed-edge semantics
        is detected rather than assumed.
        """
        return probe_multigraph_capabilities()
