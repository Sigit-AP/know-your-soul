"""Single serialized projector and content-addressed, atomically activated generations.

Task 3 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 7.4 and matrix adapter A3.

Flow (all inside one non-blocking rebuild lock, so there is exactly one
writer per corpus root):

1. Replay knowledge-affecting domain events in ``knowledge_seq`` order up to
   the target watermark (the store's current knowledge high-water mark, or an
   explicit fixed mark for deterministic rebuild probes).
2. Assemble the Graphify-schema extraction, validate it strictly (build
   warnings are gate failures), build the directed graph with ``dedup=False``
   and partition it with the pinned partitioner and hub policy.
3. Write a content-addressed generation directory (``graph.json``,
   ``GRAPH_REPORT.md``, ``analysis.json``, ``labels.json``,
   ``signatures.json``, ``graph.html``, ``manifest.json``). Every artifact is
   written through an idempotent atomic writer and fsynced; unchanged bytes
   keep their inode. The manifest binds the schema version, the pinned
   partitioner, the hub policy, the generator version, the corpus
   high-water mark and per-artifact content hashes. Dates and commit stamps
   never enter a generation.
4. Validate the whole generation with the product read-model validator; a
   failure raises and nothing is activated.
5. Activate: one store transaction compares-and-swaps
   ``active_projection_version``/``active_projection_watermark`` — an older
   knowledge watermark can never replace a newer one — then the ``CURRENT``
   pointer is swapped atomically (same-directory temp file, ``os.replace``,
   directory fsync) and only when it actually changes.

Crash discipline: the pointer swap is last, so a crash mid-activation leaves
the previous generation serving; the store CAS then disagrees with the
pointer, and :meth:`GraphProjector.repair_if_stale` — also the
``AuditLog.recover_incomplete(projection_repair=...)`` hook — re-derives the
content-addressed generation and re-points ``CURRENT``.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from soul_knowledge.domain import Event, EventKind, Node, RelationKind, canonical_json, quantize_score
from soul_knowledge.graphify_adapter import (
    ADAPTER_VERSION,
    CONFIDENCE_BY_RELATION,
    CONFIDENCE_SCORE,
    GRAPHIFY_PARTITIONER,
    PINNED_HUB_POLICY,
    GraphifyAdapter,
)
from soul_knowledge.read_model_validator import ReadModelValidator
from soul_knowledge.store import EventStore

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only fallback
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "ARTIFACT_NAMES",
    "FragmentCache",
    "GraphProjector",
    "ProjectionError",
    "ProjectionReceipt",
    "ProjectionValidationError",
    "ProjectorBusy",
    "ShrinkGuardError",
    "StalePublicationError",
]

# manifest.json is activated last and verified by canonical equality, so it is
# not part of its own artifact hash set.
ARTIFACT_NAMES = (
    "graph.json",
    "GRAPH_REPORT.md",
    "analysis.json",
    "labels.json",
    "signatures.json",
    "graph.html",
    "manifest.json",
)
HASHED_ARTIFACTS = ARTIFACT_NAMES[:-1]

_MANIFEST_VERSION = 1


class ProjectionError(RuntimeError):
    """Base class for projection contract violations."""


class ProjectorBusy(ProjectionError):
    """Another projector holds this corpus root's rebuild lock."""


class StalePublicationError(ProjectionError):
    """An older knowledge watermark tried to replace a newer one."""


class ShrinkGuardError(ProjectionError):
    """The new generation would silently drop nodes relative to the active one."""


class ProjectionValidationError(ProjectionError):
    """The extraction or the built generation failed a validation gate."""


class ProjectionReceipt(BaseModel):
    """The published projection identity (spec section 7.4)."""

    model_config = ConfigDict(frozen=True)

    projection_version: str
    source_knowledge_high_watermark: int
    content_hash: str
    generation_dir: str
    activated: bool
    node_count: int
    edge_count: int
    repaired: bool = False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class FragmentCache:
    """Content-hash fragment cache; one process per root, portable across roots.

    Keys are ``sha256(relative path + content hash)`` inside a namespace pinned
    to the fragment-generator version: mtime never participates (row 4), a
    generator bump is a full re-derivation (row 5), the cache directory can be
    moved between absolute roots and still hit (row 8), and ``prune`` bounds
    the entry count by the live document set (row 9).
    """

    def __init__(self, cache_root: str | Path, *, namespace: str | None = None) -> None:
        from soul_knowledge.fragment_generator import FRAGMENT_GENERATOR_VERSION

        self._namespace = namespace or f"fragment-generator-{FRAGMENT_GENERATOR_VERSION}"
        self._dir = Path(cache_root) / self._namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    def key_for(self, rel_path: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        return _sha256(f"soul-fragment\0{rel_path}\0{digest}".encode())

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._dir / f"{key}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None

    def put(self, key: str, fragment: Mapping[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / f"{key}.json.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(fragment))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._dir / f"{key}.json")

    def prune(self, live_keys: list[str]) -> int:
        """Drop every entry outside *live_keys*; returns the number removed."""
        live = set(live_keys)
        removed = 0
        if not self._dir.is_dir():
            return 0
        for path in sorted(self._dir.glob("*.json")):
            if path.stem not in live:
                path.unlink()
                removed += 1
        return removed

    def entries(self) -> list[str]:
        if not self._dir.is_dir():
            return []
        return sorted(path.stem for path in self._dir.glob("*.json"))


class GraphProjector:
    """The single serialized projector for one corpus root."""

    def __init__(
        self,
        store: EventStore,
        *,
        output_root: str | Path,
        adapter: GraphifyAdapter | None = None,
        validator: ReadModelValidator | None = None,
        cache: FragmentCache | None = None,
        corpus_label: str = "soul-corpus",
        allow_shrink: bool = False,
    ) -> None:
        self._store = store
        self._output_root = Path(output_root)
        self._adapter = adapter if adapter is not None else GraphifyAdapter()
        self._validator = (
            validator
            if validator is not None
            else ReadModelValidator(extra_forbidden_roots=(str(self._output_root.resolve()),))
        )
        self._cache = cache
        self._corpus_label = corpus_label
        self._allow_shrink = allow_shrink

    # ------------------------------------------------------------------ lock

    @contextmanager
    def hold_lock(self) -> Iterator[None]:
        """The single-writer rebuild lock (flock; released on process death).

        A competing projector raises :class:`ProjectorBusy` instead of
        interleaving a publication; work committed while the lock is held is
        drained by the next rebuild (matrix row 13).
        """
        self._output_root.mkdir(parents=True, exist_ok=True)
        (self._output_root / "generations").mkdir(exist_ok=True)
        fd = os.open(self._output_root / "projector.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise ProjectorBusy("another projector holds the rebuild lock for this corpus root") from error
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ----------------------------------------------------------------- replay

    def _knowledge_events(self, upto_knowledge_seq: int) -> list[Event]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM event_log WHERE kind = 'domain' AND knowledge_affecting = 1 "
                "AND knowledge_seq IS NOT NULL AND knowledge_seq <= ? ORDER BY event_seq",
                (upto_knowledge_seq,),
            ).fetchall()
        return [Event.model_validate(json.loads(str(row["payload_json"]))) for row in rows]

    def _extraction(self, events: list[Event]) -> dict[str, Any]:
        nodes: dict[str, Node] = {}
        versions: dict[str, int] = {}
        relations = []
        for event in events:
            if event.kind is EventKind.NODE_UPSERT and event.node is not None:
                versions[event.node_id] = versions.get(event.node_id, 0) + 1
                nodes[event.node_id] = event.node
            elif event.kind is EventKind.NODE_RETIRE:
                nodes.pop(event.node_id, None)
                versions.pop(event.node_id, None)
            elif event.kind is EventKind.RELATION_ADD and event.relation is not None:
                relations.append(event.relation)

        node_dicts: dict[str, dict[str, Any]] = {}
        for node_id in sorted(nodes):
            node = nodes[node_id]
            source_file = str(
                node.payload.get("source_file") or (node.provenance[-1] if node.provenance else "soul://store")
            )
            node_dicts[node_id] = {
                "id": node.node_id,
                "label": node.summary or node.node_id,
                "file_type": "document",
                "source_file": source_file,
                "_origin": "semantic",
                "domain": node.domain.value,
                "kind": node.kind.value,
                "lifecycle_status": node.lifecycle_status.value,
                "version": versions.get(node_id, node.version),
                "source_version_id": node.source_version_id,
                "provenance": list(node.provenance),
                "context_tags": list(node.context_tags),
                "scores": node.scores.quantized(),
                "confidence": str(quantize_score(node.scores.confidence)),
                "professional_level": node.professional_level,
            }

        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        def _edge(source: str, target: str, relation_kind: str) -> None:
            key = (source, target, relation_kind)
            if key in seen or source not in node_dicts or target not in node_dicts:
                return
            seen.add(key)
            confidence = CONFIDENCE_BY_RELATION.get(RelationKind(relation_kind), "EXTRACTED")
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "relation": relation_kind,
                    "confidence": confidence,
                    "confidence_score": CONFIDENCE_SCORE[confidence],
                    "weight": 1.0,
                    "source_file": node_dicts[source]["source_file"],
                }
            )

        for node_id in sorted(nodes):
            parent_id = nodes[node_id].parent_id
            if parent_id is not None:
                _edge(parent_id, node_id, "parent_of")
        for relation in relations:
            _edge(relation.source_node_id, relation.target_node_id, relation.kind.value)

        return {"nodes": list(node_dicts.values()), "edges": edges, "hyperedges": []}

    # ----------------------------------------------------------------- build

    def _build_generation(
        self,
        extraction: dict[str, Any],
        watermark: int,
        *,
        allow_shrink: bool,
    ) -> tuple[Path, dict[str, Any], int, int]:
        errors = self._adapter.validate_extraction_strict(extraction)
        if errors:
            raise ProjectionValidationError("extraction failed the build gate: " + "; ".join(errors))

        node_count = len(extraction["nodes"])
        if not allow_shrink:
            active = self.active_manifest()
            if active is not None and node_count < int(active["node_count"]):
                raise ShrinkGuardError(
                    f"refusing to publish {node_count} nodes over the active generation's "
                    f"{active['node_count']} without an explicit allow_shrink"
                )

        graph = self._adapter.build_graph(extraction)
        communities = self._adapter.partition(graph)
        signatures = self._adapter.community_signatures(communities)
        member_sig = {member: sig for sig, members in signatures.items() for member in members}
        cohesion = self._adapter.cohesion_by_signature(graph, communities)
        god_nodes = self._adapter.god_nodes(graph)

        node_list: list[dict[str, Any]] = []
        for node_id, attrs in graph.nodes(data=True):
            item = {"id": node_id, **dict(attrs)}
            sig = member_sig.get(str(node_id))
            if sig is not None:
                item["community_sig"] = sig
            node_list.append(item)
        link_list = [dict(edge) for edge in extraction["edges"]]

        graph_data = {
            "directed": True,
            "multigraph": False,
            "graph": {
                "schema_version": 1,
                "generator": "soul-knowledge",
                "partitioner": GRAPHIFY_PARTITIONER["name"],
            },
            "nodes": sorted(node_list, key=canonical_json),
            "links": sorted(link_list, key=canonical_json),
            "hyperedges": [],
        }
        artifacts: dict[str, bytes] = {"graph.json": (canonical_json(graph_data) + "\n").encode("utf-8")}

        confidences = [str(link["confidence"]) for link in link_list]
        report_lines = [
            "# SOUL Knowledge Graph Report",
            "",
            f"corpus: {self._corpus_label}",
            f"nodes: {len(node_list)}",
            f"edges: {len(link_list)}",
            f"communities: {len(signatures)}",
            "confidence: "
            + " ".join(f"{name}={confidences.count(name)}" for name in ("EXTRACTED", "INFERRED", "AMBIGUOUS")),
            "",
            "## Communities",
            *(f"- {sig}: {len(members)} nodes" for sig, members in sorted(signatures.items())),
            "",
            "## God Nodes",
            *(f"- {node['id']} (degree {node['degree']})" for node in god_nodes),
            "",
        ]
        artifacts["GRAPH_REPORT.md"] = "\n".join(report_lines).encode("utf-8")
        artifacts["analysis.json"] = (
            canonical_json(
                {
                    "counts": {
                        "nodes": len(node_list),
                        "edges": len(link_list),
                        "communities": len(signatures),
                    },
                    "god_nodes": god_nodes,
                    "structural_diagnostics": {
                        "cohesion_by_community_sig": cohesion,
                        "note": "Structural diagnostic only; never a knowledge-quality score (spec section 9).",
                    },
                }
            )
            + "\n"
        ).encode("utf-8")
        artifacts["labels.json"] = (canonical_json({sig: "UNREVIEWED" for sig in sorted(signatures)}) + "\n").encode(
            "utf-8"
        )
        artifacts["signatures.json"] = (canonical_json(dict(sorted(signatures.items()))) + "\n").encode("utf-8")

        artifact_hashes = {name: _sha256(data) for name, data in artifacts.items()}
        graph_content_hash = artifact_hashes["graph.json"]
        content_hash = _sha256(
            canonical_json(
                {
                    "adapter_version": ADAPTER_VERSION,
                    "artifacts": artifact_hashes,
                    "fragment_fingerprint": self._adapter.fragment_fingerprint,
                    "generator_version": self._adapter.generator_version,
                    "hub_policy": PINNED_HUB_POLICY,
                    "partitioner": GRAPHIFY_PARTITIONER,
                    "schema_version": 1,
                    "source_knowledge_high_watermark": watermark,
                }
            ).encode("utf-8")
        )
        projection_version = f"gen-{content_hash[:24]}"

        # graph.html is a derived, generation-keyed view; it embeds the
        # projection version, so it is hashed into the manifest but is not part
        # of the content identity (matrix row 35: never a source of truth).
        html = (
            '<!doctype html>\n<html><head><meta charset="utf-8"><title>SOUL Knowledge Graph</title></head>'
            "<body>\n"
            f"<h1>SOUL Knowledge Graph</h1>\n<p>projection: {projection_version}</p>\n"
            f"<p>nodes: {len(node_list)} edges: {len(link_list)} communities: {len(signatures)}</p>\n"
            f'<script type="application/json" id="graph">\n{json.dumps(graph_data, sort_keys=True)}\n</script>\n'
            "</body></html>\n"
        )
        artifacts["graph.html"] = html.encode("utf-8")
        artifact_hashes["graph.html"] = _sha256(artifacts["graph.html"])

        manifest: dict[str, Any] = {
            "manifest_version": _MANIFEST_VERSION,
            "projection_version": projection_version,
            "content_hash": content_hash,
            "source_knowledge_high_watermark": watermark,
            "schema_version": 1,
            "accepted_schema_versions": sorted(self._validator.accepted_schema_versions),
            "partitioner": GRAPHIFY_PARTITIONER,
            "hub_policy": PINNED_HUB_POLICY,
            "generator_version": self._adapter.generator_version,
            "adapter_version": ADAPTER_VERSION,
            "fragment_fingerprint": self._adapter.fragment_fingerprint,
            "cache_namespace": self._cache.namespace if self._cache is not None else None,
            "graph_content_hash": graph_content_hash,
            "artifacts": artifact_hashes,
            "node_count": len(node_list),
            "edge_count": len(link_list),
            "multigraph": False,
        }
        artifacts["manifest.json"] = (canonical_json(manifest) + "\n").encode("utf-8")

        generation_dir = self._output_root / "generations" / content_hash
        generation_dir.mkdir(parents=True, exist_ok=True)
        for name in ARTIFACT_NAMES:
            self._write_if_changed(generation_dir / name, artifacts[name])
        dir_fd = os.open(generation_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        return generation_dir, manifest, len(node_list), len(link_list)

    @staticmethod
    def _write_if_changed(path: Path, data: bytes) -> bool:
        """Idempotent atomic write: unchanged bytes keep inode and mtime (row 24)."""
        try:
            if path.read_bytes() == data:
                return False
        except OSError:
            pass
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True

    # ------------------------------------------------------------- activation

    def rebuild(
        self,
        *,
        upto_knowledge_seq: int | None = None,
        allow_shrink: bool | None = None,
        activate: bool = True,
    ) -> ProjectionReceipt:
        """Replay, build, validate and (by default) activate a generation."""
        with self.hold_lock():
            watermark = self._store.knowledge_high_watermark() if upto_knowledge_seq is None else upto_knowledge_seq
            extraction = self._extraction(self._knowledge_events(watermark))
            generation_dir, manifest, node_count, edge_count = self._build_generation(
                extraction,
                watermark,
                allow_shrink=self._allow_shrink if allow_shrink is None else allow_shrink,
            )
            report = self._validator.validate(generation_dir, manifest)
            if not report.ok:
                raise ProjectionValidationError(
                    "generation failed the read-model validator: " + "; ".join(report.errors)
                )
            if activate:
                self._activate(manifest, generation_dir)
            return ProjectionReceipt(
                projection_version=str(manifest["projection_version"]),
                source_knowledge_high_watermark=watermark,
                content_hash=str(manifest["content_hash"]),
                generation_dir=str(generation_dir),
                activated=activate,
                node_count=node_count,
                edge_count=edge_count,
            )

    def _activate(self, manifest: Mapping[str, Any], generation_dir: Path) -> None:
        """CAS the store pointers, then atomically swap ``CURRENT`` (fsynced).

        The store transaction rejects an older knowledge watermark, so
        out-of-order publication is impossible; the pointer swap is idempotent
        and happens only after the CAS committed.
        """
        watermark = int(manifest["source_knowledge_high_watermark"])
        with self._store.transaction() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = 'active_projection_watermark'").fetchone()
            current = int(str(row["value"])) if row is not None else -1
            if watermark < current:
                raise StalePublicationError(f"knowledge watermark {watermark} cannot replace the active {current}")
            for key, value in (
                ("active_projection_version", str(manifest["projection_version"])),
                ("active_projection_watermark", str(watermark)),
            ):
                conn.execute(
                    "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value, _now_iso()),
                )
        pointer = self._output_root / "CURRENT"
        desired = generation_dir.name + "\n"
        try:
            if pointer.read_text(encoding="utf-8") == desired:
                return
        except OSError:
            pass
        tmp = self._output_root / f"CURRENT.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(desired)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, pointer)
        dir_fd = os.open(self._output_root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # ----------------------------------------------------------------- status

    def active_generation_dir(self) -> Path | None:
        pointer = self._output_root / "CURRENT"
        try:
            name = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not name or len(name) != 64 or any(char not in "0123456789abcdef" for char in name):
            return None
        generation = self._output_root / "generations" / name
        return generation if generation.is_dir() else None

    def active_manifest(self) -> dict[str, Any] | None:
        generation = self.active_generation_dir()
        if generation is None:
            return None
        try:
            manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None
        return manifest if isinstance(manifest, dict) else None

    def repair_if_stale(self) -> ProjectionReceipt:
        """Repair a stale or torn publication; a no-op when already current.

        Stale means: no readable active manifest, the active manifest's
        knowledge watermark differs from the store's, the store's projection
        pointer disagrees with the manifest, or the generation no longer
        passes the read-model validator. This is the
        ``AuditLog.recover_incomplete(projection_repair=...)`` hook.
        """
        with self.hold_lock():
            store_watermark = self._store.knowledge_high_watermark()
            manifest = self.active_manifest()
            stale = manifest is None
            if manifest is not None:
                if int(manifest.get("source_knowledge_high_watermark", -1)) != store_watermark:
                    stale = True
                elif manifest.get("projection_version") != self._store.projection_version():
                    stale = True
                else:
                    generation = self.active_generation_dir()
                    assert generation is not None  # manifest implies a directory
                    if not self._validator.validate(generation, manifest).ok:
                        stale = True
            if not stale and manifest is not None:
                generation = self.active_generation_dir()
                assert generation is not None
                return ProjectionReceipt(
                    projection_version=str(manifest["projection_version"]),
                    source_knowledge_high_watermark=store_watermark,
                    content_hash=str(manifest["content_hash"]),
                    generation_dir=str(generation),
                    activated=False,
                    node_count=int(manifest["node_count"]),
                    edge_count=int(manifest["edge_count"]),
                    repaired=False,
                )
        receipt = self.rebuild()
        return receipt.model_copy(update={"repaired": True})
