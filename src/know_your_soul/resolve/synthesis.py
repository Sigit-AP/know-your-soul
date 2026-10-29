"""Bounded synthesis passes over the resolved graph (wave W7, SUB-103).

Re-implements the *applicable* subset of codegraph's 45 ``SYNTH_PASSES``
(RESEARCH-COVERAGE-AUDIT §4.1) — rewritten against the schema-v4 store, not
ported. The four passes close graph gaps that static extraction + primary
resolution structurally cannot see:

* ``method_class_containment`` — generalizes codegraph's fixed
  ``goMethodContains`` synth step: an orphan ``method`` symbol (no
  ``contains`` parent from the parse) gains one from its qualified-name owner
  (``Greeter.greet`` → ``Greeter``, confidence 9/10) or, failing that, the
  nearest preceding class in the same file (confidence 7/10). Ambiguity
  (0 or 2+ owners, same-line classes, would-be cycle) skips the candidate.
* ``reexport_chains`` — import A from B; B re-exports from C → A references C.
  An ``imports`` edge landing on another ``import`` node *is* a re-export;
  the chain is walked (bounded depth, cycle-safe) to the terminal non-import
  symbol and minted as a ``references`` edge (confidence 4/5).
* ``type_hint_edges`` — parked ``type_hint`` refs (a ref kind the primary
  cascade refuses as ``unknown_ref_kind``) resolve to type-kind symbols
  (``class``/``interface``/``type``) and mint ``type_of`` edges
  (confidence 9/10).
* ``callback_edges`` — named callbacks passed by reference (resolved
  ``function_ref`` refs) are linked from the registrar's *callers* to the
  callback: the dynamic-dispatch channel codegraph's callback-synthesizer
  covers with field/event pairing. Our store has no field/channel data, so
  the registrar itself is the hub; channels are capped
  (``max_callbacks_per_channel`` / ``max_dispatchers_per_channel``, the
  ``MAX_CALLBACKS_PER_CHANNEL`` / ``EVENT_FANOUT_CAP`` pattern) and the whole
  channel is skipped — never truncated — when a cap trips (confidence 2/5).

Invariants (all pinned by tests):

* provenance is always ``synthesis:<pass>`` — never the parse stamp
  (``ingest:*``) and never a resolver stamp, so synthesized edges can never
  masquerade as parsed or primarily-resolved ones;
* synthesis never creates a second edge where any same-(source,target,kind)
  edge exists, whatever its provenance (parse edges are untouchable);
* every pass is idempotent (a second run inserts zero) and deterministic
  (same store in → same rows out), and each pass can be disabled via
  :class:`SynthesisConfig`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from know_your_soul.domain import canonical_json
from know_your_soul.resolve.name_matcher import NameMatchConfig, NameMatchReport, NameMatchResolver
from know_your_soul.resolve.resolver import BatchReport, ReferenceResolver, ResolverConfig
from know_your_soul.store import EventStore, UnresolvedRefRow

__all__ = [
    "PASS_CALLBACK_EDGES",
    "PASS_METHOD_CLASS_CONTAINMENT",
    "PASS_REEXPORT_CHAINS",
    "PASS_TYPE_HINT_EDGES",
    "SYNTH_PASS_ORDER",
    "SYNTHESIS_VERSION",
    "PassReport",
    "PipelineReport",
    "SynthesisConfig",
    "SynthesisError",
    "SynthesisReport",
    "Synthesizer",
    "pass_provenance",
    "resolve_and_synthesize",
    "run_synthesis",
]

SYNTHESIS_VERSION: int = 1

PASS_METHOD_CLASS_CONTAINMENT: str = "method_class_containment"
PASS_REEXPORT_CHAINS: str = "reexport_chains"
PASS_TYPE_HINT_EDGES: str = "type_hint_edges"
PASS_CALLBACK_EDGES: str = "callback_edges"

#: Fixed pass order = merge order (codegraph's SYNTH_PASSES rule). Containment
#: first: later passes may read hierarchy. Callback last: it consumes edges
#: the resolver tier minted.
SYNTH_PASS_ORDER: tuple[str, ...] = (
    PASS_METHOD_CLASS_CONTAINMENT,
    PASS_REEXPORT_CHAINS,
    PASS_TYPE_HINT_EDGES,
    PASS_CALLBACK_EDGES,
)

#: Symbol kinds a ``type_hint`` ref may land on.
_TYPE_KINDS: frozenset[str] = frozenset({"class", "interface", "type"})

#: Symbol kinds that may act as callback registrars / dispatchers.
_CALLABLE_KINDS: frozenset[str] = frozenset({"function", "method"})


class SynthesisError(ValueError):
    """Invalid synthesis configuration (fail-closed)."""


def pass_provenance(pass_name: str) -> str:
    """The provenance stamp of one pass: ``synthesis:<name>``."""
    if pass_name not in SYNTH_PASS_ORDER:
        raise SynthesisError(f"unknown synthesis pass {pass_name!r}")
    return f"synthesis:{pass_name}"


def _check_cap(value: object, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SynthesisError(f"{label} must be an int >= 1, got {value!r}")


@dataclass(frozen=True)
class SynthesisConfig:
    """Tuning knobs; ``enabled_passes=None`` enables every pass."""

    enabled_passes: frozenset[str] | None = None
    max_callbacks_per_channel: int = 40
    max_dispatchers_per_channel: int = 6
    reexport_max_depth: int = 8
    ancestor_cap: int = 64
    skip_generated: bool = True

    def __post_init__(self) -> None:
        if self.enabled_passes is not None:
            if not isinstance(self.enabled_passes, frozenset):
                raise SynthesisError("enabled_passes must be a frozenset or None")
            unknown = self.enabled_passes - frozenset(SYNTH_PASS_ORDER)
            if unknown:
                raise SynthesisError(f"unknown synthesis passes: {sorted(unknown)}")
        for label, value in (
            ("max_callbacks_per_channel", self.max_callbacks_per_channel),
            ("max_dispatchers_per_channel", self.max_dispatchers_per_channel),
            ("reexport_max_depth", self.reexport_max_depth),
            ("ancestor_cap", self.ancestor_cap),
        ):
            _check_cap(value, label)
        if not isinstance(self.skip_generated, bool):
            raise SynthesisError("skip_generated must be a bool")

    def is_enabled(self, pass_name: str) -> bool:
        if pass_name not in SYNTH_PASS_ORDER:
            raise SynthesisError(f"unknown synthesis pass {pass_name!r}")
        return self.enabled_passes is None or pass_name in self.enabled_passes


@dataclass(frozen=True)
class PassReport:
    """Per-pass deterministic counts."""

    name: str
    candidates: int
    emitted: int
    inserted: int
    skipped_existing: int
    skipped_uncertain: int


@dataclass(frozen=True)
class SynthesisReport:
    """One full synthesis run; ``passes`` follows ``SYNTH_PASS_ORDER``."""

    passes: tuple[PassReport, ...]
    emitted: int
    inserted: int


@dataclass(frozen=True)
class PipelineReport:
    """The composed resolve → name-match → synthesis run (SUB-103 wiring)."""

    resolver: BatchReport
    name_match: NameMatchReport
    synthesis: SynthesisReport


def _ref_row(row: sqlite3.Row) -> UnresolvedRefRow:
    reason = row["failure_reason"]
    return UnresolvedRefRow(
        ref_id=int(row["ref_id"]),
        source_node_id=str(row["source_node_id"]),
        target_name=str(row["target_name"]),
        name_tail=str(row["name_tail"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        first_seen=str(row["first_seen"]),
        attempts=int(row["attempts"]),
        failure_reason=None if reason is None else str(reason),
    )


def _edge_exists(store: EventStore, source: str, target: str, kind: str) -> bool:
    """Identity-level existence, provenance-agnostic (the non-overwrite rule)."""
    with store.read() as conn:
        row = conn.execute(
            "SELECT 1 FROM graph_edges WHERE source = ? AND target = ? AND kind = ? LIMIT 1",
            (source, target, kind),
        ).fetchone()
    return row is not None


def _metadata(pass_name: str, confidence: str, **extra: object) -> str:
    return canonical_json({"synthesized_by": pass_name, "confidence": confidence, **extra})


class _Pass:
    """Accumulates one pass's deterministic counts."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.candidates = 0
        self.emitted = 0
        self.skipped_existing = 0
        self.skipped_uncertain = 0
        self.inserted = 0
        self.payloads: dict[tuple[str, str, str], dict[str, object]] = {}
        #: Optional bookkeeping run after the edge transaction (type_hint
        #: resolves its refs there, mirroring the resolver's write order).
        self.after_insert: Callable[[EventStore], None] | None = None

    def emit(self, store: EventStore, source: str, target: str, kind: str, metadata: str) -> None:
        key = (source, target, kind)
        if key in self.payloads:
            return
        if _edge_exists(store, source, target, kind):
            self.skipped_existing += 1
            return
        self.payloads[key] = {
            "source": source,
            "target": target,
            "kind": kind,
            "metadata": metadata,
            "provenance": pass_provenance(self.name),
        }
        self.emitted += 1

    def emit_existing(self) -> None:
        """Count a hit whose edge already exists (answer known, nothing minted)."""
        self.skipped_existing += 1

    def insert(self, store: EventStore) -> int:
        ordered = [self.payloads[key] for key in sorted(self.payloads)]
        self.inserted = store.add_graph_edges(ordered) if ordered else 0
        if self.after_insert is not None:
            self.after_insert(store)
        return self.inserted

    def report(self) -> PassReport:
        return PassReport(
            name=self.name,
            candidates=self.candidates,
            emitted=self.emitted,
            inserted=self.inserted,
            skipped_existing=self.skipped_existing,
            skipped_uncertain=self.skipped_uncertain,
        )


# ------------------------------------------------------- pass implementations


def _run_method_class_containment(store: EventStore, config: SynthesisConfig) -> _Pass:
    result = _Pass(PASS_METHOD_CLASS_CONTAINMENT)
    generated_filter = "AND f.generated_hint = 0" if config.skip_generated else ""
    with store.read() as conn:
        methods = conn.execute(
            "SELECT s.* FROM symbols s JOIN files f ON f.path = s.path "
            f"WHERE s.kind = 'method' {generated_filter} ORDER BY s.node_id"  # nosec B608 -- generated_filter is a fixed internal fragment
        ).fetchall()
    for row in methods:
        node_id = str(row["node_id"])
        name = str(row["name"])
        path = str(row["path"])
        line = int(row["line"])
        incoming = store.incoming_edges(node_id, kinds=("contains",))
        own = pass_provenance(PASS_METHOD_CLASS_CONTAINMENT)
        if any(edge.provenance != own for edge in incoming):
            continue  # a real (parse/resolver) parent exists — untouchable
        result.candidates += 1
        owner: str | None = None
        separator = "." if "." in name else ("::" if "::" in name else None)
        if separator is not None:
            prefix = name.rsplit(separator, 1)[0]
            classes = [
                symbol for symbol in store.symbols_in_path(path) if symbol.kind == "class" and symbol.name == prefix
            ]
            if len(classes) != 1:
                result.skipped_uncertain += 1
                continue
            owner = classes[0].node_id
            confidence = "9/10"
            rule = "qualified_prefix"
        else:
            classes = [
                symbol for symbol in store.symbols_in_path(path) if symbol.kind == "class" and symbol.line < line
            ]
            if not classes:
                continue
            best_line = max(symbol.line for symbol in classes)
            nearest = [symbol for symbol in classes if symbol.line == best_line]
            if len(nearest) != 1:
                result.skipped_uncertain += 1
                continue
            owner = nearest[0].node_id
            confidence = "7/10"
            rule = "nearest_preceding_class"
        if _is_descendant(store, node_id, owner, cap=config.ancestor_cap):
            result.skipped_uncertain += 1  # would close a contains cycle
            continue
        result.emit(
            store,
            owner,
            node_id,
            "contains",
            _metadata(PASS_METHOD_CLASS_CONTAINMENT, confidence, rule=rule),
        )
    return result


def _is_contained_below(store: EventStore, ancestor: str, node: str, *, cap: int) -> bool:
    """True when ``node`` sits in the ``contains`` subtree of ``ancestor`` (bounded BFS)."""
    if ancestor == node:
        return True
    frontier = [ancestor]
    seen = {ancestor}
    steps = 0
    while frontier and steps < cap:
        steps += 1
        current = frontier.pop(0)
        for edge in store.outgoing_edges(current, kinds=("contains",)):
            if edge.target == node:
                return True
            if edge.target not in seen:
                seen.add(edge.target)
                frontier.append(edge.target)
    return False


def _is_descendant(store: EventStore, ancestor: str, node: str, *, cap: int) -> bool:
    return _is_contained_below(store, ancestor, node, cap=cap)


def _run_reexport_chains(store: EventStore, config: SynthesisConfig) -> _Pass:
    result = _Pass(PASS_REEXPORT_CHAINS)
    with store.read() as conn:
        import_nodes = conn.execute("SELECT node_id FROM symbols WHERE kind = 'import' ORDER BY node_id").fetchall()
    for row in import_nodes:
        start = str(row["node_id"])
        result.candidates += 1
        outgoing = store.outgoing_edges(start, kinds=("imports",))
        if len(outgoing) != 1:
            continue
        first = outgoing[0].target
        first_symbol = store.symbol_by_node(first)
        if first_symbol is None or first_symbol.kind != "import":
            continue  # direct import, not a re-export
        # Walk the re-export chain to its terminal non-import symbol.
        visited = {start, first}
        via = [first]
        current = first
        terminal: str | None = None
        uncertain = False
        for _depth in range(config.reexport_max_depth):
            hops = store.outgoing_edges(current, kinds=("imports",))
            if len(hops) != 1:
                uncertain = len(hops) > 1  # dangling re-export: just skip
                break
            nxt = hops[0].target
            if nxt in visited:
                uncertain = True  # cycle
                break
            nxt_symbol = store.symbol_by_node(nxt)
            if nxt_symbol is not None and nxt_symbol.kind == "import":
                visited.add(nxt)
                via.append(nxt)
                current = nxt
                continue
            terminal = nxt
            break
        else:
            uncertain = True  # depth cap tripped
        if uncertain:
            result.skipped_uncertain += 1
            continue
        if terminal is None or terminal == start:
            continue
        result.emit(
            store,
            start,
            terminal,
            "references",
            _metadata(PASS_REEXPORT_CHAINS, "4/5", via=via),
        )
    return result


def _run_type_hint_edges(store: EventStore, config: SynthesisConfig) -> _Pass:
    result = _Pass(PASS_TYPE_HINT_EDGES)
    with store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM unresolved_refs WHERE kind = 'type_hint' AND "
            "(status = 'pending' OR (status = 'failed' AND failure_reason = 'unknown_ref_kind')) "
            "ORDER BY first_seen, ref_id"
        ).fetchall()
    refs = [_ref_row(row) for row in rows]
    result.candidates = len(refs)
    hits: list[tuple[UnresolvedRefRow, str]] = []
    for ref in refs:
        targets = [symbol for symbol in store.symbols_named(ref.target_name) if symbol.kind in _TYPE_KINDS]
        if len(targets) > 1:
            result.skipped_uncertain += 1  # ambiguity never guesses
            continue
        if not targets:
            continue  # unknown type: leave parked for a later ingestion wave
        hits.append((ref, targets[0].node_id))
    for ref, target in hits:
        if not _edge_exists(store, ref.source_node_id, target, "type_of"):
            result.emit(
                store,
                ref.source_node_id,
                target,
                "type_of",
                _metadata(PASS_TYPE_HINT_EDGES, "9/10", ref_id=ref.ref_id),
            )
        else:
            result.emit_existing()

    def _resolve_hits(store: EventStore) -> None:
        # Resolve the refs whose answer is now persisted (edge minted or
        # already present): batch edges first, then one recorded attempt per
        # ref — the resolver's crash-safe write discipline.
        for ref, _target in hits:
            current = store.unresolved_ref(ref.ref_id)
            if current is None or current.status == "resolved":
                continue
            if current.status == "failed":
                store.retry_failed_ref(ref.ref_id)
            store.record_resolution_attempt(ref.ref_id, outcome="resolved")

    result.after_insert = _resolve_hits
    return result


def _run_callback_edges(store: EventStore, config: SynthesisConfig) -> _Pass:
    result = _Pass(PASS_CALLBACK_EDGES)
    with store.read() as conn:
        rows = conn.execute(
            "SELECT * FROM unresolved_refs WHERE kind = 'function_ref' AND status = 'resolved' ORDER BY ref_id"
        ).fetchall()
    refs = [_ref_row(row) for row in rows]
    # registrar node -> ordered callback targets (via the resolver-minted edge
    # whose metadata carries this ref's identity).
    channels: dict[str, list[str]] = {}
    for ref in refs:
        registrar = ref.source_node_id
        callback: str | None = None
        for edge in store.outgoing_edges(registrar, kinds=("calls",)):
            if edge.metadata is None:
                continue
            try:
                payload = json.loads(edge.metadata)
            except ValueError, TypeError:
                continue
            if payload.get("ref_id") == ref.ref_id:
                callback = edge.target
                break
        if callback is None:
            continue
        channels.setdefault(registrar, [])
        if callback not in channels[registrar]:
            channels[registrar].append(callback)
    for registrar in sorted(channels):
        result.candidates += 1
        registrar_symbol = store.symbol_by_node(registrar)
        if registrar_symbol is None or registrar_symbol.kind not in _CALLABLE_KINDS:
            continue
        callbacks = channels[registrar]
        if len(callbacks) > config.max_callbacks_per_channel:
            result.skipped_uncertain += 1  # whole channel skipped, never truncated
            continue
        dispatchers: set[str] = set()
        for edge in store.incoming_edges(registrar, kinds=("calls",)):
            caller = edge.source
            if caller == registrar:
                continue
            caller_symbol = store.symbol_by_node(caller)
            if caller_symbol is None or caller_symbol.kind not in _CALLABLE_KINDS:
                continue  # file-level call sites are not dispatchers
            dispatchers.add(caller)
        if len(dispatchers) > config.max_dispatchers_per_channel:
            result.skipped_uncertain += 1
            continue
        for caller in sorted(dispatchers):
            for callback in callbacks:
                if caller == callback:
                    continue
                result.emit(
                    store,
                    caller,
                    callback,
                    "calls",
                    _metadata(
                        PASS_CALLBACK_EDGES,
                        "2/5",
                        channel="registered_callback",
                        registrar=registrar,
                    ),
                )
    return result


_PASS_RUNNERS = {
    PASS_METHOD_CLASS_CONTAINMENT: _run_method_class_containment,
    PASS_REEXPORT_CHAINS: _run_reexport_chains,
    PASS_TYPE_HINT_EDGES: _run_type_hint_edges,
    PASS_CALLBACK_EDGES: _run_callback_edges,
}


class Synthesizer:
    """Runs the enabled synthesis passes, in the fixed order, once."""

    def __init__(self, config: SynthesisConfig | None = None) -> None:
        self._config = config if config is not None else SynthesisConfig()

    @property
    def config(self) -> SynthesisConfig:
        return self._config

    def run(self, store: EventStore) -> SynthesisReport:
        reports: list[PassReport] = []
        for name in SYNTH_PASS_ORDER:
            if not self._config.is_enabled(name):
                reports.append(PassReport(name, 0, 0, 0, 0, 0))
                continue
            result = _PASS_RUNNERS[name](store, self._config)
            result.insert(store)  # one transaction per pass + post-insert hook
            reports.append(result.report())
        return SynthesisReport(
            passes=tuple(reports),
            emitted=sum(entry.emitted for entry in reports),
            inserted=sum(entry.inserted for entry in reports),
        )


def run_synthesis(store: EventStore, *, config: SynthesisConfig | None = None) -> SynthesisReport:
    """One-shot entry point: all enabled passes, fixed order."""
    return Synthesizer(config).run(store)


def resolve_and_synthesize(
    store: EventStore,
    *,
    limit: int | None = None,
    resolver_config: ResolverConfig | None = None,
    name_match_config: NameMatchConfig | None = None,
    synthesis_config: SynthesisConfig | None = None,
) -> PipelineReport:
    """The wired pipeline: primary cascade → name-match tier → synthesis.

    Synthesis always runs *after* primary resolution (and the name-match
    tier), as a separate pass writing separately-stamped edges — never
    interleaved with, and never disguised as, parse output.
    """
    resolver_report = ReferenceResolver(resolver_config).resolve_pending(store, limit=limit)
    name_match_report = NameMatchResolver(name_match_config).resolve_unmatched(store)
    synthesis_report = Synthesizer(synthesis_config).run(store)
    return PipelineReport(
        resolver=resolver_report,
        name_match=name_match_report,
        synthesis=synthesis_report,
    )
