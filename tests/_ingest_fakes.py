"""Shared deterministic fakes for the ingestion-pipeline tests (SUB-89).

These implement the real sibling protocols — SUB-87's
``extract_source(path, source, *, language=None, generated_hint=False)``
returning an object with ``nodes``/``edges``/``unresolved_refs`` and SUB-88's
``resolve_pending(store)`` — without depending on the sibling modules, so the
pipeline tests also pass on a checkout where they have not landed yet.
Everything here is a pure function of the input bytes.

Fixture DSL (line-oriented, language-agnostic):

* ``def name(`` at line start        -> ``function`` node
* ``class Name`` at line start       -> ``class`` node
* ``-> dotted.Name`` inside a body   -> unresolved ref (kind ``calls``)
  sourced at the most recent node (or the file node before any node)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from know_your_soul.graph_model import file_node_id, make_node_id
from know_your_soul.store import EventStore

_DEF_RE = re.compile(r"def ([A-Za-z_][A-Za-z0-9_]*)\(")
_CLASS_RE = re.compile(r"class ([A-Za-z_][A-Za-z0-9_]*)")
_REF_RE = re.compile(r"^\s*->\s*([A-Za-z_][A-Za-z0-9_.]*)\s*$")


@dataclass(frozen=True)
class FakeNode:
    node_id: str
    kind: str
    name: str
    start_line: int
    start_col: int


@dataclass(frozen=True)
class FakeEdge:
    source: str
    target: str
    kind: str
    line: int | None = None
    col: int | None = None
    metadata: str | None = None


@dataclass(frozen=True)
class FakeRef:
    source_node_id: str
    target_name: str
    name_tail: str
    kind: str


@dataclass(frozen=True)
class FakeExtraction:
    path: str
    language: str
    sha256: str
    size: int
    generated_hint: bool
    extraction_version: int
    nodes: tuple[FakeNode, ...]
    edges: tuple[FakeEdge, ...]
    unresolved_refs: tuple[FakeRef, ...]


class ToyExtractor:
    """Deterministic line-based extractor implementing the SUB-87 protocol."""

    EXTRACTION_VERSION = 1

    def __init__(self, *, version: int = 1) -> None:
        self.EXTRACTION_VERSION = version

    def extract_source(
        self,
        path: str,
        source: str,
        *,
        language: str | None = None,
        generated_hint: bool = False,
    ) -> FakeExtraction:
        file_id = file_node_id(path)
        nodes: list[FakeNode] = []
        edges: list[FakeEdge] = []
        refs: list[FakeRef] = []
        last_node = file_id
        for lineno, line in enumerate(source.splitlines(), start=1):
            match = _DEF_RE.match(line) or _CLASS_RE.match(line)
            if match is not None:
                kind = "function" if line.startswith("def ") else "class"
                name = match.group(1)
                node_id = make_node_id(path=path, kind=kind, name=name, line=lineno)
                nodes.append(FakeNode(node_id=node_id, kind=kind, name=name, start_line=lineno, start_col=0))
                edges.append(FakeEdge(source=file_id, target=node_id, kind="contains", line=lineno))
                last_node = node_id
                continue
            ref_match = _REF_RE.match(line)
            if ref_match is not None:
                target = ref_match.group(1)
                refs.append(
                    FakeRef(
                        source_node_id=last_node,
                        target_name=target,
                        name_tail=target.rsplit(".", 1)[-1],
                        kind="calls",
                    )
                )
        encoded = source.encode("utf-8")
        return FakeExtraction(
            path=path,
            language=language or "python",
            sha256=hashlib.sha256(encoded).hexdigest(),
            size=len(encoded),
            generated_hint=generated_hint,
            extraction_version=self.EXTRACTION_VERSION,
            nodes=tuple(nodes),
            edges=tuple(edges),
            unresolved_refs=tuple(refs),
        )


class FailingExtractor(ToyExtractor):
    """Extractor that raises on any file whose name contains ``boom``."""

    def extract_source(
        self, path: str, source: str, *, language: str | None = None, generated_hint: bool = False
    ) -> FakeExtraction:
        if "boom" in path:
            raise RuntimeError("synthetic extraction failure")
        return super().extract_source(path, source, language=language, generated_hint=generated_hint)


class ToyResolver:
    """Deterministic resolver implementing the SUB-88 protocol.

    Resolves a pending ref when exactly one live symbol matches the ref's
    ``name_tail`` (last dot segment); otherwise marks it failed.
    """

    def resolve_pending(self, store: EventStore) -> dict[str, int]:
        resolved = 0
        failed = 0
        for ref in store.pending_unresolved_refs():
            matches = store.symbols_named(ref.name_tail)
            if len(matches) == 1:
                store.add_graph_edge(ref.source_node_id, matches[0].node_id, ref.kind)
                store.record_resolution_attempt(ref.ref_id, outcome="resolved")
                resolved += 1
            else:
                store.record_resolution_attempt(ref.ref_id, outcome="failed")
                failed += 1
        return {"resolved": resolved, "failed": failed, "pending": 0}


def write_fixture(root: Any, files: dict[str, str]) -> None:
    """Write a deterministic fixture tree (parent dirs created, UTF-8)."""
    base = Path(root)
    for rel, text in sorted(files.items()):
        target = base / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
