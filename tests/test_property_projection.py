"""Property tests for the projection: enrichment, caps, quarantine, rebuild.

Wave-2 follow-up to ``docs/recovery/codebase-push-property-tests.md``; pins
the projection contract (spec section 7.4, rulings "Opsi A" and W3-5) on
arbitrary inputs:

* ``_truncate_at_word_boundary`` never exceeds the cap, keeps short text
  byte-identical, and only ever hard-cuts a single over-cap word;
* ``enriched_summary`` is deterministic, bounded by ``max_length``, appends
  only whole fragments, and suppresses sentences that restate included leads;
* the publish caps are fail-closed and typed (``OversizedArtifactError``),
  checked before any byte reaches disk;
* secret quarantine is per-node, idempotent, carries no secret material, and
  a quarantined node never reaches an artifact;
* rebuilds are byte-deterministic, the manifest binds every artifact hash,
  unchanged artifacts keep their inode, and an older watermark can never
  replace a newer one (``StalePublicationError``).

All hypothesis runs use ``derandomize=True`` and no wall-clock deadline.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError as ValidationErrorOnFrozen

from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from know_your_soul.importer import injective_file_id, injective_section_key
from know_your_soul.projection import (
    ARTIFACT_NAMES,
    ENRICHED_SUMMARY_MAX_LENGTH,
    FragmentCache,
    GraphProjector,
    OversizedArtifactError,
    ShrinkGuardError,
    StalePublicationError,
    _clean_sentence,
    _section_blocks,
    _section_fragments,
    _truncate_at_word_boundary,
    enriched_summary,
)
from know_your_soul.store import EventStore

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _scratch(name: str) -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix=f"soul-{name}-"))


_WORD = st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=12)
_PROSE = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters=" ."),
    max_size=80,
)
_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_SECRET_TAIL = st.text(alphabet=st.sampled_from(_ALNUM), min_size=20, max_size=30)


def _node(node_id: str, summary: str = "summary", **overrides: Any) -> Node:
    fields: dict[str, Any] = {
        "node_id": node_id,
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "summary": summary,
        "provenance": ("soul://corpus/x.md",),
        "source_version_id": "sv-1",
        "created_at": NOW,
        "updated_at": NOW,
    }
    fields.update(overrides)
    return Node(**fields)


def _upsert(store: EventStore, node: Node, key: str | None = None) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key=key or f"upsert:{node.node_id}",
    )


def _projector(store: EventStore, tmp_path: Path, **kwargs: Any) -> GraphProjector:
    return GraphProjector(store, output_root=tmp_path / "out", **kwargs)


# ------------------------------------------------------- word-boundary cap


@given(text=st.text(max_size=120), cap=st.integers(min_value=1, max_value=140))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_truncate_never_exceeds_cap(text: str, cap: int) -> None:
    assert len(_truncate_at_word_boundary(text, cap)) <= cap


@given(text=st.text(max_size=120), cap=st.integers(min_value=1, max_value=200))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_truncate_is_identity_within_cap(text: str, cap: int) -> None:
    if len(text) <= cap:
        assert _truncate_at_word_boundary(text, cap) == text


@given(text=st.text(max_size=150), cap=st.integers(min_value=1, max_value=140))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_truncate_output_is_always_a_prefix(text: str, cap: int) -> None:
    out = _truncate_at_word_boundary(text, cap)
    assert text.startswith(out)


@given(text=st.text(max_size=150), cap=st.integers(min_value=1, max_value=140))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_truncate_cuts_only_at_a_space_unless_single_word(text: str, cap: int) -> None:
    out = _truncate_at_word_boundary(text, cap)
    if len(text) <= cap or out == text:
        return
    remainder = text[len(out) :]
    # Either the cut landed on a space (word boundary) or the cap window held
    # no usable space at all (the documented hard-cut fallback).
    assert remainder.startswith(" ") or " " not in text[: cap + 1] or text[: cap + 1].rfind(" ") == 0


@given(text=st.text(max_size=120), cap=st.integers(min_value=1, max_value=140))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_truncate_is_idempotent_at_the_same_cap(text: str, cap: int) -> None:
    once = _truncate_at_word_boundary(text, cap)
    assert _truncate_at_word_boundary(once, cap) == once


@given(word=_WORD, cap=st.integers(min_value=1, max_value=10))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_truncate_hard_cuts_a_single_over_cap_word(word: str, cap: int) -> None:
    out = _truncate_at_word_boundary(word, cap)
    assert len(out) <= cap
    assert word.startswith(out)


# ---------------------------------------------------------- enriched summary


def _fragments() -> st.SearchStrategy[tuple[list[str], list[str]]]:
    lead = st.lists(_WORD, min_size=0, max_size=4)
    sentences = st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters=" ."),
            min_size=1,
            max_size=60,
        ),
        min_size=0,
        max_size=4,
    )
    return st.tuples(lead, sentences)  # type: ignore[return-value]


@given(heading=_PROSE, fragments=_fragments(), cap=st.integers(min_value=8, max_value=300))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_enriched_summary_never_exceeds_max_length(
    heading: str, fragments: tuple[list[str], list[str]], cap: int
) -> None:
    assert len(enriched_summary(heading, fragments, max_length=cap)) <= cap


@given(heading=_PROSE, fragments=_fragments())
@settings(derandomize=True, max_examples=50, deadline=None)
def test_enriched_summary_is_deterministic(heading: str, fragments: tuple[list[str], list[str]]) -> None:
    assert enriched_summary(heading, fragments) == enriched_summary(heading, fragments)


@given(heading=_PROSE, cap=st.integers(min_value=8, max_value=300))
@settings(derandomize=True, max_examples=50, deadline=None)
def test_enriched_summary_without_fragments_is_the_truncated_heading(heading: str, cap: int) -> None:
    assert enriched_summary(heading, ([], []), max_length=cap) == _truncate_at_word_boundary(heading, cap)


@given(heading=_PROSE, fragments=_fragments())
@settings(derandomize=True, max_examples=50, deadline=None)
def test_enriched_summary_starts_with_the_truncated_heading(
    heading: str, fragments: tuple[list[str], list[str]]
) -> None:
    result = enriched_summary(heading, fragments)
    truncated = _truncate_at_word_boundary(heading, ENRICHED_SUMMARY_MAX_LENGTH)
    assert result == truncated or result.startswith(f"{truncated} — ")


@given(heading=_WORD, lead=_WORD)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_enriched_summary_appends_a_fitting_lead_verbatim(heading: str, lead: str) -> None:
    result = enriched_summary(heading, ([lead], []))
    if len(heading) + len(" — ") + len(lead) <= ENRICHED_SUMMARY_MAX_LENGTH:
        assert result == f"{heading} — {lead}"
    else:
        assert result == heading


@given(heading=_WORD, lead=_WORD, gloss=_WORD)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_enriched_summary_suppresses_a_sentence_restating_an_included_lead(heading: str, lead: str, gloss: str) -> None:
    sentence = f"{lead} {gloss}"
    result = enriched_summary(heading, ([lead], [sentence]))
    # The sentence starts with the included lead, so it is skipped: the body
    # is exactly the lead digest.
    if len(heading) + 3 + len(lead) <= ENRICHED_SUMMARY_MAX_LENGTH:
        assert result == f"{heading} — {lead}"


@given(heading=_WORD, first=_WORD, second=_WORD)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_enriched_summary_appends_at_most_the_first_prose_sentence(heading: str, first: str, second: str) -> None:
    result = enriched_summary(heading, ([], [first, second]))
    # At most the first prose sentence is appended; the second never joins.
    assert result in {heading, f"{heading} — {first}"}


@given(heading=_WORD, lead=st.text(alphabet="ab", min_size=300, max_size=320))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_enriched_summary_drops_a_fragment_that_would_overflow(heading: str, lead: str) -> None:
    # A fragment that cannot fit ends the fill: nothing partial is appended.
    assert enriched_summary(heading, ([lead], [])) == heading


@given(
    heading=_PROSE,
    fragments=_fragments(),
    cap=st.integers(min_value=1, max_value=7),
)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_enriched_summary_holds_under_tiny_caps(heading: str, fragments: tuple[list[str], list[str]], cap: int) -> None:
    result = enriched_summary(heading, fragments, max_length=cap)
    assert len(result) <= cap


# ------------------------------------------------------------- block parsing


@given(body=st.lists(_PROSE, max_size=10))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_fenced_content_never_contributes_a_block(body: list[str]) -> None:
    marker = "INSIDEFENCEMARKER"
    lines = ["```", marker, "```", *body]
    blocks = _section_blocks(lines)
    assert all(marker not in block for block in blocks)


@given(item=_WORD, rest=_PROSE)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_list_item_blocks_drop_the_marker(item: str, rest: str) -> None:
    blocks = _section_blocks([f"- {item} {rest}"])
    assert blocks
    assert not blocks[0].startswith("-")
    assert blocks[0].startswith(item)


@given(lead=_WORD, gloss=_PROSE)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_bold_lead_items_yield_their_term_as_lead(lead: str, gloss: str) -> None:
    leads, _sentences = _section_fragments([f"- **{lead}** — {gloss}"])
    assert lead in leads


@given(lines=st.lists(st.text(alphabet="ab \n", max_size=20), max_size=8))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_section_blocks_are_deterministic(lines: list[str]) -> None:
    assert _section_blocks(lines) == _section_blocks(lines)


def test_section_blocks_of_empty_input_is_empty() -> None:
    assert _section_blocks([]) == []
    assert _section_fragments([]) == ([], [])


# ------------------------------------------------------------ clean sentence


@given(text=st.text(max_size=200))
@settings(derandomize=True, max_examples=50, deadline=None)
def test_clean_sentence_strips_control_and_format_characters(text: str) -> None:
    cleaned = _clean_sentence(text)
    assert all(unicodedata.category(char) not in {"Cc", "Cf"} for char in cleaned)


@given(text=st.text(max_size=200))
@settings(derandomize=True, max_examples=50, deadline=None)
def test_clean_sentence_collapses_whitespace_runs(text: str) -> None:
    cleaned = _clean_sentence(text)
    assert "  " not in cleaned
    assert cleaned == cleaned.strip()


@given(text=st.text(max_size=200))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_clean_sentence_removes_emphasis_markers(text: str) -> None:
    cleaned = _clean_sentence(text)
    assert "**" not in cleaned and "__" not in cleaned and "`" not in cleaned


@given(tail=_SECRET_TAIL)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_clean_sentence_redacts_secret_shapes(tail: str) -> None:
    secret = f"ghp_{tail}"
    cleaned = _clean_sentence(f"note {secret} end")
    assert secret not in cleaned


@given(text=st.text(max_size=120))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_clean_sentence_is_deterministic(text: str) -> None:
    assert _clean_sentence(text) == _clean_sentence(text)


# ---------------------------------------------------------- secret quarantine


def _node_dict(node_id: str, summary: str = "plain summary") -> dict[str, Any]:
    return {
        "id": node_id,
        "label": summary or node_id,
        "file_type": "document",
        "source_file": "soul://corpus/x.md",
        "domain": "P1",
        "kind": "leaf",
        "provenance": ["soul://corpus/x.md"],
        "context_tags": ["P1"],
    }


@given(tail=_SECRET_TAIL, node_id=st.uuids().map(lambda u: f"n-{u.hex[:8]}"))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_quarantine_removes_a_node_whose_summary_carries_a_secret(tail: str, node_id: str) -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    secret = f"ghp_{tail}"
    node_dicts = {node_id: _node_dict(node_id, summary=f"token {secret} here"), "clean": _node_dict("clean")}
    quarantined = projector._quarantine_secret_nodes(node_dicts)  # noqa: SLF001
    assert node_id not in node_dicts
    assert "clean" in node_dicts
    assert [entry[0] for entry in quarantined] == sorted(entry[0] for entry in quarantined)
    assert any(entry[0] == node_id for entry in quarantined)


@given(tail=_SECRET_TAIL)
@settings(derandomize=True, max_examples=25, deadline=None)
def test_quarantine_receipt_never_carries_secret_material(tail: str) -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    secret = f"ghp_{tail}"
    node_dicts = {"n-1": _node_dict("n-1", summary=f"leak {secret}")}
    quarantined = projector._quarantine_secret_nodes(node_dicts)  # noqa: SLF001
    assert quarantined
    assert secret not in repr(quarantined)
    assert all(len(entry) == 3 for entry in quarantined)  # (node_id, field, category) only


@given(tail=_SECRET_TAIL)
@settings(derandomize=True, max_examples=20, deadline=None)
def test_quarantine_is_idempotent(tail: str) -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    node_dicts = {
        "n-1": _node_dict("n-1", summary=f"ghp_{tail}"),
        "n-2": _node_dict("n-2"),
    }
    first = projector._quarantine_secret_nodes(node_dicts)  # noqa: SLF001
    second = projector._quarantine_secret_nodes(node_dicts)  # noqa: SLF001
    assert len(first) == 1
    assert second == []
    assert list(node_dicts) == ["n-2"]


@given(summary=_PROSE)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_quarantine_never_touches_clean_prose(summary: str) -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    node_dicts = {"n-1": _node_dict("n-1", summary=summary)}
    assert projector._quarantine_secret_nodes(node_dicts) == []  # noqa: SLF001
    assert "n-1" in node_dicts


@given(
    head=st.text(alphabet=st.sampled_from(_ALNUM), min_size=10, max_size=12),
    tail=st.text(alphabet=st.sampled_from(_ALNUM), min_size=10, max_size=12),
)
@settings(derandomize=True, max_examples=20, deadline=None)
def test_quarantine_catches_a_secret_split_across_fields(head: str, tail: str) -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    node = _node_dict("n-1", summary=f"prefix ghp_{head}")
    node["context_tags"] = ["P1", f"{tail} suffix"]
    quarantined = projector._quarantine_secret_nodes(node_dicts={"n-1": node})  # noqa: SLF001
    assert [entry[0] for entry in quarantined] == ["n-1"]


@given(tail=_SECRET_TAIL)
@settings(derandomize=True, max_examples=20, deadline=None)
def test_quarantine_scans_nested_payload_values(tail: str) -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    node = _node_dict("n-1")
    node["extra"] = {"nested": [f"ghp_{tail}"]}
    quarantined = projector._quarantine_secret_nodes({"n-1": node})  # noqa: SLF001
    assert [entry[0] for entry in quarantined] == ["n-1"]


# ------------------------------------------------------- rebuild determinism


def _populated_store(specs: list[tuple[str, str]]) -> EventStore:
    store = EventStore()
    for node_id, summary in specs:
        _upsert(store, _node(node_id, summary))
    return store


@given(
    specs=st.lists(
        st.tuples(
            st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=8).map(
                lambda slug: f"n-{slug}"
            ),
            _PROSE,
        ),
        min_size=1,
        max_size=4,
        unique_by=lambda item: item[0],
    )
)
@settings(derandomize=True, max_examples=5, deadline=None)
def test_rebuild_is_byte_deterministic(specs: list[tuple[str, str]]) -> None:
    store = _populated_store(specs)
    projector = _projector(store, _scratch("out"))
    first = projector.rebuild()
    second = projector.rebuild()
    assert first.projection_version == second.projection_version
    assert first.content_hash == second.content_hash
    assert first.node_count == second.node_count == len(specs)
    for name in ARTIFACT_NAMES:
        assert (Path(first.generation_dir) / name).read_bytes() == (Path(second.generation_dir) / name).read_bytes()


@given(specs=st.lists(st.tuples(st.just("n-a"), _PROSE), min_size=1, max_size=1))
@settings(derandomize=True, max_examples=3, deadline=None)
def test_unchanged_artifacts_keep_their_inode(specs: list[tuple[str, str]]) -> None:
    store = _populated_store(specs)
    projector = _projector(store, _scratch("out"))
    first = projector.rebuild()
    inode_before = (Path(first.generation_dir) / "graph.json").stat().st_ino
    second = projector.rebuild()
    inode_after = (Path(second.generation_dir) / "graph.json").stat().st_ino
    assert first.generation_dir == second.generation_dir
    assert inode_before == inode_after


@given(
    specs=st.lists(
        st.tuples(
            st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=6).map(
                lambda slug: f"n-{slug}"
            ),
            _PROSE,
        ),
        min_size=1,
        max_size=3,
        unique_by=lambda item: item[0],
    )
)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_manifest_binds_every_artifact_hash(specs: list[tuple[str, str]]) -> None:
    store = _populated_store(specs)
    receipt = _projector(store, _scratch("out")).rebuild()
    generation = Path(receipt.generation_dir)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    # manifest.json is activated last and verified by canonical equality, so
    # it is not part of its own hash set.
    assert set(manifest["artifacts"]) == set(ARTIFACT_NAMES) - {"manifest.json"}
    for name, recorded in manifest["artifacts"].items():
        assert hashlib.sha256((generation / name).read_bytes()).hexdigest() == recorded
    assert manifest["content_hash"] == receipt.content_hash
    assert manifest["source_knowledge_high_watermark"] == store.knowledge_high_watermark()


@given(
    specs=st.lists(
        st.tuples(
            st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=6).map(
                lambda slug: f"n-{slug}"
            ),
            _PROSE,
        ),
        min_size=1,
        max_size=3,
        unique_by=lambda item: item[0],
    )
)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_manifest_counts_match_the_published_graph(specs: list[tuple[str, str]]) -> None:
    store = _populated_store(specs)
    receipt = _projector(store, _scratch("out")).rebuild()
    generation = Path(receipt.generation_dir)
    graph = json.loads((generation / "graph.json").read_text(encoding="utf-8"))
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["node_count"] == len(graph["nodes"]) == receipt.node_count
    assert manifest["edge_count"] == len(graph["links"]) == receipt.edge_count
    assert receipt.source_knowledge_high_watermark == store.knowledge_high_watermark()


@given(
    specs=st.lists(
        st.tuples(
            st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd")), min_size=1, max_size=6).map(
                lambda slug: f"n-{slug}"
            ),
            _PROSE,
        ),
        min_size=1,
        max_size=3,
        unique_by=lambda item: item[0],
    )
)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_fixed_watermark_rebuild_matches_a_fresh_store(specs: list[tuple[str, str]]) -> None:
    tmp_path = _scratch("fixed")
    store_a = _populated_store(specs)
    store_b = _populated_store(specs)
    watermark = store_a.knowledge_high_watermark()
    receipt_a = GraphProjector(store_a, output_root=tmp_path / "a").rebuild(upto_knowledge_seq=watermark)
    receipt_b = GraphProjector(store_b, output_root=tmp_path / "b").rebuild(upto_knowledge_seq=watermark)
    assert receipt_a.content_hash == receipt_b.content_hash
    assert receipt_a.projection_version == receipt_b.projection_version


# ------------------------------------------------------- activation ordering


def test_an_older_watermark_can_never_replace_a_newer_one(tmp_path: Path) -> None:
    store = _populated_store([("n-a", "alpha"), ("n-b", "beta")])
    projector = _projector(store, tmp_path)
    projector.rebuild()
    with pytest.raises(StalePublicationError):
        projector.rebuild(upto_knowledge_seq=1, allow_shrink=True)
    # The newer generation is still the one being served.
    manifest = projector.active_manifest()
    assert manifest is not None
    assert manifest["node_count"] == 2


def test_repair_is_a_no_op_when_current(tmp_path: Path) -> None:
    store = _populated_store([("n-a", "alpha")])
    projector = _projector(store, tmp_path)
    projector.rebuild()
    receipt = projector.repair_if_stale()
    assert receipt.repaired is False
    assert receipt.activated is False


def test_repair_recovers_a_torn_pointer(tmp_path: Path) -> None:
    store = _populated_store([("n-a", "alpha")])
    projector = _projector(store, tmp_path)
    original = projector.rebuild()
    (tmp_path / "out" / "CURRENT").write_text("0" * 64 + "\n", encoding="utf-8")
    assert projector.active_generation_dir() is None
    receipt = projector.repair_if_stale()
    assert receipt.repaired is True
    assert receipt.content_hash == original.content_hash
    assert projector.active_manifest() is not None


def test_shrink_guard_blocks_silent_node_loss(tmp_path: Path) -> None:
    store = _populated_store([("n-a", "alpha"), ("n-b", "beta")])
    projector = _projector(store, tmp_path)
    projector.rebuild()
    store.append_domain(
        Event(
            kind=EventKind.NODE_RETIRE,
            node_id="n-b",
            domain=PrimaryDomain.P1,
            occurred_at=NOW,
        ),
        idempotency_key="retire:n-b",
    )
    with pytest.raises(ShrinkGuardError):
        projector.rebuild()
    receipt = projector.rebuild(allow_shrink=True)
    assert receipt.node_count == 1


# ------------------------------------------------------------------ publish caps


@given(bad=st.integers(min_value=-100, max_value=0))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_nonpositive_artifact_cap_is_refused_at_construction(bad: int) -> None:
    with pytest.raises(ValueError, match="max_artifact_bytes"):
        _projector(EventStore(), _scratch("out"), max_artifact_bytes=bad)


@given(cap=st.integers(min_value=1, max_value=64))
@settings(derandomize=True, max_examples=4, deadline=None)
def test_tiny_artifact_cap_fails_closed_before_any_write(cap: int) -> None:
    tmp_path = _scratch("out")
    store = _populated_store([("n-a", "alpha")])
    projector = _projector(store, tmp_path, max_artifact_bytes=cap)
    with pytest.raises(OversizedArtifactError):
        projector.rebuild()
    assert not (tmp_path / "out" / "CURRENT").exists()


@given(tail=_SECRET_TAIL)
@settings(derandomize=True, max_examples=3, deadline=None)
def test_quarantined_node_never_reaches_an_artifact(tail: str) -> None:
    tmp_path = _scratch("out")
    store = EventStore()
    _upsert(store, _node("n-clean", "clean summary"))
    _upsert(store, _node("n-poison", f"token ghp_{tail} inside"))
    projector = _projector(store, tmp_path)
    receipt = projector.rebuild()
    graph = json.loads((Path(receipt.generation_dir) / "graph.json").read_text(encoding="utf-8"))
    ids = {node["id"] for node in graph["nodes"]}
    assert "n-clean" in ids
    assert "n-poison" not in ids
    assert f"ghp_{tail}" not in json.dumps(graph)
    # The quarantine is a typed, hash-chained audit event naming only
    # ids/fields/categories — never secret material.
    with store.read() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM event_log WHERE event_type = 'security.projection_quarantine'"
        ).fetchall()
    assert len(rows) == 1
    payload = json.loads(str(rows[0]["payload_json"]))
    assert payload["quarantined"][0]["node_id"] == "n-poison"
    assert f"ghp_{tail}" not in str(rows[0]["payload_json"])


# ------------------------------------------------------------- fragment cache


@given(
    rel=st.text(
        alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="/."),
        min_size=1,
        max_size=20,
    ),
    content=st.binary(max_size=256),
)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_fragment_cache_key_is_pure_content_and_path(rel: str, content: bytes) -> None:
    cache_a = FragmentCache(_scratch("cache-a"))
    cache_b = FragmentCache(_scratch("cache-b"))
    assert cache_a.key_for(rel, content) == cache_b.key_for(rel, content)


@given(fragment=st.dictionaries(keys=st.text(alphabet="abc", min_size=1, max_size=6), values=st.integers(), max_size=5))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_fragment_cache_put_get_round_trip(fragment: dict[str, int]) -> None:
    cache = FragmentCache(_scratch("cache"))
    key = cache.key_for("doc.md", b"content")
    cache.put(key, fragment)
    assert cache.get(key) == fragment
    assert cache.entries() == [key]


@given(
    keys=st.lists(st.text(alphabet="abcdef", min_size=1, max_size=8), min_size=0, max_size=6, unique=True),
    live=st.lists(st.text(alphabet="abcdef", min_size=1, max_size=8), max_size=6, unique=True),
)
@settings(derandomize=True, max_examples=25, deadline=None)
def test_fragment_cache_prune_keeps_exactly_the_live_set(keys: list[str], live: list[str]) -> None:
    cache = FragmentCache(_scratch("cache"))
    for key in keys:
        cache.put(key, {"k": key})
    removed = cache.prune(live)
    assert removed == len(set(keys) - set(live))
    assert cache.entries() == sorted(set(keys) & set(live))
    # Pruning twice removes nothing more.
    assert cache.prune(live) == 0


def test_fragment_cache_get_of_missing_or_corrupt_entry_is_none(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path)
    assert cache.get("0" * 64) is None
    key = cache.key_for("doc.md", b"bytes")
    cache.put(key, {"ok": 1})
    (tmp_path / cache.namespace / f"{key}.json").write_text("not json", encoding="utf-8")
    assert cache.get(key) is None


def test_manifest_is_never_in_its_own_hash_set() -> None:
    # Pinned constant wiring: the hashed set is every artifact except the last.
    assert ARTIFACT_NAMES[-1] == "manifest.json"


# ------------------------------------------------------------- pointer hygiene


@given(name=st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="/.-"), max_size=80))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_active_generation_dir_rejects_malformed_pointers(name: str) -> None:
    tmp_path = _scratch("out")
    (tmp_path / "out").mkdir(parents=True, exist_ok=True)
    (tmp_path / "out" / "CURRENT").write_text(name, encoding="utf-8")
    projector = GraphProjector(EventStore(), output_root=tmp_path / "out")
    valid = len(name) == 64 and all(char in "0123456789abcdef" for char in name)
    if not valid:
        assert projector.active_generation_dir() is None
        assert projector.active_manifest() is None


def test_projection_receipt_is_frozen() -> None:
    store = _populated_store([("n-a", "alpha")])
    receipt = _projector(store, _scratch("out")).rebuild()
    with pytest.raises(ValidationErrorOnFrozen):
        receipt.node_count = 99  # type: ignore[misc]


@given(values=st.lists(_PROSE, min_size=1, max_size=6))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_sanitize_entry_collapses_every_whitespace_run(values: list[str]) -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    for value in values:
        cleaned = projector._sanitize_entry(value, "n-1", "summary")  # noqa: SLF001
        assert "\t" not in cleaned and "\n" not in cleaned and "\r" not in cleaned
        assert "  " not in cleaned
        assert cleaned == cleaned.strip()


@given(
    affecting=st.lists(st.booleans(), min_size=1, max_size=8),
)
@settings(derandomize=True, max_examples=15, deadline=None)
def test_knowledge_events_respect_the_watermark(affecting: list[bool]) -> None:
    store = EventStore()
    for index, is_affecting in enumerate(affecting):
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id=f"n-{index}",
                domain=PrimaryDomain.P1,
                node=_node(f"n-{index}", "s"),
                occurred_at=NOW,
                knowledge_affecting=is_affecting,
            ),
            idempotency_key=f"ev:{index}",
        )
    projector = GraphProjector(store, output_root="unused")
    events = projector._knowledge_events(store.knowledge_high_watermark())  # noqa: SLF001
    # Only knowledge-affecting events replay, and they are exactly the
    # affecting upserts in append order.
    assert len(events) == sum(affecting)
    assert [event.node_id for event in events] == [f"n-{index}" for index, flag in enumerate(affecting) if flag]


@given(
    headings=st.lists(
        st.text(
            alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters=" "), min_size=1, max_size=12
        ),
        min_size=1,
        max_size=5,
    )
)
@settings(derandomize=True, max_examples=20, deadline=None)
def test_source_section_fragments_replicate_the_importer_id_walk(headings: list[str]) -> None:
    from know_your_soul.projection import _HEADING_RE, _source_section_fragments

    lines = ["# Title", ""]
    for heading in headings:
        lines += [f"## {heading}", "", "Body.", ""]
    text = "\n".join(lines)
    fragments = _source_section_fragments(text, "doc.md")
    file_id = injective_file_id("doc.md")
    expected_ids = {file_id}
    used: dict[str, int] = {}
    # The importer/projection walk keys sections by the regex-captured
    # heading text (outer whitespace is stripped by ``_HEADING_RE``), so
    # replicate the capture exactly instead of reusing the raw strategy
    # value (a heading like "a " projects under the id of "a").
    for line in lines:
        match = _HEADING_RE.match(line)
        if match is None or len(match.group(1)) != 2:
            continue
        key = injective_section_key(match.group(2))
        occurrence = used.get(key, 0) + 1
        used[key] = occurrence
        expected_ids.add(f"{file_id}.{key}" if occurrence == 1 else f"{file_id}.{key}-n{occurrence}")
    assert set(fragments) == expected_ids


@given(
    first=st.dictionaries(keys=st.text(alphabet="ab", min_size=1, max_size=4), values=st.integers(), max_size=3),
    second=st.dictionaries(keys=st.text(alphabet="ab", min_size=1, max_size=4), values=st.integers(), max_size=3),
)
@settings(derandomize=True, max_examples=15, deadline=None)
def test_fragment_cache_put_overwrites_in_place(first: dict[str, int], second: dict[str, int]) -> None:
    cache = FragmentCache(_scratch("cache"))
    key = cache.key_for("doc.md", b"bytes")
    cache.put(key, first)
    cache.put(key, second)
    assert cache.get(key) == second
    assert cache.entries() == [key]


def test_artifact_names_are_the_pinned_set() -> None:
    assert ARTIFACT_NAMES == (
        "graph.json",
        "GRAPH_REPORT.md",
        "analysis.json",
        "labels.json",
        "signatures.json",
        "graph.html",
        "manifest.json",
    )


@given(fragments=_fragments())
@settings(derandomize=True, max_examples=20, deadline=None)
def test_enriched_summary_default_cap_is_the_published_limit(
    fragments: tuple[list[str], list[str]],
) -> None:
    assert len(enriched_summary("heading", fragments)) <= ENRICHED_SUMMARY_MAX_LENGTH == 256


@given(
    tails=st.lists(_SECRET_TAIL, min_size=1, max_size=3),
)
@settings(derandomize=True, max_examples=10, deadline=None)
def test_quarantine_reports_sorted_node_ids(tails: list[str]) -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    node_dicts = {f"n-{index}": _node_dict(f"n-{index}", summary=f"ghp_{tail}") for index, tail in enumerate(tails)}
    quarantined = projector._quarantine_secret_nodes(node_dicts)  # noqa: SLF001
    assert [entry[0] for entry in quarantined] == sorted(f"n-{index}" for index in range(len(tails)))
    assert node_dicts == {}


def test_joined_secret_category_needs_at_least_two_fields() -> None:
    projector = GraphProjector(EventStore(), output_root="unused")
    assert projector._joined_secret_category([]) is None  # noqa: SLF001
    assert projector._joined_secret_category(["ghp_" + "a" * 24]) is None  # noqa: SLF001


@given(specs=st.lists(st.tuples(st.just("n-a"), _PROSE), min_size=1, max_size=1))
@settings(derandomize=True, max_examples=2, deadline=None)
def test_manifest_records_the_pinned_build_identity(specs: list[tuple[str, str]]) -> None:
    from know_your_soul.graphify_adapter import ADAPTER_VERSION, GRAPHIFY_PARTITIONER, PINNED_HUB_POLICY

    store = _populated_store(specs)
    receipt = _projector(store, _scratch("out")).rebuild()
    manifest = json.loads((Path(receipt.generation_dir) / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["partitioner"] == GRAPHIFY_PARTITIONER
    assert manifest["hub_policy"] == PINNED_HUB_POLICY
    assert manifest["adapter_version"] == ADAPTER_VERSION
    assert manifest["manifest_version"] == 1
    assert manifest["schema_version"] == 1
    assert manifest["multigraph"] is False
