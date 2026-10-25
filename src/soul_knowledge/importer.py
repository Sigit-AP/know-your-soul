"""Deterministic structured seed import.

Task 3 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7 (event flow) and 13.5 (deterministic generation), matrix
row 10 (manifest-anchored incremental import).

Imports are *structured*: the caller supplies an explicit
:class:`~soul_knowledge.domain.PrimaryDomain` mapping and a logical
``source_uri``; no semantic AI extraction is involved. The importer

* registers the hash-identified source version through ``SourceRegistry``,
* guarantees the knowledge root and the seven primary roots exist,
* parses the source with the deterministic fragment generator (adapter A1),
* appends node/relation events with import-scoped idempotency keys, so a
  duplicate import replays idempotently and never moves the knowledge
  watermark, and
* resolves wikilinks against the store only when the target file has itself
  been imported, preserving relation integrity.

Directory imports are incremental through a manifest that is *anchored* to
the resolved corpus root and keyed by content hash, never stat data: a
manifest written for another root fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, Relation, RelationKind
from soul_knowledge.events import Invocation
from soul_knowledge.fragment_generator import FRAGMENT_GENERATOR_VERSION
from soul_knowledge.graphify_adapter import GraphifyAdapter
from soul_knowledge.source_registry import ParserVersions, SourceKind, SourceRegistry
from soul_knowledge.store import EventStore

__all__ = [
    "DEFAULT_PARSER_VERSIONS",
    "KNOWLEDGE_ROOT_ID",
    "ImportManifestError",
    "ImportReceipt",
    "SeedImporter",
]

KNOWLEDGE_ROOT_ID = "KROOT"

DEFAULT_PARSER_VERSIONS = ParserVersions(
    parser_version="markdown-heading-1",
    normalization_version="utf8-nfc-crlf-lf-1",
    engine_version=FRAGMENT_GENERATOR_VERSION,
    scoring_contract_version="0-unscored",
)
"""The compatibility versions stamped onto every imported source version."""


class ImportManifestError(RuntimeError):
    """The incremental import manifest is missing, unreadable or mis-anchored."""


class ImportReceipt(BaseModel):
    """The result of one structured import (spec section 7: ids and counts)."""

    model_config = ConfigDict(frozen=True)

    source_version_id: str
    content_sha256: str
    domain: PrimaryDomain
    node_ids: tuple[str, ...] = ()
    relations_added: int = 0
    events_appended: int = 0
    idempotent_replay: bool = False
    unresolved_links: tuple[str, ...] = ()
    partial: bool = False


class SeedImporter:
    """Imports structured markdown sources into the event store."""

    def __init__(
        self,
        store: EventStore,
        *,
        registry: SourceRegistry | None = None,
        adapter: GraphifyAdapter | None = None,
        parser_versions: ParserVersions | None = None,
        state_dir: str | Path | None = None,
        actor: str = "seed-importer",
    ) -> None:
        self._store = store
        self._registry = registry if registry is not None else SourceRegistry(store)
        self._adapter = adapter if adapter is not None else GraphifyAdapter()
        self._parser_versions = parser_versions if parser_versions is not None else DEFAULT_PARSER_VERSIONS
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._actor = actor

    # ---------------------------------------------------------------- roots

    def ensure_roots(self, source_version_id: str) -> None:
        """Guarantee the knowledge root and the seven primary roots exist.

        Root events use fixed idempotency keys, so the roots are materialized
        exactly once no matter how many imports run.
        """
        root = Node(
            node_id=KNOWLEDGE_ROOT_ID,
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            payload={"source_file": "soul://roots"},
            provenance=("soul://roots",),
            source_version_id=source_version_id,
        )
        self._upsert(root, source_version_id, idempotency_key=f"soul:root:{KNOWLEDGE_ROOT_ID}")
        for domain in PrimaryDomain:
            primary = Node(
                node_id=domain.value,
                parent_id=KNOWLEDGE_ROOT_ID,
                kind=NodeKind.PRIMARY,
                domain=domain,
                summary=domain.title,
                payload={"source_file": "soul://roots"},
                provenance=("soul://roots",),
                source_version_id=source_version_id,
            )
            self._upsert(primary, source_version_id, idempotency_key=f"soul:root:{domain.value}")
            self._relate(
                Relation(kind=RelationKind.PARENT_OF, source_node_id=KNOWLEDGE_ROOT_ID, target_node_id=domain.value),
                domain=domain,
                source_version_id=source_version_id,
                idempotency_key=f"soul:root-rel:{domain.value}",
            )

    # ---------------------------------------------------------------- files

    def import_file(
        self,
        path: Path,
        *,
        domain: PrimaryDomain,
        source_uri: str,
        source_scope: str | None = None,
    ) -> ImportReceipt:
        """Import one structured markdown file under an explicit domain mapping.

        ``source_scope`` overrides the id scope; directory imports pass the
        corpus-relative path so same-named files in different directories get
        distinct file-scoped ids.
        """
        if not isinstance(domain, PrimaryDomain):
            raise TypeError(f"an explicit PrimaryDomain mapping is required, got {domain!r}")
        path = Path(path)
        content = path.read_bytes()
        scope = source_scope if source_scope is not None else path.name
        version = self._registry.observe(path.resolve(), SourceKind.OPERATOR_IMPORT, self._parser_versions)
        self.ensure_roots(version.source_version_id)
        fragment = self._adapter.fragment_from_markdown(source_file=scope, content=content)

        children: dict[str, int] = {}
        for edge in fragment.edges:
            if edge.get("relation") == "contains":
                children[str(edge["source"])] = children.get(str(edge["source"]), 0) + 1

        node_ids: list[str] = []
        appended = 0
        replays = 0
        for index, item in enumerate(fragment.nodes):
            node_id = str(item["id"])
            if index == 0:
                kind, parent_id = NodeKind.CHILD, domain.value
            else:
                kind = NodeKind.BRANCH if children.get(node_id) else NodeKind.LEAF
                parent_id = next(
                    (str(edge["source"]) for edge in fragment.edges if edge.get("target") == node_id),
                    domain.value,
                )
            node = Node(
                node_id=node_id,
                parent_id=parent_id,
                kind=kind,
                domain=domain,
                summary=str(item.get("label") or node_id),
                payload={
                    "source_file": scope,
                    "heading_level": int(item.get("heading_level", 0)),
                },
                provenance=(source_uri,),
                context_tags=(domain.value, "seed-import"),
                source_version_id=version.source_version_id,
            )
            receipt = self._upsert(
                node, version.source_version_id, idempotency_key=f"import:{version.source_version_id}:{node_id}"
            )
            appended += 0 if receipt.idempotent_replay else 1
            replays += 1 if receipt.idempotent_replay else 0
            node_ids.append(node_id)

        relations_added = 0
        if node_ids:
            receipt = self._relate(
                Relation(kind=RelationKind.PARENT_OF, source_node_id=domain.value, target_node_id=node_ids[0]),
                domain=domain,
                source_version_id=version.source_version_id,
                idempotency_key=f"import:{version.source_version_id}:{domain.value}:parent_of:{node_ids[0]}",
            )
            relations_added += 0 if receipt.idempotent_replay else 1
            appended += 0 if receipt.idempotent_replay else 1
            replays += 1 if receipt.idempotent_replay else 0
        for edge in fragment.edges:
            if edge.get("relation") != "contains":
                continue
            source_id, target_id = str(edge["source"]), str(edge["target"])
            receipt = self._relate(
                Relation(kind=RelationKind.PARENT_OF, source_node_id=source_id, target_node_id=target_id),
                domain=domain,
                source_version_id=version.source_version_id,
                idempotency_key=f"import:{version.source_version_id}:{source_id}:parent_of:{target_id}",
            )
            relations_added += 0 if receipt.idempotent_replay else 1
            appended += 0 if receipt.idempotent_replay else 1
            replays += 1 if receipt.idempotent_replay else 0

        unresolved: list[str] = []
        for edge in fragment.edges:
            if edge.get("relation") != "references":
                continue
            target_id = str(edge["target"])
            if self._store.node_version(target_id) == 0:
                unresolved.append(target_id)
                continue
            source_id = str(edge["source"])
            receipt = self._relate(
                Relation(kind=RelationKind.DEPENDS_ON, source_node_id=source_id, target_node_id=target_id),
                domain=domain,
                source_version_id=version.source_version_id,
                idempotency_key=f"import:{version.source_version_id}:{source_id}:depends_on:{target_id}",
            )
            relations_added += 0 if receipt.idempotent_replay else 1
            appended += 0 if receipt.idempotent_replay else 1
            replays += 1 if receipt.idempotent_replay else 0

        total = appended + replays
        return ImportReceipt(
            source_version_id=version.source_version_id,
            content_sha256=hashlib.sha256(content).hexdigest(),
            domain=domain,
            node_ids=tuple(node_ids),
            relations_added=relations_added,
            events_appended=appended,
            idempotent_replay=total > 0 and appended == 0,
            unresolved_links=tuple(sorted(unresolved)),
            partial=fragment.partial,
        )

    def import_directory(
        self,
        root: Path,
        *,
        domain: PrimaryDomain,
        source_uri_prefix: str = "soul://corpus",
    ) -> list[ImportReceipt]:
        """Incrementally import every markdown file under *root*.

        The manifest is anchored to the resolved root and keyed by content
        hash: unchanged files are skipped, a content change re-imports exactly
        the changed file, and a manifest anchored to a different root fails
        closed (matrix row 10). A second pass resolves wikilinks whose targets
        were imported in the first pass.
        """
        if self._state_dir is None:
            raise ImportManifestError("import_directory requires a state_dir for the anchored manifest")
        root = Path(root).resolve()
        manifest_path = self._state_dir / "import_manifest.json"
        previous: dict[str, dict[str, str]] = {}
        if manifest_path.exists():
            try:
                stored = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ImportManifestError(f"unreadable import manifest {manifest_path}: {error}") from error
            if stored.get("root") != str(root):
                raise ImportManifestError(
                    f"import manifest is anchored to {stored.get('root')!r}, not {str(root)!r}; refusing to trust it"
                )
            previous = dict(stored.get("files", {}))

        current: dict[str, str] = {}
        for file in sorted(root.rglob("*.md"), key=lambda item: item.as_posix()):
            relative = file.relative_to(root).as_posix()
            current[relative] = hashlib.sha256(file.read_bytes()).hexdigest()

        receipts: list[ImportReceipt] = []
        imported: dict[str, ImportReceipt] = {}
        for relative, digest in sorted(current.items()):
            if previous.get(relative, {}).get("sha256") == digest:
                continue
            receipt = self.import_file(
                root / relative,
                domain=domain,
                source_uri=f"{source_uri_prefix}/{relative}",
                source_scope=relative,
            )
            receipts.append(receipt)
            imported[relative] = receipt
        # Second pass: resolve links whose targets arrived in the first pass.
        # Every event key is import-scoped, so this pass is free of duplicates.
        for relative in sorted(current):
            self.import_file(
                root / relative,
                domain=domain,
                source_uri=f"{source_uri_prefix}/{relative}",
                source_scope=relative,
            )

        manifest = {
            "root": str(root),
            "files": {
                relative: {
                    "sha256": digest,
                    "source_version_id": (
                        imported[relative].source_version_id
                        if relative in imported
                        else previous.get(relative, {}).get("source_version_id", "")
                    ),
                }
                for relative, digest in sorted(current.items())
            },
        }
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._state_dir / f"import_manifest.json.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, manifest_path)
        return receipts

    # -------------------------------------------------------------- retire

    def retire_source(self, source_scope: str) -> tuple[str, ...]:
        """Retire every live node imported from *source_scope* (fix F2).

        A source the watcher reports as ``REMOVED`` must not leave its nodes
        in the store or the projection: each live node whose import scope
        (``payload["source_file"]``) matches gets a ``node.retire`` domain
        event through :meth:`EventStore.commit` with a retire-scoped
        idempotency key, so a replayed removal is a no-op and the retired
        nodes stay in the append-only history as superseded revisions.
        Returns the node ids this call newly retired.
        """
        live: dict[str, Node] = {}
        for event in self._store.replay_domain():
            if event.kind is EventKind.NODE_UPSERT and event.node is not None:
                live[event.node_id] = event.node
            elif event.kind is EventKind.NODE_RETIRE:
                live.pop(event.node_id, None)
        retired: list[str] = []
        for node_id in sorted(live):
            node = live[node_id]
            if node.payload.get("source_file") != source_scope:
                continue
            version = self._store.node_version(node_id)
            request_hash = hashlib.sha256(f"retire-source\0{source_scope}\0{node_id}\0{version}".encode()).hexdigest()
            receipt = self._store.commit(
                Event(
                    kind=EventKind.NODE_RETIRE,
                    node_id=node_id,
                    domain=node.domain,
                    node=node,
                    source_version_id=node.source_version_id,
                    actor=self._actor,
                ),
                invocation=Invocation.create(
                    "knowledge.retire_source",
                    request_hash=request_hash,
                    actor=self._actor,
                    idempotency_key=f"retire:{source_scope}:{node_id}:{version}",
                ),
            )
            if not receipt.idempotent_replay:
                retired.append(node_id)
        return tuple(retired)

    # -------------------------------------------------------------- plumbing

    def _upsert(self, node: Node, source_version_id: str, *, idempotency_key: str):
        return self._store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id=node.node_id,
                domain=node.domain,
                node=node,
                source_version_id=source_version_id,
                actor=self._actor,
            ),
            idempotency_key=idempotency_key,
        )

    def _relate(self, relation: Relation, *, domain: PrimaryDomain, source_version_id: str, idempotency_key: str):
        return self._store.append_domain(
            Event(
                kind=EventKind.RELATION_ADD,
                node_id=relation.source_node_id,
                domain=domain,
                relation=relation,
                source_version_id=source_version_id,
                actor=self._actor,
            ),
            idempotency_key=idempotency_key,
        )
