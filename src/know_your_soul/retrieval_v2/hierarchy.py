"""Call hierarchy for retrieval v2 (wave W4).

Depth-bounded ``callers``/``callees`` traversal over ``calls``/``imports``/
``references`` edges (RESEARCH-QUERY §1C), with canonical output: entries
are sorted by ``(depth, path, line, col, node_id)`` and multiple
definitions of the same name get one section each, ordered by
``(path, line, col, node_id)`` (codegraph ``groupDefinitions``).
Cycles are cut by a visited set; ``contains`` edges are never traversed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from know_your_soul.store import EventStore, SymbolRow

__all__ = [
    "DEFAULT_EDGE_KINDS",
    "CallHierarchy",
    "HierarchyEntry",
    "HierarchyError",
    "HierarchyReport",
    "HierarchyResult",
    "NodeDescriptor",
    "callees",
    "callers",
]

#: Edge kinds traversed by call hierarchy (SUB-90 addendum: references).
DEFAULT_EDGE_KINDS: tuple[str, ...] = ("calls", "imports", "references")

DEPTH_MIN, DEPTH_MAX = 1, 8
LIMIT_MIN, LIMIT_MAX = 1, 500

_KIND_RE = re.compile(r"[a-z][a-z0-9_]{0,31}")


class HierarchyError(ValueError):
    """Invalid hierarchy argument (fail-closed)."""


@dataclass(frozen=True)
class NodeDescriptor:
    """Display fields for a traversal endpoint (symbol row or bare node id)."""

    node_id: str
    name: str
    kind: str
    path: str
    line: int
    col: int


@dataclass(frozen=True)
class HierarchyEntry:
    """One discovered neighbour with the edge that reached it."""

    node_id: str
    name: str
    kind: str
    path: str
    line: int
    col: int
    depth: int
    via_edge_kind: str
    via_line: int | None
    from_node_id: str


@dataclass(frozen=True)
class HierarchyResult:
    """Traversal outcome for one definition of the queried symbol."""

    direction: Literal["callers", "callees"]
    root: NodeDescriptor
    entries: tuple[HierarchyEntry, ...]
    total: int
    truncated: bool

    def render(self) -> str:
        title = "Callers" if self.direction == "callers" else "Callees"
        location = f"{self.root.path}:{self.root.line}" if self.root.path else self.root.node_id
        lines = [f"{title} of {self.root.name} ({self.root.kind}) — {location}"]
        if not self.entries:
            lines.append("  (none)")
        for entry in self.entries:
            entry_location = f"{entry.path}:{entry.line}" if entry.path else entry.node_id
            via = f"via {entry.via_edge_kind}" + (f" @ {entry.via_line}" if entry.via_line is not None else "")
            lines.append(f"  d{entry.depth} {entry.name} ({entry.kind}) — {entry_location} {via}")
        if self.truncated:
            lines.append(f"  > Showing {len(self.entries)} of {self.total}")
        return "\n".join(lines)


@dataclass(frozen=True)
class HierarchyReport:
    """All definitions of the queried symbol, in canonical order."""

    direction: Literal["callers", "callees"]
    query: str
    definitions: tuple[HierarchyResult, ...]

    def render(self) -> str:
        title = "Callers" if self.direction == "callers" else "Callees"
        if not self.definitions:
            return f"{title} of '{self.query}' — no matching symbol"
        body = "\n".join(result.render() for result in self.definitions)
        if len(self.definitions) > 1:
            return f"{title} of '{self.query}' — {len(self.definitions)} distinct definitions\n{body}"
        return body


def _describe(store: EventStore, node_id: str) -> NodeDescriptor:
    symbol = store.symbol_by_node(node_id)
    if symbol is not None:
        return NodeDescriptor(
            node_id=symbol.node_id,
            name=symbol.name,
            kind=symbol.kind,
            path=symbol.path,
            line=symbol.line,
            col=symbol.col,
        )
    return NodeDescriptor(node_id=node_id, name=node_id, kind="node", path="", line=0, col=0)


class CallHierarchy:
    """Depth-bounded callers/callees over the universal edge table."""

    def callers(
        self,
        store: EventStore,
        symbol: str,
        *,
        file: str | None = None,
        depth: int = 1,
        kinds: tuple[str, ...] = DEFAULT_EDGE_KINDS,
        limit: int = 50,
    ) -> HierarchyReport:
        return self._run(store, symbol, "callers", file=file, depth=depth, kinds=kinds, limit=limit)

    def callees(
        self,
        store: EventStore,
        symbol: str,
        *,
        file: str | None = None,
        depth: int = 1,
        kinds: tuple[str, ...] = DEFAULT_EDGE_KINDS,
        limit: int = 50,
    ) -> HierarchyReport:
        return self._run(store, symbol, "callees", file=file, depth=depth, kinds=kinds, limit=limit)

    # ------------------------------------------------------------ internals

    def _run(
        self,
        store: EventStore,
        symbol: str,
        direction: Literal["callers", "callees"],
        *,
        file: str | None,
        depth: int,
        kinds: tuple[str, ...],
        limit: int,
    ) -> HierarchyReport:
        self._validate(depth=depth, kinds=kinds, limit=limit)
        definitions = self._resolve_definitions(store, symbol, file)
        results = tuple(
            self._traverse(store, definition, direction, depth=depth, kinds=kinds, limit=limit)
            for definition in definitions
        )
        return HierarchyReport(direction=direction, query=symbol, definitions=results)

    @staticmethod
    def _validate(*, depth: int, kinds: tuple[str, ...], limit: int) -> None:
        if isinstance(depth, bool) or not isinstance(depth, int) or not (DEPTH_MIN <= depth <= DEPTH_MAX):
            raise HierarchyError(f"depth must be an int in [{DEPTH_MIN}, {DEPTH_MAX}], got {depth!r}")
        if isinstance(limit, bool) or not isinstance(limit, int) or not (LIMIT_MIN <= limit <= LIMIT_MAX):
            raise HierarchyError(f"limit must be an int in [{LIMIT_MIN}, {LIMIT_MAX}], got {limit!r}")
        for kind in kinds:
            if not _KIND_RE.fullmatch(kind):
                raise HierarchyError(f"edge kind {kind!r} must match {_KIND_RE.pattern}")

    @staticmethod
    def _resolve_definitions(store: EventStore, symbol: str, file: str | None) -> list[SymbolRow]:
        by_id = store.symbol_by_node(symbol)
        if by_id is not None:
            return [] if (file is not None and by_id.path != file) else [by_id]
        matches = store.symbols_named(symbol, case_insensitive=True)
        if file is not None:
            matches = [match for match in matches if match.path == file]
        return sorted(matches, key=lambda match: (match.path, match.line, match.col, match.node_id))

    @staticmethod
    def _traverse(
        store: EventStore,
        root: SymbolRow,
        direction: Literal["callers", "callees"],
        *,
        depth: int,
        kinds: tuple[str, ...],
        limit: int,
    ) -> HierarchyResult:
        visited = {root.node_id}
        frontier = [root.node_id]
        entries: list[HierarchyEntry] = []
        for level in range(1, depth + 1):
            next_frontier: list[str] = []
            for node in frontier:
                edges = (
                    store.incoming_edges(node, kinds=kinds)
                    if direction == "callers"
                    else store.outgoing_edges(node, kinds=kinds)
                )
                for edge in edges:
                    other = edge.source if direction == "callers" else edge.target
                    if other in visited:
                        continue
                    visited.add(other)
                    descriptor = _describe(store, other)
                    entries.append(
                        HierarchyEntry(
                            node_id=descriptor.node_id,
                            name=descriptor.name,
                            kind=descriptor.kind,
                            path=descriptor.path,
                            line=descriptor.line,
                            col=descriptor.col,
                            depth=level,
                            via_edge_kind=edge.kind,
                            via_line=edge.line,
                            from_node_id=node,
                        )
                    )
                    next_frontier.append(other)
            frontier = next_frontier
        total = len(entries)
        entries.sort(key=lambda entry: (entry.depth, entry.path, entry.line, entry.col, entry.node_id))
        return HierarchyResult(
            direction=direction,
            root=NodeDescriptor(
                node_id=root.node_id,
                name=root.name,
                kind=root.kind,
                path=root.path,
                line=root.line,
                col=root.col,
            ),
            entries=tuple(entries[:limit]),
            total=total,
            truncated=total > limit,
        )


def callers(store: EventStore, symbol: str, **kwargs: object) -> HierarchyReport:
    """One-shot convenience wrapper (see :meth:`CallHierarchy.callers`)."""
    return CallHierarchy().callers(store, symbol, **kwargs)  # type: ignore[arg-type]


def callees(store: EventStore, symbol: str, **kwargs: object) -> HierarchyReport:
    """One-shot convenience wrapper (see :meth:`CallHierarchy.callees`)."""
    return CallHierarchy().callees(store, symbol, **kwargs)  # type: ignore[arg-type]
