"""End-to-end ingestion pipeline (Wave 2, SUB-89).

Walks a directory (or a single file) under an ingestion root in canonical
bytewise order and, per file: detects the language, runs the SUB-87
extractor, and writes the proposal rows through the schema-v3 surfaces of
:mod:`know_your_soul.store` (``files`` / ``symbols`` / ``graph_edges`` /
``unresolved_refs``). A resolve pass (SUB-88) then turns pending cross-file
references into edges.

Contract v3 posture (``docs/contracts/ingestion-contract-v3.md``):

* **Proposal-only.** The pipeline writes only the v3 ingestion tables; the
  knowledge domain (``event_log`` domain events, ``node_revision``, the
  projection) is never touched. The returned :class:`IngestionReport` is the
  proposal summary — node/edge/ref counts plus primary-split suggestions
  from :func:`know_your_soul.graph_model.propose_primary_splits` — to be
  applied to the main graph exclusively through the operator-gated
  propose/apply path.
* **Exactly one audit event per invocation** (``knowledge.ingest_file`` or
  ``knowledge.ingest_directory``), success and typed failure alike, carrying
  the input sha256, the ``extraction_version`` and the proposal counts —
  never input content.
* **Deterministic**: traversal order is root-relative paths sorted bytewise;
  every iteration inside the pipeline is sorted, so identical input bytes
  under an identical extractor version yield byte-identical v3 rows (the one
  volatile column is ``unresolved_refs.first_seen``, mirroring the contract's
  "modulo the per-invocation audit id" clause).

Sibling dependency protocol (imported lazily, typed fallback):

* ``know_your_soul.ingest.extractor`` (SUB-87) exposes
  ``EXTRACTION_VERSION: int`` and
  ``extract_source(path, source, *, language=None, generated_hint=False)``
  returning a result with ``nodes`` (``.node_id``/``.kind``/``.name``/
  ``.start_line``/``.start_col``), ``edges``
  (``.source``/``.target``/``.kind``) and ``unresolved_refs``
  (``.source_node_id``/``.target_name``/``.name_tail``/``.kind`` — the agreed
  raw ref shape). Language detection defers to
  ``know_your_soul.ingest.languages.language_for_path`` when importable, with
  a local table (adding the W3 document formats) as fallback. A missing
  module raises :class:`ExtractorUnavailableError`.
* ``know_your_soul.resolve.resolver`` (SUB-88) exposes
  ``ReferenceResolver().resolve_pending(store)`` returning a batch report
  with ``resolved``/``failed``/``pending_remaining`` counts. A missing module
  raises :class:`ResolverUnavailableError` from
  :meth:`IngestionPipeline.resolve_pending`; ``ingest_path`` reports it as
  ``resolution_status="unavailable"`` instead of crashing the walk.

Incremental tracking (codegraph rule): a cheap ``(size, mtime)`` pre-filter
skips unchanged files without reading them; a mismatch is confirmed by
sha256 — same hash means only the stat fields are resynced, a real change
means a bounded cascade (the file's nodes, their edges, and refs they
sourced are deleted in one transaction) followed by re-extraction. Edges
from *other* files pointing at deleted nodes are resurrected as ``pending``
unresolved refs (codegraph ``resurrectRefFromDroppedEdge``) so the next
resolve pass can re-bind them.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from know_your_soul.audit import AuditLog, failure_outcome, success_outcome
from know_your_soul.domain import canonical_json
from know_your_soul.events import Invocation
from know_your_soul.graph_model import (
    GraphEdge,
    GraphModelError,
    GraphNode,
    SplitThresholds,
    UniversalGraph,
    file_node_id,
    make_node_id,
    propose_primary_splits,
)
from know_your_soul.store import EventStore

__all__ = [
    "DEFAULT_MAX_FILE_BYTES",
    "EXTRACTION_VERSION_FALLBACK",
    "ExtractorUnavailableError",
    "FileReport",
    "IngestionBudget",
    "IngestionBudgetError",
    "IngestionError",
    "IngestionPipeline",
    "IngestionReport",
    "PathNotFoundError",
    "PathNotReadableError",
    "PathOutsideRootError",
    "ResolverUnavailableError",
    "canonical_ingestion_dump",
    "detect_language",
    "is_generated_hint",
]

#: Used in the audit event when the extractor module has not landed yet and a
#: test double drives extraction — the real version is SUB-87's constant.
EXTRACTION_VERSION_FALLBACK = 0

#: Phase-1 languages (master plan W2) plus the W3 document formats; a file
#: with no registered language fails typed ``UNSUPPORTED_MEDIA_TYPE``. When
#: the SUB-87 language registry is importable it wins; this table is the
#: fallback and adds formats the code-only registry does not know.
_LANGUAGE_BY_EXTENSION: dict[str, str] = {
    "bash": "bash",
    "c": "c",
    "cc": "cpp",
    "cjs": "javascript",
    "cpp": "cpp",
    "cs": "csharp",
    "cxx": "cpp",
    "go": "go",
    "h": "c",
    "hpp": "cpp",
    "java": "java",
    "js": "javascript",
    "jsx": "javascript",
    "markdown": "markdown",
    "md": "markdown",
    "mjs": "javascript",
    "pdf": "pdf",
    "php": "php",
    "py": "python",
    "pyi": "python",
    "rb": "ruby",
    "rs": "rust",
    "sh": "bash",
    "ts": "typescript",
    "tsx": "typescript",
    "txt": "text",
}
_LANGUAGE_BY_FILENAME: dict[str, str] = {
    ".bashrc": "bash",
    "dockerfile": "dockerfile",
    "makefile": "makefile",
}

#: Directories that never participate in a walk (codegraph scope-ignore).
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git", ".hypothesis", ".mypy_cache", ".pytest_cache", ".venv",
        "__pycache__", "build", "dist", "node_modules", "venv",
    }
)

_GENERATED_MARKERS: tuple[bytes, ...] = (b"DO NOT EDIT", b"@generated")
_GENERATED_PATH_PARTS: frozenset[str] = frozenset({"build", "dist", "node_modules"})

DEFAULT_MAX_FILE_BYTES = 4 * 1024 * 1024

#: Edge kinds that resolution creates; only these resurrect as pending refs
#: when their target is deleted (``contains`` is hierarchy, never a ref).
_HIERARCHY_KIND = "contains"

try:  # SUB-87 language registry is canonical when present
    from know_your_soul.ingest.languages import language_for_path as _sibling_language_for_path
except ImportError:  # pragma: no cover - exercised only before SUB-87 lands
    _sibling_language_for_path = None


# --------------------------------------------------------------------- errors


class IngestionError(RuntimeError):
    """Base class for typed ingestion failures; ``code`` is the API shape."""

    code = "INGESTION_ERROR"


class PathNotFoundError(IngestionError):
    code = "PATH_NOT_FOUND"


class PathNotReadableError(IngestionError):
    code = "PATH_NOT_READABLE"


class PathOutsideRootError(IngestionError):
    code = "PATH_OUTSIDE_ROOT"


class UnsupportedMediaTypeError(IngestionError):
    code = "UNSUPPORTED_MEDIA_TYPE"


class FileTooLargeError(IngestionError):
    code = "FILE_TOO_LARGE"


class IngestionBudgetError(IngestionError):
    code = "INGESTION_BUDGET_EXCEEDED"


class ExtractorUnavailableError(NotImplementedError):
    """The SUB-87 extractor module has not landed and none was injected."""

    code = "EXTRACTOR_UNAVAILABLE"


class ResolverUnavailableError(NotImplementedError):
    """The SUB-88 resolver module has not landed and none was injected."""

    code = "RESOLVER_UNAVAILABLE"


# ------------------------------------------------------------------ languages


def detect_language(relpath: str) -> str | None:
    """Map a root-relative path to its language, deterministically.

    The SUB-87 registry (``language_for_path``) wins when importable;
    otherwise the local table applies — basenames win over extensions
    (``Makefile``), extensions match case-insensitively. Unknown means
    ``None`` — never a guess.
    """
    if not isinstance(relpath, str) or not relpath:
        return None
    if _sibling_language_for_path is not None:
        detected = _sibling_language_for_path(relpath)
        if detected is not None:
            return detected
    basename = relpath.rsplit("/", 1)[-1]
    by_name = _LANGUAGE_BY_FILENAME.get(basename.lower())
    if by_name is not None:
        return by_name
    if "." not in basename:
        return None
    extension = basename.rsplit(".", 1)[-1].lower()
    return _LANGUAGE_BY_EXTENSION.get(extension)


def is_generated_hint(relpath: str, content: bytes) -> bool:
    """Deterministic generated-file heuristic (path parts + head markers)."""
    parts = set(relpath.split("/"))
    if parts & _GENERATED_PATH_PARTS:
        return True
    head = content[:512]
    return any(marker in head for marker in _GENERATED_MARKERS)


# -------------------------------------------------------------------- budget


@dataclass(frozen=True)
class IngestionBudget:
    """Fail-closed caps for one ingestion invocation.

    ``max_files`` bounds the walk (contract range [1, 65536]); the others
    cap the totals written to the v3 tables — exceeding any of them fails
    the invocation typed with :class:`IngestionBudgetError`.
    """

    max_files: int = 1024
    max_nodes: int = 65536
    max_edges: int = 131072
    max_refs: int = 65536
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES

    def __post_init__(self) -> None:
        if isinstance(self.max_files, bool) or not isinstance(self.max_files, int) or not 1 <= self.max_files <= 65536:
            raise ValueError(f"max_files must be an int in [1, 65536], got {self.max_files!r}")
        for name in ("max_nodes", "max_edges", "max_refs", "max_file_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be an int >= 1, got {value!r}")


# -------------------------------------------------------------------- report


@dataclass(frozen=True)
class FileReport:
    """One walk entry, in traversal order; failure entries carry the code."""

    relpath: str
    status: str  # "proposed" | "skipped" | "failed"
    language: str | None = None
    nodes: int = 0
    edges: int = 0
    refs: int = 0
    sha256: str | None = None
    size: int | None = None
    error_code: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"relpath": self.relpath, "status": self.status}
        if self.language is not None:
            result["language"] = self.language
        if self.status == "proposed":
            result["nodes"] = self.nodes
            result["edges"] = self.edges
            result["refs"] = self.refs
        if self.sha256 is not None:
            result["sha256"] = self.sha256
        if self.size is not None:
            result["size"] = self.size
        if self.error_code is not None:
            result["error_code"] = self.error_code
        return result


@dataclass(frozen=True)
class IngestionReport:
    """The proposal-only summary of one ingestion invocation."""

    status: str  # "proposal" (typed failures raise instead)
    root: str
    input_path: str
    input_sha256: str
    extraction_version: int
    files: tuple[FileReport, ...]
    stats: Mapping[str, int]
    totals: Mapping[str, int]
    proposal: Mapping[str, int]
    primary_suggestions: tuple[Mapping[str, Any], ...]
    guards_hit: tuple[str, ...] = ()
    resolution: Mapping[str, int] | None = None
    resolution_status: str = "skipped"  # "ok" | "skipped" | "unavailable"
    audit_event_id: str | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "root": self.root,
            "input": {"path": self.input_path, "sha256": self.input_sha256},
            "extraction_version": self.extraction_version,
            "stats": dict(self.stats),
            "totals": dict(self.totals),
            "proposal": dict(self.proposal),
            "primary_suggestions": [dict(s) for s in self.primary_suggestions],
            "guards_hit": list(self.guards_hit),
            "resolution": None if self.resolution is None else dict(self.resolution),
            "resolution_status": self.resolution_status,
            "audit_event_id": self.audit_event_id,
            "duration_ms": self.duration_ms,
            "files": [entry.to_dict() for entry in self.files],
        }


# ------------------------------------------------------- dependency loading


def _default_extractor() -> Any:
    """Import the SUB-87 extractor module; typed fallback when absent."""
    try:
        from know_your_soul.ingest import extractor
    except ImportError as error:
        raise ExtractorUnavailableError(
            "know_your_soul.ingest.extractor is not available (SUB-87 has not landed); "
            "inject an extractor implementing the documented protocol"
        ) from error
    return extractor


def _default_resolver() -> Any | None:
    """Import the SUB-88 resolver module; ``None`` when it has not landed."""
    try:
        from know_your_soul.resolve import resolver
    except ImportError:
        return None
    return resolver


def _normalize_resolution(result: Any) -> dict[str, int]:
    """Normalize a resolver batch report (SUB-88 ``BatchReport`` or mapping)."""
    if isinstance(result, Mapping):
        return {
            "resolved": int(result["resolved"]),
            "failed": int(result["failed"]),
            "pending": int(result.get("pending", result.get("pending_remaining", 0))),
        }
    return {
        "resolved": int(result.resolved),
        "failed": int(result.failed),
        "pending": int(result.pending_remaining),
    }


def _field(item: Any, name: str, default: Any = None) -> Any:
    """Attribute-or-key access so both dataclass and mapping results work."""
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _normalize_nodes(extraction: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for raw in extraction.nodes:
        nodes.append(
            {
                "node_id": str(_field(raw, "node_id")),
                "kind": str(_field(raw, "kind")),
                "name": str(_field(raw, "name")),
                "line": int(_field(raw, "line", _field(raw, "start_line"))),
                "col": _field(raw, "col", _field(raw, "start_col")),
            }
        )
    return nodes


def _normalize_edges(extraction: Any) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for raw in extraction.edges:
        edges.append(
            {
                "source": str(_field(raw, "source")),
                "target": str(_field(raw, "target")),
                "kind": str(_field(raw, "kind")),
                "line": _field(raw, "line"),
                "col": _field(raw, "col"),
                "metadata": _field(raw, "metadata"),
            }
        )
    return edges


def _normalize_refs(extraction: Any) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for raw in extraction.unresolved_refs:
        target_name = str(_field(raw, "target_name"))
        tail = _field(raw, "name_tail") or target_name.rsplit(".", 1)[-1]
        refs.append(
            {
                "source_node_id": str(_field(raw, "source_node_id")),
                "target_name": target_name,
                "name_tail": str(tail),
                "kind": str(_field(raw, "kind")),
            }
        )
    return refs


# ------------------------------------------------------------------ pipeline


class IngestionPipeline:
    """Directory/file ingestion into the schema-v3 tables, proposal-only."""

    def __init__(
        self,
        store: EventStore,
        *,
        extractor: Any | None = None,
        resolver: Any | None = None,
        budget: IngestionBudget | None = None,
        split_thresholds: SplitThresholds | None = None,
        actor: str = "operator",
    ) -> None:
        self._store = store
        self._extractor = extractor
        self._resolver = resolver
        self._budget = budget if budget is not None else IngestionBudget()
        self._split_thresholds = split_thresholds
        self._actor = actor
        self._audit = AuditLog(store)

    # ------------------------------------------------------------ public API

    def ingest_path(
        self,
        path: str | Path,
        *,
        incremental: bool = True,
        recursive: bool = True,
        resolve: bool = True,
    ) -> IngestionReport:
        """Ingest one file or directory tree; exactly one audit event."""
        started = time.monotonic()
        raw = str(path)
        invocation = Invocation.create(
            self._operation_name_for(raw),
            request_hash=hashlib.sha256(
                canonical_json(
                    {
                        "path": raw,
                        "incremental": incremental,
                        "recursive": recursive,
                        "max_files": self._budget.max_files,
                    }
                ).encode("utf-8")
            ).hexdigest(),
            actor=self._actor,
        )
        self._audit.begin(invocation)
        try:
            report = self._run(raw, incremental=incremental, recursive=recursive, resolve=resolve, started=started)
        except (IngestionError, ExtractorUnavailableError) as error:
            outcome = failure_outcome(invocation, started, error=_typed(error))
            self._audit.complete(invocation.invocation_id, outcome)
            raise
        outcome = success_outcome(
            invocation,
            started,
            result_references=self._audit_references(report),
            projection_outcome="skipped",
        )
        receipt = self._audit.complete(invocation.invocation_id, outcome)
        return replace(report, audit_event_id=receipt.audit_event_id, duration_ms=outcome.duration_ms)

    def resolve_pending(self) -> Mapping[str, int]:
        """Run the SUB-88 resolve pass; typed fallback when it has not landed."""
        resolver = self._resolver if self._resolver is not None else _default_resolver()
        if resolver is None:
            raise ResolverUnavailableError(
                "know_your_soul.resolve.resolver is not available (SUB-88 has not landed); "
                "inject a resolver implementing the documented protocol"
            )
        if hasattr(resolver, "ReferenceResolver"):
            resolver = resolver.ReferenceResolver()
        result = resolver.resolve_pending(self._store)
        return _normalize_resolution(result)

    # --------------------------------------------------------------- walking

    @staticmethod
    def _operation_name_for(raw: str) -> str:
        # The verb is fixed by the path kind; a missing path is still audited
        # (as a failure) under the directory verb.
        return "knowledge.ingest_file" if Path(raw).is_file() else "knowledge.ingest_directory"

    def _run(self, raw: str, *, incremental: bool, recursive: bool, resolve: bool, started: float) -> IngestionReport:
        candidate = Path(raw)
        if not candidate.exists():
            raise PathNotFoundError(f"input path {raw!r} does not exist")
        try:
            resolved_input = candidate.resolve(strict=True)
        except OSError as error:
            raise PathNotReadableError(f"input path {raw!r} cannot be resolved: {error}") from error
        try:
            is_dir = resolved_input.is_dir()
        except OSError as error:
            raise PathNotReadableError(f"input path {raw!r} is not readable: {error}") from error
        if is_dir:
            root = resolved_input
            rel_input = "."
        else:
            root = resolved_input.parent
            rel_input = resolved_input.name

        extractor = self._extractor if self._extractor is not None else _default_extractor()
        extraction_version = int(getattr(extractor, "EXTRACTION_VERSION", EXTRACTION_VERSION_FALLBACK))

        entries = self._walk(root, rel_input if not is_dir else None, recursive=recursive)

        stats = {"proposed": 0, "failed": 0, "skipped": 0, "deleted": 0, "scanned": 0}
        totals = {"nodes": 0, "edges": 0, "refs_parked": 0}
        reports: list[FileReport] = []
        digest = hashlib.sha256()
        guards: list[str] = []
        seen: set[str] = set()
        introduced_tails: set[str] = set()
        proposals_nodes: list[GraphNode] = []
        proposals_edges: list[GraphEdge] = []

        for abs_path, relpath in entries:
            if stats["scanned"] >= self._budget.max_files:
                guards.append("max_files")
                break
            stats["scanned"] += 1
            seen.add(relpath)
            entry = self._ingest_one(
                root,
                abs_path,
                relpath,
                extractor=extractor,
                extraction_version=extraction_version,
                incremental=incremental,
                stats=stats,
                totals=totals,
                digest=digest,
                introduced_tails=introduced_tails,
                proposals_nodes=proposals_nodes,
                proposals_edges=proposals_edges,
            )
            reports.append(entry)

        if is_dir:
            stats["deleted"] = self._delete_missing(seen, digest) if incremental else 0

        # Failed refs retry when a changed/landed file introduced a matching
        # name tail (codegraph #1240): move them back to pending so the
        # resolve pass below re-binds them. Deterministic: tails sorted,
        # store queries ordered.
        if resolve:
            for tail in sorted(introduced_tails):
                for failed_ref in self._store.failed_unresolved_refs(name_tail=tail):
                    self._store.retry_failed_ref(failed_ref.ref_id)

        resolution: Mapping[str, int] | None = None
        resolution_status = "skipped"
        if resolve:
            try:
                resolution = self.resolve_pending()
                resolution_status = "ok"
            except ResolverUnavailableError:
                resolution_status = "unavailable"

        suggestions = self._primary_suggestions(rel_input, proposals_nodes, proposals_edges)
        return IngestionReport(
            status="proposal",
            root=str(root),
            input_path=rel_input,
            input_sha256=digest.hexdigest(),
            extraction_version=extraction_version,
            files=tuple(reports),
            stats=stats,
            totals=totals,
            proposal={
                "nodes": totals["nodes"],
                "edges": totals["edges"],
                "refs_parked": totals["refs_parked"],
            },
            primary_suggestions=suggestions,
            guards_hit=tuple(guards),
            resolution=resolution,
            resolution_status=resolution_status,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    def _walk(
        self, root: Path, single: str | None, *, recursive: bool
    ) -> list[tuple[Path, str]]:
        """Canonical-order (absolute path, relpath) pairs, bytewise by relpath.

        The store's own database file (and its WAL/SHM sidecars) is never
        part of the corpus — ingesting the state file into itself would be a
        feedback loop, so it is excluded from the walk outright.
        """
        if self._store.db_path == ":memory:":
            store_paths: set[str] = set()
        else:
            real_db = os.path.realpath(self._store.db_path)
            store_paths = {real_db, real_db + "-wal", real_db + "-shm"}
        if single is not None:
            return [(root / single, single)]
        found: list[tuple[Path, str]] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
            if not recursive and Path(dirpath) != root:
                dirnames[:] = []
                continue
            for name in sorted(filenames):
                abs_path = Path(dirpath) / name
                if os.path.realpath(abs_path) in store_paths:
                    continue
                relpath = abs_path.relative_to(root).as_posix()
                found.append((abs_path, relpath))
        found.sort(key=lambda pair: pair[1].encode("utf-8"))
        return found

    # ------------------------------------------------------------- per file

    def _ingest_one(
        self,
        root: Path,
        abs_path: Path,
        relpath: str,
        *,
        extractor: Any,
        extraction_version: int,
        incremental: bool,
        stats: dict[str, int],
        totals: dict[str, int],
        digest: Any,
        introduced_tails: set[str] | None,
        proposals_nodes: list[GraphNode],
        proposals_edges: list[GraphEdge],
    ) -> FileReport:
        try:
            real = abs_path.resolve(strict=True)
            if not real.is_relative_to(root):
                raise PathOutsideRootError(f"{relpath!r} resolves outside the ingestion root")
        except PathOutsideRootError:
            stats["failed"] += 1
            digest.update(f"{relpath}\0ERROR:PATH_OUTSIDE_ROOT\n".encode())
            return FileReport(relpath=relpath, status="failed", error_code="PATH_OUTSIDE_ROOT")
        except OSError:
            stats["failed"] += 1
            digest.update(f"{relpath}\0ERROR:PATH_NOT_READABLE\n".encode())
            return FileReport(relpath=relpath, status="failed", error_code="PATH_NOT_READABLE")

        stat = real.stat()
        size = int(stat.st_size)
        mtime = int(stat.st_mtime)
        if size > self._budget.max_file_bytes:
            stats["failed"] += 1
            digest.update(f"{relpath}\0ERROR:FILE_TOO_LARGE\n".encode())
            return FileReport(relpath=relpath, status="failed", size=size, error_code="FILE_TOO_LARGE")
        language = detect_language(relpath)
        if language is None:
            stats["failed"] += 1
            digest.update(f"{relpath}\0ERROR:UNSUPPORTED_MEDIA_TYPE\n".encode())
            return FileReport(relpath=relpath, status="failed", size=size, error_code="UNSUPPORTED_MEDIA_TYPE")

        tracked = self._store.get_file(relpath) if incremental else None
        if (
            tracked is not None
            and tracked.size == size
            and tracked.mtime == mtime
            and tracked.extraction_version == extraction_version
        ):
            stats["skipped"] += 1
            digest.update(f"{relpath}\0{tracked.sha256}\n".encode())
            return FileReport(relpath=relpath, status="skipped", language=language, sha256=tracked.sha256, size=size)

        try:
            content = real.read_bytes()
        except OSError:
            stats["failed"] += 1
            digest.update(f"{relpath}\0ERROR:PATH_NOT_READABLE\n".encode())
            return FileReport(
                relpath=relpath, status="failed", language=language, size=size, error_code="PATH_NOT_READABLE"
            )
        sha256 = hashlib.sha256(content).hexdigest()
        digest.update(f"{relpath}\0{sha256}\n".encode())

        if tracked is not None and tracked.sha256 == sha256 and tracked.extraction_version == extraction_version:
            # Pre-filter suspect confirmed unchanged: resync the stat fields.
            self._store.upsert_file(
                path=relpath,
                sha256=sha256,
                size=size,
                mtime=mtime,
                language=language,
                generated_hint=tracked.generated_hint,
                extraction_version=extraction_version,
            )
            stats["skipped"] += 1
            return FileReport(relpath=relpath, status="skipped", language=language, sha256=sha256, size=size)

        try:
            source = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            stats["failed"] += 1
            return FileReport(relpath=relpath, status="failed", language=language, size=size, error_code="DECODE_ERROR")

        try:
            extraction = extractor.extract_source(relpath, source, language=None, generated_hint=False)
        except Exception as error:  # noqa: BLE001 - per-file failure never aborts the walk
            stats["failed"] += 1
            code = getattr(error, "code", None)
            if not isinstance(code, str):
                code = "EXTRACTION_FAILED"
            return FileReport(relpath=relpath, status="failed", language=language, size=size, error_code=code)

        nodes_all = _normalize_nodes(extraction)
        edges = _normalize_edges(extraction)
        refs = _normalize_refs(extraction)
        # The SUB-87 extractor emits the file node itself; the ToyExtractor
        # protocol does not. Partition so the file node is counted exactly
        # once and never pollutes the symbol lookup surface.
        file_id = file_node_id(relpath)
        nodes = [node for node in nodes_all if node["node_id"] != file_id]
        self._check_budget(totals, nodes=len(nodes) + 1, edges=len(edges), refs=len(refs))

        generated = bool(getattr(extraction, "generated_hint", False)) or is_generated_hint(relpath, content)
        with self._store.transaction() as conn:
            if tracked is not None:
                self._cascade_file(conn, relpath)
            self._write_batch(
                conn,
                relpath=relpath,
                nodes=nodes,
                edges=edges,
                refs=refs,
                sha256=sha256,
                size=size,
                mtime=mtime,
                language=str(getattr(extraction, "language", language)),
                generated=generated,
                extraction_version=int(getattr(extraction, "extraction_version", extraction_version)),
            )
        stats["proposed"] += 1
        totals["nodes"] += len(nodes) + 1
        totals["edges"] += len(edges)
        totals["refs_parked"] += len(refs)
        if introduced_tails is not None:
            introduced_tails.update(node["name"] for node in nodes)
        self._collect_proposal_nodes(relpath, file_id, nodes, edges, proposals_nodes, proposals_edges)
        return FileReport(
            relpath=relpath,
            status="proposed",
            language=language,
            nodes=len(nodes) + 1,
            edges=len(edges),
            refs=len(refs),
            sha256=sha256,
            size=size,
        )

    def _check_budget(self, totals: Mapping[str, int], *, nodes: int, edges: int, refs: int) -> None:
        candidates = (
            ("nodes", totals["nodes"] + nodes, self._budget.max_nodes),
            ("edges", totals["edges"] + edges, self._budget.max_edges),
            ("refs", totals["refs_parked"] + refs, self._budget.max_refs),
        )
        for name, projected, cap in candidates:
            if projected > cap:
                raise IngestionBudgetError(
                    f"INGESTION_BUDGET_EXCEEDED: {name} budget exceeded: {projected} > {cap} (fail-closed)"
                )

    # --------------------------------------------------------------- storage
    #
    # The batch below mirrors the exact statements of the store v3 helpers
    # (``upsert_file`` / ``upsert_symbol`` / ``add_graph_edges`` /
    # ``insert_unresolved_ref``) inside ONE transaction: each helper opens its
    # own transaction, which cannot nest, and a file batch must be atomic —
    # the ``files`` row is written last as the commit marker so a crash
    # mid-batch is re-ingested on the next run.

    def _write_batch(
        self,
        conn: sqlite3.Connection,
        *,
        relpath: str,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        refs: list[dict[str, Any]],
        sha256: str,
        size: int,
        mtime: int,
        language: str,
        generated: bool,
        extraction_version: int,
    ) -> None:
        for node in nodes:
            col = node["col"]
            conn.execute(
                "INSERT INTO symbols (node_id, name, kind, path, line, col) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (node_id) DO UPDATE SET "
                "name = excluded.name, kind = excluded.kind, path = excluded.path, "
                "line = excluded.line, col = excluded.col",
                (node["node_id"], node["name"], node["kind"], relpath, node["line"], -1 if col is None else int(col)),
            )
        for edge in edges:
            conn.execute(
                "INSERT OR IGNORE INTO graph_edges (source, target, kind, line, col, metadata, provenance) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    edge["source"],
                    edge["target"],
                    edge["kind"],
                    edge["line"],
                    edge["col"],
                    edge["metadata"],
                    "ingestion",
                ),
            )
        for ref in refs:
            conn.execute(
                "INSERT INTO unresolved_refs (source_node_id, target_name, name_tail, kind, status, "
                "first_seen, attempts) "
                "VALUES (?, ?, ?, ?, 'pending', ?, 0)",
                (ref["source_node_id"], ref["target_name"], ref["name_tail"], ref["kind"], f"ingest:{sha256[:16]}"),
            )
        conn.execute(
            "INSERT INTO files (path, sha256, size, mtime, language, generated_hint, extraction_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (path) DO UPDATE SET "
            "sha256 = excluded.sha256, size = excluded.size, mtime = excluded.mtime, "
            "language = excluded.language, generated_hint = excluded.generated_hint, "
            "extraction_version = excluded.extraction_version",
            (relpath, sha256, size, mtime, language, int(generated), extraction_version),
        )

    def _cascade_file(self, conn: sqlite3.Connection, relpath: str) -> None:
        """Bounded cascade: a file's nodes, their edges, and their refs.

        Edges from surviving nodes pointing INTO the deleted set resurrect as
        ``pending`` unresolved refs (never ``contains`` — hierarchy is not a
        reference), so the next resolve pass re-binds them.
        """
        file_id = file_node_id(relpath)
        symbol_rows = conn.execute(
            "SELECT node_id, name FROM symbols WHERE path = ? ORDER BY node_id", (relpath,)
        ).fetchall()
        old_nodes = [file_id] + [str(row["node_id"]) for row in symbol_rows]
        names = {str(row["node_id"]): str(row["name"]) for row in symbol_rows}
        names[file_id] = relpath
        placeholders = ", ".join("?" for _ in old_nodes)
        incoming = conn.execute(
            f"SELECT source, target, kind FROM graph_edges WHERE target IN ({placeholders}) "
            "ORDER BY kind, source, IFNULL(line, -1), IFNULL(col, -1), edge_id",
            tuple(old_nodes),
        ).fetchall()
        old_set = set(old_nodes)
        for row in incoming:
            source, target, kind = str(row["source"]), str(row["target"]), str(row["kind"])
            if kind == _HIERARCHY_KIND or source in old_set:
                continue
            conn.execute(
                "INSERT INTO unresolved_refs (source_node_id, target_name, name_tail, kind, status, "
                "first_seen, attempts) "
                "VALUES (?, ?, ?, ?, 'pending', ?, 0)",
                (source, names[target], names[target].rsplit(".", 1)[-1], kind, f"resurrect:{relpath}"),
            )
        conn.execute(
            f"DELETE FROM graph_edges WHERE source IN ({placeholders}) OR target IN ({placeholders})",
            tuple(old_nodes) + tuple(old_nodes),
        )
        conn.execute(f"DELETE FROM unresolved_refs WHERE source_node_id IN ({placeholders})", tuple(old_nodes))
        conn.execute("DELETE FROM symbols WHERE path = ?", (relpath,))

    def _delete_missing(self, seen: set[str], digest: Any) -> int:
        """Cascade tracked files that vanished from the walk (incremental)."""
        tracked = {row.path for row in self._store.list_files()}
        deleted = 0
        for relpath in sorted(tracked - seen):
            with self._store.transaction() as conn:
                self._cascade_file(conn, relpath)
                conn.execute("DELETE FROM files WHERE path = ?", (relpath,))
            digest.update(f"{relpath}\0DELETED\n".encode())
            deleted += 1
        return deleted

    # ---------------------------------------------------- proposal summary

    def _collect_proposal_nodes(
        self,
        relpath: str,
        file_id: str,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        out_nodes: list[GraphNode],
        out_edges: list[GraphEdge],
    ) -> None:
        try:
            out_nodes.append(GraphNode(node_id=file_id, kind="file", name=Path(relpath).name, path=relpath))
        except (GraphModelError, ValueError):
            return
        known = {file_id}
        for node in nodes:
            line = int(node["line"])
            raw_col = node["col"]
            col: int | None = None if raw_col is None else int(raw_col)
            # The extractor mints the id; the id carries a :col suffix only
            # when the allocator disambiguated a collision. Reconstruct the
            # matching (line, col) pair so GraphNode's identity check passes.
            try:
                if str(node["node_id"]) == make_node_id(path=relpath, kind=node["kind"], name=node["name"], line=line):
                    col = None
                elif col is not None and str(node["node_id"]) != make_node_id(
                    path=relpath, kind=node["kind"], name=node["name"], line=line, col=col
                ):
                    continue
                graph_node = GraphNode(
                    node_id=str(node["node_id"]),
                    kind=str(node["kind"]),
                    name=str(node["name"]),
                    path=relpath,
                    line=line,
                    col=col,
                )
            except (GraphModelError, ValueError, KeyError):
                continue
            out_nodes.append(graph_node)
            known.add(graph_node.node_id)
        for edge in edges:
            if str(edge["kind"]) != _HIERARCHY_KIND:
                continue
            source, target = str(edge["source"]), str(edge["target"])
            if source not in known or target not in known:
                continue
            try:
                out_edges.append(GraphEdge(source=source, target=target, kind=_HIERARCHY_KIND))
            except (GraphModelError, ValueError):
                continue

    def _primary_suggestions(
        self,
        rel_input: str,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> tuple[Mapping[str, Any], ...]:
        """Primary-split suggestions over the ingested batch (proposal-only)."""
        from know_your_soul.domain import PrimaryDomain

        root_id = make_node_id(path=rel_input, kind="primary", name="ingest-root")
        graph = UniversalGraph()
        try:
            graph.add_node(
                GraphNode(
                    node_id=root_id,
                    kind="primary",
                    name="ingest-root",
                    path=rel_input,
                    domain=PrimaryDomain.P1,
                    purpose="ingestion proposal root",
                )
            )
        except (GraphModelError, ValueError):
            return ()
        for node in nodes:
            try:
                graph.add_node(node)
            except GraphModelError:
                continue
        for edge in edges:
            try:
                graph.add_edge(edge)
            except GraphModelError:
                continue
        file_ids = sorted(n.node_id for n in nodes if n.kind == "file" and graph.contains_parent(n.node_id) is None)
        for fid in file_ids:
            try:
                graph.add_edge(GraphEdge(source=root_id, target=fid, kind="contains"))
            except GraphModelError:
                continue
        try:
            proposals = propose_primary_splits(graph, root_id, self._split_thresholds)
        except GraphModelError:
            return ()
        return tuple(
            {
                "root_id": proposal.root_id,
                "suggested_name": proposal.suggested_name,
                "domain": None if proposal.domain is None else str(proposal.domain),
                "member_count": len(proposal.member_node_ids),
                "reasons": list(proposal.reasons),
                "metrics": [[name, value] for name, value in proposal.metrics],
            }
            for proposal in proposals
        )

    # ------------------------------------------------------------------ audit

    @staticmethod
    def _audit_references(report: IngestionReport) -> Mapping[str, Any]:
        return {
            "input_path": report.input_path,
            "input_sha256": report.input_sha256,
            "extraction_version": report.extraction_version,
            "proposed": report.stats["proposed"],
            "failed": report.stats["failed"],
            "skipped": report.stats["skipped"],
            "deleted": report.stats["deleted"],
            "nodes": report.totals["nodes"],
            "edges": report.totals["edges"],
            "refs_parked": report.totals["refs_parked"],
            "resolution_status": report.resolution_status,
        }


def _typed(error: BaseException) -> BaseException:
    return error


# ------------------------------------------------------------- determinism


def canonical_ingestion_dump(store: EventStore, *, include_ids: bool = True) -> str:
    """A byte-stable dump of the v3 ingestion tables.

    Every persisted column is a pure function of the input bytes and the
    extraction version — including ``unresolved_refs.first_seen``, which the
    pipeline stamps with deterministic provenance markers
    (``ingest:<sha256[:16]>`` / ``resurrect:<relpath>``) rather than a wall
    clock, so two ingestions of identical bytes are byte-identical.

    ``include_ids=False`` drops the AUTOINCREMENT rowids (``edge_id`` /
    ``ref_id``): an incremental run that deletes and re-writes a file
    legitimately re-numbers those rows, so convergence comparisons between
    an incremental history and a fresh ingest use the logical form.
    """
    with store.read() as conn:
        files = conn.execute(
            "SELECT path, sha256, size, mtime, language, generated_hint, extraction_version FROM files ORDER BY path"
        ).fetchall()
        symbols = conn.execute("SELECT * FROM symbols ORDER BY node_id").fetchall()
        edges = conn.execute("SELECT * FROM graph_edges ORDER BY edge_id").fetchall()
        refs = conn.execute("SELECT * FROM unresolved_refs ORDER BY ref_id").fetchall()

    def _rows(rows: Any, drop: tuple[str, ...]) -> list[dict[str, Any]]:
        return [{k: v for k, v in dict(row).items() if k not in drop} for row in rows]

    edge_rows = _rows(edges, () if include_ids else ("edge_id",))
    ref_rows = _rows(refs, () if include_ids else ("ref_id",))
    if not include_ids:
        # Logical form: order by content, not insertion history.
        edge_rows.sort(key=lambda r: (r["source"], r["target"], r["kind"], r["line"] or -1, r["col"] or -1))
        ref_rows.sort(key=lambda r: (r["source_node_id"], r["target_name"], r["kind"], r["status"]))
    return canonical_json(
        {
            "files": [dict(row) for row in files],
            "symbols": [dict(row) for row in symbols],
            "graph_edges": edge_rows,
            "unresolved_refs": ref_rows,
        }
    )
