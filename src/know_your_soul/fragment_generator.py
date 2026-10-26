"""Deterministic local fragment generator replacing model-backed extraction.

Task 3 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 13.5 and matrix rows 1-3 (mandatory adapter A1).

The generator parses markdown structure (headings, wikilinks) with one pinned
grammar and emits Graphify's node/edge schema with stable file-scoped ids
derived from the relative source path via Graphify's own ``make_id`` recipe.
There is no model backend here and nothing in this module can reach one: ids,
labels and edges are pure functions of the source bytes.

* Sources are decoded as UTF-8 and normalized (``\\r\\n``/``\\r`` -> ``\\n``,
  NFC) so the same logical document produces the same fragment regardless of
  checkout line-ending conventions (matrix row 14).
* A source larger than ``max_source_bytes`` is truncated and stamped
  ``partial``; truncated fragments are never cached or served from cache
  (matrix row 7).
* ``FRAGMENT_PROMPT_FINGERPRINT`` is the stable synthetic-prompt fingerprint
  every fragment carries so deterministic output stays attributable
  (matrix row 6).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from graphify.ids import make_id

from know_your_soul.domain import canonical_json

__all__ = [
    "FRAGMENT_GENERATOR_VERSION",
    "FRAGMENT_PROMPT_FINGERPRINT",
    "MAX_SOURCE_BYTES",
    "PINNED_GRAMMARS",
    "FragmentError",
    "FragmentGenerator",
    "GeneratedFragment",
    "file_scoped_id",
    "slugify",
]

FRAGMENT_GENERATOR_VERSION = "1.0.0"
"""Bumping this version changes fragment identity and invalidates the cache."""

PINNED_GRAMMARS: tuple[str, ...] = ("markdown",)
"""The complete grammar set of the enabled surface (matrix row 1)."""

MAX_SOURCE_BYTES = 1_048_576
"""Sources beyond this size are truncated and stamped ``partial``."""

FRAGMENT_PROMPT_FINGERPRINT: str = hashlib.sha256(
    canonical_json(
        {
            "generator": "know-your-soul-deterministic-fragment-generator",
            "generator_version": FRAGMENT_GENERATOR_VERSION,
            "grammars": list(PINNED_GRAMMARS),
            "semantics": "markdown-structure-v1",
        }
    ).encode("utf-8")
).hexdigest()
"""Stable synthetic prompt fingerprint; a bump is a logged full re-derivation."""

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)(?:\|[^\[\]]*)?\]\]")


class FragmentError(ValueError):
    """A source cannot be turned into a fragment (e.g. it is not UTF-8)."""


def slugify(text: str) -> str:
    """Deterministic slug: NFKC, casefolded, non-word runs collapsed."""
    normalized = unicodedata.normalize("NFKC", text).casefold()
    slug = re.sub(r"[^\w]+", "-", normalized, flags=re.UNICODE).strip("-")
    return slug or "section"


def file_scoped_id(rel_path: str) -> str:
    """The stable node id for a source file, derived from its relative path.

    Uses Graphify's own canonical id recipe (``graphify.ids.make_id``) so
    ids reconcile with Graphify's build-time re-derivation instead of
    ghosting. The suffix is dropped and separators collapse, so
    ``docs/v1/API Ref.md`` becomes ``docs_v1_api_ref`` on every host.
    """
    normalized = rel_path.replace("\\", "/").lstrip("/")
    while ".." in normalized:
        normalized = normalized.replace("..", "")
    stem = PurePosixPath(normalized).with_suffix("").as_posix()
    return make_id(stem.replace("/", "_")) or "doc"


@dataclass(frozen=True)
class GeneratedFragment:
    """The raw deterministic fragment for one source file (pre-validation)."""

    source_file: str
    partial: bool
    fingerprint: str
    generator_version: str
    nodes: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    edges: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {"nodes": list(self.nodes), "edges": list(self.edges), "hyperedges": []}


class FragmentGenerator:
    """Markdown-structure fragment generator; the only extraction engine."""

    generator_version = FRAGMENT_GENERATOR_VERSION

    def __init__(self, *, max_source_bytes: int = MAX_SOURCE_BYTES) -> None:
        if max_source_bytes < 1:
            raise ValueError("max_source_bytes must be positive")
        self._max_source_bytes = max_source_bytes

    @property
    def max_source_bytes(self) -> int:
        return self._max_source_bytes

    def generate(self, *, source_file: str, content: bytes) -> GeneratedFragment:
        """Parse *content* into Graphify-schema nodes and edges.

        ``source_file`` is the corpus-relative path; it scopes every id the
        fragment emits and is what later per-file re-extraction replaces on.
        """
        partial = len(content) > self._max_source_bytes
        raw = content[: self._max_source_bytes] if partial else content
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise FragmentError(f"{source_file!r} is not UTF-8: {error}") from error
        text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))

        file_id = file_scoped_id(source_file)
        headings: list[tuple[int, str]] = [
            (len(match.group(1)), match.group(2)) for line in text.split("\n") if (match := _HEADING_RE.match(line))
        ]
        title = headings[0][1] if headings and headings[0][0] == 1 else None
        sections = headings[1:] if title is not None else headings

        file_node: dict[str, Any] = {
            "id": file_id,
            "label": title or PurePosixPath(source_file).stem,
            "file_type": "document",
            "source_file": source_file,
            "heading_level": 0,
        }
        nodes: list[dict[str, Any]] = [file_node]
        edges: list[dict[str, Any]] = []

        stack: list[tuple[int, str]] = []
        used: dict[str, int] = {}
        for level, heading in sections:
            base = slugify(heading)
            occurrence = used.get(base, 0) + 1
            used[base] = occurrence
            section_id = make_id(file_id, base if occurrence == 1 else f"{base}_{occurrence}")
            while stack and stack[-1][0] >= level:
                stack.pop()
            parent_id = stack[-1][1] if stack else file_id
            stack.append((level, section_id))
            nodes.append(
                {
                    "id": section_id,
                    "label": heading,
                    "file_type": "document",
                    "source_file": source_file,
                    "heading_level": level,
                }
            )
            edges.append(
                {
                    "source": parent_id,
                    "target": section_id,
                    "relation": "contains",
                    "confidence": "EXTRACTED",
                    "source_file": source_file,
                }
            )

        seen_links: set[str] = set()
        for match in _WIKILINK_RE.finditer(text):
            target_text = match.group(1).strip()
            if not target_text or target_text in seen_links:
                continue
            seen_links.add(target_text)
            target_path = target_text if PurePosixPath(target_text).suffix else f"{target_text}.md"
            edges.append(
                {
                    "source": file_id,
                    "target": file_scoped_id(target_path),
                    "relation": "references",
                    "confidence": "EXTRACTED",
                    "source_file": source_file,
                }
            )

        return GeneratedFragment(
            source_file=source_file,
            partial=partial,
            fingerprint=FRAGMENT_PROMPT_FINGERPRINT,
            generator_version=FRAGMENT_GENERATOR_VERSION,
            nodes=tuple(nodes),
            edges=tuple(edges),
        )
