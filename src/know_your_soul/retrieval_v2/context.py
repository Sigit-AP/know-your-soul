"""Context assembly for retrieval v2 (wave W4).

Token budgeting bertingkat (RESEARCH-QUERY §5, codegraph
``allocateExploreBudget``): every candidate gets a proportional share of
the token budget derived from its score; a candidate whose share cannot
afford its full form is **skeletonized** (signature + provenance only,
marked ``· skeleton (signature only)``); a final deterministic trim pass
guarantees the hard invariant ``total_tokens <= budget_tokens``.

Every emitted entry always carries provenance: ``path:line`` and the
stable ``node_id``. Ordering is canonical (``-score, node_id``), so output
is invariant under input permutation and duplicate node ids are deduped
keeping the highest score.
"""

from __future__ import annotations

from dataclasses import dataclass

from know_your_soul.retrieval_v2.hierarchy import DEFAULT_EDGE_KINDS
from know_your_soul.store import EventStore

__all__ = [
    "MIN_BUDGET_TOKENS",
    "SKELETON_MARKER",
    "AssembledContext",
    "ContextConfig",
    "ContextEntry",
    "ContextError",
    "ContextItem",
    "assemble_context",
]

SKELETON_MARKER = "· skeleton (signature only)"

#: Budgets below the fixed frame cost cannot honour the invariant — rejected.
MIN_BUDGET_TOKENS = 16


class ContextError(ValueError):
    """Invalid context configuration or item (fail-closed)."""


@dataclass(frozen=True)
class ContextConfig:
    """Budget and shaping knobs for :func:`assemble_context`."""

    budget_tokens: int
    chars_per_token: int = 4
    max_share_fraction: float = 0.5

    def __post_init__(self) -> None:
        if (
            isinstance(self.budget_tokens, bool)
            or not isinstance(self.budget_tokens, int)
            or self.budget_tokens < MIN_BUDGET_TOKENS
        ):
            raise ContextError(
                f"budget_tokens must be an int >= {MIN_BUDGET_TOKENS} (frame cost), got {self.budget_tokens!r}"
            )
        if (
            isinstance(self.chars_per_token, bool)
            or not isinstance(self.chars_per_token, int)
            or self.chars_per_token < 1
        ):
            raise ContextError(f"chars_per_token must be an int >= 1, got {self.chars_per_token!r}")
        if not (0.0 < float(self.max_share_fraction) <= 1.0):
            raise ContextError(f"max_share_fraction must be in (0, 1], got {self.max_share_fraction!r}")


@dataclass(frozen=True)
class ContextItem:
    """One scored candidate for assembly (e.g. a :class:`ScoredSymbol`)."""

    node_id: str
    name: str
    kind: str
    path: str
    line: int
    col: int
    score: float

    def __post_init__(self) -> None:
        if not isinstance(self.node_id, str) or not self.node_id:
            raise ContextError(f"node_id must be a non-empty string, got {self.node_id!r}")
        if isinstance(self.score, bool) or not isinstance(self.score, (int, float)):
            raise ContextError(f"score must be a number, got {self.score!r}")
        object.__setattr__(self, "score", float(self.score))


@dataclass(frozen=True)
class ContextEntry:
    """One emitted entry: rendered text, measured tokens, provenance."""

    node_id: str
    path: str
    line: int
    text: str
    tokens: int
    skeleton: bool
    score: float


@dataclass(frozen=True)
class AssembledContext:
    """The assembled context; ``render()`` output never exceeds the budget."""

    entries: tuple[ContextEntry, ...]
    omitted: tuple[str, ...]
    budget_tokens: int
    total_tokens: int

    def render(self) -> str:
        lines = [f"# Retrieval context (budget {self.budget_tokens} tokens)"]
        lines.extend(entry.text for entry in self.entries)
        if self.omitted:
            lines.append(f"> omitted: {len(self.omitted)}")
        return "\n".join(lines)


def _tokens(text: str, chars_per_token: int) -> int:
    if not text:
        return 0
    return (len(text) + chars_per_token - 1) // chars_per_token


def _skeleton_text(item: ContextItem) -> str:
    return f"### {item.name} ({item.kind}) — {item.path}:{item.line} {SKELETON_MARKER} (id {item.node_id})"


def _full_text(store: EventStore, item: ContextItem) -> str:
    parts: list[str] = []
    for kind in sorted(DEFAULT_EDGE_KINDS):
        outgoing = len(store.outgoing_edges(item.node_id, kinds=(kind,)))
        incoming = len(store.incoming_edges(item.node_id, kinds=(kind,)))
        if outgoing or incoming:
            parts.append(f"{kind} {outgoing} out / {incoming} in")
    summary = ", ".join(parts) if parts else "none"
    return "\n".join(
        [
            f"### {item.name} ({item.kind}) — {item.path}:{item.line}",
            f"id: {item.node_id}",
            f"score: {item.score:.6f}",
            f"edges: {summary}",
        ]
    )


def assemble_context(
    store: EventStore, items: list[ContextItem] | tuple[ContextItem, ...], config: ContextConfig
) -> AssembledContext:
    """Assemble ``items`` into a budget-respecting, deterministic context.

    Pipeline: dedupe by node id (highest score wins) → canonical order →
    proportional shares (score-weighted, capped by ``max_share_fraction``)
    → skeletonize what its share cannot afford → trim pass that
    downgrades/pops from the tail until the rendered text fits the budget.
    """
    unique: dict[str, ContextItem] = {}
    for item in items:
        existing = unique.get(item.node_id)
        if existing is None or item.score > existing.score:
            unique[item.node_id] = item
    ordered = sorted(unique.values(), key=lambda item: (-item.score, item.node_id))

    weights = [max(item.score, 0.0) for item in ordered]
    total_weight = sum(weights)
    if total_weight <= 0.0:
        weights = [1.0] * len(ordered)
        total_weight = float(len(ordered))
    share_cap = config.budget_tokens * config.max_share_fraction

    skeletons: dict[str, str] = {item.node_id: _skeleton_text(item) for item in ordered}
    fulls: dict[str, str] = {item.node_id: _full_text(store, item) for item in ordered}

    use_full: dict[str, bool] = {}
    for item, weight in zip(ordered, weights, strict=True):
        share = min(config.budget_tokens * weight / total_weight, share_cap)
        use_full[item.node_id] = _tokens(fulls[item.node_id], config.chars_per_token) <= share

    kept: list[ContextItem] = list(ordered)
    omitted: list[str] = []

    def build() -> AssembledContext:
        entries = []
        for current in kept:
            text = fulls[current.node_id] if use_full[current.node_id] else skeletons[current.node_id]
            entries.append(
                ContextEntry(
                    node_id=current.node_id,
                    path=current.path,
                    line=current.line,
                    text=text,
                    tokens=_tokens(text, config.chars_per_token),
                    skeleton=not use_full[current.node_id],
                    score=current.score,
                )
            )
        rendered = AssembledContext(
            entries=tuple(entries),
            omitted=tuple(sorted(omitted)),
            budget_tokens=config.budget_tokens,
            total_tokens=0,
        )
        return AssembledContext(
            entries=rendered.entries,
            omitted=rendered.omitted,
            budget_tokens=rendered.budget_tokens,
            total_tokens=_tokens(rendered.render(), config.chars_per_token),
        )

    # Deterministic trim pass: downgrade full entries (tail first), then
    # drop tail entries to the omitted list, until the budget holds.
    while True:
        assembled = build()
        if assembled.total_tokens <= config.budget_tokens:
            return assembled
        downgraded = False
        for current in reversed(kept):
            if use_full[current.node_id]:
                use_full[current.node_id] = False
                downgraded = True
                break
        if not downgraded:
            if not kept:
                return assembled  # frame alone exceeds budget; cannot happen (config floor)
            omitted.append(kept.pop().node_id)
