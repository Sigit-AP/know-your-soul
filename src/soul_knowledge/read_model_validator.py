"""Product read-model validator: fail closed on a published generation.

Task 3 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 13.5 and matrix adapter A4 (rows 15, 21, 22, 33, 34, 39).

Graphify's own validators check *extraction* payloads and tolerate unknown
top-level keys, dangling endpoints and unversioned schemas. The product read
model cannot: a published generation is validated as a whole — manifest hash
agreement, unknown schema versions fail closed, unknown top-level keys fail,
endpoints must resolve, file types and confidences stay inside the Graphify
vocabularies, junk nodes (blank labels, path-shaped ids, unsanitized
rationale/concept tiers, missing provenance) fail, no absolute checkout path
or secret-shaped string may appear in any artifact, and the graph must load
through the pinned Graphify loader with direction and confidence restored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from graphify.validate import VALID_CONFIDENCES, VALID_FILE_TYPES, validate_extraction
from pydantic import BaseModel, ConfigDict, Field

from soul_knowledge.domain import canonical_json
from soul_knowledge.graphify_adapter import ACCEPTED_SCHEMA_VERSIONS, GraphifyAdapter

__all__ = [
    "HASHED_ARTIFACTS",
    "REQUIRED_MANIFEST_KEYS",
    "CollapsedLink",
    "ReadModelValidator",
    "ValidationReport",
]

HASHED_ARTIFACTS = ("graph.json", "GRAPH_REPORT.md", "analysis.json", "labels.json", "signatures.json", "graph.html")

REQUIRED_MANIFEST_KEYS = frozenset(
    {
        "manifest_version",
        "projection_version",
        "content_hash",
        "source_knowledge_high_watermark",
        "schema_version",
        "partitioner",
        "artifacts",
        "graph_content_hash",
        "node_count",
        "edge_count",
        "multigraph",
    }
)

_ALLOWED_GRAPH_KEYS = frozenset({"directed", "multigraph", "graph", "nodes", "links", "hyperedges"})
_REQUIRED_GRAPH_KEYS = frozenset({"directed", "multigraph", "graph", "nodes", "links"})

_ID_RE = re.compile(r"^[\w.:-]+$", re.UNICODE)
# An absolute POSIX path of 3+ segments that is not part of a URI (the char
# before the leading slash must not be a scheme/authority character).
_ABS_PATH_RE = re.compile(r"(?<![\w@:+/.-])/(?:[\w@.+-]+/){2,}[\w@.+-]+")
_DRIVE_RE = re.compile(r"(?<![\w@:+/.-])[A-Za-z]:[\\/]")
_SECRET_RES = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[bap]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|secret|password)\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}"),
)


class CollapsedLink(BaseModel):
    """One node pair whose parallel typed links collapse to a single edge.

    ``graph.json`` preserves distinct typed relations on the same node pair
    (the explicit simple-graph decision, matrix row 39), but a NetworkX
    simple-graph load keeps only one edge per pair. The collapse is loud:
    every affected pair is named here with its relation kinds so consumers
    (Task 4 retrieval) read the links list instead of assuming 1:1 pair→edge.
    """

    model_config = ConfigDict(frozen=True)

    source: str
    target: str
    relations: tuple[str, ...]


class ValidationReport(BaseModel):
    """The fail-closed verdict for one generation directory."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    errors: tuple[str, ...] = Field(default_factory=tuple)
    node_count: int = 0
    edge_count: int = 0
    collapsed_links: tuple[CollapsedLink, ...] = Field(default_factory=tuple)


class ReadModelValidator:
    """Validates a published generation directory against its manifest."""

    def __init__(
        self,
        *,
        accepted_schema_versions: frozenset[int] = ACCEPTED_SCHEMA_VERSIONS,
        extra_forbidden_roots: tuple[str, ...] = (),
        adapter: GraphifyAdapter | None = None,
    ) -> None:
        self._accepted_schema_versions = frozenset(accepted_schema_versions)
        self._forbidden_roots = tuple(root for root in extra_forbidden_roots if root)
        self._adapter = adapter if adapter is not None else GraphifyAdapter()

    @property
    def accepted_schema_versions(self) -> frozenset[int]:
        return self._accepted_schema_versions

    def validate(self, generation_dir: Path, manifest: Mapping[str, Any]) -> ValidationReport:
        """Fail closed on any contract violation in *generation_dir*.

        Raises ``FileNotFoundError`` if the directory does not exist; every
        other violation is collected into the report.
        """
        generation_dir = Path(generation_dir)
        if not generation_dir.is_dir():
            raise FileNotFoundError(f"generation directory {generation_dir} does not exist")
        errors: list[str] = []

        # --------------------------------------------------- manifest binding
        missing_keys = sorted(REQUIRED_MANIFEST_KEYS - set(manifest))
        if missing_keys:
            errors.append(f"manifest is missing required keys: {', '.join(missing_keys)}")
        manifest_path = generation_dir / "manifest.json"
        try:
            on_disk = manifest_path.read_bytes()
        except OSError:
            errors.append("manifest.json is missing from the generation directory")
        else:
            if on_disk != (canonical_json(dict(manifest)) + "\n").encode("utf-8"):
                errors.append("manifest.json on disk does not match the supplied manifest")

        artifacts = manifest.get("artifacts")
        if not isinstance(artifacts, Mapping) or set(artifacts) != set(HASHED_ARTIFACTS):
            errors.append(f"manifest artifacts must be exactly {sorted(HASHED_ARTIFACTS)}")
            artifacts = {}
        for name in HASHED_ARTIFACTS:
            path = generation_dir / name
            if not path.is_file():
                errors.append(f"missing artifact {name}")
                continue
            import hashlib

            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if artifacts.get(name) != actual:
                errors.append(f"hash mismatch for artifact {name}")

        # ------------------------------------------------------- graph schema
        node_count, edge_count = 0, 0
        collapsed: list[CollapsedLink] = []
        graph_path = generation_dir / "graph.json"
        graph: dict[str, Any] | None = None
        if graph_path.is_file():
            try:
                parsed = json.loads(graph_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as error:
                errors.append(f"graph.json is not valid JSON: {error}")
            else:
                if not isinstance(parsed, dict):
                    errors.append("graph.json must be a JSON object")
                else:
                    graph = parsed

        if graph is not None:
            unknown = sorted(set(graph) - _ALLOWED_GRAPH_KEYS)
            if unknown:
                errors.append(f"graph.json carries unknown top-level keys: {', '.join(unknown)}")
            missing = sorted(_REQUIRED_GRAPH_KEYS - set(graph))
            if missing:
                errors.append(f"graph.json is missing required keys: {', '.join(missing)}")
            raw_meta: Any = graph.get("graph")
            graph_meta: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
            schema_version = graph_meta.get("schema_version")
            if schema_version not in self._accepted_schema_versions:
                errors.append(
                    f"unknown schema version {schema_version!r}; accepted: {sorted(self._accepted_schema_versions)}"
                )
            if graph.get("multigraph") is not False:
                errors.append("the knowledge graph is an explicit simple-graph decision: multigraph must be false")

            raw_nodes: Any = graph.get("nodes")
            raw_links: Any = graph.get("links")
            nodes: list[Any] = raw_nodes if isinstance(raw_nodes, list) else []
            links: list[Any] = raw_links if isinstance(raw_links, list) else []
            node_count, edge_count = len(nodes), len(links)

            for error in validate_extraction(graph):
                errors.append(f"extraction schema: {error}")
            for node in nodes:
                if isinstance(node, dict):
                    errors.extend(self._junk_node_reasons(node))
            for index, link in enumerate(links):
                if not isinstance(link, dict):
                    continue
                confidence = link.get("confidence")
                if confidence not in VALID_CONFIDENCES:
                    errors.append(f"link {index} has invalid confidence {confidence!r}")
                if "confidence_score" not in link:
                    errors.append(f"link {index} is missing confidence_score")
                for endpoint in ("source", "target"):
                    value = link.get(endpoint)
                    if isinstance(value, str) and PurePosixPath(value).is_absolute():
                        errors.append(f"link {index} {endpoint} is an absolute path")
            for node in nodes:
                if isinstance(node, dict):
                    source_file = node.get("source_file")
                    if isinstance(source_file, str) and (
                        PurePosixPath(source_file).is_absolute() or _DRIVE_RE.search(source_file)
                    ):
                        errors.append(f"node {node.get('id')!r} has an absolute source_file {source_file!r}")

            # Loud collapse report: every node pair carrying parallel typed
            # links collapses to one edge in the simple-graph load (row 39).
            # The collapse is reported, never silently dropped, and never a
            # gate failure — consumers read the links list (finding F1).
            links_by_pair: dict[tuple[str, str], list[str]] = {}
            for link in links:
                if not isinstance(link, dict):
                    continue
                source, target = link.get("source"), link.get("target")
                if isinstance(source, str) and isinstance(target, str):
                    links_by_pair.setdefault((source, target), []).append(str(link.get("relation")))
            for (source, target), relations in sorted(links_by_pair.items()):
                if len(relations) > 1:
                    collapsed.append(CollapsedLink(source=source, target=target, relations=tuple(sorted(relations))))

            # Load through the pinned loader: direction and confidence backfill
            # must survive the round trip (matrix row 34).
            try:
                loaded = self._adapter.load_graph(graph_path)
            except Exception as error:  # noqa: BLE001 - any loader failure fails closed
                errors.append(f"the pinned Graphify loader rejects graph.json: {error}")
            else:
                if not loaded.is_directed():
                    errors.append("the pinned loader did not restore a directed graph")
                if loaded.number_of_nodes() != len(nodes):
                    errors.append("the pinned loader lost nodes on the round trip")
                if loaded.number_of_edges() > len(links):
                    errors.append("the pinned loader produced more edges than graph.json declares")

        # ------------------------------------- portable headers and secrets
        for name in HASHED_ARTIFACTS:
            path = generation_dir / name
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for root in self._forbidden_roots:
                if root in text:
                    errors.append(f"artifact {name} leaks the absolute checkout path {root!r}")
            match = _ABS_PATH_RE.search(text)
            if match is not None:
                errors.append(f"artifact {name} contains an absolute path {match.group(0)!r}")
            for pattern in _SECRET_RES:
                secret = pattern.search(text)
                if secret is not None:
                    errors.append(f"artifact {name} contains a secret-shaped string {secret.group(0)[:8]}…")
                    break

        return ValidationReport(
            ok=not errors,
            errors=tuple(errors),
            node_count=node_count,
            edge_count=edge_count,
            collapsed_links=tuple(collapsed),
        )

    def _junk_node_reasons(self, node: Mapping[str, Any]) -> list[str]:
        """The product junk-node classifier (matrix row 21)."""
        reasons: list[str] = []
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id:
            reasons.append("junk node with a missing or non-string id")
        elif "/" in node_id or "\\" in node_id or ".." in node_id or not _ID_RE.fullmatch(node_id):
            reasons.append(f"junk node {node_id!r}: id is path-shaped or outside the semantic id charset")
        label = node.get("label")
        if not isinstance(label, str) or not label.strip():
            reasons.append(f"junk node {node_id!r}: blank label")
        file_type = node.get("file_type")
        if file_type not in VALID_FILE_TYPES:
            reasons.append(f"junk node {node_id!r}: invalid file_type {file_type!r}")
        elif file_type in {"rationale", "concept"}:
            reasons.append(f"junk node {node_id!r}: unsanitized {file_type} tier")
        if not node.get("source_version_id"):
            reasons.append(f"junk node {node_id!r}: missing source_version_id provenance")
        return reasons
