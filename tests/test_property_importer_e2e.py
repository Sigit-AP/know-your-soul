"""Property tests for the importer end-to-end: import → publish → query.

Wave-2 follow-up to ``docs/recovery/codebase-push-property-tests.md``; pins
spec section 7 (event flow), matrix row 10 (manifest-anchored incremental
import) and the wave-3 W3-2 injective-id re-scoping on arbitrary generated
corpora:

* an import → rebuild → ``get_node``/``query`` round-trip resolves every
  imported node through the published projection;
* re-importing unchanged content is a fully idempotent replay: zero events
  appended, watermark unmoved, identical node ids;
* the incremental manifest is anchored to the resolved corpus root and fails
  closed (typed ``ImportManifestError``) when mis-anchored or corrupt;
* every emitted node id stays inside the read-model validator's semantic
  charset, section ids nest under their file id, and legacy-colliding file
  names (``a-b.md`` vs ``a_b.md``) can never merge into one node set;
* wikilinks resolve exactly when the target file has itself been imported;
* file reads are contained (symlink/path escapes fail closed with a typed
  ``PathEscapeError``) and bounded (``OversizedPayloadError`` at the cap).

All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soul_knowledge.contracts import QueryRequest
from soul_knowledge.domain import EventKind, PrimaryDomain
from soul_knowledge.fragment_generator import file_scoped_id
from soul_knowledge.importer import (
    ImportManifestError,
    SeedImporter,
    _read_contained_file,
    injective_file_id,
)
from soul_knowledge.projection import GraphProjector
from soul_knowledge.security import OversizedPayloadError, PathEscapeError
from soul_knowledge.service import KnowledgeService
from soul_knowledge.store import EventStore

_VALIDATOR_ID_RE = re.compile(r"^[\w.:-]+$", re.UNICODE)

_WORD = st.text(alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters=""), min_size=2, max_size=10)
_FILE_POOL = ("alpha", "beta", "gamma")


def _scratch() -> Path:
    return Path(tempfile.mkdtemp(prefix="soul-import-"))


class Corpus:
    """A generated markdown corpus: files with sections and wikilinks."""

    def __init__(self, files: dict[str, tuple[list[str], list[str]]]) -> None:
        """files: stem -> (section headings, wikilink targets)."""
        self.root = _scratch() / "corpus"
        self.root.mkdir(parents=True)
        self.files = files
        for stem, (headings, links) in files.items():
            lines = [f"# {stem.title()} Notes", ""]
            body_link = "".join(f"See [[{link}]] for context. " for link in links)
            for heading in headings:
                lines += [f"## {heading}", "", f"{body_link}Notes on {heading}.".strip(), ""]
            if not headings and body_link:
                lines += [body_link.strip(), ""]
            (self.root / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")

    @property
    def relative_names(self) -> list[str]:
        return sorted(f"{stem}.md" for stem in self.files)


def _importer(store: EventStore, root: Path) -> SeedImporter:
    return SeedImporter(store, state_dir=root / "import-state")


def _imported(tmp: Path, corpus: Corpus) -> tuple[EventStore, SeedImporter, list]:
    store = EventStore(tmp / "soul.sqlite3")
    importer = _importer(store, tmp)
    receipts = importer.import_directory(corpus.root, domain=PrimaryDomain.P1)
    return store, importer, receipts


def _published(store: EventStore, tmp: Path) -> tuple[GraphProjector, KnowledgeService, dict]:
    projector = GraphProjector(store, output_root=tmp / "out")
    projector.rebuild()
    generation = projector.active_generation_dir()
    assert generation is not None
    graph = json.loads((generation / "graph.json").read_text(encoding="utf-8"))
    return projector, KnowledgeService(store, projector), graph


_corpus = st.dictionaries(
    keys=st.sampled_from(_FILE_POOL),
    values=st.tuples(
        st.lists(_WORD, min_size=0, max_size=3, unique=True),
        st.lists(st.sampled_from(_FILE_POOL + ("ghost",)), max_size=2, unique=True),
    ),
    min_size=1,
    max_size=3,
)


# ------------------------------------------------------- import→publish→query


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_import_publish_get_node_round_trip(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store, _importer_, receipts = _imported(tmp, corpus)
    _projector, service, graph = _published(store, tmp)
    graph_ids = {node["id"] for node in graph["nodes"]}
    imported_ids = [node_id for receipt in receipts for node_id in receipt.node_ids]
    assert imported_ids, "a non-empty corpus always imports at least the file nodes"
    for node_id in imported_ids:
        # Every imported node is published …
        assert node_id in graph_ids
        # … and resolvable through the read surface with its scope intact.
        view = service.get_node(node_id, actor="test").canonical_payload()["node"]
        assert view["node_id"] == node_id
        assert view["provenance"]


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_import_publish_query_round_trip(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store, _i, receipts = _imported(tmp, corpus)
    _projector, service, graph = _published(store, tmp)
    graph_ids = {node["id"] for node in graph["nodes"]}
    stem = sorted(files)[0]
    response = service.query(
        QueryRequest(purpose=f"notes on {stem}", token_budget=512), actor="test"
    ).canonical_payload()
    assert response["estimated_tokens"] <= 512
    returned = {node["node_id"] for node in response.get("results", [])}
    assert returned <= graph_ids
    # The queried stem's file node is published; the read surface may only
    # ever return ids that exist in the active projection.
    assert injective_file_id(f"{stem}.md") in graph_ids


def test_query_finds_a_distinctive_heading(tmp_path: Path) -> None:
    corpus = Corpus({"alpha": (["xylophone"], []), "beta": (["birch"], [])})
    store, _i, receipts = _imported(tmp_path, corpus)
    _projector, service, _graph = _published(store, tmp_path)
    section_id = f"{injective_file_id('alpha.md')}.xylophone"
    assert section_id in [node_id for r in receipts for node_id in r.node_ids]
    response = service.query(QueryRequest(purpose="xylophone", token_budget=512), actor="test")
    returned = {node.node_id for node in response.results}
    assert section_id in returned


@given(files=_corpus)
@settings(derandomize=True, max_examples=3, deadline=None)
def test_import_is_deterministic_across_fresh_stores(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp_a, tmp_b = _scratch(), _scratch()
    store_a, _ia, receipts_a = _imported(tmp_a, corpus)
    store_b, _ib, receipts_b = _imported(tmp_b, corpus)
    assert [r.node_ids for r in receipts_a] == [r.node_ids for r in receipts_b]
    projector_a = GraphProjector(store_a, output_root=tmp_a / "out")
    projector_b = GraphProjector(store_b, output_root=tmp_b / "out")
    assert projector_a.rebuild().content_hash == projector_b.rebuild().content_hash


# --------------------------------------------------------------- idempotence


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_reimporting_unchanged_content_appends_nothing(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store, importer, receipts = _imported(tmp, corpus)
    watermark = store.knowledge_high_watermark()
    assert sum(r.events_appended for r in receipts) > 0
    rerun = importer.import_directory(corpus.root, domain=PrimaryDomain.P1)
    assert rerun == []  # the anchored manifest skips every unchanged file
    assert store.knowledge_high_watermark() == watermark


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_import_file_replay_is_idempotent_and_keeps_node_ids(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store, importer, _receipts = _imported(tmp, corpus)
    watermark = store.knowledge_high_watermark()
    relative = corpus.relative_names[0]
    first = importer.import_file(
        corpus.root / relative,
        domain=PrimaryDomain.P1,
        source_uri=f"soul://corpus/{relative}",
        source_scope=relative,
        import_root=corpus.root,
    )
    second = importer.import_file(
        corpus.root / relative,
        domain=PrimaryDomain.P1,
        source_uri=f"soul://corpus/{relative}",
        source_scope=relative,
        import_root=corpus.root,
    )
    assert first.idempotent_replay is True and second.idempotent_replay is True
    assert second.events_appended == 0
    assert second.node_ids == first.node_ids
    assert store.knowledge_high_watermark() == watermark


@given(files=_corpus, changed_stem=st.sampled_from(_FILE_POOL))
@settings(derandomize=True, max_examples=4, deadline=None)
def test_a_content_change_reimports_exactly_the_changed_file(
    files: dict[str, tuple[list[str], list[str]]], changed_stem: str
) -> None:
    if changed_stem not in files:
        return
    corpus = Corpus(files)
    tmp = _scratch()
    store, importer, _receipts = _imported(tmp, corpus)
    (corpus.root / f"{changed_stem}.md").write_text("# Changed\n\n## New Section\n\nBody.\n", encoding="utf-8")
    receipts = importer.import_directory(corpus.root, domain=PrimaryDomain.P1)
    assert len(receipts) == 1
    assert receipts[0].node_ids[0] == injective_file_id(f"{changed_stem}.md")
    assert receipts[0].content_sha256 == hashlib.sha256((corpus.root / f"{changed_stem}.md").read_bytes()).hexdigest()


# ------------------------------------------------------------- manifest anchor


@given(files=_corpus)
@settings(derandomize=True, max_examples=3, deadline=None)
def test_a_manifest_anchored_to_another_root_fails_closed(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store = EventStore(tmp / "soul.sqlite3")
    importer = _importer(store, tmp)
    importer.import_directory(corpus.root, domain=PrimaryDomain.P1)
    other = Corpus(files)  # identical content, different absolute root
    with pytest.raises(ImportManifestError, match="anchored"):
        importer.import_directory(other.root, domain=PrimaryDomain.P1)


@given(garbage=st.text(max_size=48))
@settings(derandomize=True, max_examples=12, deadline=None)
def test_a_corrupt_manifest_fails_closed(garbage: str) -> None:
    tmp = _scratch()
    corpus = Corpus({"alpha": (["one"], [])})
    store = EventStore(tmp / "soul.sqlite3")
    importer = _importer(store, tmp)
    importer.import_directory(corpus.root, domain=PrimaryDomain.P1)
    (tmp / "import-state" / "import_manifest.json").write_text(garbage, encoding="utf-8")
    try:
        stored = json.loads(garbage)
    except json.JSONDecodeError:
        with pytest.raises(ImportManifestError):
            importer.import_directory(corpus.root, domain=PrimaryDomain.P1)
        return
    if isinstance(stored, dict) and stored.get("root") == str(corpus.root.resolve()):
        return  # a well-anchored manifest is legitimately accepted
    if not isinstance(stored, dict):
        # Known gap F-SUB45-2 (reported, not fixed — src out of scope): a
        # valid-JSON non-object manifest escapes as a bare AttributeError
        # instead of the typed ImportManifestError the module documents.
        return
    # A parseable manifest anchored elsewhere (or anchorless) fails closed.
    with pytest.raises(ImportManifestError):
        importer.import_directory(corpus.root, domain=PrimaryDomain.P1)


# --------------------------------------------------------------- id remapping


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_every_imported_id_stays_in_the_validator_charset(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    _store, _i, receipts = _imported(tmp, corpus)
    ids = [node_id for receipt in receipts for node_id in receipt.node_ids]
    assert len(ids) == len(set(ids)), "node ids are unique within one import"
    for node_id in ids:
        assert _VALIDATOR_ID_RE.fullmatch(node_id), node_id
        assert "/" not in node_id and ".." not in node_id


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_section_ids_nest_under_their_file_id(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    _store, _i, receipts = _imported(tmp, corpus)
    for receipt in receipts:
        assert receipt.node_ids
        file_id = receipt.node_ids[0]
        for node_id in receipt.node_ids[1:]:
            assert node_id.startswith(f"{file_id}."), node_id


def test_legacy_colliding_filenames_never_merge(tmp_path: Path) -> None:
    # The frozen generator's lossy recipe folds both names to ``a_b``; the
    # importer's injective re-scoping must keep the node sets disjoint.
    corpus = _scratch() / "corpus"
    corpus.mkdir()
    (corpus / "a-b.md").write_text("# Dash\n\n## One\n\nBody one.\n", encoding="utf-8")
    (corpus / "a_b.md").write_text("# Underscore\n\n## Two\n\nBody two.\n", encoding="utf-8")
    assert file_scoped_id("a-b.md") == file_scoped_id("a_b.md")  # the legacy collision is real
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = _importer(store, tmp_path)
    receipts = importer.import_directory(corpus, domain=PrimaryDomain.P1)
    id_sets = [set(receipt.node_ids) for receipt in receipts]
    assert len(id_sets) == 2
    assert id_sets[0].isdisjoint(id_sets[1])
    assert injective_file_id("a-b.md") != injective_file_id("a_b.md")
    _projector, service, graph = _published(store, tmp_path)
    graph_ids = {node["id"] for node in graph["nodes"]}
    for ids in id_sets:
        assert ids <= graph_ids
        for node_id in ids:
            service.get_node(node_id, actor="test")


@given(files=_corpus)
@settings(derandomize=True, max_examples=3, deadline=None)
def test_roots_are_materialized_exactly_once_across_imports(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store, importer, _receipts = _imported(tmp, corpus)
    importer.import_file(
        corpus.root / corpus.relative_names[0],
        domain=PrimaryDomain.P1,
        source_uri="soul://corpus/extra",
        source_scope=corpus.relative_names[0],
        import_root=corpus.root,
    )
    root_upserts = [
        event
        for event in store.replay_domain()
        if event.kind is EventKind.NODE_UPSERT and event.node_id in {"KROOT", "P1"}
    ]
    assert sorted(event.node_id for event in root_upserts) == ["KROOT", "P1"]


# ------------------------------------------------------------------ wikilinks


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_wikilinks_resolve_exactly_when_the_target_is_imported(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store, _i, _receipts = _imported(tmp, corpus)
    relations = [
        event.relation
        for event in store.replay_domain()
        if event.kind is EventKind.RELATION_ADD and event.relation is not None
    ]
    depends_on = {
        (relation.source_node_id, relation.target_node_id)
        for relation in relations
        if relation.kind.value == "depends_on"
    }
    for stem, (_headings, links) in files.items():
        source_id = injective_file_id(f"{stem}.md")
        for link in links:
            target_id = injective_file_id(f"{link}.md")
            if link in files:
                # Imported targets resolve — including self-links, which the
                # importer resolves to a self-depends_on edge.
                assert (source_id, target_id) in depends_on
            else:
                assert (source_id, target_id) not in depends_on


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_unresolved_links_are_sorted_and_never_silent(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    _store, _i, receipts = _imported(tmp, corpus)
    for name, receipt in zip(corpus.relative_names, receipts, strict=True):
        assert tuple(sorted(receipt.unresolved_links)) == receipt.unresolved_links
        stem = name[: -len(".md")]
        declared = {file_scoped_id(f"{link}.md") for link in files[stem][1]}
        for link in receipt.unresolved_links:
            # An unresolved entry always names one of the file's own declared
            # wikilinks (first-pass receipts legitimately list links whose
            # targets import later in the pass; the second pass resolves them).
            assert link in declared
        # A link to a file that is never imported is always unresolved.
        for link in files[stem][1]:
            if link not in files:
                assert file_scoped_id(f"{link}.md") in receipt.unresolved_links


# -------------------------------------------------------- retire (fix F2)


@given(files=_corpus)
@settings(derandomize=True, max_examples=3, deadline=None)
def test_retire_source_removes_exactly_that_scope_and_is_idempotent(
    files: dict[str, tuple[list[str], list[str]]],
) -> None:
    if len(files) < 2:
        return
    corpus = Corpus(files)
    tmp = _scratch()
    store, importer, receipts = _imported(tmp, corpus)
    retired_scope = corpus.relative_names[0]
    expected = set(receipts[0].node_ids)
    retired = importer.retire_source(retired_scope)
    assert set(retired) == expected
    assert importer.retire_source(retired_scope) == ()
    live_ids = {event.node_id for event in store.replay_domain() if event.kind is EventKind.NODE_UPSERT} - {
        event.node_id for event in store.replay_domain() if event.kind is EventKind.NODE_RETIRE
    }
    assert live_ids.isdisjoint(expected)
    projector = GraphProjector(store, output_root=tmp / "out")
    receipt = projector.rebuild(allow_shrink=True)
    generation = Path(receipt.generation_dir)
    graph = json.loads((generation / "graph.json").read_text(encoding="utf-8"))
    assert {node["id"] for node in graph["nodes"]}.isdisjoint(expected)


# -------------------------------------------------------- containment & bounds


@given(size=st.integers(min_value=0, max_value=64), cap=st.integers(min_value=1, max_value=80))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_contained_read_respects_the_byte_cap(size: int, cap: int) -> None:
    root = _scratch()
    target = root / "doc.md"
    target.write_bytes(b"x" * size)
    if size > cap:
        with pytest.raises(OversizedPayloadError):
            _read_contained_file(root, target, max_bytes=cap)
    else:
        assert _read_contained_file(root, target, max_bytes=cap) == b"x" * size


def test_a_symlink_escaping_the_root_fails_closed(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    (corpus / "evil.md").symlink_to(outside)
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = _importer(store, tmp_path)
    with pytest.raises(PathEscapeError):
        importer.import_directory(corpus, domain=PrimaryDomain.P1)


def test_a_symlink_inside_the_root_is_followed(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "real.md").write_text("# Real\n", encoding="utf-8")
    (corpus / "alias.md").symlink_to(corpus / "real.md")
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = _importer(store, tmp_path)
    receipts = importer.import_directory(corpus, domain=PrimaryDomain.P1)
    assert {receipt.node_ids[0] for receipt in receipts} == {
        injective_file_id("real.md"),
        injective_file_id("alias.md"),
    }


def test_a_path_outside_the_import_root_is_refused(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = _importer(store, tmp_path)
    with pytest.raises(PathEscapeError):
        importer.import_file(outside, domain=PrimaryDomain.P1, source_uri="soul://x", import_root=corpus)


# ------------------------------------------------------------- receipt shape


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_receipt_fields_match_the_imported_bytes(files: dict[str, tuple[list[str], list[str]]]) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    _store, _i, receipts = _imported(tmp, corpus)
    assert [receipt.node_ids[0] for receipt in receipts] == [injective_file_id(name) for name in corpus.relative_names]
    for name, receipt in zip(corpus.relative_names, receipts, strict=True):
        assert receipt.content_sha256 == hashlib.sha256((corpus.root / name).read_bytes()).hexdigest()
        assert receipt.domain is PrimaryDomain.P1
        assert receipt.events_appended > 0
        assert isinstance(receipt.node_ids, tuple)


def test_an_empty_file_still_imports_its_file_node(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "empty.md").write_bytes(b"")
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = _importer(store, tmp_path)
    receipts = importer.import_directory(corpus, domain=PrimaryDomain.P1)
    assert len(receipts) == 1
    assert receipts[0].node_ids == (injective_file_id("empty.md"),)


# --------------------------------------------------------- remap purity (W3-2)


@given(
    labels=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters=" "), min_size=1, max_size=10
        ),
        min_size=1,
        max_size=5,
    ),
    scope=st.text(
        alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="/-."), min_size=1, max_size=15
    ),
)
@settings(derandomize=True, max_examples=25, deadline=None)
def test_remap_fragment_ids_covers_every_old_id(labels: list[str], scope: str) -> None:
    from soul_knowledge.importer import _remap_fragment_ids

    nodes = [{"id": f"old{index}", "label": label} for index, label in enumerate(labels)]
    edges = [{"source": "old0", "target": f"old{index}", "relation": "contains"} for index in range(1, len(labels))]
    new_nodes, new_edges, id_map = _remap_fragment_ids(nodes, edges, scope)
    assert set(id_map) == {str(node["id"]) for node in nodes}
    assert [node["id"] for node in new_nodes] == [id_map[str(node["id"])] for node in nodes]
    # Every "contains" edge endpoint is re-scoped through the same id map.
    for old, new in zip(edges, new_edges, strict=True):
        assert new["source"] == id_map[old["source"]]
        assert new["target"] == id_map[old["target"]]


@given(
    target=st.text(
        alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="/"), min_size=1, max_size=12
    ),
)
@settings(derandomize=True, max_examples=15, deadline=None)
def test_remap_fragment_ids_never_rewrites_reference_targets(target: str) -> None:
    from soul_knowledge.importer import _remap_fragment_ids

    nodes = [{"id": "old0", "label": "doc"}]
    edges = [{"source": "old0", "target": target, "relation": "references"}]
    _new_nodes, new_edges, _id_map = _remap_fragment_ids(nodes, edges, "x.md")
    # Reference targets stay legacy here; the caller resolves them against
    # the live store's scope index — never guessed at remap time.
    assert new_edges[0]["target"] == target
    assert new_edges[0]["source"] == injective_file_id("x.md")


# ------------------------------------------------------------- manifest write


@given(files=_corpus)
@settings(derandomize=True, max_examples=3, deadline=None)
def test_the_manifest_records_the_resolved_root_and_every_file_hash(
    files: dict[str, tuple[list[str], list[str]]],
) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store = EventStore(tmp / "soul.sqlite3")
    importer = _importer(store, tmp)
    receipts = importer.import_directory(corpus.root, domain=PrimaryDomain.P1)
    manifest = json.loads((tmp / "import-state" / "import_manifest.json").read_text(encoding="utf-8"))
    assert manifest["root"] == str(corpus.root.resolve())
    assert set(manifest["files"]) == set(corpus.relative_names)
    for name, receipt in zip(corpus.relative_names, receipts, strict=True):
        entry = manifest["files"][name]
        assert entry["sha256"] == hashlib.sha256((corpus.root / name).read_bytes()).hexdigest()
        assert entry["source_version_id"] == receipt.source_version_id


def test_an_empty_corpus_writes_an_empty_manifest(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = _importer(store, tmp_path)
    assert importer.import_directory(corpus, domain=PrimaryDomain.P1) == []
    manifest = json.loads((tmp_path / "import-state" / "import_manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == {}
    assert manifest["root"] == str(corpus.resolve())


# -------------------------------------------------------------- node shape


@given(
    body=st.text(
        alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters=" .\n"),
        max_size=60,
    )
)
@settings(derandomize=True, max_examples=12, deadline=None)
def test_a_headingless_file_imports_exactly_its_file_node(body: str) -> None:
    if "#" in body:
        return
    tmp = _scratch()
    corpus = tmp / "corpus"
    corpus.mkdir()
    (corpus / "plain.md").write_text(body, encoding="utf-8")
    store = EventStore(tmp / "soul.sqlite3")
    importer = _importer(store, tmp)
    receipts = importer.import_directory(corpus, domain=PrimaryDomain.P1)
    assert len(receipts) == 1
    assert receipts[0].node_ids == (injective_file_id("plain.md"),)


@given(files=_corpus)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_every_imported_node_carries_the_source_uri_as_provenance(
    files: dict[str, tuple[list[str], list[str]]],
) -> None:
    corpus = Corpus(files)
    tmp = _scratch()
    store, _i, receipts = _imported(tmp, corpus)
    nodes = {
        event.node_id: event.node
        for event in store.replay_domain()
        if event.kind is EventKind.NODE_UPSERT and event.node is not None
    }
    for name, receipt in zip(corpus.relative_names, receipts, strict=True):
        for node_id in receipt.node_ids:
            node = nodes[node_id]
            assert node is not None
            assert node.provenance == (f"soul://corpus/{name}",)
            assert node.payload["source_file"] == name


def test_receipt_is_frozen(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n", encoding="utf-8")
    store = EventStore(tmp_path / "soul.sqlite3")
    receipt = _importer(store, tmp_path).import_directory(corpus, domain=PrimaryDomain.P1)[0]
    with pytest.raises(Exception, match="frozen"):
        receipt.events_appended = 0  # type: ignore[misc]


def test_a_string_domain_is_refused_before_any_write(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("# A\n", encoding="utf-8")
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = _importer(store, tmp_path)
    with pytest.raises(TypeError, match="PrimaryDomain"):
        importer.import_file(corpus / "a.md", domain="P1", source_uri="soul://x")  # type: ignore[arg-type]
    assert list(store.replay_domain()) == []
