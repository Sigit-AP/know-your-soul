"""Contract tests for the deterministic fragment generator and Graphify schema parity.

Task 3 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
matrix rows 1, 2, 3 and 16 (``docs/research/graphify-feature-matrix.md``).

The generator replaces model-backed semantic extraction: it parses markdown
structure deterministically and emits Graphify's node/edge schema with
file-scoped ids. Every fragment passes Graphify's own
``validate_semantic_fragment`` / ``sanitize_semantic_fragment`` /
``validate_extraction`` before it may touch the graph.
"""

from __future__ import annotations

import json
import sys

import pytest

from soul_knowledge.fragment_generator import (
    FRAGMENT_GENERATOR_VERSION,
    PINNED_GRAMMARS,
    FragmentGenerator,
    file_scoped_id,
)
from soul_knowledge.graphify_adapter import ExtractionFragment, FragmentValidationError, GraphifyAdapter

MARKDOWN = (
    b"# Alpha Lessons\n"
    b"\n"
    b"## First Lesson\n"
    b"\n"
    b"Always roll the database back before retrying.\n"
    b"\n"
    b"## Second Lesson\n"
    b"\n"
    b"See [[beta]] and [[ghost]] for the counterexample.\n"
)


def test_pinned_grammar_set_imports() -> None:
    """Row 1: exactly the markdown grammar is pinned and importable."""
    assert PINNED_GRAMMARS == ("markdown",)
    import importlib

    module = importlib.import_module("graphify.extractors.markdown")
    assert module is not None
    # The generator itself pulls in no grammar extractor: it parses markdown
    # structure directly and borrows only Graphify's id recipe.
    import soul_knowledge.fragment_generator as generator_module

    assert not any(name.startswith("graphify.extractors") for name in vars(generator_module))


def test_markdown_structure_nodes() -> None:
    """Row 2: markdown structure becomes a file node plus heading nodes."""
    fragment = FragmentGenerator().generate(source_file="alpha.md", content=MARKDOWN)
    file_id = file_scoped_id("alpha.md")
    ids = [node["id"] for node in fragment.nodes]
    assert ids[0] == file_id
    first, second = f"{file_id}_first_lesson", f"{file_id}_second_lesson"
    assert first in ids and second in ids
    file_node = fragment.nodes[0]
    assert file_node["label"] == "Alpha Lessons"
    assert file_node["file_type"] == "document"
    assert file_node["source_file"] == "alpha.md"
    edges = {(edge["source"], edge["target"], edge["relation"]) for edge in fragment.edges}
    assert (file_id, first, "contains") in edges
    assert (file_id, second, "contains") in edges
    # Wikilinks become file-scoped references edges (resolved by the importer).
    assert (file_id, file_scoped_id("beta.md"), "references") in edges
    assert (file_id, file_scoped_id("ghost.md"), "references") in edges
    assert all(edge["confidence"] == "EXTRACTED" for edge in fragment.edges)


def test_schema_parity_without_model_import() -> None:
    """Row 3: fragments pass Graphify's own schema validators; no model module loads."""
    from graphify.semantic_cleanup import validate_semantic_fragment
    from graphify.validate import validate_extraction

    adapter = GraphifyAdapter()
    fragment = adapter.fragment_from_markdown(source_file="alpha.md", content=MARKDOWN)
    assert isinstance(fragment, ExtractionFragment)
    assert fragment.generator_version == FRAGMENT_GENERATOR_VERSION
    payload = fragment.to_dict()
    assert validate_semantic_fragment(payload) == []
    real_errors = [error for error in validate_extraction(payload) if "does not match any node id" not in error]
    assert real_errors == []
    forbidden = [
        name
        for name in sys.modules
        if name.split(".")[0] in {"openai", "anthropic", "tiktoken", "boto3", "transformers"}
        or name.startswith("graphify.llm")
    ]
    assert forbidden == []


def test_sanitizer_bounds_ids_and_sizes() -> None:
    """Row 16: crafted ids are rejected and rationale-tier nodes are stripped."""
    adapter = GraphifyAdapter()
    base = {"label": "x", "file_type": "document", "source_file": "a.md"}
    with pytest.raises(FragmentValidationError):
        adapter.generate_fragment(nodes=[{**base, "id": "bad/id"}], edges=[])
    with pytest.raises(FragmentValidationError):
        adapter.generate_fragment(nodes=[{**base, "id": "x" * 300}], edges=[])
    with pytest.raises(FragmentValidationError):
        adapter.generate_fragment(nodes=[{**base, "id": ".."}], edges=[])
    fragment = adapter.generate_fragment(
        nodes=[
            {**base, "id": "doc_a"},
            {**base, "id": "note_1", "file_type": "rationale"},
        ],
        edges=[
            {
                "source": "doc_a",
                "target": "note_1",
                "relation": "contains",
                "confidence": "EXTRACTED",
                "source_file": "a.md",
            }
        ],
    )
    assert [node["id"] for node in fragment.nodes] == ["doc_a"]
    assert fragment.edges == ()


def test_generation_is_deterministic_and_ids_are_file_scoped() -> None:
    generator = FragmentGenerator()
    first = generator.generate(source_file="docs/Alpha Plan.md", content=MARKDOWN)
    second = generator.generate(source_file="docs/Alpha Plan.md", content=MARKDOWN)
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(second.to_dict(), sort_keys=True)
    scoped = file_scoped_id("docs/Alpha Plan.md")
    assert scoped == file_scoped_id("docs/Alpha Plan.md")
    assert "/" not in scoped and "\\" not in scoped and ".." not in scoped
    assert scoped != file_scoped_id("docs/Beta Plan.md")


def test_crlf_and_lf_sources_produce_identical_fragments() -> None:
    generator = FragmentGenerator()
    lf = generator.generate(source_file="alpha.md", content=MARKDOWN)
    crlf = generator.generate(source_file="alpha.md", content=MARKDOWN.replace(b"\n", b"\r\n"))
    assert lf.to_dict() == crlf.to_dict()


def test_truncation_is_stamped_partial() -> None:
    truncated = FragmentGenerator(max_source_bytes=64).generate(source_file="big.md", content=MARKDOWN * 100)
    assert truncated.partial is True
    whole = FragmentGenerator().generate(source_file="big.md", content=MARKDOWN)
    assert whole.partial is False
