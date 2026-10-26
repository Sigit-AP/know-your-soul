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

Residual audit wave-1 (R2): every file read is contained and bounded.

* Contained — each candidate resolves through
  :func:`soul_knowledge.security.resolve_within_root` against the import root
  (the corpus root for directory imports, the file's own directory for
  standalone file imports): a symlink or path that resolves outside the root
  fails closed with a typed ``PathEscapeError`` and its bytes are never read.
* Bounded — a file larger than :data:`MAX_IMPORT_FILE_BYTES` (and a manifest
  larger than :data:`MAX_IMPORT_MANIFEST_BYTES`) fails closed with a typed
  ``OversizedPayloadError`` before its content is read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, Relation, RelationKind
from soul_knowledge.events import Invocation
from soul_knowledge.fragment_generator import FRAGMENT_GENERATOR_VERSION, file_scoped_id
from soul_knowledge.graphify_adapter import GraphifyAdapter
from soul_knowledge.security import OversizedPayloadError, resolve_within_root
from soul_knowledge.source_registry import ParserVersions, SourceKind, SourceRegistry
from soul_knowledge.store import EventStore

__all__ = [
    "DEFAULT_PARSER_VERSIONS",
    "KNOWLEDGE_ROOT_ID",
    "MAX_IMPORT_FILE_BYTES",
    "MAX_IMPORT_MANIFEST_BYTES",
    "ImportManifestError",
    "ImportReceipt",
    "SeedImporter",
    "injective_file_id",
    "injective_section_key",
]

KNOWLEDGE_ROOT_ID = "KROOT"

MAX_IMPORT_FILE_BYTES = 1_048_576
"""Per-file import budget: 1 MiB, matching ``security.DEFAULT_MAX_PAYLOAD_BYTES``.

Structured seed markdown is operator-curated prose; real corpus files are
kilobytes. One MiB bounds the worst-case parse/hash work per file without
touching any legitimate seed.
"""

MAX_IMPORT_MANIFEST_BYTES = 1_048_576
"""Budget for the incremental import manifest read (manifests are tiny JSON)."""

DEFAULT_PARSER_VERSIONS = ParserVersions(
    parser_version="markdown-heading-1",
    normalization_version="utf8-nfc-crlf-lf-1",
    engine_version=FRAGMENT_GENERATOR_VERSION,
    scoring_contract_version="0-unscored",
)
"""The compatibility versions stamped onto every imported source version."""


class ImportManifestError(RuntimeError):
    """The incremental import manifest is missing, unreadable or mis-anchored."""


# ---------------------------------------------------------- injective ids
#
# Wave-3 W3-2: the fragment generator's ``file_scoped_id`` (Graphify's
# ``make_id`` recipe) is *lossy*: it casefolds and collapses every non-word
# run (``-``, space, ``.`` ...) to ``_``, so ``a-b.md`` ≡ ``a_b.md`` ≡
# ``a b.md`` and ``a/b_c.md`` ≡ ``a_b/c.md`` — two corpus files silently
# merged into one node set. The importer therefore re-scopes every fragment
# onto the injective scheme below before any event is written.
#
# Encoding E: every ``\w`` character (letters, digits, ``_``, Unicode word
# characters) is kept verbatim; the escape marker ``-`` and every non-``\w``
# character become ``-<lowercase hex codepoint>-``. E is *provably*
# injective: an image string decodes uniquely (literal runs are ``\w+``;
# every ``-`` opens an escape group of hex digits closed by the next ``-``;
# ``-`` never appears literally), so distinct strings never share an image.
#
# * File id: the normalized path stem (same normalization as the legacy
#   recipe: separator style, ``..`` collapse, final-suffix drop), encoded
#   per ``/``-segment and joined with ``:``. ``:`` never appears inside an
#   E-image, so the segment split is unambiguous and distinct normalized
#   paths yield distinct ids. Path-separator ``:`` keeps ids inside the
#   read-model validator's semantic charset (``[\w.:-]+``, no ``/``/``..``).
# * Section id: ``<file id>.<E(heading)>`` plus an ``-n<occurrence>`` suffix
#   for repeated identical headings. File ids never contain ``.`` and E-image
#   keys never contain ``.`` or the substring ``-n<digits>`` (``n`` is not a
#   hex digit, so a valid escape group cannot start it), so the composition
#   is injective in (path, heading, occurrence) and a file id can never
#   collide with a section id.
#
# Ids stay deterministic and host-stable (pure functions of the relative
# path / heading text), and pure ``\w``-only names keep their natural ids
# (``alpha.md`` -> ``alpha``).

_ID_LITERAL_RE = re.compile(r"\w", re.UNICODE)

_EMPTY_FILE_ID = "doc-"
"""Fallback for an empty normalized stem; never an E-image (dangling escape)."""


def _escape_id_text(text: str) -> str:
    """Reversible, injective encoding of arbitrary text into the id charset."""
    parts: list[str] = []
    for char in text:
        if char != "-" and _ID_LITERAL_RE.fullmatch(char):
            parts.append(char)
        else:
            parts.append(f"-{ord(char):x}-")
    return "".join(parts)


def injective_file_id(rel_path: str) -> str:
    """The collision-free node id for a source file, derived from its path.

    Injective modulo the declared path normalization: two corpus-relative
    markdown paths whose normalized stems differ always map to different
    ids (closes wave-3 W3-2). Deterministic and host-stable.
    """
    normalized = rel_path.replace("\\", "/").lstrip("/")
    while ".." in normalized:
        normalized = normalized.replace("..", "")
    stem = PurePosixPath(normalized).with_suffix("").as_posix()
    # Empty segments arise only from (leading) slash equivalences the legacy
    # recipe also collapsed; they carry no identity and are dropped.
    segments = [segment for segment in stem.split("/") if segment]
    encoded = ":".join(_escape_id_text(segment) for segment in segments)
    return encoded or _EMPTY_FILE_ID


def injective_section_key(heading: str) -> str:
    """The injective per-heading key used to compose section node ids."""
    return _escape_id_text(heading)


def _section_node_id(file_id: str, heading: str, occurrence: int) -> str:
    base = f"{file_id}.{injective_section_key(heading)}"
    return base if occurrence == 1 else f"{base}-n{occurrence}"


def _remap_fragment_ids(
    nodes: list[dict], edges: list[dict], scope: str
) -> tuple[list[dict], list[dict], dict[str, str]]:
    """Re-scope one fragment's old (lossy) ids onto the injective scheme.

    The fragment generator is a frozen contract, so the remap happens here,
    at the import boundary, before any event reaches the store. Section ids
    are re-derived from the fragment's own node order and labels (the
    generator emits the file node first, then section nodes in heading
    order), with occurrence numbering per encoded heading — exactly the walk
    the projector's enrichment replica performs from the same source bytes.

    Returns ``(nodes, edges, id_map)``; ``references`` edge *targets* keep
    their old ids here and are resolved against the live store's scope index
    by the caller.
    """
    if not nodes:
        return [], edges, {}
    old_file_id = str(nodes[0]["id"])
    new_file_id = injective_file_id(scope)
    id_map: dict[str, str] = {old_file_id: new_file_id}
    used: dict[str, int] = {}
    new_nodes: list[dict] = []
    for index, item in enumerate(nodes):
        old_id = str(item["id"])
        if index == 0:
            new_id = new_file_id
        else:
            heading = str(item.get("label") or old_id)
            key = injective_section_key(heading)
            occurrence = used.get(key, 0) + 1
            used[key] = occurrence
            new_id = _section_node_id(new_file_id, heading, occurrence)
        id_map[old_id] = new_id
        new_nodes.append({**item, "id": new_id})
    new_edges: list[dict] = []
    for edge in edges:
        remapped = dict(edge)
        if str(remapped.get("source")) in id_map:
            remapped["source"] = id_map[str(remapped["source"])]
        if remapped.get("relation") == "contains" and str(remapped.get("target")) in id_map:
            remapped["target"] = id_map[str(remapped["target"])]
        new_edges.append(remapped)
    return new_nodes, new_edges, id_map


def _read_contained_file(root: str | Path, path: str | Path, *, max_bytes: int = MAX_IMPORT_FILE_BYTES) -> bytes:
    """Read *path* only when it resolves inside *root* and fits *max_bytes*.

    Fails closed: a symlink/path escaping the root raises a typed
    ``PathEscapeError`` before any byte is read; an oversized file raises a
    typed ``OversizedPayloadError`` from the stat metadata, and the actual
    byte count is re-checked after the read so a racing writer cannot smuggle
    extra content past the guard.
    """
    resolved = resolve_within_root(root, path)
    size = resolved.stat().st_size
    if size > max_bytes:
        raise OversizedPayloadError(
            f"import file {str(path)!r} is {size} bytes, exceeding the {max_bytes}-byte import cap"
        )
    content = resolved.read_bytes()
    if len(content) > max_bytes:
        raise OversizedPayloadError(
            f"import file {str(path)!r} grew past the {max_bytes}-byte import cap while being read"
        )
    return content


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
        import_root: str | Path | None = None,
    ) -> ImportReceipt:
        """Import one structured markdown file under an explicit domain mapping.

        ``source_scope`` overrides the id scope; directory imports pass the
        corpus-relative path so same-named files in different directories get
        distinct file-scoped ids. ``import_root`` pins the containment root
        (directory imports pass the corpus root); the default is the file's
        own directory, so a standalone import can never follow a symlink or
        path outside the directory the caller named.
        """
        if not isinstance(domain, PrimaryDomain):
            raise TypeError(f"an explicit PrimaryDomain mapping is required, got {domain!r}")
        path = Path(path)
        content = _read_contained_file(import_root if import_root is not None else path.parent, path)
        scope = source_scope if source_scope is not None else path.name
        version = self._registry.observe(path.resolve(), SourceKind.OPERATOR_IMPORT, self._parser_versions)
        self.ensure_roots(version.source_version_id)
        fragment = self._adapter.fragment_from_markdown(source_file=scope, content=content)
        # W3-2: re-scope the fragment's lossy ``make_id`` ids onto the
        # injective scheme before any event reaches the store, so two corpus
        # files whose legacy ids collide can never merge into one node set.
        nodes, edges, _id_map = _remap_fragment_ids(
            [dict(item) for item in fragment.nodes], [dict(item) for item in fragment.edges], scope
        )

        children: dict[str, int] = {}
        for edge in edges:
            if edge.get("relation") == "contains":
                children[str(edge["source"])] = children.get(str(edge["source"]), 0) + 1

        node_ids: list[str] = []
        appended = 0
        replays = 0
        for index, item in enumerate(nodes):
            node_id = str(item["id"])
            if index == 0:
                kind, parent_id = NodeKind.CHILD, domain.value
            else:
                kind = NodeKind.BRANCH if children.get(node_id) else NodeKind.LEAF
                parent_id = next(
                    (str(edge["source"]) for edge in edges if edge.get("target") == node_id),
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
        for edge in edges:
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
        reference_edges = [edge for edge in edges if edge.get("relation") == "references"]
        # Wikilink targets arrive as *legacy* lossy ids (the frozen generator
        # recipe); resolve them to the target file's injective id through the
        # live store's scope index. A legacy id shared by two live scopes is
        # ambiguous and fails safe to unresolved — never a guessed target.
        target_map: dict[str, str | None] | None = None
        for edge in reference_edges:
            if target_map is None:
                target_map = self._live_reference_targets()
            legacy_target = str(edge["target"])
            if legacy_target not in target_map:
                unresolved.append(legacy_target)
                continue
            target_id = target_map[legacy_target]
            if target_id is None or self._store.node_version(target_id) == 0:
                unresolved.append(legacy_target)
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
                stored = json.loads(
                    _read_contained_file(self._state_dir, manifest_path, max_bytes=MAX_IMPORT_MANIFEST_BYTES).decode(
                        "utf-8"
                    )
                )
            except (OSError, ValueError) as error:
                # ValueError covers both JSONDecodeError and UnicodeDecodeError
                # (F-SUB45-2): every unreadable manifest fails closed typed.
                raise ImportManifestError(f"unreadable import manifest {manifest_path}: {error}") from error
            if not isinstance(stored, dict):
                raise ImportManifestError(
                    f"unreadable import manifest {manifest_path}: expected a JSON object, got {type(stored).__name__}"
                )
            if stored.get("root") != str(root):
                raise ImportManifestError(
                    f"import manifest is anchored to {stored.get('root')!r}, not {str(root)!r}; refusing to trust it"
                )
            files = stored.get("files", {})
            if not isinstance(files, dict) or any(not isinstance(entry, dict) for entry in files.values()):
                raise ImportManifestError(
                    f"unreadable import manifest {manifest_path}: 'files' must map paths to entry objects"
                )
            previous = dict(files)

        current: dict[str, str] = {}
        for file in sorted(root.rglob("*.md"), key=lambda item: item.as_posix()):
            relative = file.relative_to(root).as_posix()
            current[relative] = hashlib.sha256(_read_contained_file(root, file)).hexdigest()

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
                import_root=root,
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
                import_root=root,
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

    def _live_reference_targets(self) -> dict[str, str | None]:
        """Legacy wikilink target id -> the live target's injective id.

        Keyed by the *legacy* ``file_scoped_id`` of every live import scope
        (what the frozen generator emits as a ``references`` edge target);
        valued by that scope's injective file id. A legacy id shared by two
        live scopes is a genuine legacy collision and maps to ``None`` —
        ambiguous, never guessed.
        """
        live: dict[str, Node] = {}
        for event in self._store.replay_domain():
            if event.kind is EventKind.NODE_UPSERT and event.node is not None:
                live[event.node_id] = event.node
            elif event.kind is EventKind.NODE_RETIRE:
                live.pop(event.node_id, None)
        scopes: dict[str, set[str]] = {}
        for node in live.values():
            scope = node.payload.get("source_file")
            if isinstance(scope, str) and scope and "://" not in scope:
                scopes.setdefault(file_scoped_id(scope), set()).add(scope)
        return {
            legacy: injective_file_id(sorted(scope_set)[0]) if len(scope_set) == 1 else None
            for legacy, scope_set in scopes.items()
        }

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
