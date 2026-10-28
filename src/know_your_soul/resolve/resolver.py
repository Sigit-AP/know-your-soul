"""Two-phase reference resolver (wave W2, MASTER-PLAN pillar 3).

Strategy cascade per pending ref — codegraph's import-resolver / name-matcher
pair reduced to three deterministic tiers (RESEARCH-INGESTION §2e):

a. **exact** — ``symbols.name == target_name`` (seeks ``idx_symbols_name``).
b. **name_tail** — symbols whose name equals the tail or ends with
   ``.<tail>`` / ``::<tail>`` (qualified-name suffix).
c. **import_path** — module-based languages only: Python dotted/relative
   imports resolved against the ``files`` table (``.py`` then
   ``/__init__.py``), JS/TS relative specifiers resolved against the source
   directory with the extension/index-name search order. Bare JS/TS
   specifiers fail as ``external_module``.

Ambiguity never guesses: a tier with 2+ candidates defers to the next tier
(import-path can disambiguate via the module path, e.g. ``pkg.mod.func``);
if no later tier resolves, the ref fails with the *first* ambiguity reason.
All failure reasons are deterministic strings (see ``_FAILURE_REASONS``).

Batch discipline: one read pass over the pending queue (oldest first, capped
fail-closed by ``limit``/``ResolverConfig.batch_limit``), then one
``add_graph_edges`` transaction, then one ``record_resolution_attempt`` per
ref. A crash between the two write phases replays cleanly — edge identity
dedups and refs stay pending. Every emitted edge carries the ref identity in
``metadata`` (``ref_id``/``ref_name``/``strategy``) so a later file removal
can resurrect the ref (codegraph ``resurrectRefFromDroppedEdge``).
"""

from __future__ import annotations

import posixpath
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from know_your_soul.domain import canonical_json
from know_your_soul.graph_model import file_node_id, is_file_node_id
from know_your_soul.store import EventStore, FileRow, SymbolRow, UnresolvedRefRow

__all__ = [
    "PROVENANCE",
    "REF_KIND_TO_EDGE_KIND",
    "RESOLVER_VERSION",
    "BatchReport",
    "ReferenceResolver",
    "RefOutcome",
    "ResolverConfig",
    "ResolverError",
    "resolve_pending",
]

RESOLVER_VERSION: int = 1

#: Provenance stamp on every resolver-minted edge.
PROVENANCE: str = f"resolver:two-phase:v{RESOLVER_VERSION}"

#: Ref kinds the resolver accepts, mapped to the edge kind they mint
#: (``function_ref`` is codegraph's call-site alias for a ``calls`` edge).
#: W4 addendum (temuan SUB-90): W3 document ingestion parks markdown/
#: plaintext link refs as ``references`` — mapped to the frozen core edge
#: kind ``references`` (graph_model.CoreEdgeKind.REFERENCES).
REF_KIND_TO_EDGE_KIND: dict[str, str] = {
    "calls": "calls",
    "imports": "imports",
    "function_ref": "calls",
    "references": "references",
}

#: Extension → language fallback when the source file has no ``files`` row
#: (or its language is ``unknown``).
_EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascriptreact",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescriptreact",
}


class ResolverError(ValueError):
    """Invalid resolver configuration or batch argument (fail-closed)."""


def _validate_limit(value: int | None, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ResolverError(f"{label} must be None or an int >= 1, got {value!r}")


@dataclass(frozen=True)
class ResolverConfig:
    """Tuning knobs for :class:`ReferenceResolver` (all deterministic)."""

    batch_limit: int | None = None
    js_extensions: tuple[str, ...] = (".ts", ".tsx", ".js", ".jsx")
    js_index_names: tuple[str, ...] = ("index.ts", "index.tsx", "index.js", "index.jsx")
    python_suffixes: tuple[str, ...] = (".py", "/__init__.py")
    module_languages: frozenset[str] = frozenset(
        {"python", "javascript", "typescript", "javascriptreact", "typescriptreact"}
    )

    def __post_init__(self) -> None:
        _validate_limit(self.batch_limit, "batch_limit")
        for label, sequence in (
            ("js_extensions", self.js_extensions),
            ("js_index_names", self.js_index_names),
            ("python_suffixes", self.python_suffixes),
        ):
            if not sequence:
                raise ResolverError(f"{label} must be a non-empty sequence")
            if not all(isinstance(item, str) and item for item in sequence):
                raise ResolverError(f"{label} entries must be non-empty strings")
        if not self.module_languages:
            raise ResolverError("module_languages must be non-empty")
        if not all(isinstance(language, str) and language for language in self.module_languages):
            raise ResolverError("module_languages entries must be non-empty strings")


@dataclass(frozen=True)
class RefOutcome:
    """The deterministic verdict for one processed ref."""

    ref_id: int
    status: Literal["resolved", "failed"]
    strategy: Literal["exact", "name_tail", "import_path"] | None
    target_node_id: str | None
    reason: str | None


@dataclass(frozen=True)
class BatchReport:
    """Per-batch resolution counts; ``outcomes`` is sorted by ``ref_id``."""

    processed: int
    resolved: int
    failed: int
    pending_remaining: int
    edges_inserted: int
    outcomes: tuple[RefOutcome, ...]


@dataclass(frozen=True)
class _Decision:
    strategy: Literal["exact", "name_tail", "import_path"] | None
    target_node_id: str | None
    reason: str | None


def _hit(strategy: Literal["exact", "name_tail", "import_path"], target_node_id: str) -> _Decision:
    return _Decision(strategy, target_node_id, None)


def _fail(reason: str) -> _Decision:
    return _Decision(None, None, reason)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _file_row(row: sqlite3.Row) -> FileRow:
    return FileRow(
        path=str(row["path"]),
        sha256=str(row["sha256"]),
        size=int(row["size"]),
        mtime=int(row["mtime"]),
        language=str(row["language"]),
        generated_hint=bool(row["generated_hint"]),
        extraction_version=int(row["extraction_version"]),
    )


def _symbol_row(row: sqlite3.Row) -> SymbolRow:
    return SymbolRow(
        node_id=str(row["node_id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        path=str(row["path"]),
        line=int(row["line"]),
        col=int(row["col"]),
    )


class ReferenceResolver:
    """Turns pending ``unresolved_refs`` into ``calls``/``imports`` edges."""

    def __init__(self, config: ResolverConfig | None = None) -> None:
        self._config = config if config is not None else ResolverConfig()

    @property
    def config(self) -> ResolverConfig:
        return self._config

    # ------------------------------------------------------------ public API

    def resolve_pending(self, store: EventStore, *, limit: int | None = None) -> BatchReport:
        """Resolve one batch of pending refs (oldest first), capped fail-closed.

        Read phase is pure; the write phase is one ``add_graph_edges``
        transaction followed by one ``record_resolution_attempt`` per ref.
        """
        _validate_limit(limit, "limit")
        effective_limit = limit if limit is not None else self._config.batch_limit
        refs = store.pending_unresolved_refs(limit=effective_limit)
        decisions: list[tuple[UnresolvedRefRow, _Decision]] = []
        edge_payloads: list[dict[str, object]] = []
        for ref in refs:
            decision = self._resolve_ref(store, ref)
            decisions.append((ref, decision))
            if decision.reason is None:
                assert decision.target_node_id is not None and decision.strategy is not None
                edge_payloads.append(
                    {
                        "source": ref.source_node_id,
                        "target": decision.target_node_id,
                        "kind": REF_KIND_TO_EDGE_KIND[ref.kind],
                        "metadata": canonical_json(
                            {"ref_id": ref.ref_id, "ref_name": ref.target_name, "strategy": decision.strategy}
                        ),
                        "provenance": PROVENANCE,
                    }
                )
        edges_inserted = store.add_graph_edges(edge_payloads) if edge_payloads else 0
        outcomes: list[RefOutcome] = []
        for ref, decision in decisions:
            status: Literal["resolved", "failed"] = "resolved" if decision.reason is None else "failed"
            store.record_resolution_attempt(ref.ref_id, outcome=status)
            outcomes.append(
                RefOutcome(
                    ref_id=ref.ref_id,
                    status=status,
                    strategy=decision.strategy,
                    target_node_id=decision.target_node_id,
                    reason=decision.reason,
                )
            )
        outcomes.sort(key=lambda outcome: outcome.ref_id)
        resolved = sum(1 for outcome in outcomes if outcome.status == "resolved")
        return BatchReport(
            processed=len(outcomes),
            resolved=resolved,
            failed=len(outcomes) - resolved,
            pending_remaining=len(store.pending_unresolved_refs()),
            edges_inserted=edges_inserted,
            outcomes=tuple(outcomes),
        )

    def resurrect_failed(
        self, store: EventStore, *, name_tail: str | None = None, limit: int | None = None
    ) -> tuple[int, ...]:
        """Move failed refs back to pending (codegraph #1240 incremental retry).

        Pass ``name_tail`` to retry only the refs a newly landed/changed file
        could satisfy; ``attempts`` is preserved by the store.
        """
        _validate_limit(limit, "limit")
        failed = store.failed_unresolved_refs(name_tail=name_tail)
        if limit is not None:
            failed = failed[:limit]
        moved: list[int] = []
        for ref in failed:
            store.retry_failed_ref(ref.ref_id)
            moved.append(ref.ref_id)
        return tuple(moved)

    # ------------------------------------------------------- strategy cascade

    def _resolve_ref(self, store: EventStore, ref: UnresolvedRefRow) -> _Decision:
        edge_kind = REF_KIND_TO_EDGE_KIND.get(ref.kind)
        if edge_kind is None:
            return _fail("unknown_ref_kind")
        context = self._source_context(store, ref.source_node_id)
        if context is None:
            return _fail("unknown_source")
        source_path, language = context
        deferred_reason: str | None = None
        # (a) exact symbol match
        exact = store.symbols_named(ref.target_name)
        if len(exact) == 1:
            return _hit("exact", exact[0].node_id)
        if len(exact) > 1:
            deferred_reason = f"ambiguous_exact:{len(exact)}"
        # (b) name_tail match (skip when the tail adds no information)
        tail = ref.name_tail
        if tail and tail != ref.target_name:
            candidates = self._tail_candidates(store, tail)
            if len(candidates) == 1:
                return _hit("name_tail", candidates[0].node_id)
            if len(candidates) > 1 and deferred_reason is None:
                deferred_reason = f"ambiguous_tail:{len(candidates)}"
        # (c) import-path resolution (module-based languages)
        if language in self._config.module_languages and self._import_path_applies(ref.kind, ref.target_name, language):
            if language == "python":
                decision = self._resolve_python_import(store, source_path, ref.target_name, edge_kind)
            else:
                decision = self._resolve_js_import(store, source_path, ref.target_name)
            if decision is not None:
                return decision
        if deferred_reason is not None:
            return _fail(deferred_reason)
        return _fail("no_resolution")

    def _source_context(self, store: EventStore, source_node_id: str) -> tuple[str, str] | None:
        """(source file path, language) or None when the source node is unknown."""
        path: str | None = None
        symbol = store.symbol_by_node(source_node_id)
        if symbol is not None:
            path = symbol.path
        elif is_file_node_id(source_node_id):
            candidate = source_node_id[len("file:") :]
            if store.get_file(candidate) is not None:
                path = candidate
        if path is None:
            return None
        file_row = store.get_file(path)
        language = (
            file_row.language
            if file_row is not None and file_row.language != "unknown"
            else _EXTENSION_LANGUAGES.get(PurePosixPath(path).suffix, "unknown")
        )
        return path, language

    def _import_path_applies(self, ref_kind: str, target_name: str, language: str) -> bool:
        if ref_kind == "imports":
            return True
        if language == "python":
            return target_name.startswith(".") or "." in target_name
        return target_name.startswith(".") or "/" in target_name

    def _tail_candidates(self, store: EventStore, tail: str) -> list[SymbolRow]:
        found: dict[str, SymbolRow] = {symbol.node_id: symbol for symbol in store.symbols_named(tail)}
        like = _escape_like(tail)
        with store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM symbols WHERE name LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
                "ORDER BY path, line, col, node_id",
                (f"%.{like}", f"%::{like}"),
            ).fetchall()
        for row in rows:
            symbol = _symbol_row(row)
            found.setdefault(symbol.node_id, symbol)
        return sorted(found.values(), key=lambda symbol: (symbol.path, symbol.line, symbol.col, symbol.node_id))

    # -------------------------------------------------------- module lookups

    def _module_file(self, store: EventStore, rel: str) -> tuple[str, FileRow | int | None]:
        """Exact-then-suffix file match: ``("hit", row)`` / ``("ambiguous", n)`` / ``("miss", None)``."""
        exact = store.get_file(rel)
        if exact is not None:
            return ("hit", exact)
        with store.read() as conn:
            rows = conn.execute(
                "SELECT * FROM files WHERE path LIKE ? ESCAPE '\\' ORDER BY path",
                (f"%/{_escape_like(rel)}",),
            ).fetchall()
        if not rows:
            return ("miss", None)
        if len(rows) > 1:
            return ("ambiguous", len(rows))
        return ("hit", _file_row(rows[0]))

    def _symbol_in_file(self, store: EventStore, path: str, tail: str) -> tuple[str, SymbolRow | int | None]:
        matches = [
            symbol
            for symbol in store.symbols_in_path(path)
            if symbol.name == tail or symbol.name.endswith(f".{tail}") or symbol.name.endswith(f"::{tail}")
        ]
        if len(matches) == 1:
            return ("hit", matches[0])
        if not matches:
            return ("miss", None)
        return ("ambiguous", len(matches))

    def _resolve_python_import(
        self, store: EventStore, source_path: str, target_name: str, edge_kind: str
    ) -> _Decision | None:
        dots = len(target_name) - len(target_name.lstrip("."))
        rest = target_name[dots:]
        if dots:
            base = PurePosixPath(source_path).parent
            for _ in range(dots - 1):
                base = base.parent
            prefix = "" if str(base) in ("", ".") else str(base)
        else:
            prefix = ""
        segments = [segment for segment in rest.split(".") if segment] if rest else []
        # Deterministic split order: longest module + symbol tail first, then
        # the whole target as a module, then (relative only) the package
        # __init__ with the last segment as a symbol (`from . import x`).
        splits: list[tuple[list[str], str | None]] = []
        if len(segments) >= 2:
            splits.append((segments[:-1], segments[-1]))
        if segments:
            splits.append((segments, None))
        if dots:
            splits.append(([], segments[-1] if segments else None))
        fallback: FileRow | None = None
        for module_segments, symbol_tail in splits:
            module_rel = "/".join(([prefix] if prefix else []) + module_segments)
            if not module_rel:
                continue  # root-level relative import has no anchor package
            module_row: FileRow | None = None
            for candidate in (f"{module_rel}{suffix}" for suffix in self._config.python_suffixes):
                status, payload = self._module_file(store, candidate)
                if status == "ambiguous":
                    return _fail(f"ambiguous_module:{payload}")
                if status == "hit":
                    assert isinstance(payload, FileRow)
                    module_row = payload
                    break
            if module_row is None:
                continue
            if symbol_tail is None:
                if edge_kind == "imports":
                    return _hit("import_path", file_node_id(module_row.path))
                continue  # a calls edge never binds a bare module
            symbol_status, symbol_payload = self._symbol_in_file(store, module_row.path, symbol_tail)
            if symbol_status == "hit":
                assert isinstance(symbol_payload, SymbolRow)
                return _hit("import_path", symbol_payload.node_id)
            if symbol_status == "ambiguous":
                return _fail(f"ambiguous_module_symbol:{symbol_payload}")
            if edge_kind != "imports":
                return _fail(f"symbol_not_in_module:{symbol_tail}")
            if fallback is None:
                fallback = module_row  # `from pkg.mod import missing` still binds the module
        if fallback is not None:
            return _hit("import_path", file_node_id(fallback.path))
        return None

    def _resolve_js_import(self, store: EventStore, source_path: str, target_name: str) -> _Decision | None:
        if not (target_name.startswith("./") or target_name.startswith("../")):
            return _fail("external_module")
        joined = posixpath.normpath(posixpath.join(posixpath.dirname(source_path), target_name))
        candidates: list[str] = []
        for candidate in (
            [joined]
            + [f"{joined}{extension}" for extension in self._config.js_extensions]
            + [f"{joined}/{index_name}" for index_name in self._config.js_index_names]
        ):
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            status, payload = self._module_file(store, candidate)
            if status == "ambiguous":
                return _fail(f"ambiguous_module:{payload}")
            if status == "hit":
                assert isinstance(payload, FileRow)
                return _hit("import_path", file_node_id(payload.path))
        return None


def resolve_pending(
    store: EventStore, *, limit: int | None = None, config: ResolverConfig | None = None
) -> BatchReport:
    """One-shot convenience entry point for the pipeline (SUB-89)."""
    return ReferenceResolver(config).resolve_pending(store, limit=limit)
