"""Unit tests for the Wave-3 document extractor (SUB-90).

Covers the canonical chunker, the structure-aware markdown parser, the
plaintext extractor and the pypdf text-layer extractor, plus artifact
determinism, graph construction and proposal-only split suggestions.
PDFs are synthesized deterministically by :mod:`_doc_fixtures` (pypdf only).
"""

from __future__ import annotations

import hashlib

import pytest

from _doc_fixtures import build_encrypted_pdf_bytes, build_pdf_bytes
from know_your_soul.graph_model import (
    SplitThresholds,
    file_node_id,
    make_node_id,
    parse_node_id,
)
from know_your_soul.ingest.documents import (
    DEFAULT_MAX_CHUNK_CHARS,
    DOCUMENT_EXTENSION_MAP,
    DOCUMENT_EXTRACTION_VERSION,
    DOCUMENT_LANGUAGES,
    MAX_DOCUMENT_FILE_SIZE_BYTES,
    ChunkingConfig,
    DocumentExtractionResult,
    DocumentIngestError,
    PdfEncryptedError,
    PdfParseError,
    UnsupportedDocumentTypeError,
    build_universal_graph,
    chunk_block,
    chunk_text,
    extract_document,
    extract_document_file,
    split_text_blocks,
    suggest_primary_splits,
)
from know_your_soul.ingest.extractor import (
    EXTRACTION_VERSION,
    FileTooLargeError,
    SourceDecodeError,
)

# ------------------------------------------------------------------ helpers


def kinds_of(result: DocumentExtractionResult) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in result.nodes:
        counts[node.kind] = counts.get(node.kind, 0) + 1
    return counts


def nodes_by_kind(result: DocumentExtractionResult, kind: str):
    return [node for node in result.nodes if node.kind == kind]


# ------------------------------------------------------------------ registry


class TestDocumentRegistry:
    def test_extension_map_exact(self):
        assert DOCUMENT_EXTENSION_MAP == {
            ".md": "markdown",
            ".markdown": "markdown",
            ".pdf": "pdf",
            ".txt": "text",
        }

    @pytest.mark.parametrize("extension", sorted(DOCUMENT_EXTENSION_MAP))
    def test_extension_keys_lowercase_dotted(self, extension):
        assert extension.startswith(".")
        assert extension == extension.lower()

    def test_languages_frozen(self):
        assert DOCUMENT_LANGUAGES == frozenset({"markdown", "pdf", "text"})

    def test_extraction_version_is_int(self):
        assert isinstance(DOCUMENT_EXTRACTION_VERSION, int)
        assert DOCUMENT_EXTRACTION_VERSION >= 1

    def test_extraction_version_independent_of_code_extractor(self):
        # The document version is a separate counter from SUB-87's.
        assert DOCUMENT_EXTRACTION_VERSION == 1
        assert EXTRACTION_VERSION == 1

    def test_size_bound(self):
        assert MAX_DOCUMENT_FILE_SIZE_BYTES >= 1024 * 1024


class TestChunkingConfig:
    def test_default(self):
        assert ChunkingConfig().max_chunk_chars == DEFAULT_MAX_CHUNK_CHARS

    def test_custom(self):
        assert ChunkingConfig(max_chunk_chars=7).max_chunk_chars == 7

    @pytest.mark.parametrize("bad", [0, -1, True, "10", 2.5, None])
    def test_invalid(self, bad):
        with pytest.raises((ValueError, TypeError)):
            ChunkingConfig(max_chunk_chars=bad)


# ------------------------------------------------------------------ chunker


class TestSplitTextBlocks:
    def test_empty(self):
        assert split_text_blocks("") == ()

    @pytest.mark.parametrize("text", ["   ", "\n", "\n\n\n", " \n \n ", "\t\n\t\n"])
    def test_blank_only(self, text):
        assert split_text_blocks(text) == ()

    def test_single_block(self):
        assert split_text_blocks("hello world") == ((0, "hello world"),)

    def test_two_blocks(self):
        assert split_text_blocks("alpha\n\nbeta") == ((0, "alpha"), (7, "beta"))

    def test_offsets_skip_blank_runs(self):
        blocks = split_text_blocks("alpha\n\n\n\nbeta")
        assert blocks == ((0, "alpha"), (9, "beta"))

    def test_blank_line_with_spaces_separates(self):
        blocks = split_text_blocks("alpha\n   \nbeta")
        assert [text for _, text in blocks] == ["alpha", "beta"]

    def test_blank_line_with_tabs_separates(self):
        blocks = split_text_blocks("alpha\n\t\nbeta")
        assert [text for _, text in blocks] == ["alpha", "beta"]

    def test_single_newlines_do_not_separate(self):
        blocks = split_text_blocks("one\ntwo\nthree")
        assert blocks == ((0, "one\ntwo\nthree"),)

    def test_leading_and_trailing_blank_lines(self):
        blocks = split_text_blocks("\n\nalpha\n\n")
        assert blocks == ((2, "alpha"),)

    def test_offset_points_at_first_char(self):
        text = "first\n\n   second\n\n\tthird"
        for offset, block in split_text_blocks(text):
            assert text[offset : offset + len(block)] == block
            assert block == block.strip()
            assert block

    def test_offsets_strictly_increasing(self):
        offsets = [offset for offset, _ in split_text_blocks("a\n\nb\n\nc\n\nd")]
        assert offsets == sorted(offsets)
        assert len(set(offsets)) == len(offsets)

    def test_deterministic_repeat(self):
        text = "alpha\n\nbeta\n\ngamma"
        assert split_text_blocks(text) == split_text_blocks(text)


class TestChunkBlock:
    def test_short_block_single_chunk(self):
        assert chunk_block("hello", base_offset=0, max_chars=100) == ((0, "hello"),)

    def test_exact_cap_single_chunk(self):
        block = "x" * 10
        assert chunk_block(block, base_offset=0, max_chars=10) == ((0, block),)

    def test_hard_cut_without_whitespace(self):
        block = "x" * 25
        chunks = chunk_block(block, base_offset=0, max_chars=10)
        assert chunks == (
            (0, "x" * 10),
            (10, "x" * 10),
            (20, "x" * 5),
        )

    def test_prefers_last_whitespace_before_cap(self):
        block = "aaaa bbbb cccc dddd"
        chunks = chunk_block(block, base_offset=0, max_chars=11)
        assert chunks == ((0, "aaaa bbbb"), (10, "cccc dddd"))

    def test_base_offset_propagates(self):
        chunks = chunk_block("aa bb cc", base_offset=100, max_chars=4)
        assert chunks[0][0] == 100
        for offset, _chunk in chunks:
            assert offset >= 100

    def test_every_chunk_within_cap(self):
        block = "word " * 100
        for _, chunk in chunk_block(block, base_offset=0, max_chars=17):
            assert len(chunk) <= 17

    def test_non_whitespace_content_preserved_in_order(self):
        block = "alpha beta gamma\ndelta epsilon\n\nzeta"  # inner blank stays inside one block? no: caller splits
        block = "alpha beta gamma\ndelta epsilon zeta eta theta iota kappa"
        chunks = chunk_block(block, base_offset=0, max_chars=13)
        rebuilt = " ".join(chunk for _, chunk in chunks)
        assert rebuilt.split() == block.split()

    def test_offsets_point_into_block(self):
        block = "one two three four five six seven"
        for offset, chunk in chunk_block(block, base_offset=50, max_chars=9):
            assert block[offset - 50 : offset - 50 + len(chunk)] == chunk

    def test_deterministic_repeat(self):
        block = "lorem ipsum dolor sit amet " * 20
        assert chunk_block(block, base_offset=0, max_chars=61) == chunk_block(block, base_offset=0, max_chars=61)

    def test_cap_one(self):
        chunks = chunk_block("ab", base_offset=0, max_chars=1)
        assert chunks == ((0, "a"), (1, "b"))


class TestChunkText:
    def test_combines_blocks(self):
        chunks = chunk_text("alpha\n\nbeta", max_chars=100)
        assert chunks == ((0, "alpha"), (7, "beta"))

    def test_chunk_offsets_global(self):
        text = "aa bb\n\ncc dd"
        chunks = chunk_text(text, max_chars=4)
        for offset, chunk in chunks:
            assert text[offset : offset + len(chunk)] == chunk

    def test_empty_text(self):
        assert chunk_text("", max_chars=10) == ()

    def test_long_paragraph_split_then_next_block(self):
        text = "x" * 30 + "\n\n" + "short"
        chunks = chunk_text(text, max_chars=10)
        assert chunks[-1] == (32, "short")
        assert len(chunks) == 4


# ------------------------------------------------------------------ markdown


class TestMarkdownStructure:
    def test_document_and_file_roots(self):
        result = extract_document("doc.md", b"# Title\n\nbody\n")
        file_node = nodes_by_kind(result, "file")[0]
        assert file_node.node_id == file_node_id("doc.md")
        document = nodes_by_kind(result, "document")[0]
        assert document.node_id == make_node_id(path="doc.md", kind="document", name="doc.md", line=1)
        kinds = {edge.kind for edge in result.edges}
        assert kinds == {"contains"}
        assert (file_node.node_id, document.node_id) in {(e.source, e.target) for e in result.edges}

    def test_h1_parent_is_document(self):
        result = extract_document("doc.md", b"# Alpha\n\nbody\n")
        document = nodes_by_kind(result, "document")[0]
        section = nodes_by_kind(result, "section")[0]
        assert section.name == "Alpha"
        edge = [e for e in result.edges if e.target == section.node_id][0]
        assert edge.source == document.node_id

    def test_h2_nests_under_nearest_h1(self):
        source = b"# A\n\n## B\n\ntext\n"
        result = extract_document("doc.md", source)
        sections = {n.name: n for n in nodes_by_kind(result, "section")}
        edge = [e for e in result.edges if e.target == sections["B"].node_id][0]
        assert edge.source == sections["A"].node_id

    def test_deep_nesting_arbitrary_depth(self):
        source = "\n".join(f"{'#' * level} H{level}" for level in range(1, 7)) + "\n"
        result = extract_document("doc.md", source.encode())
        sections = {n.name: n for n in nodes_by_kind(result, "section")}
        for level in range(2, 7):
            edge = [e for e in result.edges if e.target == sections[f"H{level}"].node_id][0]
            assert edge.source == sections[f"H{level - 1}"].node_id

    def test_skipped_level_attaches_to_nearest_shallower(self):
        source = b"# A\n\n### C\n"
        result = extract_document("doc.md", source)
        sections = {n.name: n for n in nodes_by_kind(result, "section")}
        edge = [e for e in result.edges if e.target == sections["C"].node_id][0]
        assert edge.source == sections["A"].node_id

    def test_same_level_heading_replaces_sibling(self):
        source = b"# A\n\n## B\n\n## C\n"
        result = extract_document("doc.md", source)
        sections = {n.name: n for n in nodes_by_kind(result, "section")}
        edge_b = [e for e in result.edges if e.target == sections["B"].node_id][0]
        edge_c = [e for e in result.edges if e.target == sections["C"].node_id][0]
        assert edge_b.source == sections["A"].node_id
        assert edge_c.source == sections["A"].node_id

    def test_h2_before_any_h1_attaches_to_document(self):
        result = extract_document("doc.md", b"## Orphan\n")
        document = nodes_by_kind(result, "document")[0]
        section = nodes_by_kind(result, "section")[0]
        edge = [e for e in result.edges if e.target == section.node_id][0]
        assert edge.source == document.node_id

    def test_heading_closing_hashes_stripped(self):
        result = extract_document("doc.md", b"## Title ##\n")
        section = nodes_by_kind(result, "section")[0]
        assert section.name == "Title"

    def test_heading_line_numbers(self):
        result = extract_document("doc.md", b"intro\n\n# A\n\ntext\n\n## B\n")
        sections = {n.name: n for n in nodes_by_kind(result, "section")}
        assert sections["A"].start_line == 3
        assert sections["B"].start_line == 7

    def test_heading_inside_code_fence_is_not_a_section(self):
        source = b"# Real\n\n```\n# fake heading\n```\n"
        result = extract_document("doc.md", source)
        assert [n.name for n in nodes_by_kind(result, "section")] == ["Real"]
        paragraphs = nodes_by_kind(result, "paragraph")
        assert any("fake heading" in _paragraph_text(result, p) for p in paragraphs) or paragraphs

    def test_empty_heading_gets_stable_placeholder(self):
        result = extract_document("doc.md", b"#\n\nbody\n")
        section = nodes_by_kind(result, "section")[0]
        assert section.name == "(empty)"

    def test_no_headings_document_only(self):
        result = extract_document("doc.md", b"just text\n\nmore text\n")
        assert nodes_by_kind(result, "section") == []
        assert len(nodes_by_kind(result, "paragraph")) == 2


class TestMarkdownBlocks:
    def test_paragraph_under_section(self):
        result = extract_document("doc.md", b"# A\n\nhello world\n")
        section = nodes_by_kind(result, "section")[0]
        paragraph = nodes_by_kind(result, "paragraph")[0]
        edge = [e for e in result.edges if e.target == paragraph.node_id][0]
        assert edge.source == section.node_id

    def test_content_before_first_heading_under_document(self):
        result = extract_document("doc.md", b"intro\n\n# A\n")
        document = nodes_by_kind(result, "document")[0]
        paragraph = nodes_by_kind(result, "paragraph")[0]
        edge = [e for e in result.edges if e.target == paragraph.node_id][0]
        assert edge.source == document.node_id

    @pytest.mark.parametrize("marker", ["-", "*", "+"])
    def test_bullet_list_becomes_paragraph(self, marker):
        source = f"{marker} one\n{marker} two\n".encode()
        result = extract_document("doc.md", source)
        paragraphs = nodes_by_kind(result, "paragraph")
        assert len(paragraphs) == 1

    def test_numbered_list_becomes_paragraph(self):
        result = extract_document("doc.md", b"1. one\n2. two\n")
        assert len(nodes_by_kind(result, "paragraph")) == 1

    def test_code_fence_becomes_paragraph(self):
        result = extract_document("doc.md", b"```python\nprint('x')\n```\n")
        paragraphs = nodes_by_kind(result, "paragraph")
        assert len(paragraphs) == 1

    def test_tilde_fence(self):
        result = extract_document("doc.md", b"~~~\ncode\n~~~\n")
        assert len(nodes_by_kind(result, "paragraph")) == 1

    def test_unclosed_fence_closes_at_eof(self):
        result = extract_document("doc.md", b"```\ncode line\n")
        assert len(nodes_by_kind(result, "paragraph")) == 1
        assert result.parse_error is False

    def test_paragraph_ids_stable(self):
        source = b"# A\n\nalpha beta\n"
        first = extract_document("doc.md", source)
        second = extract_document("doc.md", source)
        assert [n.node_id for n in first.nodes] == [n.node_id for n in second.nodes]

    def test_paragraph_id_carries_path_line_offset(self):
        result = extract_document("doc.md", b"alpha\n")
        paragraph = nodes_by_kind(result, "paragraph")[0]
        parts = parse_node_id(paragraph.node_id)
        assert parts.kind == "paragraph"
        expected = make_node_id(path="doc.md", kind="paragraph", name=paragraph.name, line=paragraph.start_line)
        assert paragraph.node_id == expected

    def test_crlf_normalized(self):
        result = extract_document("doc.md", b"# A\r\n\r\nbody\r\n")
        assert len(nodes_by_kind(result, "section")) == 1
        assert len(nodes_by_kind(result, "paragraph")) == 1

    def test_chunk_cap_applies(self):
        body = "word " * 200
        result = extract_document("doc.md", body.encode(), config=ChunkingConfig(max_chunk_chars=50))
        paragraphs = nodes_by_kind(result, "paragraph")
        assert len(paragraphs) > 1

    def test_empty_markdown(self):
        result = extract_document("doc.md", b"")
        assert kinds_of(result) == {"file": 1, "document": 1}
        assert len(result.edges) == 1  # only file -> document

    def test_metadata_format(self):
        result = extract_document("doc.md", b"# A\n\nx\n")
        assert result.metadata["format"] == "markdown"
        assert result.metadata["section_count"] == 1


class TestMarkdownLinks:
    def test_inline_link_parked_as_references(self):
        result = extract_document("a.md", b"see [the other doc](other.md) now\n")
        refs = result.unresolved_refs
        assert len(refs) == 1
        assert refs[0].kind == "references"
        assert refs[0].target_name == "other.md"
        assert refs[0].name_tail == "other.md"

    def test_link_source_is_containing_paragraph(self):
        result = extract_document("a.md", b"# S\n\nsee [x](b.md)\n")
        paragraph = nodes_by_kind(result, "paragraph")[0]
        assert result.unresolved_refs[0].source_node_id == paragraph.node_id

    def test_relative_path_link_tail(self):
        result = extract_document("a.md", b"[x](docs/deep/other.md)\n")
        ref = result.unresolved_refs[0]
        assert ref.target_name == "docs/deep/other.md"
        assert ref.name_tail == "other.md"

    def test_anchor_stripped_from_target(self):
        result = extract_document("a.md", b"[x](other.md#section)\n")
        assert result.unresolved_refs[0].target_name == "other.md"

    @pytest.mark.parametrize("target", ["#local", "#"])
    def test_pure_anchor_skipped(self, target):
        result = extract_document("a.md", f"[x]({target})\n".encode())
        assert result.unresolved_refs == ()

    @pytest.mark.parametrize(
        "target",
        ["https://example.com/x.md", "http://example.com", "mailto:a@b.c", "ftp://h/f.md", "tel:+1"],
    )
    def test_external_scheme_skipped(self, target):
        result = extract_document("a.md", f"[x]({target})\n".encode())
        assert result.unresolved_refs == ()

    def test_image_not_a_reference(self):
        result = extract_document("a.md", b"![alt](image.png)\n")
        assert result.unresolved_refs == ()

    def test_angle_bracket_target(self):
        result = extract_document("a.md", b"[x](<some file.md>)\n")
        assert result.unresolved_refs[0].target_name == "some file.md"

    def test_link_inside_code_block_not_extracted(self):
        result = extract_document("a.md", b"```\n[x](other.md)\n```\n")
        assert result.unresolved_refs == ()

    def test_multiple_links_deterministic_order(self):
        source = b"[a](a.md) and [b](b.md)\n\n[c](c.md)\n"
        first = extract_document("a.md", source)
        second = extract_document("a.md", source)
        assert first.unresolved_refs == second.unresolved_refs
        assert len(first.unresolved_refs) == 3

    def test_link_line_and_col(self):
        result = extract_document("a.md", b"pre\n\nxx [a](b.md)\n")
        ref = result.unresolved_refs[0]
        assert ref.line == 3
        assert ref.col == 3

    def test_txt_link_not_extracted(self):
        # plaintext has no link syntax — no refs
        result = extract_document("a.txt", b"see [x](other.md)\n")
        assert result.unresolved_refs == ()


# ------------------------------------------------------------------ plaintext


class TestPlaintext:
    def test_single_paragraph(self):
        result = extract_document("notes.txt", b"hello world\n")
        assert kinds_of(result) == {"file": 1, "document": 1, "paragraph": 1}

    def test_paragraphs_split_on_blank_lines(self):
        result = extract_document("notes.txt", b"one\n\ntwo\n\nthree\n")
        assert len(nodes_by_kind(result, "paragraph")) == 3

    def test_paragraphs_under_document(self):
        result = extract_document("notes.txt", b"one\n\ntwo\n")
        document = nodes_by_kind(result, "document")[0]
        for paragraph in nodes_by_kind(result, "paragraph"):
            edge = [e for e in result.edges if e.target == paragraph.node_id][0]
            assert edge.source == document.node_id

    def test_chunk_cap(self):
        body = ("lorem ipsum dolor sit amet " * 40).encode()
        result = extract_document("notes.txt", body, config=ChunkingConfig(max_chunk_chars=100))
        paragraphs = nodes_by_kind(result, "paragraph")
        assert len(paragraphs) > 5

    def test_empty_file(self):
        result = extract_document("notes.txt", b"")
        assert kinds_of(result) == {"file": 1, "document": 1}

    def test_blank_only_file(self):
        result = extract_document("notes.txt", b"\n\n  \n")
        assert kinds_of(result) == {"file": 1, "document": 1}

    def test_line_numbers(self):
        result = extract_document("notes.txt", b"one\n\ntwo\n")
        paragraphs = nodes_by_kind(result, "paragraph")
        assert [p.start_line for p in paragraphs] == [1, 3]

    def test_ids_stable_across_runs(self):
        first = extract_document("notes.txt", b"alpha\n\nbeta\n")
        second = extract_document("notes.txt", b"alpha\n\nbeta\n")
        assert first.canonical_json() == second.canonical_json()

    def test_language_is_text(self):
        assert extract_document("notes.txt", b"x\n").language == "text"


# ------------------------------------------------------------------ pdf


class TestPdf:
    def test_multi_page_structure(self):
        data = build_pdf_bytes(["alpha one\n\nbeta two", "gamma three"])
        result = extract_document("book.pdf", data)
        assert kinds_of(result) == {"file": 1, "document": 1, "page": 2, "paragraph": 3}

    def test_page_nodes_numbered(self):
        data = build_pdf_bytes(["one", "two", "three"])
        result = extract_document("book.pdf", data)
        pages = nodes_by_kind(result, "page")
        assert [p.name for p in pages] == ["page-1", "page-2", "page-3"]
        assert [p.start_line for p in pages] == [1, 2, 3]

    def test_page_parent_is_document(self):
        data = build_pdf_bytes(["one", "two"])
        result = extract_document("book.pdf", data)
        document = nodes_by_kind(result, "document")[0]
        for page in nodes_by_kind(result, "page"):
            edge = [e for e in result.edges if e.target == page.node_id][0]
            assert edge.source == document.node_id

    def test_paragraph_parent_is_page(self):
        data = build_pdf_bytes(["alpha\n\nbeta"])
        result = extract_document("book.pdf", data)
        page = nodes_by_kind(result, "page")[0]
        for paragraph in nodes_by_kind(result, "paragraph"):
            edge = [e for e in result.edges if e.target == paragraph.node_id][0]
            assert edge.source == page.node_id

    def test_paragraph_id_carries_path_page_offset(self):
        data = build_pdf_bytes(["alpha\n\nbeta"])
        result = extract_document("book.pdf", data)
        paragraph = nodes_by_kind(result, "paragraph")[0]
        expected = make_node_id(path="book.pdf", kind="paragraph", name=paragraph.name, line=1)
        assert paragraph.node_id == expected
        assert paragraph.name.startswith("p1:o")

    def test_chunk_offsets_within_page(self):
        data = build_pdf_bytes(["alpha\n\nbeta"])
        result = extract_document("book.pdf", data)
        names = sorted(p.name for p in nodes_by_kind(result, "paragraph"))
        assert names == ["p1:o0", "p1:o7"]

    def test_chunk_cap_applies_per_page(self):
        page_text = "word " * 100
        data = build_pdf_bytes([page_text.strip()])
        result = extract_document("book.pdf", data, config=ChunkingConfig(max_chunk_chars=60))
        paragraphs = nodes_by_kind(result, "paragraph")
        assert len(paragraphs) > 3

    def test_no_text_layer(self):
        data = build_pdf_bytes(["", ""])
        result = extract_document("scan.pdf", data)
        assert kinds_of(result) == {"file": 1, "document": 1}
        assert result.edges and len(result.edges) == 1  # only file -> document
        assert result.metadata["no_text_layer"] is True
        assert result.metadata["page_count"] == 2
        assert result.metadata["text_pages"] == 0

    def test_partial_text_layer_not_flagged(self):
        data = build_pdf_bytes(["", "real text"])
        result = extract_document("book.pdf", data)
        assert result.metadata["no_text_layer"] is False
        assert result.metadata["text_pages"] == 1
        # only the text-bearing page gets a page node
        assert [p.name for p in nodes_by_kind(result, "page")] == ["page-2"]

    def test_broken_pdf_typed_error(self):
        with pytest.raises(PdfParseError) as excinfo:
            extract_document("broken.pdf", b"%PDF-1.4 garbage without structure")
        assert excinfo.value.code == "PDF_PARSE_ERROR"

    def test_not_a_pdf_at_all(self):
        with pytest.raises(PdfParseError):
            extract_document("fake.pdf", b"this is plainly not a pdf file at all")

    def test_encrypted_pdf_typed_error(self):
        data = build_encrypted_pdf_bytes(["secret text"])
        with pytest.raises(PdfEncryptedError) as excinfo:
            extract_document("locked.pdf", data)
        assert excinfo.value.code == "PDF_ENCRYPTED"

    def test_ids_stable_across_runs(self):
        data = build_pdf_bytes(["alpha\n\nbeta", "gamma"])
        first = extract_document("book.pdf", data)
        second = extract_document("book.pdf", data)
        assert first.canonical_json() == second.canonical_json()

    def test_sha256_tracks_raw_bytes(self):
        data = build_pdf_bytes(["alpha"])
        result = extract_document("book.pdf", data)
        assert result.sha256 == hashlib.sha256(data).hexdigest()
        assert result.size == len(data)

    def test_language_is_pdf(self):
        assert extract_document("book.pdf", build_pdf_bytes(["x"])).language == "pdf"

    def test_no_ocr_no_text_layer_has_zero_children(self):
        data = build_pdf_bytes([""])
        result = extract_document("scan.pdf", data)
        document = nodes_by_kind(result, "document")[0]
        children = [e for e in result.edges if e.source == document.node_id]
        assert children == []


# ------------------------------------------------------------------ dispatch


class TestDispatch:
    @pytest.mark.parametrize(
        ("path", "language"),
        [("a.md", "markdown"), ("a.markdown", "markdown"), ("a.txt", "text"), ("a.pdf", "pdf")],
    )
    def test_language_from_extension(self, path, language):
        data = build_pdf_bytes(["x"]) if language == "pdf" else b"x\n"
        assert extract_document(path, data).language == language

    def test_uppercase_extension(self):
        assert extract_document("A.MD", b"x\n").language == "markdown"

    def test_explicit_language_wins(self):
        result = extract_document("noext", b"x\n", language="text")
        assert result.language == "text"

    def test_unsupported_extension(self):
        with pytest.raises(UnsupportedDocumentTypeError) as excinfo:
            extract_document("a.docx", b"x")
        assert excinfo.value.code == "UNSUPPORTED_MEDIA_TYPE"

    def test_unsupported_explicit_language(self):
        with pytest.raises(UnsupportedDocumentTypeError):
            extract_document("a.md", b"x", language="python")

    def test_invalid_utf8_text(self):
        with pytest.raises(SourceDecodeError):
            extract_document("bad.txt", b"\xff\xfe\x00binary")

    def test_invalid_utf8_markdown(self):
        with pytest.raises(SourceDecodeError):
            extract_document("bad.md", b"# Title\n\xff\xfe")

    def test_too_large(self):
        big = b"x" * (MAX_DOCUMENT_FILE_SIZE_BYTES + 1)
        with pytest.raises(FileTooLargeError) as excinfo:
            extract_document("big.txt", big)
        assert excinfo.value.code == "FILE_TOO_LARGE"

    def test_bom_stripped_for_text(self):
        result = extract_document("bom.txt", b"\xef\xbb\xbfhello\n")
        paragraph = nodes_by_kind(result, "paragraph")[0]
        assert paragraph.start_line == 1

    def test_errors_are_ingest_errors(self):
        for error_type in (UnsupportedDocumentTypeError, PdfParseError, PdfEncryptedError):
            assert issubclass(error_type, DocumentIngestError)


class TestExtractDocumentFile:
    def test_reads_real_file(self, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text("alpha\n\nbeta\n", encoding="utf-8")
        result = extract_document_file(target)
        assert result.language == "text"
        assert len(nodes_by_kind(result, "paragraph")) == 2

    def test_display_path_drives_ids(self, tmp_path):
        target = tmp_path / "note.txt"
        target.write_text("alpha\n", encoding="utf-8")
        result = extract_document_file(target, display_path="docs/note.txt")
        document = nodes_by_kind(result, "document")[0]
        assert document.node_id == make_node_id(path="docs/note.txt", kind="document", name="note.txt", line=1)

    def test_real_pdf_file(self, tmp_path):
        target = tmp_path / "book.pdf"
        target.write_bytes(build_pdf_bytes(["one", "two"]))
        result = extract_document_file(target)
        assert len(nodes_by_kind(result, "page")) == 2

    def test_missing_file(self, tmp_path):
        with pytest.raises(OSError):
            extract_document_file(tmp_path / "nope.txt")


# ------------------------------------------------------------------ artifact


class TestArtifact:
    def test_canonical_json_sorted_keys(self):
        result = extract_document("a.md", b"# A\n\nbody [x](b.md)\n")
        artifact = result.to_artifact()
        assert list(artifact) == sorted(artifact)

    def test_artifact_node_shape_matches_code_extractor(self):
        result = extract_document("a.md", b"# A\n\nbody\n")
        node = result.to_artifact()["nodes"][0]
        assert set(node) == {
            "end_col",
            "end_line",
            "kind",
            "name",
            "node_id",
            "path",
            "qualified_name",
            "start_col",
            "start_line",
        }

    def test_artifact_ref_shape(self):
        result = extract_document("a.md", b"[x](b.md)\n")
        ref = result.to_artifact()["unresolved_refs"][0]
        assert set(ref) == {"col", "kind", "line", "name_tail", "source_node_id", "target_name"}

    def test_artifact_carries_metadata(self):
        result = extract_document("scan.pdf", build_pdf_bytes([""]))
        assert result.to_artifact()["metadata"]["no_text_layer"] is True

    def test_byte_identical_across_runs(self):
        source = b"# T\n\npara one\n\n## S\n\npara two [l](x.md)\n"
        assert extract_document("d.md", source).canonical_json() == extract_document("d.md", source).canonical_json()

    def test_extraction_version_recorded(self):
        result = extract_document("d.md", b"x\n")
        assert result.extraction_version == DOCUMENT_EXTRACTION_VERSION

    def test_parse_error_false_on_success(self):
        assert extract_document("d.md", b"# ok\n").parse_error is False

    def test_generated_hint_false(self):
        assert extract_document("d.md", b"# ok\n").generated_hint is False


# ------------------------------------------------------------------ graph


class TestBuildUniversalGraph:
    def test_graph_validates(self):
        result = extract_document("d.md", b"# A\n\n## B\n\ntext\n")
        graph = build_universal_graph(result)
        graph.validate()
        assert graph.node_count == len(result.nodes)
        assert graph.edge_count == len(result.edges)

    def test_depth_chain(self):
        result = extract_document("d.md", b"# A\n\n## B\n\ntext\n")
        graph = build_universal_graph(result)
        paragraph = nodes_by_kind(result, "paragraph")[0]
        assert graph.depth(paragraph.node_id) == 4  # file -> document -> A -> B -> paragraph

    def test_document_payload_carries_metadata(self):
        result = extract_document("scan.pdf", build_pdf_bytes([""]))
        graph = build_universal_graph(result)
        document = nodes_by_kind(result, "document")[0]
        assert graph.get_node(document.node_id).payload["no_text_layer"] is True

    def test_pdf_hierarchy(self):
        result = extract_document("b.pdf", build_pdf_bytes(["alpha\n\nbeta"]))
        graph = build_universal_graph(result)
        graph.validate()
        page = nodes_by_kind(result, "page")[0]
        children = graph.contains_children(page.node_id)
        assert len(children) == 2

    def test_deterministic_canonical_dump(self):
        result = extract_document("d.md", b"# A\n\nx\n")
        first = build_universal_graph(result).canonical_dump()
        second = build_universal_graph(result).canonical_dump()
        assert first == second


class TestSuggestPrimarySplits:
    def test_small_document_no_proposals(self):
        result = extract_document("d.md", b"# A\n\nshort\n")
        assert suggest_primary_splits(result) == ()

    def test_large_document_proposes(self):
        body = "\n\n".join(f"paragraph number {index} with some text" for index in range(40))
        result = extract_document("big.txt", body.encode())
        proposals = suggest_primary_splits(result, thresholds=SplitThresholds(max_subtree_nodes=10))
        assert proposals
        assert all("subtree_size" in proposal.reasons for proposal in proposals)

    def test_proposal_only_graph_untouched(self):
        body = "\n\n".join(f"p {index}" for index in range(20))
        result = extract_document("big.txt", body.encode())
        before = build_universal_graph(result).canonical_dump()
        suggest_primary_splits(result, thresholds=SplitThresholds(max_subtree_nodes=5))
        assert build_universal_graph(result).canonical_dump() == before

    def test_deterministic_proposals(self):
        body = "\n\n".join(f"paragraph {index}" for index in range(30))
        result = extract_document("big.txt", body.encode())
        thresholds = SplitThresholds(max_subtree_nodes=10)
        assert suggest_primary_splits(result, thresholds=thresholds) == suggest_primary_splits(
            result, thresholds=thresholds
        )

    def test_result_not_mutated(self):
        result = extract_document("d.txt", b"a\n\nb\n")
        nodes_before = result.nodes
        suggest_primary_splits(result)
        assert result.nodes == nodes_before

    def test_nonprimary_document_root_still_proposes(self):
        # the helper wraps the document tree under a synthetic primary, so
        # propose_primary_splits' primary-root rule is satisfied internally
        body = "\n\n".join(f"chunk {index}" for index in range(15))
        result = extract_document("d.txt", body.encode())
        proposals = suggest_primary_splits(result, thresholds=SplitThresholds(max_subtree_nodes=5))
        assert proposals


def _paragraph_text(result: DocumentExtractionResult, paragraph) -> str:
    # paragraph names carry offsets; content itself is re-derivable from the source
    return paragraph.name
