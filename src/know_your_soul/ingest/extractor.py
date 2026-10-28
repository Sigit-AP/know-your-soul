"""Tree-sitter extraction engine for code ingestion (wave W2, SUB-87).

Spec: RESEARCH-INGESTION §2b/§2c/§5 (codegraph ``TreeSitterExtractor``) adapted
to the frozen W1 contract (:mod:`know_your_soul.graph_model`) and the W1 store
helpers (:mod:`know_your_soul.store`). This module:

* parses with ``tree_sitter_language_pack.get_parser`` (prebuilt grammars);
* walks the AST in canonical child order with a scope stack, emitting one
  node per declaration (``kind:sha256(path:kind:name:line)[:col]`` ids via
  :func:`~know_your_soul.graph_model.make_node_id`), ``contains`` edges rooted
  at the ``file:<path>`` root node, and *unresolved* references for every
  cross-file name (calls and imports) — resolution is a separate pass by
  contract;
* bounds reads at 1 MiB (``FILE_TOO_LARGE``), detects the language from the
  extension (``UNSUPPORTED_MEDIA_TYPE``), decodes UTF-8 strictly
  (``DECODE_ERROR``), and flags generated/minified files (``generated_hint`` —
  a relevance hint, never a filter, per codegraph §5);
* produces a canonical, artifact-ready dict: sorted collections, no sets,
  byte-identical output across runs and across ``PYTHONHASHSEED`` values.

No model, provider, clock or randomness anywhere in this module.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tree_sitter_language_pack import get_parser

from know_your_soul.domain import canonical_json
from know_your_soul.graph_model import CoreNodeKind, file_node_id, make_node_id
from know_your_soul.ingest.languages import (
    EXTENSION_MAP,
    ImportRule,
    LanguageSpec,
    VariableRule,
    get_spec,
    language_for_path,
)

if TYPE_CHECKING:
    from tree_sitter import Node as TSNode

__all__ = [
    "EXTRACTION_VERSION",
    "MAX_SOURCE_FILE_SIZE_BYTES",
    "ExtractedEdge",
    "ExtractedNode",
    "ExtractionError",
    "ExtractionResult",
    "FileTooLargeError",
    "IngestError",
    "SourceDecodeError",
    "TreeSitterExtractor",
    "UnresolvedRef",
    "UnsupportedMediaTypeError",
    "detect_generated",
    "extract_file",
    "extract_source",
]

#: Version of the *extraction content* (codegraph EXTRACTION_VERSION pattern);
#: independent of the storage schema version.
EXTRACTION_VERSION = 1

#: Files above this size are never read (codegraph MAX_SOURCE_FILE_SIZE_BYTES).
MAX_SOURCE_FILE_SIZE_BYTES = 1024 * 1024

_MEMBER_FIELDS = ("property", "attribute", "name", "field")
_MAX_NAME_DEPTH = 6


# --------------------------------------------------------------------- errors


class IngestError(Exception):
    """Base class for ingestion failures; ``code`` is the machine-readable API."""

    code = "INGEST_ERROR"


class FileTooLargeError(IngestError):
    """The file exceeds the 1 MiB bound; it was never read in full."""

    code = "FILE_TOO_LARGE"

    def __init__(self, path: str, *, size: int, limit: int) -> None:
        super().__init__(f"{path!r} is {size} bytes, over the {limit}-byte source limit; refusing to parse")
        self.path = path
        self.size = size
        self.limit = limit


class UnsupportedMediaTypeError(IngestError):
    """No registered language claims this file's extension."""

    code = "UNSUPPORTED_MEDIA_TYPE"

    def __init__(self, path: str) -> None:
        super().__init__(f"{path!r}: no language registered for this extension")
        self.path = path


class SourceDecodeError(IngestError):
    """The source is not valid UTF-8."""

    code = "DECODE_ERROR"

    def __init__(self, path: str) -> None:
        super().__init__(f"{path!r}: source is not valid UTF-8; refusing to guess an encoding")
        self.path = path


class ExtractionError(IngestError):
    """The walker hit an unresolvable internal collision (fail-closed)."""

    code = "EXTRACTION_ERROR"


# ------------------------------------------------------------------ datatypes


@dataclass(frozen=True)
class ExtractedNode:
    """One graph node, artifact-ready. Lines are 1-indexed, cols 0-indexed."""

    node_id: str
    kind: str
    name: str
    qualified_name: str
    path: str
    start_line: int
    end_line: int
    start_col: int
    end_col: int


@dataclass(frozen=True)
class ExtractedEdge:
    """One ``contains`` edge (the only edge kind the extractor emits)."""

    source: str
    target: str
    kind: str = "contains"


@dataclass(frozen=True)
class UnresolvedRef:
    """One parked cross-file reference for the two-phase resolver."""

    source_node_id: str
    target_name: str
    name_tail: str
    kind: str  # "calls" | "imports"
    line: int
    col: int


@dataclass(frozen=True)
class ExtractionResult:
    """The deterministic output of extracting one source file."""

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


# --------------------------------------------------------- generated detection
# Patterns ported from codegraph src/extraction/generated-detection.ts (§5):
# relevance hint only — generated files still enter the graph.

_GENERATED_NAME_MARKERS: tuple[str, ...] = (
    ".pb.go",
    "_grpc.pb.go",
    "_mock.go",
    "_mocks.go",
    ".generated.js",
    ".generated.ts",
    ".gen.js",
    ".gen.ts",
    ".pb.js",
    ".pb.ts",
    "-min.js",
    ".min.js",
    "_pb2.py",
    ".pb.cc",
    ".pb.h",
    ".g.cs",
    "grpc.cs",
    ".generated.rs",
    ".pb.swift",
    ".g.dart",
    ".freezed.dart",
)

_BANNER_SCAN_CHARS = 8192
_BANNER_SCAN_LINES = 60
_COMMENT_LEADERS = ("#", "//", "/*", "*", "<!--", "--", ";", "%")
_BANNER_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"code generated\b.*\bdo not edit",
        r"@generated\b",
        r"<auto-generated>",
        r"\bauto-generated\b",
        r"automatically generated",
    )
)

_MINIFIED_EXTENSIONS = frozenset({".js", ".mjs", ".cjs"})
_MINIFIED_MIN_BYTES = 4096
_MINIFIED_LONG_LINE = 1000
_MINIFIED_LONG_LINE_FRACTION = 0.5
_MINIFIED_PUNCTUATION_FRACTION = 0.03
_MINIFIED_PUNCTUATION = frozenset(";{}(),")


def _is_generated_name(path: str) -> bool:
    lowered = path.lower()
    name = lowered.rsplit("/", 1)[-1]
    return any(name.endswith(marker) for marker in _GENERATED_NAME_MARKERS)


def _has_generated_banner(source: str) -> bool:
    lines = source.split("\n")[:_BANNER_SCAN_LINES]
    head = "\n".join(lines)[:_BANNER_SCAN_CHARS]
    if "generat" not in head.lower():
        return False
    in_block_comment = False
    for raw_line in head.split("\n"):
        line = raw_line.strip().lower()
        if "/*" in line and "*/" not in line.split("/*", 1)[1]:
            in_block_comment = True
        is_comment = in_block_comment or any(line.startswith(leader) for leader in _COMMENT_LEADERS)
        if is_comment and any(pattern.search(line) for pattern in _BANNER_PATTERNS):
            return True
        if "*/" in line:
            in_block_comment = False
    return False


def _is_minified(path: str, source: str) -> bool:
    suffix = ("." + path.lower().rsplit(".", 1)[-1]) if "." in path else ""
    if suffix not in _MINIFIED_EXTENSIONS:
        return False
    if len(source) < _MINIFIED_MIN_BYTES:
        return False
    long_line_chars = 0
    punctuation = 0
    for line in source.split("\n"):
        if len(line) >= _MINIFIED_LONG_LINE:
            long_line_chars += len(line)
            punctuation += sum(1 for char in line if char in _MINIFIED_PUNCTUATION)
    if long_line_chars < _MINIFIED_LONG_LINE_FRACTION * len(source):
        return False
    return punctuation >= _MINIFIED_PUNCTUATION_FRACTION * long_line_chars


def detect_generated(path: str, source: str) -> bool:
    """Heuristic generated/minified flag (name, banner, minification)."""
    return _is_generated_name(path) or _has_generated_banner(source) or _is_minified(path, source)


# ------------------------------------------------------------------ the walker


@dataclass(frozen=True)
class _Scope:
    """One entry of the scope stack. ``node_id`` is None for containers."""

    node_id: str | None
    kind: str  # core kind, or "container" for transparent scopes
    name: str


class _IdAllocator:
    """codegraph NodeIdAllocator: col suffix only on (path,kind,name,line) collision."""

    def __init__(self) -> None:
        self._used: set[str] = set()

    def allocate(self, *, path: str, kind: str, name: str, line: int, col: int) -> str:
        base = make_node_id(path=path, kind=kind, name=name, line=line)
        if base not in self._used:
            self._used.add(base)
            return base
        with_col = make_node_id(path=path, kind=kind, name=name, line=line, col=col)
        if with_col not in self._used:
            self._used.add(with_col)
            return with_col
        raise ExtractionError(f"{path!r}: cannot allocate a unique id for {kind} {name!r} at {line}:{col}")


class TreeSitterExtractor:
    """AST walker driven by a declarative :class:`LanguageSpec`.

    One instance per (file, language) extraction; all mutable state is local
    to :meth:`extract`, so results depend only on the input bytes.
    """

    def __init__(self, spec: LanguageSpec) -> None:
        self._spec = spec
        self._parser = get_parser(spec.parser)
        self._declaration_kinds = dict(spec.declaration_kinds)
        self._conditional_kinds = {entry[0]: entry for entry in spec.conditional_kinds}
        self._container_types = dict(spec.container_types)
        self._call_types = frozenset(spec.call_types)
        self._variable_rules = {rule.node_type: rule for rule in spec.variable_rules}

    # ------------------------------------------------------------- entry point

    def extract(self, path: str, source: str, *, generated_hint: bool = False) -> ExtractionResult:
        data = source.encode("utf-8")
        tree = self._parser.parse(data)
        self._data = data
        self._path = path
        self._nodes: list[ExtractedNode] = []
        self._edges: list[ExtractedEdge] = []
        self._refs: list[UnresolvedRef] = []
        self._ref_keys: set[tuple[str, str, str, int, int]] = set()
        self._allocator = _IdAllocator()

        line_count = source.count("\n") + 1
        root_id = file_node_id(path)
        self._nodes.append(
            ExtractedNode(
                node_id=root_id,
                kind=CoreNodeKind.FILE.value,
                name=path.rsplit("/", 1)[-1],
                qualified_name=path,
                path=path,
                start_line=1,
                end_line=max(1, line_count),
                start_col=0,
                end_col=0,
            )
        )
        self._walk(tree.root_node, [_Scope(root_id, CoreNodeKind.FILE.value, path)])
        return ExtractionResult(
            path=path,
            language=self._spec.name,
            sha256=hashlib.sha256(data).hexdigest(),
            size=len(data),
            generated_hint=generated_hint,
            parse_error=bool(tree.root_node.has_error),
            extraction_version=EXTRACTION_VERSION,
            nodes=tuple(self._nodes),
            edges=tuple(self._edges),
            unresolved_refs=tuple(self._refs),
        )

    # ----------------------------------------------------------------- walking

    def _walk(self, node: TSNode, scopes: list[_Scope]) -> None:
        node_type = node.type
        kind = self._declaration_kind(node)
        if kind is not None:
            self._declare(node, kind, scopes)
            return
        container_field = self._container_types.get(node_type)
        if container_field is not None:
            name_node = node.child_by_field_name(container_field)
            name = self._text(name_node) if name_node is not None else "<anonymous>"
            scopes.append(_Scope(None, "container", name))
            for child in node.children:
                self._walk(child, scopes)
            scopes.pop()
            return
        import_rule = self._match_import(node)
        if import_rule is not None:
            self._emit_import(node, import_rule, scopes)
            return
        if node_type in self._call_types:
            self._emit_call(node, scopes)
            for child in node.children:
                self._walk(child, scopes)
            return
        variable_rule = self._variable_rules.get(node_type)
        if variable_rule is not None and self._variable_declared(node, variable_rule, scopes):
            return
        for child in node.children:
            self._walk(child, scopes)

    def _declaration_kind(self, node: TSNode) -> str | None:
        conditional = self._conditional_kinds.get(node.type)
        if conditional is not None:
            _node_type, mapping, default = conditional
            child = node.child_by_field_name("type")
            child_type = child.type if child is not None else ""
            for candidate, kind in mapping:
                if candidate == child_type:
                    return kind
            return default
        return self._declaration_kinds.get(node.type)

    # -------------------------------------------------------------- declarations

    def _declare(self, node: TSNode, kind: str, scopes: list[_Scope]) -> None:
        spec = self._spec
        name = self._declaration_name(node)
        if name is None:
            for child in node.children:
                self._walk(child, scopes)
            return
        if kind == CoreNodeKind.FUNCTION.value and spec.scoped_functions_become_methods:
            innermost = scopes[-1]
            if innermost.kind == "container" or innermost.kind in spec.method_scope_kinds:
                kind = CoreNodeKind.METHOD.value
        start_line = node.start_point[0] + 1
        start_col = node.start_point[1]
        node_id = self._allocator.allocate(path=self._path, kind=kind, name=name, line=start_line, col=start_col)
        parent_id = self._nearest_node_id(scopes)
        qualified = "::".join([*(scope.name for scope in scopes[1:] if scope.name), name])
        self._nodes.append(
            ExtractedNode(
                node_id=node_id,
                kind=kind,
                name=name,
                qualified_name=qualified,
                path=self._path,
                start_line=start_line,
                end_line=node.end_point[0] + 1,
                start_col=start_col,
                end_col=node.end_point[1],
            )
        )
        self._edges.append(ExtractedEdge(source=parent_id, target=node_id))
        if kind in spec.scope_kinds:
            scopes.append(_Scope(node_id, kind, name))
            for child in node.children:
                self._walk(child, scopes)
            scopes.pop()
        else:
            for child in node.children:
                self._walk(child, scopes)

    def _declaration_name(self, node: TSNode) -> str | None:
        resolved = self._resolve_name(node, self._spec.name_fields, depth=0)
        if resolved is not None:
            return resolved
        # fallback: first child whose type looks like an identifier (covers
        # field-less declarations such as Go package_clause / Java package).
        for child in node.children:
            if child.type in self._spec.identifier_types or child.type.endswith("_identifier"):
                return self._text(child)
        return None

    def _resolve_name(self, node: TSNode, fields: tuple[str, ...], depth: int) -> str | None:
        if depth > _MAX_NAME_DEPTH:
            return None
        if node.type in self._spec.identifier_types or node.type.endswith("_identifier"):
            return self._text(node)
        for field in fields:
            child = node.child_by_field_name(field)
            if child is not None:
                resolved = self._resolve_name(child, fields, depth + 1)
                if resolved:
                    return resolved
        return None

    # ----------------------------------------------------------------- imports

    def _match_import(self, node: TSNode) -> ImportRule | None:
        for rule in self._spec.import_rules:
            if rule.node_type != node.type:
                continue
            if rule.match_text:
                callee = self._call_target_name(node)
                if callee not in rule.match_text:
                    continue
            return rule
        return None

    def _emit_import(self, node: TSNode, rule: ImportRule, scopes: list[_Scope]) -> None:
        target: str | None = None
        for field in rule.target_fields:
            child = node.child_by_field_name(field)
            if child is not None:
                target = self._text(child)
                break
        if target is None and rule.target_regex:
            match = re.search(rule.target_regex, self._text(node))
            if match is not None:
                target = match.group(1)
        if target is None:
            return
        target = target.strip().strip(rule.strip_chars).strip()
        if not target:
            return
        line = node.start_point[0] + 1
        col = node.start_point[1]
        name = _name_tail(target)
        node_id = self._allocator.allocate(
            path=self._path, kind=CoreNodeKind.IMPORT.value, name=name, line=line, col=col
        )
        parent_id = self._nearest_node_id(scopes)
        self._nodes.append(
            ExtractedNode(
                node_id=node_id,
                kind=CoreNodeKind.IMPORT.value,
                name=name,
                qualified_name=name,
                path=self._path,
                start_line=line,
                end_line=node.end_point[0] + 1,
                start_col=col,
                end_col=node.end_point[1],
            )
        )
        self._edges.append(ExtractedEdge(source=parent_id, target=node_id))
        self._add_ref(node_id, target, "imports", line, col)

    # ------------------------------------------------------------------- calls

    def _emit_call(self, node: TSNode, scopes: list[_Scope]) -> None:
        target = self._call_target_name(node)
        if not target:
            return
        source_id = self._nearest_callable_id(scopes)
        self._add_ref(source_id, target, "calls", node.start_point[0] + 1, node.start_point[1])

    def _call_target_name(self, node: TSNode) -> str | None:
        for field in self._spec.call_name_fields:
            child = node.child_by_field_name(field)
            if child is not None:
                return self._member_name(child)
        return None

    def _member_name(self, node: TSNode) -> str | None:
        """The callee's own name: identifiers as-is; member expressions reduce
        to their property segment (``obj.method`` → ``method``)."""
        if node.type in self._spec.identifier_types or node.type.endswith("_identifier"):
            return self._text(node)
        for field in _MEMBER_FIELDS:
            child = node.child_by_field_name(field)
            if child is not None:
                resolved = self._member_name(child)
                if resolved:
                    return resolved
        if node.named_child_count == 1:
            return self._member_name(node.named_children[0])
        text = self._text(node).strip()
        return text or None

    # --------------------------------------------------------------- variables

    def _variable_declared(self, node: TSNode, rule: VariableRule, scopes: list[_Scope]) -> bool:
        """Handle a variable rule; returns True when the node was consumed."""
        name_node = node.child_by_field_name(rule.name_field)
        if name_node is None or (
            name_node.type not in self._spec.identifier_types and not name_node.type.endswith("_identifier")
        ):
            return False
        value_node = node.child_by_field_name("value")
        if rule.function_value_types and value_node is not None and value_node.type in rule.function_value_types:
            # `const f = (x) => ...` — a function node named by the variable,
            # scoped over the function body (codegraph arrow handling).
            name = self._text(name_node)
            line = node.start_point[0] + 1
            col = node.start_point[1]
            node_id = self._allocator.allocate(
                path=self._path, kind=CoreNodeKind.FUNCTION.value, name=name, line=line, col=col
            )
            parent_id = self._nearest_node_id(scopes)
            self._nodes.append(
                ExtractedNode(
                    node_id=node_id,
                    kind=CoreNodeKind.FUNCTION.value,
                    name=name,
                    qualified_name="::".join([*(scope.name for scope in scopes[1:] if scope.name), name]),
                    path=self._path,
                    start_line=line,
                    end_line=node.end_point[0] + 1,
                    start_col=col,
                    end_col=node.end_point[1],
                )
            )
            self._edges.append(ExtractedEdge(source=parent_id, target=node_id))
            scopes.append(_Scope(node_id, CoreNodeKind.FUNCTION.value, name))
            self._walk(value_node, scopes)
            scopes.pop()
            return True
        # plain variable/constant: only at file/class-ish scope (never in
        # function bodies — codegraph value-ref noise control).
        if scopes[-1].kind not in (
            CoreNodeKind.FILE.value,
            CoreNodeKind.MODULE.value,
            *self._spec.method_scope_kinds,
        ):
            return False
        name = self._text(name_node)
        kind = rule.kind
        if rule.const_when_uppercase and name.upper() == name and any(c.isalpha() for c in name):
            kind = CoreNodeKind.CONSTANT.value
        if rule.const_keyword and node.parent is not None:
            first = node.parent.children[0] if node.parent.children else None
            if first is not None and self._text(first) == rule.const_keyword:
                kind = CoreNodeKind.CONSTANT.value
        line = node.start_point[0] + 1
        col = node.start_point[1]
        node_id = self._allocator.allocate(path=self._path, kind=kind, name=name, line=line, col=col)
        parent_id = self._nearest_node_id(scopes)
        self._nodes.append(
            ExtractedNode(
                node_id=node_id,
                kind=kind,
                name=name,
                qualified_name="::".join([*(scope.name for scope in scopes[1:] if scope.name), name]),
                path=self._path,
                start_line=line,
                end_line=node.end_point[0] + 1,
                start_col=col,
                end_col=node.end_point[1],
            )
        )
        self._edges.append(ExtractedEdge(source=parent_id, target=node_id))
        # walk the initializer for calls, without the declarator node itself
        for child in node.children:
            self._walk(child, scopes)
        return True

    # ----------------------------------------------------------------- helpers

    def _add_ref(self, source_id: str, target: str, kind: str, line: int, col: int) -> None:
        key = (source_id, target, kind, line, col)
        if key in self._ref_keys:
            return
        self._ref_keys.add(key)
        self._refs.append(
            UnresolvedRef(
                source_node_id=source_id,
                target_name=target,
                name_tail=_name_tail(target),
                kind=kind,
                line=line,
                col=col,
            )
        )

    @staticmethod
    def _nearest_node_id(scopes: list[_Scope]) -> str:
        for scope in reversed(scopes):
            if scope.node_id is not None:
                return scope.node_id
        raise ExtractionError("scope stack lost its file root")  # pragma: no cover

    @staticmethod
    def _nearest_callable_id(scopes: list[_Scope]) -> str:
        for scope in reversed(scopes):
            if scope.kind in (CoreNodeKind.FUNCTION.value, CoreNodeKind.METHOD.value) and scope.node_id:
                return scope.node_id
        return TreeSitterExtractor._nearest_node_id(scopes)

    def _text(self, node: TSNode) -> str:
        start, end = node.start_byte, node.end_byte
        return self._data[start:end].decode("utf-8", errors="replace")


def _name_tail(target: str) -> str:
    """The store's retry key: the last dot segment (then last ``::`` segment)."""
    return target.rsplit(".", 1)[-1].rsplit("::", 1)[-1]


# ---------------------------------------------------------------- public API


def extract_source(
    path: str,
    source: str,
    *,
    language: str | None = None,
    generated_hint: bool = False,
) -> ExtractionResult:
    """Extract one in-memory source string under the contract path *path*."""
    resolved = language if language is not None else language_for_path(path)
    if resolved is None:
        raise UnsupportedMediaTypeError(path)
    encoded = source.encode("utf-8")
    if len(encoded) > MAX_SOURCE_FILE_SIZE_BYTES:
        raise FileTooLargeError(path, size=len(encoded), limit=MAX_SOURCE_FILE_SIZE_BYTES)
    return TreeSitterExtractor(get_spec(resolved)).extract(path, source, generated_hint=generated_hint)


def extract_file(path: str | Path, *, display_path: str | None = None) -> ExtractionResult:
    """Read *path* with the 1 MiB bound and extract it.

    ``display_path`` (defaults to ``str(path)``) is the contract path embedded
    in node ids — pass the project-relative path for stable identities.
    """
    file_path = Path(path)
    contract_path = display_path if display_path is not None else str(file_path)
    language = language_for_path(file_path.name)
    if language is None:
        raise UnsupportedMediaTypeError(str(file_path))
    size = file_path.stat().st_size
    if size > MAX_SOURCE_FILE_SIZE_BYTES:
        raise FileTooLargeError(str(file_path), size=size, limit=MAX_SOURCE_FILE_SIZE_BYTES)
    data = file_path.read_bytes()
    if len(data) > MAX_SOURCE_FILE_SIZE_BYTES:
        # the file grew between stat and read — fail closed (codegraph §5)
        raise FileTooLargeError(str(file_path), size=len(data), limit=MAX_SOURCE_FILE_SIZE_BYTES)
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise SourceDecodeError(str(file_path)) from error
    generated = detect_generated(file_path.name, source)
    result = extract_source(contract_path, source, language=language, generated_hint=generated)
    # sha256/size track the raw file bytes (change detection), not the
    # BOM-stripped decode.
    return replace(result, sha256=hashlib.sha256(data).hexdigest(), size=len(data))


#: Referenced for API parity with the store's ``files.language`` column;
#: callers can list every routable extension via ``EXTENSION_MAP``.
_KNOWN_EXTENSIONS: tuple[str, ...] = tuple(sorted(EXTENSION_MAP))
