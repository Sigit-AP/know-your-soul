"""Document extraction for the universal knowledge graph (wave W3, SUB-90).

Spec: MASTER-PLAN W3 — PDF (pypdf text layer), structure-aware markdown and
plaintext, adapted to the frozen W1 contract (:mod:`know_your_soul.graph_model`)
and the W2 sibling protocols (:mod:`know_your_soul.ingest.extractor` shapes,
:mod:`know_your_soul.ingest.pipeline` duck-typed result protocol). This module:

* routes by extension via :data:`DOCUMENT_EXTENSION_MAP` (``.pdf`` /
  ``.md`` / ``.markdown`` / ``.txt``); anything else fails typed
  :class:`UnsupportedDocumentTypeError` — the W2 ``EXTENSION_MAP`` in
  ``ingest/languages.py`` stays frozen (its tests pin ``language_for_path``
  to ``None`` for document extensions and require every value to name a
  tree-sitter language);
* emits the same artifact shape as the code extractor: the ``file:<path>``
  root node, a ``document`` root under it, then format-specific structure —
  PDF: ``page`` per text-bearing page with ``paragraph`` chunks; markdown:
  ``section`` nodes nested by heading level (arbitrary depth via a heading
  stack) with paragraph/list/code-block ``paragraph`` chunks; plaintext:
  ``paragraph`` chunks. Node ids are
  ``kind:sha256(path:kind:name:line)`` with the page/line and the character
  offset encoded in the name (``p<page>:o<offset>`` / ``l<line>:o<offset>``);
* chunks canonically: split at blank-line paragraph boundaries, cap each
  chunk at :data:`DEFAULT_MAX_CHUNK_CHARS` (configurable), break oversized
  paragraphs at the last whitespace inside the window, hard-cut otherwise —
  a pure function of the input text;
* parks inter-document markdown links as ``unresolved_refs`` with kind
  ``references`` (the two-phase resolver lane; the W2 resolver's kind table
  predates ``references`` and fails them typed ``unknown_ref_kind`` — a
  documented residual, see ``docs/recovery/w3-documents.md``);
* never OCRs: a PDF with no text layer yields the ``document`` node with
  ``metadata["no_text_layer"] = True`` and zero children; a broken PDF fails
  typed :class:`PdfParseError`, an encrypted one :class:`PdfEncryptedError`;
* offers :func:`suggest_primary_splits`, a proposal-only wrapper over
  :func:`know_your_soul.graph_model.propose_primary_splits` for oversized
  documents — it never mutates the graph it inspects.

No model, provider, clock or randomness anywhere in this module.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from pypdf import PdfReader

from know_your_soul.domain import PrimaryDomain, canonical_json
from know_your_soul.graph_model import (
    CoreNodeKind,
    GraphEdge,
    GraphNode,
    SplitProposal,
    SplitThresholds,
    UniversalGraph,
    file_node_id,
    make_node_id,
    propose_primary_splits,
)
from know_your_soul.ingest.extractor import (
    ExtractedEdge,
    ExtractedNode,
    FileTooLargeError,
    IngestError,
    SourceDecodeError,
    UnresolvedRef,
)

__all__ = [
    "DEFAULT_MAX_CHUNK_CHARS",
    "DOCUMENT_EXTRACTION_VERSION",
    "DOCUMENT_EXTENSION_MAP",
    "DOCUMENT_LANGUAGES",
    "MAX_DOCUMENT_FILE_SIZE_BYTES",
    "ChunkingConfig",
    "DocumentExtractionResult",
    "DocumentIngestError",
    "PdfEncryptedError",
    "PdfParseError",
    "UnsupportedDocumentTypeError",
    "build_universal_graph",
    "chunk_block",
    "chunk_text",
    "extract_document",
    "extract_document_file",
    "split_text_blocks",
    "suggest_primary_splits",
]

#: Version of the *document extraction content*; independent of the storage
#: schema version and of the code extractor's ``EXTRACTION_VERSION``.
DOCUMENT_EXTRACTION_VERSION: int = 1

#: Extension → document language. The W2 ``languages.EXTENSION_MAP`` is a
#: frozen code-only registry, so the document formats live here.
DOCUMENT_EXTENSION_MAP: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".pdf": "pdf",
    ".txt": "text",
}

#: Language names this module extracts.
DOCUMENT_LANGUAGES: frozenset[str] = frozenset({"markdown", "pdf", "text"})

#: Documents above this size are never read in full (fail-closed).
MAX_DOCUMENT_FILE_SIZE_BYTES = 16 * 1024 * 1024

#: Default canonical chunk size in characters.
DEFAULT_MAX_CHUNK_CHARS = 2000

#: Fixed timestamp for graph nodes built from artifacts: extraction is a pure
#: function of bytes, so graph construction must not leak a wall clock.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_WHITESPACE = " \t\n"


# --------------------------------------------------------------------- errors


class DocumentIngestError(IngestError):
    """Base class for document-ingestion failures; ``code`` is the API shape."""

    code = "DOCUMENT_INGEST_ERROR"


class UnsupportedDocumentTypeError(DocumentIngestError):
    """No document language claims this path/language."""

    code = "UNSUPPORTED_MEDIA_TYPE"

    def __init__(self, path: str) -> None:
        super().__init__(f"{path!r}: no document language registered for this file")
        self.path = path


class PdfParseError(DocumentIngestError):
    """The PDF is malformed; pypdf could not read its structure or pages."""

    code = "PDF_PARSE_ERROR"

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"{path!r}: PDF parse failed: {reason}")
        self.path = path
        self.reason = reason


class PdfEncryptedError(DocumentIngestError):
    """The PDF is encrypted and the empty password did not open it. No OCR,
    no password guessing — fail typed."""

    code = "PDF_ENCRYPTED"

    def __init__(self, path: str) -> None:
        super().__init__(f"{path!r}: PDF is encrypted; refusing to guess a password")
        self.path = path


# ------------------------------------------------------------------- chunking


@dataclass(frozen=True)
class ChunkingConfig:
    """Tuning for the canonical chunker (deterministic)."""

    max_chunk_chars: int = DEFAULT_MAX_CHUNK_CHARS

    def __post_init__(self) -> None:
        value = self.max_chunk_chars
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"max_chunk_chars must be an int >= 1, got {value!r}")


_BLANK_LINE_RE = re.compile(r"\n[ \t]*\n")


def split_text_blocks(text: str) -> tuple[tuple[int, str], ...]:
    """Split *text* into paragraphs at blank-line boundaries.

    Returns ``(offset, block)`` pairs where *offset* is the index of the
    block's first (non-whitespace) character in *text*. Whitespace-only
    blocks are dropped; offsets are strictly increasing.
    """
    blocks: list[tuple[int, str]] = []
    start = 0
    for match in _BLANK_LINE_RE.finditer(text):
        _append_block(text, start, match.start(), blocks)
        start = match.end()
    _append_block(text, start, len(text), blocks)
    return tuple(blocks)


def _append_block(text: str, start: int, end: int, out: list[tuple[int, str]]) -> None:
    raw = text[start:end]
    stripped = raw.strip()
    if not stripped:
        return
    lead = len(raw) - len(raw.lstrip())
    out.append((start + lead, stripped))


def chunk_block(block: str, *, base_offset: int, max_chars: int) -> tuple[tuple[int, str], ...]:
    """Chunk one paragraph, capping each chunk at *max_chars* characters.

    A block within the cap is one chunk. Otherwise the split point is the
    last whitespace inside the window; a window with no whitespace is cut
    hard at the cap. Every chunk is within the cap, non-whitespace content
    is preserved in order exactly once, and offsets are relative to
    *base_offset* (the block's offset in its source unit).
    """
    chunks: list[tuple[int, str]] = []
    start = 0
    total = len(block)
    while start < total:
        end = min(start + max_chars, total)
        if end < total:
            cut = -1
            index = end - 1
            while index > start:
                if block[index] in _WHITESPACE:
                    cut = index
                    break
                index -= 1
            if cut > start:
                end = cut
        raw = block[start:end]
        piece = raw.strip()
        lead = len(raw) - len(raw.lstrip())
        if piece:
            chunks.append((base_offset + start + lead, piece))
        start = end
        while start < total and block[start] in _WHITESPACE:
            start += 1
    return tuple(chunks)


def chunk_text(text: str, *, max_chars: int) -> tuple[tuple[int, str], ...]:
    """Split *text* into paragraphs, then chunk each paragraph canonically."""
    chunks: list[tuple[int, str]] = []
    for offset, block in split_text_blocks(text):
        chunks.extend(chunk_block(block, base_offset=offset, max_chars=max_chars))
    return tuple(chunks)


# -------------------------------------------------------------------- result


@dataclass(frozen=True)
class DocumentExtractionResult:
    """The deterministic output of extracting one document.

    Mirrors the W2 :class:`ExtractionResult` protocol (``nodes`` / ``edges``
    / ``unresolved_refs`` / ``language`` / ``sha256`` / ``size`` /
    ``generated_hint`` / ``parse_error`` / ``extraction_version``) so the
    ingestion pipeline consumes it unchanged, plus a ``metadata`` mapping for
    document-level facts (``no_text_layer``, ``page_count``, ...).
    """

    path: str
    language: str
    sha256: str
    size: int
    generated_hint: bool
    parse_error: bool
    extraction_version: int
    nodes: tuple[ExtractedNode, ...]
    edges: tuple[ExtractedEdge, ...]
    unresolved_refs: tuple[UnresolvedRef, ...]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_artifact(self) -> dict[str, Any]:
        """The canonical artifact dict: sorted keys, sorted collections."""
        nodes = sorted(self.nodes, key=lambda n: (n.start_line, n.start_col, n.node_id))
        edges = sorted(self.edges, key=lambda e: (e.source, e.target, e.kind))
        refs = sorted(
            self.unresolved_refs,
            key=lambda r: (r.source_node_id, r.target_name, r.kind, r.line, r.col),
        )
        return {
            "edges": [{"kind": edge.kind, "source": edge.source, "target": edge.target} for edge in edges],
            "extraction_version": self.extraction_version,
            "generated_hint": self.generated_hint,
            "language": self.language,
            "metadata": dict(self.metadata),
            "nodes": [
                {
                    "end_col": node.end_col,
                    "end_line": node.end_line,
                    "kind": node.kind,
                    "name": node.name,
                    "node_id": node.node_id,
                    "path": node.path,
                    "qualified_name": node.qualified_name,
                    "start_col": node.start_col,
                    "start_line": node.start_line,
                }
                for node in nodes
            ],
            "parse_error": self.parse_error,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "unresolved_refs": [
                {
                    "col": ref.col,
                    "kind": ref.kind,
                    "line": ref.line,
                    "name_tail": ref.name_tail,
                    "source_node_id": ref.source_node_id,
                    "target_name": ref.target_name,
                }
                for ref in refs
            ],
        }

    def canonical_json(self) -> str:
        """Byte-stable one-line JSON of :meth:`to_artifact` (sorted keys)."""
        return canonical_json(self.to_artifact())


# ------------------------------------------------------------------ internals


@dataclass(frozen=True)
class _Block:
    """One scanned markdown block."""

    kind: str  # "heading" | "paragraph" | "list" | "code"
    start_line: int
    text: str
    level: int = 0


_HEADING_RE = re.compile(r"^[ \t]{0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_TRAILING_HASHES_RE = re.compile(r"[ \t]+#+$")
_FENCE_OPEN_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_LIST_ITEM_RE = re.compile(r"^[ \t]{0,3}(?:[-*+]|\d{1,9}[.)])(?:[ \t]+|$)")
_LINK_RE = re.compile(r"(?<!!)\[[^\]\n]*\]\(\s*(<[^>\n]*>|[^\s)]+)(?:[ \t]+[^)\n]*)?\)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def _line_starts(text: str) -> list[int]:
    starts = [0]
    for index, char in enumerate(text):
        if char == "\n":
            starts.append(index + 1)
    return starts


def _line_of(starts: list[int], offset: int) -> int:
    """The 1-indexed line containing *offset* (binary-search free: offsets
    are small and the walk is deterministic)."""
    line = 1
    for index, start in enumerate(starts):
        if start > offset:
            break
        line = index + 1
    return line


def _scan_markdown(text: str) -> list[_Block]:
    """Line-oriented markdown scan: ATX headings, fenced code, lists, paragraphs."""
    blocks: list[_Block] = []
    buf: list[str] = []
    buf_kind = ""
    buf_start = 0
    fence_char = ""
    fence_len = 0
    fence_lines: list[str] = []
    fence_content_start = 0
    in_fence = False

    def flush_buffer() -> None:
        nonlocal buf, buf_kind, buf_start
        if buf:
            blocks.append(_Block(kind=buf_kind, start_line=buf_start, text="\n".join(buf)))
            buf = []
            buf_kind = ""

    for line_no, line in enumerate(text.split("\n"), start=1):
        if in_fence:
            closing = re.match(r"^[ \t]{0,3}" + re.escape(fence_char) + "{" + str(fence_len) + r",}[ \t]*$", line)
            if closing:
                blocks.append(_Block(kind="code", start_line=fence_content_start, text="\n".join(fence_lines)))
                in_fence = False
                fence_lines = []
            else:
                fence_lines.append(line)
            continue
        fence = _FENCE_OPEN_RE.match(line)
        if fence:
            flush_buffer()
            marker = fence.group(1)
            fence_char = marker[0]
            fence_len = len(marker)
            fence_lines = []
            fence_content_start = line_no + 1
            in_fence = True
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush_buffer()
            level = len(heading.group(1))
            title = _TRAILING_HASHES_RE.sub("", heading.group(2) or "").strip()
            blocks.append(_Block(kind="heading", start_line=line_no, text=title or "(empty)", level=level))
            continue
        if not line.strip():
            flush_buffer()
            continue
        if _LIST_ITEM_RE.match(line):
            if buf_kind and buf_kind != "list":
                flush_buffer()
            if not buf_kind:
                buf_kind = "list"
                buf_start = line_no
                buf = []
            buf.append(line)
            continue
        if not buf_kind:
            buf_kind = "paragraph"
            buf_start = line_no
            buf = []
        buf.append(line)
    flush_buffer()
    if in_fence:
        # CommonMark: an unclosed fence closes at end of input — not an error.
        blocks.append(_Block(kind="code", start_line=fence_content_start, text="\n".join(fence_lines)))
    return blocks


def _section_end_lines(blocks: list[_Block], total_lines: int) -> dict[int, int]:
    """block index → end line: the line before the next heading of equal or
    shallower level, else the last line of the document."""
    ends: dict[int, int] = {}
    stack: list[int] = []
    for index, block in enumerate(blocks):
        if block.kind != "heading":
            continue
        while stack and blocks[stack[-1]].level >= block.level:
            end_index = stack.pop()
            ends_at = block.start_line - 1
            ends[end_index] = max(ends_at, blocks[end_index].start_line)
        stack.append(index)
    for index in stack:
        ends[index] = total_lines
    return ends


def _extract_link_targets(
    block_text: str,
    block_start_line: int,
    chunks: tuple[tuple[int, str], ...],
    chunk_node_ids: tuple[str, ...],
) -> list[tuple[str, str, str, int, int]]:
    """Inline markdown links → (source_node_id, target_name, name_tail, line, col).

    Only inter-document targets are parked: pure anchors and scheme-ful URLs
    (``https:``, ``mailto:``, ...) are skipped. The source is the chunk whose
    span contains the link.
    """
    found: list[tuple[str, str, str, int, int]] = []
    for match in _LINK_RE.finditer(block_text):
        target = match.group(1)
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1]
        if not target or target.startswith("#") or _SCHEME_RE.match(target):
            continue
        target_doc = target.split("#", 1)[0]
        if not target_doc:
            continue
        name_tail = target_doc.rsplit("/", 1)[-1]
        if not name_tail:
            continue
        local = match.start()
        # deterministic attribution: the last chunk starting at or before the link
        source_id = chunk_node_ids[0]
        for (chunk_offset, _chunk), node_id in zip(chunks, chunk_node_ids, strict=True):
            if chunk_offset <= local:
                source_id = node_id
            else:
                break
        line = block_start_line + block_text.count("\n", 0, local)
        line_start = block_text.rfind("\n", 0, local) + 1
        col = local - line_start
        found.append((source_id, target_doc, name_tail, line, col))
    return found


# ------------------------------------------------------------------ markdown


def _extract_markdown(path: str, source: str, config: ChunkingConfig) -> DocumentExtractionResult:
    starts = _line_starts(source)
    total_lines = len(starts)
    basename = PurePosixPath(path).name
    file_id = file_node_id(path)
    document_id = make_node_id(path=path, kind=CoreNodeKind.DOCUMENT.value, name=basename, line=1)

    nodes: list[ExtractedNode] = [
        ExtractedNode(
            node_id=file_id,
            kind=CoreNodeKind.FILE.value,
            name=basename,
            qualified_name=path,
            path=path,
            start_line=1,
            end_line=max(1, total_lines),
            start_col=0,
            end_col=0,
        ),
        ExtractedNode(
            node_id=document_id,
            kind=CoreNodeKind.DOCUMENT.value,
            name=basename,
            qualified_name=path,
            path=path,
            start_line=1,
            end_line=max(1, total_lines),
            start_col=0,
            end_col=0,
        ),
    ]
    edges: list[ExtractedEdge] = [ExtractedEdge(source=file_id, target=document_id)]
    refs: list[UnresolvedRef] = []
    ref_keys: set[tuple[str, str, str, int, int]] = set()

    blocks = _scan_markdown(source)
    section_ends = _section_end_lines(blocks, total_lines)
    section_stack: list[tuple[int, str, str]] = []
    section_count = 0
    chunk_count = 0

    for index, block in enumerate(blocks):
        if block.kind == "heading":
            while section_stack and section_stack[-1][0] >= block.level:
                section_stack.pop()
            parent_id = section_stack[-1][1] if section_stack else document_id
            node_id = make_node_id(path=path, kind=CoreNodeKind.SECTION.value, name=block.text, line=block.start_line)
            qualified = "::".join([path, *(entry[2] for entry in section_stack), block.text])
            nodes.append(
                ExtractedNode(
                    node_id=node_id,
                    kind=CoreNodeKind.SECTION.value,
                    name=block.text,
                    qualified_name=qualified,
                    path=path,
                    start_line=block.start_line,
                    end_line=section_ends.get(index, block.start_line),
                    start_col=0,
                    end_col=0,
                )
            )
            edges.append(ExtractedEdge(source=parent_id, target=node_id))
            section_stack.append((block.level, node_id, block.text))
            section_count += 1
            continue

        raw = block.text
        stripped = raw.strip()
        if not stripped:
            continue
        lead = len(raw) - len(raw.lstrip())
        base_offset = starts[block.start_line - 1] + lead
        parent_id = section_stack[-1][1] if section_stack else document_id
        chunks = chunk_block(stripped, base_offset=base_offset, max_chars=config.max_chunk_chars)
        if not chunks:
            continue
        chunk_ids: list[str] = []
        end_line = block.start_line + stripped.count("\n")
        for offset, _chunk in chunks:
            name = f"l{block.start_line}:o{offset}"
            node_id = make_node_id(path=path, kind=CoreNodeKind.PARAGRAPH.value, name=name, line=block.start_line)
            qualified = "::".join([path, *(entry[2] for entry in section_stack), name])
            nodes.append(
                ExtractedNode(
                    node_id=node_id,
                    kind=CoreNodeKind.PARAGRAPH.value,
                    name=name,
                    qualified_name=qualified,
                    path=path,
                    start_line=block.start_line,
                    end_line=end_line,
                    start_col=0,
                    end_col=0,
                )
            )
            edges.append(ExtractedEdge(source=parent_id, target=node_id))
            chunk_ids.append(node_id)
            chunk_count += 1
        if block.kind != "code":
            relative_chunks = tuple((offset - base_offset, chunk) for offset, chunk in chunks)
            for source_id, target, tail, line, col in _extract_link_targets(
                stripped, block.start_line, relative_chunks, tuple(chunk_ids)
            ):
                key = (source_id, target, "references", line, col)
                if key in ref_keys:
                    continue
                ref_keys.add(key)
                refs.append(
                    UnresolvedRef(
                        source_node_id=source_id,
                        target_name=target,
                        name_tail=tail,
                        kind="references",
                        line=line,
                        col=col,
                    )
                )

    return DocumentExtractionResult(
        path=path,
        language="markdown",
        sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        size=len(source.encode("utf-8")),
        generated_hint=False,
        parse_error=False,
        extraction_version=DOCUMENT_EXTRACTION_VERSION,
        nodes=tuple(nodes),
        edges=tuple(edges),
        unresolved_refs=tuple(refs),
        metadata={
            "format": "markdown",
            "section_count": section_count,
            "chunk_count": chunk_count,
            "link_count": len(refs),
        },
    )


# ------------------------------------------------------------------ plaintext


def _extract_plaintext(path: str, source: str, config: ChunkingConfig) -> DocumentExtractionResult:
    starts = _line_starts(source)
    total_lines = len(starts)
    basename = PurePosixPath(path).name
    file_id = file_node_id(path)
    document_id = make_node_id(path=path, kind=CoreNodeKind.DOCUMENT.value, name=basename, line=1)

    nodes: list[ExtractedNode] = [
        ExtractedNode(
            node_id=file_id,
            kind=CoreNodeKind.FILE.value,
            name=basename,
            qualified_name=path,
            path=path,
            start_line=1,
            end_line=max(1, total_lines),
            start_col=0,
            end_col=0,
        ),
        ExtractedNode(
            node_id=document_id,
            kind=CoreNodeKind.DOCUMENT.value,
            name=basename,
            qualified_name=path,
            path=path,
            start_line=1,
            end_line=max(1, total_lines),
            start_col=0,
            end_col=0,
        ),
    ]
    edges: list[ExtractedEdge] = [ExtractedEdge(source=file_id, target=document_id)]

    chunk_count = 0
    for offset, chunk in chunk_text(source, max_chars=config.max_chunk_chars):
        line = _line_of(starts, offset)
        name = f"l{line}:o{offset}"
        node_id = make_node_id(path=path, kind=CoreNodeKind.PARAGRAPH.value, name=name, line=line)
        nodes.append(
            ExtractedNode(
                node_id=node_id,
                kind=CoreNodeKind.PARAGRAPH.value,
                name=name,
                qualified_name=f"{path}::{name}",
                path=path,
                start_line=line,
                end_line=line + chunk.count("\n"),
                start_col=0,
                end_col=0,
            )
        )
        edges.append(ExtractedEdge(source=document_id, target=node_id))
        chunk_count += 1

    return DocumentExtractionResult(
        path=path,
        language="text",
        sha256=hashlib.sha256(source.encode("utf-8")).hexdigest(),
        size=len(source.encode("utf-8")),
        generated_hint=False,
        parse_error=False,
        extraction_version=DOCUMENT_EXTRACTION_VERSION,
        nodes=tuple(nodes),
        edges=tuple(edges),
        unresolved_refs=(),
        metadata={"format": "text", "chunk_count": chunk_count},
    )


# ------------------------------------------------------------------------ pdf


def _extract_pdf(path: str, data: bytes, config: ChunkingConfig) -> DocumentExtractionResult:
    try:
        reader = PdfReader(io.BytesIO(data))
        encrypted = bool(reader.is_encrypted)
    except Exception as error:  # pypdf raises a family of parse errors
        raise PdfParseError(path, str(error) or type(error).__name__) from error
    if encrypted:
        try:
            outcome = reader.decrypt("")
        except Exception as error:
            raise PdfParseError(path, str(error) or type(error).__name__) from error
        if not outcome:
            raise PdfEncryptedError(path)
    try:
        page_texts = [(page.extract_text() or "") for page in reader.pages]
    except Exception as error:
        raise PdfParseError(path, str(error) or type(error).__name__) from error

    page_count = len(page_texts)
    text_pages = sum(1 for text in page_texts if text.strip())
    no_text_layer = text_pages == 0

    basename = PurePosixPath(path).name
    file_id = file_node_id(path)
    document_id = make_node_id(path=path, kind=CoreNodeKind.DOCUMENT.value, name=basename, line=1)

    nodes: list[ExtractedNode] = [
        ExtractedNode(
            node_id=file_id,
            kind=CoreNodeKind.FILE.value,
            name=basename,
            qualified_name=path,
            path=path,
            start_line=1,
            end_line=max(1, page_count),
            start_col=0,
            end_col=0,
        ),
        ExtractedNode(
            node_id=document_id,
            kind=CoreNodeKind.DOCUMENT.value,
            name=basename,
            qualified_name=path,
            path=path,
            start_line=1,
            end_line=max(1, page_count),
            start_col=0,
            end_col=0,
        ),
    ]
    edges: list[ExtractedEdge] = [ExtractedEdge(source=file_id, target=document_id)]

    chunk_count = 0
    if not no_text_layer:
        for page_no, page_text in enumerate(page_texts, start=1):
            chunks = chunk_text(page_text, max_chars=config.max_chunk_chars)
            if not chunks:
                continue  # a text-less page in a mixed scan gets no page node
            page_name = f"page-{page_no}"
            page_id = make_node_id(path=path, kind=CoreNodeKind.PAGE.value, name=page_name, line=page_no)
            nodes.append(
                ExtractedNode(
                    node_id=page_id,
                    kind=CoreNodeKind.PAGE.value,
                    name=page_name,
                    qualified_name=f"{path}::{page_name}",
                    path=path,
                    start_line=page_no,
                    end_line=page_no,
                    start_col=0,
                    end_col=0,
                )
            )
            edges.append(ExtractedEdge(source=document_id, target=page_id))
            for offset, _chunk in chunks:
                name = f"p{page_no}:o{offset}"
                node_id = make_node_id(path=path, kind=CoreNodeKind.PARAGRAPH.value, name=name, line=page_no)
                nodes.append(
                    ExtractedNode(
                        node_id=node_id,
                        kind=CoreNodeKind.PARAGRAPH.value,
                        name=name,
                        qualified_name=f"{path}::{page_name}::{name}",
                        path=path,
                        start_line=page_no,
                        end_line=page_no,
                        start_col=0,
                        end_col=0,
                    )
                )
                edges.append(ExtractedEdge(source=page_id, target=node_id))
                chunk_count += 1

    return DocumentExtractionResult(
        path=path,
        language="pdf",
        sha256=hashlib.sha256(data).hexdigest(),
        size=len(data),
        generated_hint=False,
        parse_error=False,
        extraction_version=DOCUMENT_EXTRACTION_VERSION,
        nodes=tuple(nodes),
        edges=tuple(edges),
        unresolved_refs=(),
        metadata={
            "format": "pdf",
            "no_text_layer": no_text_layer,
            "page_count": page_count,
            "text_pages": text_pages,
            "chunk_count": chunk_count,
        },
    )


# ------------------------------------------------------------------ dispatch


def _language_for_document_path(path: str) -> str | None:
    suffix = PurePosixPath(path).suffix.lower()
    return DOCUMENT_EXTENSION_MAP.get(suffix)


def extract_document(
    path: str,
    data: bytes,
    *,
    language: str | None = None,
    config: ChunkingConfig | None = None,
) -> DocumentExtractionResult:
    """Extract one document from its raw bytes.

    *path* is the contract path embedded in node ids. *language* defaults to
    the extension mapping; an explicit language must be a document language.
    """
    if not isinstance(path, str) or not path:
        raise DocumentIngestError("path must be a non-empty string")
    if not isinstance(data, bytes):
        raise DocumentIngestError(f"data must be bytes, got {type(data).__name__}")
    resolved = language if language is not None else _language_for_document_path(path)
    if resolved not in DOCUMENT_LANGUAGES:
        raise UnsupportedDocumentTypeError(path)
    if len(data) > MAX_DOCUMENT_FILE_SIZE_BYTES:
        raise FileTooLargeError(path, size=len(data), limit=MAX_DOCUMENT_FILE_SIZE_BYTES)
    config = config if config is not None else ChunkingConfig()
    if resolved == "pdf":
        return _extract_pdf(path, data, config)
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SourceDecodeError(path) from error
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    if resolved == "markdown":
        result = _extract_markdown(path, source, config)
    else:
        result = _extract_plaintext(path, source, config)
    # sha256/size track the raw file bytes (change detection), not the
    # BOM-stripped, newline-normalized text.
    return replace(result, sha256=hashlib.sha256(data).hexdigest(), size=len(data))


def extract_document_file(
    path: str | Path,
    *,
    display_path: str | None = None,
    config: ChunkingConfig | None = None,
) -> DocumentExtractionResult:
    """Read *path* with the size bound and extract it.

    ``display_path`` (defaults to ``str(path)``) is the contract path embedded
    in node ids — pass the project-relative path for stable identities.
    """
    file_path = Path(path)
    contract_path = display_path if display_path is not None else str(file_path)
    language = _language_for_document_path(file_path.name)
    if language is None:
        raise UnsupportedDocumentTypeError(str(file_path))
    size = file_path.stat().st_size
    if size > MAX_DOCUMENT_FILE_SIZE_BYTES:
        raise FileTooLargeError(str(file_path), size=size, limit=MAX_DOCUMENT_FILE_SIZE_BYTES)
    data = file_path.read_bytes()
    if len(data) > MAX_DOCUMENT_FILE_SIZE_BYTES:
        # the file grew between stat and read — fail closed
        raise FileTooLargeError(str(file_path), size=len(data), limit=MAX_DOCUMENT_FILE_SIZE_BYTES)
    return extract_document(contract_path, data, language=language, config=config)


# ---------------------------------------------------------------------- graph


def build_universal_graph(result: DocumentExtractionResult) -> UniversalGraph:
    """Build a :class:`UniversalGraph` from an extraction result.

    Node timestamps are pinned to the epoch so construction is a pure
    function of the artifact; the document node carries the extraction
    metadata (e.g. ``no_text_layer``) as its payload.
    """
    graph = UniversalGraph()
    for node in result.nodes:
        payload: dict[str, Any] = dict(result.metadata) if node.kind == CoreNodeKind.DOCUMENT.value else {}
        if node.kind == CoreNodeKind.FILE.value:
            graph_node = GraphNode(
                node_id=node.node_id,
                kind=node.kind,
                name=node.name,
                path=node.path,
                payload=payload,
                updated_at=_EPOCH,
            )
        else:
            graph_node = GraphNode(
                node_id=node.node_id,
                kind=node.kind,
                name=node.name,
                path=node.path,
                line=node.start_line,
                payload=payload,
                updated_at=_EPOCH,
            )
        graph.add_node(graph_node)
    for edge in result.edges:
        graph.add_edge(GraphEdge(source=edge.source, target=edge.target, kind=edge.kind))
    return graph


def suggest_primary_splits(
    result: DocumentExtractionResult,
    *,
    thresholds: SplitThresholds | None = None,
    domain: PrimaryDomain = PrimaryDomain.P1,
) -> tuple[SplitProposal, ...]:
    """Proposal-only split suggestions for an (oversized) extracted document.

    The extraction tree is wrapped under a synthetic primary root so the
    frozen :func:`propose_primary_splits` applies; the input result and the
    graph built from it are never mutated beyond that local wrapper.
    """
    graph = build_universal_graph(result)
    root_id = make_node_id(path=result.path, kind=CoreNodeKind.PRIMARY.value, name="document-root")
    graph.add_node(
        GraphNode(
            node_id=root_id,
            kind=CoreNodeKind.PRIMARY.value,
            name="document-root",
            path=result.path,
            domain=domain,
            purpose="document split proposal root",
            updated_at=_EPOCH,
        )
    )
    for node in graph.nodes():
        if node.node_id != root_id and graph.contains_parent(node.node_id) is None:
            graph.add_edge(GraphEdge(source=root_id, target=node.node_id, kind="contains"))
    return propose_primary_splits(graph, root_id, thresholds)
