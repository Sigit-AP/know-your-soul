"""Deterministic export orchestrator for generation artifacts (ATM F-B8).

One registry maps artifact names to format specs. A spec carries an explicit
per-artifact byte cap, a producer (read model → canonical bytes) and a
round-trip validator (parse the produced bytes back and assert the
structure). Batch production is fail-closed and deterministic: artifacts are
produced in sorted-name order, a cap breach or a round-trip defect raises a
typed error before any byte reaches a generation, and the same read model
always yields byte-identical output.

The producer interface is deliberately generic — any callable
``read_model -> Mapping`` (a future ``analytics/*`` module, a report section,
a format exporter) becomes a registry entry through
:meth:`ExportRegistry.register_json` without this package importing the
module: the wave integration doc (``docs/recovery/atm-wave-integration.md``)
shows the one-line registration.

No AI, no network, no runtime dependencies beyond the standard library and
the project's own canonical JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from know_your_soul.domain import canonical_json

__all__ = [
    "DEFAULT_EXPORT_MAX_BYTES",
    "EXPORT_KINDS",
    "ArtifactSchema",
    "ExportCapExceededError",
    "ExportError",
    "ExportFormat",
    "ExportRegistry",
    "ExportRoundTripError",
    "UnknownExportFormatError",
    "default_registry",
    "manifest_entries",
    "produce_exports",
]

# Every export artifact has an explicit cap; this is only the *default* for
# ``register_json`` callers that do not pick one. A cap is a fail-closed
# typed breach, never a silent truncation.
DEFAULT_EXPORT_MAX_BYTES = 64 * 1024

# The two producer families: ``format`` exporters (graph serializations) and
# ``analytics`` modules (derived knowledge statistics). The CLI commands
# select on this axis by default.
EXPORT_KINDS = frozenset({"format", "analytics"})

# Artifact names are portable relative paths: one to three ``/``-separated
# segments of identifier-safe characters, each segment starting with an
# alphanumeric, never absolute, never containing ``..`` — so a registry entry
# can never escape the generation directory it is written into.
_ARTIFACT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(\.[A-Za-z0-9._-]+)?")
_MAX_NAME_SEGMENTS = 3
_MAX_NAME_CHARS = 128


class ExportError(RuntimeError):
    """Base class for export-orchestration contract violations."""


class UnknownExportFormatError(ExportError):
    """A caller named a format the registry does not hold."""


class ExportCapExceededError(ExportError):
    """A produced artifact exceeded its explicit per-format byte cap."""


class ExportRoundTripError(ExportError):
    """A produced artifact failed its own parse-back structural validation."""


@dataclass(frozen=True)
class ArtifactSchema:
    """The validator-facing view of one registered artifact format."""

    max_bytes: int
    roundtrip: Callable[[bytes], None]


@dataclass(frozen=True)
class ExportFormat:
    """One registered artifact format.

    ``producer`` maps the generation read model (the parsed ``graph.json``
    node-link mapping) to canonical bytes; ``roundtrip`` parses those bytes
    back and raises on any structural defect. Both must be deterministic and
    free of wall-clock, randomness, network and model calls.
    """

    name: str
    version: int
    kind: str
    max_bytes: int
    producer: Callable[[Mapping[str, Any]], bytes]
    roundtrip: Callable[[bytes], None]


def _validate_artifact_name(name: str) -> None:
    if not name or len(name) > _MAX_NAME_CHARS:
        raise ExportError(f"artifact name {name!r} is empty or over {_MAX_NAME_CHARS} chars")
    segments = name.split("/")
    if not 1 <= len(segments) <= _MAX_NAME_SEGMENTS:
        raise ExportError(f"artifact name {name!r} must have 1..{_MAX_NAME_SEGMENTS} '/'-separated segments")
    for segment in segments:
        if segment in {".", ".."} or _ARTIFACT_NAME_RE.fullmatch(segment) is None:
            raise ExportError(f"artifact name {name!r} carries an unsafe segment {segment!r}")


def _canonical_json_roundtrip(name: str) -> Callable[[bytes], None]:
    """Byte-exact round trip for canonical-JSON artifacts.

    Parses the bytes, requires a top-level object, and re-serializes through
    the pinned canonical JSON: any drift (non-canonical key order, duplicate
    keys collapsed by the parser, non-object payload) is a defect.
    """

    def _roundtrip(data: bytes) -> None:
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExportRoundTripError(f"artifact {name} does not parse as UTF-8 JSON: {error}") from error
        if not isinstance(parsed, dict):
            raise ExportRoundTripError(f"artifact {name} must be a JSON object, got {type(parsed).__name__}")
        if (canonical_json(parsed) + "\n").encode("utf-8") != data:
            raise ExportRoundTripError(f"artifact {name} is not in canonical-JSON form")

    return _roundtrip


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ExportRegistry:
    """Name → :class:`ExportFormat` registry; the single extension point."""

    def __init__(self, formats: Iterable[ExportFormat] = ()) -> None:
        self._formats: dict[str, ExportFormat] = {}
        for spec in formats:
            self.register(spec)

    def register(self, spec: ExportFormat) -> None:
        """Register *spec*; duplicate names and unsafe shapes fail fast."""
        _validate_artifact_name(spec.name)
        if spec.name in self._formats:
            raise ExportError(f"duplicate artifact name {spec.name!r} in the export registry")
        if spec.kind not in EXPORT_KINDS:
            raise ExportError(f"artifact {spec.name!r} has unknown kind {spec.kind!r}; expected {sorted(EXPORT_KINDS)}")
        if spec.max_bytes < 1:
            raise ExportError(f"artifact {spec.name!r} max_bytes must be positive")
        if spec.version < 1:
            raise ExportError(f"artifact {spec.name!r} version must be a positive integer")
        self._formats[spec.name] = spec

    def register_json(
        self,
        name: str,
        fn: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        *,
        kind: str = "analytics",
        version: int = 1,
        max_bytes: int = DEFAULT_EXPORT_MAX_BYTES,
    ) -> None:
        """Register a canonical-JSON artifact from a ``read_model -> Mapping`` callable.

        This is the generic seam for analytics modules (SUB-60/61/62 and
        later waves): any deterministic callable over the read model becomes
        a capped, round-trip-validated, manifest-bound artifact.
        """

        def _producer(read_model: Mapping[str, Any]) -> bytes:
            return (canonical_json(fn(read_model)) + "\n").encode("utf-8")

        self.register(
            ExportFormat(
                name=name,
                version=version,
                kind=kind,
                max_bytes=max_bytes,
                producer=_producer,
                roundtrip=_canonical_json_roundtrip(name),
            )
        )

    def get(self, name: str) -> ExportFormat:
        try:
            return self._formats[name]
        except KeyError:
            raise UnknownExportFormatError(
                f"unknown export format {name!r}; registered: {sorted(self._formats)}"
            ) from None

    def names(self, *, kind: str | None = None) -> tuple[str, ...]:
        """Registered names in canonical (sorted) order, optionally by kind."""
        if kind is not None and kind not in EXPORT_KINDS:
            raise ExportError(f"unknown kind {kind!r}; expected {sorted(EXPORT_KINDS)}")
        return tuple(name for name in sorted(self._formats) if kind is None or self._formats[name].kind == kind)

    def fingerprint(self) -> str:
        """A content hash of the registry shape (name → version/kind/cap).

        Pinned into the generation manifest so a registry change is a
        generation-identity change — two writers with different registries
        can never collide on one content hash.
        """
        shape = {
            name: {"version": spec.version, "kind": spec.kind, "max_bytes": spec.max_bytes}
            for name, spec in sorted(self._formats.items())
        }
        return _sha256(canonical_json(shape).encode("utf-8"))

    def schema_view(self) -> dict[str, ArtifactSchema]:
        """The validator-facing schemas for every registered format."""
        return {
            name: ArtifactSchema(max_bytes=spec.max_bytes, roundtrip=spec.roundtrip)
            for name, spec in self._formats.items()
        }

    def __len__(self) -> int:
        return len(self._formats)


def produce_exports(
    registry: ExportRegistry,
    read_model: Mapping[str, Any],
    *,
    select: Iterable[str] | None = None,
) -> dict[str, bytes]:
    """Produce artifacts for *select* (default: everything), fail-closed.

    Production runs in sorted-name order; each artifact is cap-checked and
    round-trip-validated the moment it exists, so a defective format raises a
    typed error naming the artifact before any publish step can see it.
    """
    if select is None:
        names = registry.names()
    else:
        names = tuple(sorted(dict.fromkeys(select)))
        for name in names:
            registry.get(name)  # raises UnknownExportFormatError on a miss
    artifacts: dict[str, bytes] = {}
    for name in names:
        spec = registry.get(name)
        produced: Any = spec.producer(read_model)
        if not isinstance(produced, (bytes, bytearray)):
            raise ExportError(f"producer for artifact {name} returned {type(produced).__name__}, expected bytes")
        data = bytes(produced)
        if len(data) > spec.max_bytes:
            raise ExportCapExceededError(f"artifact {name} is {len(data)} bytes, over its {spec.max_bytes}-byte cap")
        try:
            spec.roundtrip(data)
        except ExportError:
            raise
        except Exception as error:  # noqa: BLE001 - any defect fails closed, typed
            raise ExportRoundTripError(f"artifact {name} failed its round-trip validation: {error}") from error
        artifacts[name] = data
    return artifacts


def manifest_entries(registry: ExportRegistry, artifacts: Mapping[str, bytes]) -> dict[str, dict[str, Any]]:
    """Manifest entries for produced artifacts: name → sha256/size/version/kind."""
    entries: dict[str, dict[str, Any]] = {}
    for name in sorted(artifacts):
        spec = registry.get(name)
        data = artifacts[name]
        entries[name] = {
            "sha256": _sha256(data),
            "size": len(data),
            "version": spec.version,
            "kind": spec.kind,
        }
    return entries


# ------------------------------------------------------------- built-in set


def _node_index(read_model: Mapping[str, Any]) -> Mapping[str, Any]:
    """``exports/node-index.json``: the deterministic node id index.

    A real derivation from the projection read model (ids, domain, kind,
    version) — the index every ad-hoc export consumer resolves names against.
    """
    raw_nodes = read_model.get("nodes")
    nodes = [node for node in raw_nodes if isinstance(node, dict)] if isinstance(raw_nodes, list) else []
    index = [
        {
            "id": str(node.get("id")),
            "domain": str(node.get("domain")),
            "kind": str(node.get("kind")),
            "version": int(node.get("version", 0)) if isinstance(node.get("version"), int) else 0,
        }
        for node in nodes
    ]
    index.sort(key=lambda entry: entry["id"])
    return {"schema_version": 1, "artifact": "node-index", "nodes": index}


def _domain_rollup(read_model: Mapping[str, Any]) -> Mapping[str, Any]:
    """``analytics/domain-rollup.json``: per-domain node and internal-edge counts.

    The baseline analytics artifact proving the registry seam end to end
    until the wave-A analytics modules register their own entries through the
    same generic interface.
    """
    raw_nodes = read_model.get("nodes")
    nodes = [node for node in raw_nodes if isinstance(node, dict)] if isinstance(raw_nodes, list) else []
    domain_of = {str(node.get("id")): str(node.get("domain")) for node in nodes}
    rollup: dict[str, dict[str, int]] = {
        domain: {"nodes": 0, "internal_edges": 0} for domain in sorted(set(domain_of.values()))
    }
    for domain in domain_of.values():
        rollup[domain]["nodes"] += 1
    raw_links = read_model.get("links")
    links = [link for link in raw_links if isinstance(link, dict)] if isinstance(raw_links, list) else []
    for link in links:
        source, target = str(link.get("source")), str(link.get("target"))
        if source in domain_of and target in domain_of and domain_of[source] == domain_of[target]:
            rollup[domain_of[source]]["internal_edges"] += 1
    return {"schema_version": 1, "artifact": "domain-rollup", "domains": rollup}


def default_registry() -> ExportRegistry:
    """The built-in registry: one format exporter + one analytics module.

    Later waves extend this by registering their own modules (the function
    body is the single registration point; nothing here imports wave-A/B
    modules that do not exist yet).
    """
    registry = ExportRegistry()
    registry.register_json("exports/node-index.json", _node_index, kind="format")
    registry.register_json("analytics/domain-rollup.json", _domain_rollup, kind="analytics")
    return registry
