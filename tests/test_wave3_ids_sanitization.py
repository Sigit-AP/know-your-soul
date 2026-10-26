"""Wave-3 audit fixes (SUB-33): injective file-scoped ids, sanitize/redact
ordering and strength, publish-side label caps, generation-dir parent fsync.

* W3-2: ``file_scoped_id`` (Graphify ``make_id`` recipe) collapses ``-``,
  spaces, case and path separators, so ``a-b.md`` / ``a_b.md`` / ``a b.md``
  and ``a/b_c.md`` / ``a_b/c.md`` silently merged into one node set at
  import. The importer now re-scopes every fragment onto
  :func:`injective_file_id`, a reversible escape encoding that provably never
  maps two distinct (normalized) source paths to the same id.
* W3-3: ``projection._clean_sentence`` redacted *before* stripping zero-width
  characters, reconstituting secret shapes into published artifacts. The
  order is now normalize/strip first, redact last.
* W3-4: ``redact_text`` is bypassed by ZWSP-split, fullwidth and homoglyph
  obfuscation and covers no high-entropy blob shape. It now matches against
  an NFKC + zero-width-stripped + confusable-folded view and redacts
  high-entropy base64-ish runs, while clean input stays byte-identical.
* W3-5: published labels are capped at ``ENRICHED_SUMMARY_MAX_LENGTH`` on the
  write side, at a word boundary, so the read-side 256-char limit can never
  cut a term mid-word.
* W3-6: raw newlines/tabs in store summaries are collapsed to single spaces
  at publish time, so read surfaces never fuse words.
* Durability (SUB-29 side finding): creating a new generation directory now
  fsyncs the parent ``generations/`` directory.
"""

from __future__ import annotations

import json
import os
import random
import string
from pathlib import Path

from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from know_your_soul.fragment_generator import file_scoped_id
from know_your_soul.importer import SeedImporter, injective_file_id, injective_section_key
from know_your_soul.projection import (
    ENRICHED_SUMMARY_MAX_LENGTH,
    GraphProjector,
    _clean_sentence,
    enriched_summary,
)
from know_your_soul.security import SECRET_PATTERNS, redact_text
from know_your_soul.store import EventStore

# --------------------------------------------------------------------- W3-2

COLLIDING_PAIRS: tuple[tuple[str, str], ...] = (
    ("a-b.md", "a_b.md"),
    ("a-b.md", "a b.md"),
    ("a_b.md", "a b.md"),
    ("a/b_c.md", "a_b/c.md"),
    ("10-Q Report.md", "10_q_report.md"),
    ("Alpha.md", "alpha.md"),
)


def test_injective_file_id_separates_every_wave3_collision_pair() -> None:
    for left, right in COLLIDING_PAIRS:
        # The old scheme provably merges the pair (the W3-2 defect)...
        assert file_scoped_id(left) == file_scoped_id(right), (left, right)
        # ...and the injective scheme provably separates it.
        assert injective_file_id(left) != injective_file_id(right), (left, right)


def test_injective_file_id_is_deterministic_and_keeps_pure_names_natural() -> None:
    assert injective_file_id("alpha.md") == "alpha"
    assert injective_file_id("docs/alpha.md") == "docs:alpha"
    assert injective_file_id("a-b.md") == "a-2d-b"
    assert injective_file_id("a b.md") == "a-20-b"
    assert injective_file_id("a/b_c.md") == "a:b_c"
    assert injective_file_id("a_b/c.md") == "a_b:c"
    probe = "docs/Alpha Plan.md"
    assert injective_file_id(probe) == injective_file_id(probe)
    assert injective_file_id("..\\..\\a-b.md") == injective_file_id("a-b.md")


def test_injective_file_id_random_distinct_paths_never_collide() -> None:
    """Property probe-as-test: 20k seeded random paths, zero collisions.

    Injectivity holds modulo the declared path normalization (separator
    style, ``..`` collapse, final-suffix drop); this generator never emits
    those equivalences, so distinct paths must yield distinct ids.
    """
    rng = random.Random(20261025)
    alphabet = string.ascii_letters + string.digits + "-_ .👁\u200b"
    seen: dict[str, str] = {}
    collisions: list[tuple[str, str, str]] = []
    samples = 0
    while samples < 20000:
        depth = rng.randint(0, 2)
        segments = ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 8))) for _ in range(depth)]
        name = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))
        path = "/".join([*segments, f"{name}.md"])
        if ".." in path or any(segment in ("", ".") for segment in path.split("/")):
            continue  # declared normalizations (".." collapse, "." segments) are equivalences
        samples += 1
        node_id = injective_file_id(path)
        previous = seen.setdefault(node_id, path)
        if previous != path:
            collisions.append((node_id, previous, path))
    assert collisions == [], collisions[:3]


def _imported_graph(tmp_path: Path, files: dict[str, bytes]) -> tuple[EventStore, dict[str, dict]]:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name, content in files.items():
        target = corpus / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    receipt = GraphProjector(store, output_root=tmp_path / "out").rebuild()
    graph = json.loads((Path(receipt.generation_dir) / "graph.json").read_text(encoding="utf-8"))
    return store, {str(node["id"]): node for node in graph["nodes"]}


def test_colliding_corpus_files_no_longer_merge_end_to_end(tmp_path: Path) -> None:
    """The W3-2 probe shape: ``alpha-one.md`` + ``alpha_one.md`` in one corpus."""
    store, nodes = _imported_graph(
        tmp_path,
        {
            "alpha-one.md": b"# Alpha One\n\n## Section A\n\nContent of file ONE.\n",
            "alpha_one.md": b"# Alpha Two\n\n## Section B\n\nContent of the SECOND file only.\n",
        },
    )
    id_one = injective_file_id("alpha-one.md")
    id_two = injective_file_id("alpha_one.md")
    assert id_one != id_two
    assert id_one in nodes and id_two in nodes
    # Each file node keeps its OWN title (the merge used to let the second win).
    assert str(nodes[id_one]["label"]).startswith("Alpha One")
    assert str(nodes[id_two]["label"]).startswith("Alpha Two")
    # Both sections survive, under their own file, enriched from their own body.
    section_a = f"{id_one}.{injective_section_key('Section A')}"
    section_b = f"{id_two}.{injective_section_key('Section B')}"
    assert section_a in nodes and section_b in nodes
    assert "Content of file ONE." in str(nodes[section_a]["label"])
    assert "Content of the SECOND file only." in str(nodes[section_b]["label"])
    assert store.current_node(id_one) is not None
    assert store.current_node(id_two) is not None


def test_wikilinks_resolve_to_injective_target_ids(tmp_path: Path) -> None:
    store, _ = _imported_graph(
        tmp_path,
        {
            "alpha-one.md": b"# Alpha One\n\nSee [[Beta Two]] and [[ghost-file]] here.\n",
            "Beta Two.md": b"# Beta Two\n\nThe target body.\n",
        },
    )
    from know_your_soul.domain import RelationKind

    source_id = injective_file_id("alpha-one.md")
    target_id = injective_file_id("Beta Two.md")
    relations = store.relations_from(source_id)
    assert any(r.kind is RelationKind.DEPENDS_ON and r.target_node_id == target_id for r in relations)


def test_importer_and_projection_derive_identical_section_ids(tmp_path: Path) -> None:
    """Tricky headings (differing only by `-`/`_`, duplicated) keep parity."""
    content = (
        b"# Tricky\n\n## A-B\n\nFirst body sentence here.\n\n"
        b"## A_B\n\nSecond body sentence here.\n\n## A-B\n\nThird occurrence body.\n"
    )
    store, nodes = _imported_graph(tmp_path, {"tricky.md": content})
    file_id = injective_file_id("tricky.md")
    key = injective_section_key("A-B")
    expected = (
        f"{file_id}.{key}",
        f"{file_id}.{injective_section_key('A_B')}",
        f"{file_id}.{key}-n2",
    )
    for section_id in expected:
        assert section_id in nodes, section_id
    # Enrichment matched the same ids (proof the two id walks agree).
    assert "First body sentence here." in str(nodes[expected[0]]["label"])
    assert "Second body sentence here." in str(nodes[expected[1]]["label"])
    assert "Third occurrence body." in str(nodes[expected[2]]["label"])
    assert store.current_node(expected[2]) is not None


# --------------------------------------------------------------------- W3-3


def test_clean_sentence_strips_zero_width_before_redacting() -> None:
    """W3-3: redact-then-strip reconstituted the exact AWS key shape."""
    poisoned = "AKIA\u200bIOSF\u200cODNN7EXAMPLE"
    cleaned = _clean_sentence(poisoned)
    for _name, pattern in SECRET_PATTERNS:
        assert pattern.search(cleaned) is None
    assert "[REDACTED" in cleaned


def test_clean_sentence_redacts_last_so_emphasis_cannot_hide_secrets() -> None:
    cleaned = _clean_sentence("pass**word**: hunter2secret")
    assert "hunter2secret" not in cleaned
    assert "[REDACTED" in cleaned


# --------------------------------------------------------------------- W3-4


def test_redact_text_catches_zero_width_split_secret() -> None:
    redacted = redact_text("AKIA\u200bIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted
    assert "[REDACTED" in redacted


def test_redact_text_catches_tab_split_and_zwsp_bearer() -> None:
    assert "AKIAIOSFODNN7EXAMPLE" not in redact_text("AKIA\tIOSFODNN7EXAMPLE")
    redacted = redact_text("bearer abcdef\u200bghijklmnpqrstuvwxyz")
    assert "abcdefghijklmnpqrstuvwxyz" not in redacted


def test_redact_text_catches_fullwidth_credential_separator() -> None:
    redacted = redact_text("password：hunter2secret")  # fullwidth colon
    assert "hunter2secret" not in redacted
    assert "[REDACTED" in redacted


def test_redact_text_catches_homoglyph_credential_assignment() -> None:
    # Cyrillic а (U+0430) inside the credential key — the wave-3 end-to-end bypass.
    redacted = redact_text("pаssword: hunter2secret")
    assert "hunter2secret" not in redacted
    assert "[REDACTED" in redacted


def test_redact_text_redacts_high_entropy_blobs() -> None:
    blob = "dGhpcyBpcyBhIGJhc2U2NCBlbmNvZGVkIHNlY3JldCB0b2tlbiBmb3IgdGVzdHM="
    redacted = redact_text(f"the blob {blob} appeared")
    assert blob not in redacted
    assert "[REDACTED" in redacted


def test_redact_text_leaves_hashes_uuids_and_prose_alone() -> None:
    """Two-way guard: legitimate hash-shaped content is not redacted."""
    sha256 = "2d38d30862f3629710781ea5384f19215f9b55d3dcf7ac22f01af92034fc0f7d"
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    prose = "café Übermensch — plain prose with fullwidth ＡＢＣ and emoji 🎉"
    assert redact_text(f"commit {sha256} is pinned") == f"commit {sha256} is pinned"
    assert redact_text(f"id {uuid}") == f"id {uuid}"
    assert redact_text(prose) == prose


# --------------------------------------------------------------------- W3-5


def _append_node(store: EventStore, node: Node, key: str) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
            actor="test",
        ),
        idempotency_key=key,
    )


def _published_labels(tmp_path: Path, store: EventStore) -> dict[str, str]:
    receipt = GraphProjector(store, output_root=tmp_path / "out").rebuild()
    graph = json.loads((Path(receipt.generation_dir) / "graph.json").read_text(encoding="utf-8"))
    return {str(node["id"]): str(node["label"]) for node in graph["nodes"]}


def test_enriched_summary_caps_oversized_heading_at_word_boundary() -> None:
    heading = " ".join(f"w{i:03d}" for i in range(80))
    result = enriched_summary(heading, ([], []))
    assert len(result) <= ENRICHED_SUMMARY_MAX_LENGTH
    assert result == heading[: ENRICHED_SUMMARY_MAX_LENGTH + 1].rsplit(" ", 1)[0]
    assert not result.endswith("w0")  # never mid-word


def test_publish_caps_long_store_summaries_at_word_boundary(tmp_path: Path) -> None:
    """W3-5: a 645-char store summary published verbatim, then cut mid-word."""
    long_summary = " ".join(f"tok{i:03d}" for i in range(80)) + " ZEBRATAILMARKER"
    assert len(long_summary) > ENRICHED_SUMMARY_MAX_LENGTH
    store = EventStore(tmp_path / "soul.sqlite3")
    _append_node(
        store,
        Node(
            node_id="longnode",
            kind=NodeKind.LEAF,
            domain=PrimaryDomain.P1,
            summary=long_summary,
            payload={"source_file": "soul://ops"},
            provenance=("soul://ops",),
            source_version_id="sv-test",
        ),
        "k-long",
    )
    label = _published_labels(tmp_path, store)["longnode"]
    assert len(label) <= ENRICHED_SUMMARY_MAX_LENGTH
    expected = long_summary[: ENRICHED_SUMMARY_MAX_LENGTH + 1].rsplit(" ", 1)[0]
    assert label == expected
    assert label.endswith("tok035")  # whole words only; no mid-token cut


# --------------------------------------------------------------------- W3-6


def test_publish_collapses_raw_newlines_and_tabs_in_summaries(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    _append_node(
        store,
        Node(
            node_id="multilines",
            kind=NodeKind.LEAF,
            domain=PrimaryDomain.P1,
            summary="Line one\n# Injected Heading\nLine three\twith tab",
            payload={"source_file": "soul://ops"},
            provenance=("soul://ops",),
            source_version_id="sv-test",
        ),
        "k-multi",
    )
    label = _published_labels(tmp_path, store)["multilines"]
    assert "\n" not in label and "\t" not in label and "\r" not in label
    assert "Line one # Injected Heading Line three with tab" in label


# ------------------------------------------------------- durability (SUB-29)


def test_new_generation_dir_parent_is_fsynced(tmp_path: Path, monkeypatch) -> None:
    """A crash after writing a new generation must see the dir entry persist."""
    fsynced: list[str] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        try:
            fsynced.append(os.readlink(f"/proc/self/fd/{fd}"))
        except OSError:
            fsynced.append(str(fd))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy)
    _store, _nodes = _imported_graph(tmp_path, {"alpha.md": b"# Alpha\n\nBody text.\n"})
    generations_dir = str(tmp_path / "out" / "generations")
    assert generations_dir in fsynced, fsynced
