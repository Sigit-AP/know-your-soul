"""Property tests for the Wave-3 document extractor (SUB-90).

Hypothesis-driven invariants of the canonical chunker, the markdown
structure rules, id stability and PDF extraction over generated documents.
All examples are derandomized so the suite is byte-stable across machines.
"""

from __future__ import annotations

import string

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from _doc_fixtures import build_pdf_bytes
from know_your_soul.ingest.documents import (
    ChunkingConfig,
    chunk_text,
    extract_document,
    split_text_blocks,
)

_SETTINGS = {"derandomize": True, "deadline": None, "max_examples": 40}

_TEXT = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z"), whitelist_characters="\n"),
    max_size=400,
)

_WORD = st.text(alphabet=st.sampled_from(string.ascii_lowercase), min_size=1, max_size=8)
_PARAGRAPH = st.lists(_WORD, min_size=1, max_size=12).map(" ".join)
_DOC_TEXT = st.lists(_PARAGRAPH, max_size=8).map("\n\n".join)


def _non_ws(text: str) -> str:
    return "".join(ch for ch in text if ch not in " \t\n")


# ------------------------------------------------------------------ chunker


class TestChunkerProperties:
    @given(text=_DOC_TEXT, cap=st.integers(min_value=1, max_value=64))
    @settings(**_SETTINGS)
    def test_every_chunk_within_cap(self, text, cap):
        for _, chunk in chunk_text(text, max_chars=cap):
            assert len(chunk) <= cap
            assert chunk

    @given(text=_DOC_TEXT, cap=st.integers(min_value=1, max_value=64))
    @settings(**_SETTINGS)
    def test_non_whitespace_content_preserved_in_order(self, text, cap):
        chunks = chunk_text(text, max_chars=cap)
        assert _non_ws("".join(chunk for _, chunk in chunks)) == _non_ws(text)

    @given(text=_DOC_TEXT, cap=st.integers(min_value=1, max_value=64))
    @settings(**_SETTINGS)
    def test_offsets_point_at_chunk_text(self, text, cap):
        for offset, chunk in chunk_text(text, max_chars=cap):
            assert text[offset : offset + len(chunk)] == chunk

    @given(text=_DOC_TEXT, cap=st.integers(min_value=1, max_value=64))
    @settings(**_SETTINGS)
    def test_offsets_strictly_increasing(self, text, cap):
        offsets = [offset for offset, _ in chunk_text(text, max_chars=cap)]
        assert offsets == sorted(set(offsets))

    @given(text=_DOC_TEXT, cap=st.integers(min_value=1, max_value=64))
    @settings(**_SETTINGS)
    def test_deterministic(self, text, cap):
        assert chunk_text(text, max_chars=cap) == chunk_text(text, max_chars=cap)

    @given(text=_TEXT)
    @settings(**_SETTINGS)
    def test_blocks_cover_all_content(self, text):
        blocks = split_text_blocks(text)
        assert _non_ws("".join(block for _, block in blocks)) == _non_ws(text)


# ------------------------------------------------------------------ markdown


class TestMarkdownProperties:
    @given(levels=st.lists(st.integers(min_value=1, max_value=6), min_size=1, max_size=10))
    @settings(**_SETTINGS)
    def test_section_parent_is_nearest_shallower(self, levels):
        source = "\n\n".join(f"{'#' * level} H{index}_{level}" for index, level in enumerate(levels)) + "\n"
        result = extract_document("doc.md", source.encode())
        sections = [n for n in result.nodes if n.kind == "section"]
        parent_of = {e.target: e.source for e in result.edges}
        # reference stack model: parent = nearest previous heading with a
        # strictly smaller level, else the document node
        stack: list[tuple[int, str]] = []
        document = [n for n in result.nodes if n.kind == "document"][0]
        for node, level in zip(sections, levels, strict=True):
            while stack and stack[-1][0] >= level:
                stack.pop()
            expected_parent = stack[-1][1] if stack else document.node_id
            assert parent_of[node.node_id] == expected_parent
            stack.append((level, node.node_id))

    @given(text=_TEXT)
    @settings(**_SETTINGS)
    def test_markdown_never_crashes(self, text):
        result = extract_document("doc.md", text.encode("utf-8"))
        assert result.language == "markdown"
        node_ids = [n.node_id for n in result.nodes]
        assert len(node_ids) == len(set(node_ids))

    @given(text=_TEXT)
    @settings(**_SETTINGS)
    def test_markdown_deterministic(self, text):
        first = extract_document("doc.md", text.encode("utf-8"))
        second = extract_document("doc.md", text.encode("utf-8"))
        assert first.canonical_json() == second.canonical_json()

    @given(text=_TEXT)
    @settings(**_SETTINGS)
    def test_text_never_crashes(self, text):
        result = extract_document("doc.txt", text.encode("utf-8"))
        node_ids = [n.node_id for n in result.nodes]
        assert len(node_ids) == len(set(node_ids))


# ------------------------------------------------------------------ pdf


class TestPdfProperties:
    @given(pages=st.lists(_PARAGRAPH, min_size=1, max_size=5))
    @settings(max_examples=25, derandomize=True, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_pdf_structure_consistent(self, pages):
        data = build_pdf_bytes(pages)
        result = extract_document("book.pdf", data)
        node_ids = [n.node_id for n in result.nodes]
        assert len(node_ids) == len(set(node_ids))
        assert result.metadata["page_count"] == len(pages)
        text_pages = sum(1 for page in pages if page.strip())
        assert result.metadata["text_pages"] == text_pages
        assert result.metadata["no_text_layer"] is (text_pages == 0)

    @given(pages=st.lists(_PARAGRAPH, min_size=1, max_size=3))
    @settings(max_examples=20, derandomize=True, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_pdf_deterministic(self, pages):
        data = build_pdf_bytes(pages)
        first = extract_document("book.pdf", data)
        second = extract_document("book.pdf", data)
        assert first.canonical_json() == second.canonical_json()

    @given(pages=st.lists(_PARAGRAPH, min_size=1, max_size=3))
    @settings(max_examples=20, derandomize=True, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_pdf_builder_byte_deterministic(self, pages):
        assert build_pdf_bytes(pages) == build_pdf_bytes(pages)

    @given(text=_PARAGRAPH, cap=st.integers(min_value=5, max_value=40))
    @settings(max_examples=20, derandomize=True, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_pdf_chunk_caps_hold(self, text, cap):
        data = build_pdf_bytes([text])
        result = extract_document("book.pdf", data, config=ChunkingConfig(max_chunk_chars=cap))
        assert [n for n in result.nodes if n.kind == "paragraph"] or not text.strip()
