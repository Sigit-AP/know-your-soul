"""Deterministic primary-root routing, lexical ranking and bounded traversal.

Task 4 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 8 and 13.1, matrix adapter A5 (row 37).

Design rules:

* A supplied primary domain routes directly to its stable root. There is no
  global keyword fallback when the root is missing (spec 13.1).
* Ambiguity and no-match are typed outcomes; a tie is never resolved to the
  first graph-order node.
* Ranking uses stable node IDs, aliases, summaries and context tags. Source
  paths and provenance contribute zero relevance. IDF is computed over every
  searchable knowledge field.
* Traversal reads the ``graph.json`` *links list* — never NetworkX edges — so
  parallel typed relations on one node pair survive (the simple-graph load
  collapse reported by ``ValidationReport.collapsed_links``). Traversal has
  an explicit relation allowlist plus maximum nodes, edges and depth; a
  render budget alone never bounds it.
"""

from __future__ import annotations

import re
import unicodedata
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any, Literal

from soul_knowledge.contracts import AmbiguousDomain, UnknownDomain
from soul_knowledge.domain import PrimaryDomain

__all__ = [
    "DOMAIN_ALIASES",
    "FIELD_WEIGHTS",
    "SEARCHABLE_FIELDS",
    "STOPWORDS",
    "AffectedHit",
    "AffectedReport",
    "BudgetedTraversal",
    "GraphLink",
    "LexicalIndex",
    "NeighborhoodExplanation",
    "PathOutcome",
    "PrimaryRouter",
    "RankedHit",
    "RelationalGraph",
    "RouteOutcome",
    "TraversalReport",
    "estimated_tokens",
    "normalize_term",
    "query_terms",
    "resolve_domain_input",
    "sanitize_text",
    "tokenize",
]

# Bilingual (Indonesian/English) aliases for the seven stable primary domains.
DOMAIN_ALIASES: dict[PrimaryDomain, tuple[str, ...]] = {
    PrimaryDomain.P1: ("pembelajaran", "pelajaran", "learning", "lessons"),
    PrimaryDomain.P2: ("gagasan", "idea", "ideas"),
    PrimaryDomain.P3: ("tindakan", "action", "actions"),
    PrimaryDomain.P4: ("keputusan", "decision", "decisions"),
    PrimaryDomain.P5: ("pemikiran", "penalaran", "thinking", "reasoning"),
    PrimaryDomain.P6: ("disiplin", "discipline"),
    PrimaryDomain.P7: ("level profesional", "profesional", "professional level", "professionalism"),
}

# Ranking searches exactly these fields (spec 13.1); provenance and source
# paths are never indexed, so they contribute zero relevance.
SEARCHABLE_FIELDS = ("node_id", "aliases", "summary", "context_tags")
FIELD_WEIGHTS: dict[str, Decimal] = {
    "node_id": Decimal(4),
    "aliases": Decimal(3),
    "context_tags": Decimal(2),
    "summary": Decimal(1),
}
# Field weight enters the score through a sub-linear multiplier, never a hard
# one: FIELD_BOOST_STEP keeps the strongest field at 1.15x so term coverage
# still dominates (spec 13.1 field-complete IDF).
FIELD_BOOST_STEP = Decimal("0.05")

_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)
_MIN_PREFIX_LENGTH = 3
# How many of a candidate root's strongest hits count toward its routing score.
_ROOT_HIT_DEPTH = 3

# Query hygiene for natural-language questions (bench finding 2026-10-25).
# Closed, deterministic lists only: no model, no corpus statistics at import.
STOPWORDS: frozenset[str] = frozenset(
    {
        # English function words
        "a",
        "about",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "done",
        "down",
        "during",
        "each",
        "either",
        "else",
        "ever",
        "every",
        "few",
        "for",
        "from",
        "further",
        "give",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "let",
        "like",
        "list",
        "many",
        "may",
        "me",
        "might",
        "mine",
        "more",
        "most",
        "much",
        "must",
        "my",
        "never",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "ought",
        "our",
        "ours",
        "out",
        "over",
        "own",
        "per",
        "put",
        "same",
        "shall",
        "she",
        "should",
        "show",
        "so",
        "some",
        "such",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "upon",
        "us",
        "use",
        "used",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "whether",
        "which",
        "while",
        "who",
        "whom",
        "whose",
        "why",
        "will",
        "with",
        "within",
        "without",
        "would",
        "you",
        "your",
        "yours",
        "s",
        "t",
        # Indonesian function words (the corpus and prompts are bilingual)
        "ada",
        "adalah",
        "agar",
        "akan",
        "atau",
        "bagaimana",
        "bahwa",
        "belum",
        "bisa",
        "dalam",
        "dan",
        "dari",
        "dengan",
        "di",
        "ialah",
        "ini",
        "itu",
        "jika",
        "juga",
        "kalau",
        "kamu",
        "karena",
        "ke",
        "kita",
        "maka",
        "oleh",
        "pada",
        "secara",
        "sebagai",
        "sehingga",
        "saja",
        "saya",
        "sudah",
        "supaya",
        "tentang",
        "tidak",
        "untuk",
        "yang",
    }
)

# Words that end in "s" but are not plurals; stripping would corrupt them.
_NO_STRIP: frozenset[str] = frozenset(
    {
        "access",
        "address",
        "analysis",
        "basis",
        "bias",
        "business",
        "chaos",
        "class",
        "corpus",
        "focus",
        "gas",
        "lens",
        "less",
        "means",
        "plus",
        "process",
        "progress",
        "series",
        "status",
        "stress",
        "unless",
        "various",
        "versus",
    }
)

# Latin/Greek plurals whose singular is not a suffix strip.
_IRREGULAR: dict[str, str] = {
    "analyses": "analysis",
    "biases": "bias",
    "criteria": "criterion",
    "indices": "index",
    "matrices": "matrix",
}


def normalize_term(term: str) -> str:
    """A conservative, deterministic singular form (plural/possessive only).

    Only regular English plurals are folded, and only when the result stays a
    word of three or more characters; ambiguous "-s" endings are listed in
    :data:`_NO_STRIP` and left untouched. Applied symmetrically to indexed
    fields and to query terms, so the mapping is consistent by construction.
    """
    if len(term) <= 3 or term in _NO_STRIP:
        return term
    irregular = _IRREGULAR.get(term)
    if irregular is not None:
        return irregular
    if term.endswith("ies") and len(term) > 4:
        return term[:-3] + "y"
    if term.endswith("sses"):
        return term[:-2]
    if term.endswith("s") and not term.endswith(("ss", "us", "is", "as", "os")):
        return term[:-1]
    return term


def query_terms(text: str) -> list[str]:
    """The query side of the index: tokens minus stopwords, then normalized."""
    cleaned: list[str] = []
    for token in tokenize(text):
        if token in STOPWORDS:
            continue
        normalized = normalize_term(token)
        if normalized and normalized not in STOPWORDS:
            cleaned.append(normalized)
    return list(dict.fromkeys(cleaned))


def tokenize(text: str) -> list[str]:
    """Casefolded unicode word tokens; control characters never survive."""
    return _TOKEN_RE.findall(sanitize_text(text).casefold())


def sanitize_text(value: str, *, max_length: int = 512) -> str:
    """Strip control, bidi and zero-width characters and cap length (spec 13.1)."""
    cleaned = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"})
    return cleaned[:max_length]


def estimated_tokens(payload: Any) -> int:
    """estimated_tokens = ceil(utf8_byte_length / 4) of the canonical payload."""
    from soul_knowledge.domain import canonical_json

    return -(-len(canonical_json(payload).encode("utf-8")) // 4)


# ------------------------------------------------------------- domain routing


def _domain_keys() -> dict[str, PrimaryDomain]:
    keys: dict[str, PrimaryDomain] = {}
    for domain in PrimaryDomain:
        candidates = {domain.value, domain.title, *DOMAIN_ALIASES[domain]}
        candidates |= {part for piece in candidates for part in piece.split("/")}
        for candidate in candidates:
            keys.setdefault(candidate.casefold(), domain)
    return keys


_DOMAIN_KEYS = _domain_keys()


def resolve_domain_input(text: str) -> PrimaryDomain:
    """Resolve a caller-supplied domain string to exactly one primary domain.

    Exact casefolded match first (code, title, bilingual alias); otherwise a
    prefix match with at least three characters. Zero matches is
    :class:`UnknownDomain`; more than one is :class:`AmbiguousDomain` — a
    primary root is never guessed.
    """
    normalized = sanitize_text(text).casefold().strip()
    if not normalized:
        raise UnknownDomain("an empty domain string does not identify a primary domain")
    exact = _DOMAIN_KEYS.get(normalized)
    if exact is not None:
        return exact
    if len(normalized) >= _MIN_PREFIX_LENGTH:
        matches = {domain for key, domain in _DOMAIN_KEYS.items() if key.startswith(normalized)}
        if not matches:
            matches = {
                domain
                for key, domain in _DOMAIN_KEYS.items()
                if any(token.startswith(normalized) for token in tokenize(key))
            }
        if len(matches) > 1:
            raise AmbiguousDomain(
                f"domain {text!r} matches multiple primary domains",
                candidates=tuple(sorted(matches, key=lambda domain: domain.value)),
            )
        if matches:
            return next(iter(matches))
    raise UnknownDomain(f"domain {text!r} does not identify a primary domain")


# ------------------------------------------------------------ lexical ranking


@dataclass(frozen=True)
class RankedHit:
    """One positive lexical hit plus the field rank that breaks coverage ties."""

    node_id: str
    score: Decimal
    field_rank: Decimal = Decimal(0)


class LexicalIndex:
    """A deterministic field-complete TF-IDF-style index over graph nodes.

    Document frequency counts a term once per node when it appears in *any*
    searchable field (node id, aliases, summary, context tags), so the IDF is
    field-complete. Ranking is a pure function of the indexed content: ties
    break on the ascending node id, never on graph order.
    """

    def __init__(self, entries: Iterable[tuple[str, Mapping[str, frozenset[str]]]]) -> None:
        prepared: list[tuple[str, dict[str, frozenset[str]]]] = []
        frequency: dict[str, int] = {}
        for node_id, fields in entries:
            cleaned = {
                field: frozenset(normalize_term(term) for term in terms)
                for field, terms in fields.items()
                if field in SEARCHABLE_FIELDS
            }
            prepared.append((node_id, cleaned))
            present: set[str] = set()
            for terms in cleaned.values():
                present |= set(terms)
            for term in present:
                frequency[term] = frequency.get(term, 0) + 1
        prepared.sort(key=lambda item: item[0])
        self._entries = tuple(prepared)
        self._frequency = frequency
        self._size = len(prepared)

    @classmethod
    def from_entries(cls, entries: Iterable[Mapping[str, Any]]) -> LexicalIndex:
        """Build from explicit ``{"node_id", "fields": {field: terms}}`` maps."""
        return cls(
            (
                str(entry["node_id"]),
                {field: frozenset(terms) for field, terms in dict(entry.get("fields", {})).items()},
            )
            for entry in entries
        )

    @classmethod
    def from_graph_nodes(cls, nodes: Iterable[Mapping[str, Any]]) -> LexicalIndex:
        """Build from projection node dicts.

        Only node id, (primary-domain) aliases, label/summary and context tags
        are indexed. ``provenance`` and ``source_file`` are deliberately never
        read: source paths and provenance contribute zero relevance (spec
        13.1).
        """
        entries: list[tuple[str, dict[str, frozenset[str]]]] = []
        for node in nodes:
            node_id = str(node.get("id"))
            aliases: set[str] = set()
            domain = node.get("domain")
            if node.get("kind") == "primary" and domain in {member.value for member in PrimaryDomain}:
                member = PrimaryDomain(str(domain))
                aliases = {alias.casefold() for alias in DOMAIN_ALIASES[member]} | {
                    part.casefold() for alias in DOMAIN_ALIASES[member] for part in tokenize(alias)
                }
            label = str(node.get("label") or node_id)
            tags = [str(tag) for tag in node.get("context_tags", []) if isinstance(tag, str)]
            fields = {
                # ``tokenize`` keeps underscores inside \w+, so a raw slug like
                # "07_calibration_bayes" would index as a single token and the
                # node_id field — the contract's strongest field — would never
                # fire for any real corpus id. Stable ids are slugs; their
                # underscore-separated segments are the indexable terms.
                "node_id": frozenset(tokenize(node_id.replace("_", " "))),
                "aliases": frozenset(aliases),
                "summary": frozenset(tokenize(label)),
                "context_tags": frozenset(term for tag in tags for term in tokenize(tag)),
            }
            entries.append((node_id, fields))
        return cls(entries)

    @property
    def size(self) -> int:
        return self._size

    def document_frequency(self, term: str) -> int:
        return self._frequency.get(term, 0)

    def idf(self, term: str) -> Decimal:
        """``ln((N+1)/(df+1)) + 1`` in Decimal precision 34 — no binary floats."""
        with localcontext() as context:
            context.prec = 34
            ratio = Decimal(self._size + 1) / Decimal(self.document_frequency(term) + 1)
            return ratio.ln() + 1

    def score(self, fields: Mapping[str, frozenset[str]], query_terms: Iterable[str]) -> Decimal:
        """Multiplicative relevance: ``sum(idf(term) * soft_boost(field))``.

        Each matched query term contributes its IDF multiplied by the *soft*
        boost of the strongest field it appears in. Coverage stays dominant —
        a node matching two terms beats a node matching one term — while field
        signal still counts, because the boost is deliberately sub-linear
        (``1 + (weight - 1) * FIELD_BOOST_STEP``, i.e. 1.15 at most). A hard
        multiplier let one common term in an alias outrank a two-term summary
        match, which the field-complete IDF contract forbids.
        """
        with localcontext() as context:
            context.prec = 34
            total = Decimal(0)
            for term in dict.fromkeys(normalize_term(item) for item in query_terms):
                if self.document_frequency(term) == 0:
                    continue
                boost = self._term_boost(fields, term)
                if boost == 0:
                    continue
                total += self.idf(term) * boost
            return total

    @staticmethod
    def _term_boost(fields: Mapping[str, frozenset[str]], term: str) -> Decimal:
        """The soft boost of the strongest searchable field holding *term* (0 = absent)."""
        best = Decimal(0)
        for field in SEARCHABLE_FIELDS:
            if term in fields.get(field, ()):
                best = max(best, Decimal(1) + (FIELD_WEIGHTS[field] - 1) * FIELD_BOOST_STEP)
        return best

    def field_rank(self, fields: Mapping[str, frozenset[str]], query_terms: Iterable[str]) -> Decimal:
        """The strongest field a matched query term appears in (tie-breaker only)."""
        best = Decimal(0)
        for term in dict.fromkeys(normalize_term(item) for item in query_terms):
            if self.document_frequency(term) == 0:
                continue
            for field in SEARCHABLE_FIELDS:
                if term in fields.get(field, ()):
                    best = max(best, FIELD_WEIGHTS[field])
        return best

    def rank(self, query_terms: Iterable[str]) -> tuple[RankedHit, ...]:
        """Positive-score hits ordered by ``(-score, -field_rank, node_id)``."""
        terms = tuple(dict.fromkeys(normalize_term(item) for item in query_terms))
        hits = [
            RankedHit(
                node_id=node_id,
                score=self.score(fields, terms),
                field_rank=self.field_rank(fields, terms),
            )
            for node_id, fields in self._entries
        ]
        positive = [hit for hit in hits if hit.score > 0]
        positive.sort(key=lambda hit: (-hit.score, -hit.field_rank, hit.node_id))
        return tuple(positive)


# --------------------------------------------------------------- primary route


@dataclass(frozen=True)
class RouteOutcome:
    """The typed routing verdict; never a guessed root."""

    status: Literal["direct", "ranked", "ambiguous", "no_match", "missing_root"]
    domain: PrimaryDomain | None = None
    root_node_id: str | None = None
    candidates: tuple[str, ...] = ()


class PrimaryRouter:
    """Routes a query to one stable primary root over projection node dicts.

    Spec 8/13.1: a supplied domain routes directly to its stable root and a
    missing root is a typed failure with no keyword fallback. Without a domain,
    deterministic lexical ranking selects candidate *roots* — every ranked hit
    is attributed to its primary ancestor through the ``parent_of`` links, and
    the root that accumulates the strongest coverage wins. Ties stay typed
    ambiguity; the first graph-order node is never chosen.
    """

    def __init__(
        self,
        nodes: Iterable[Mapping[str, Any]],
        links: Iterable[GraphLink] = (),
    ) -> None:
        self._nodes = {str(node.get("id")): node for node in nodes}
        self._primary_ids = sorted(node_id for node_id, node in self._nodes.items() if node.get("kind") == "primary")
        self._primary_id_set = frozenset(self._primary_ids)
        # Deterministic single parent per child: the lexicographically smallest
        # parent wins when the projection carries parallel parent links.
        parent: dict[str, str] = {}
        for link in links:
            if link.relation != "parent_of":
                continue
            current = parent.get(link.target)
            if current is None or link.source < current:
                parent[link.target] = link.source
        self._parent = parent
        self._index = LexicalIndex.from_graph_nodes(self._nodes.values())

    def _primary_root(self, node_id: str) -> str | None:
        """The nearest ``primary`` ancestor, resolved through ``parent_of``."""
        current = node_id
        seen: set[str] = set()
        while current is not None and current not in seen:
            seen.add(current)
            node = self._nodes.get(current)
            if node is None:
                return None
            if node.get("kind") == "primary":
                return current
            nxt = self._parent.get(current)
            if nxt is None:
                # A projected child always carries its primary domain; bind it
                # to the primary root whose stable ID is that domain.
                domain = node.get("domain")
                return str(domain) if isinstance(domain, str) and domain in self._primary_id_set else None
            current = nxt
        return None

    def route(
        self,
        *,
        domain: str | None,
        purpose: str,
        context_tags: Iterable[str],
    ) -> RouteOutcome:
        if domain is not None:
            resolved = resolve_domain_input(domain)
            root_id = resolved.value
            if root_id not in self._nodes:
                # No keyword fallback: lexical matches elsewhere are irrelevant.
                return RouteOutcome(status="missing_root", domain=resolved)
            return RouteOutcome(status="direct", domain=resolved, root_node_id=root_id)
        terms = query_terms(purpose)
        for tag in context_tags:
            terms.extend(query_terms(str(tag)))
        # Aggregate per root over its three strongest hits, discounted by
        # reciprocal rank: the strongest hit weighs 1, the second 1/2, the
        # third 1/3. A plain sum let a wide domain win on hit count alone; a
        # plain max ignored breadth; and an *undiscounted* top-three sum let a
        # root whose top hit was weaker win purely because a single-common-term
        # secondary hit padded its total (bench 2026-10-25, q16). The discount
        # keeps breadth meaningful only when the supporting hits are strong.
        per_root: dict[str, list[Decimal]] = {}
        for hit in self._index.rank(terms):
            root_id = self._primary_root(hit.node_id)
            if root_id is None:
                continue
            per_root.setdefault(root_id, []).append(hit.score)
        with localcontext() as context:
            context.prec = 34
            totals = {
                root_id: sum(
                    score / Decimal(rank)
                    for rank, score in enumerate(sorted(scores, reverse=True)[:_ROOT_HIT_DEPTH], start=1)
                )
                for root_id, scores in per_root.items()
            }
        if not totals:
            return RouteOutcome(status="no_match")
        best = max(totals.values())
        winning = sorted(root_id for root_id, total in totals.items() if total == best)
        if len(winning) > 1:
            return RouteOutcome(status="ambiguous", candidates=tuple(winning))
        root_id = winning[0]
        domain_value = self._nodes[root_id].get("domain")
        resolved = PrimaryDomain(str(domain_value)) if domain_value in {m.value for m in PrimaryDomain} else None
        return RouteOutcome(status="ranked", domain=resolved, root_node_id=root_id)


# ------------------------------------------------------------ bounded traversal


@dataclass(frozen=True)
class GraphLink:
    """One typed directed link from the ``graph.json`` links list."""

    source: str
    target: str
    relation: str


@dataclass(frozen=True)
class TraversalReport:
    nodes: tuple[str, ...]
    edges: tuple[GraphLink, ...]
    cross_edges: tuple[GraphLink, ...]
    guards_hit: tuple[str, ...]


class BudgetedTraversal:
    """Relevance- and dependency-bounded traversal over the links list.

    Explicit guards: relation allowlist, maximum nodes, maximum edges and
    maximum depth. Neighbour order is sorted by ``(relation, target)`` so BFS
    and DFS orders are deterministic. Nodes are deduplicated; allowlisted
    links whose endpoints are both visited but which were not traversed are
    reported as cross edges.
    """

    def __init__(self, links: Iterable[GraphLink]) -> None:
        adjacency: dict[str, list[GraphLink]] = {}
        all_links: list[GraphLink] = []
        for link in links:
            adjacency.setdefault(link.source, []).append(link)
            all_links.append(link)
        for neighbours in adjacency.values():
            neighbours.sort(key=lambda link: (link.relation, link.target))
        self._adjacency = adjacency
        all_links.sort(key=lambda link: (link.source, link.relation, link.target))
        self.links: tuple[GraphLink, ...] = tuple(all_links)

    @classmethod
    def from_graph_json(cls, graph: Mapping[str, Any]) -> BudgetedTraversal:
        """Read the ``links`` list verbatim — never a collapsed edge view."""
        links = []
        for link in graph.get("links", []):
            if isinstance(link, Mapping):
                links.append(
                    GraphLink(
                        source=str(link.get("source")),
                        target=str(link.get("target")),
                        relation=str(link.get("relation")),
                    )
                )
        return cls(links)

    def traverse(
        self,
        start: str,
        *,
        depth: int,
        mode: str,
        relation_allowlist: frozenset[str] | None,
        max_nodes: int,
        max_edges: int,
    ) -> TraversalReport:
        if not 1 <= depth <= 6:
            raise ValueError("depth must be within [1,6]")
        if mode not in {"bfs", "dfs"}:
            raise ValueError("mode must be 'bfs' or 'dfs'")

        def allowed(link: GraphLink) -> bool:
            return relation_allowlist is None or link.relation in relation_allowlist

        order: list[str] = []
        traversed: list[GraphLink] = []
        guards: set[str] = set()
        seen = {start}
        edge_count = 0
        frontier: deque[tuple[str, int]] = deque([(start, 0)])
        exhausted = False
        while frontier and not exhausted:
            if mode == "bfs":
                node, level = frontier.popleft()
            else:
                node, level = frontier.pop()
            order.append(node)
            if len(order) >= max_nodes:
                guards.add("max_nodes")
                break
            if level >= depth:
                if any(allowed(link) for link in self._adjacency.get(node, [])):
                    guards.add("depth")
                continue
            pending: list[tuple[str, int]] = []
            for link in self._adjacency.get(node, []):
                if not allowed(link):
                    continue
                if edge_count >= max_edges:
                    guards.add("max_edges")
                    exhausted = True
                    break
                edge_count += 1
                traversed.append(link)
                if link.target not in seen:
                    seen.add(link.target)
                    pending.append((link.target, level + 1))
            if mode == "bfs":
                frontier.extend(pending)
            else:
                # LIFO pop takes from the right; pushing the sorted neighbour
                # list in reverse yields deterministic pre-order DFS.
                for item in reversed(pending):
                    frontier.append(item)

        traversed_keys = {(link.source, link.target, link.relation) for link in traversed}
        visited = set(order)
        cross = sorted(
            (
                link
                for source, neighbours in self._adjacency.items()
                for link in neighbours
                if link.source in visited
                and link.target in visited
                and (link.source, link.target, link.relation) not in traversed_keys
            ),
            key=lambda link: (link.source, link.relation, link.target),
        )
        return TraversalReport(
            nodes=tuple(order),
            edges=tuple(traversed),
            cross_edges=tuple(cross),
            guards_hit=tuple(sorted(guards)),
        )


# --------------------------------------- relational verbs (contract v2, G1)
#
# Operator ruling 2026-10-26 (progress.md): the Graphify-parity verbs
# ``path`` / ``explain`` / ``affected`` / ``god_nodes`` join the surface as
# contract v2 (addendum ``docs/contracts/retrieval-verbs-v2.md``). They read
# the same ``graph.json`` links list as :class:`BudgetedTraversal` — never a
# collapsed simple-graph edge view — and every ordering is a pure function of
# the link rows: adjacency sorted by ``(relation, peer)``, rankings by
# ``(-degree, node_id)``. No model, no randomness, no wall-clock input.


@dataclass(frozen=True)
class PathOutcome:
    """The typed shortest-path verdict; ``no_path`` is a result, not an error.

    ``nodes`` lists the path endpoints in order (a self-path is the singleton
    ``(node,)`` with zero edges); ``edges`` carries the traversed typed links,
    so parallel relations on one node pair survive verbatim.
    """

    status: Literal["found", "no_path"]
    nodes: tuple[str, ...] = ()
    edges: tuple[GraphLink, ...] = ()


@dataclass(frozen=True)
class NeighborhoodExplanation:
    """One node's relational neighborhood: incident links plus per-relation counts."""

    outgoing: tuple[GraphLink, ...]
    incoming: tuple[GraphLink, ...]
    relation_counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class AffectedHit:
    """One reverse-dependency hit: ``node_id`` depends on the seed via ``via``."""

    node_id: str
    depth: int
    relation: str
    via: str


@dataclass(frozen=True)
class AffectedReport:
    hits: tuple[AffectedHit, ...]
    guards_hit: tuple[str, ...]


class RelationalGraph:
    """Directed relational views over the verbatim links list (contract v2).

    Outgoing adjacency is sorted by ``(relation, target)`` and incoming by
    ``(relation, source)``, so BFS discovery order, neighborhood listings and
    degree rankings are all deterministic functions of the graph artifact.
    """

    def __init__(self, links: Iterable[GraphLink]) -> None:
        outgoing: dict[str, list[GraphLink]] = {}
        incoming: dict[str, list[GraphLink]] = {}
        degree: dict[str, int] = {}
        for link in links:
            outgoing.setdefault(link.source, []).append(link)
            incoming.setdefault(link.target, []).append(link)
            # Measured degree: each link row counts, so parallel typed
            # relations on one node pair contribute separately (the links
            # list is the source of truth, never a collapsed edge view).
            degree[link.source] = degree.get(link.source, 0) + 1
            degree[link.target] = degree.get(link.target, 0) + 1
        for neighbours in outgoing.values():
            neighbours.sort(key=lambda link: (link.relation, link.target))
        for neighbours in incoming.values():
            neighbours.sort(key=lambda link: (link.relation, link.source))
        self._outgoing = outgoing
        self._incoming = incoming
        self._degree = degree

    def shortest_path(self, from_id: str, to_id: str) -> PathOutcome:
        """BFS shortest directed path; ties break on the sorted adjacency.

        First-discovery parents over the sorted adjacency make the
        reconstructed path a pure function of the links list. A self-path is
        the singleton node with zero edges; an unreachable (or unknown)
        target is the typed ``no_path`` outcome, never an exception.
        """
        if from_id == to_id:
            return PathOutcome(status="found", nodes=(from_id,), edges=())
        parent: dict[str, GraphLink] = {}
        seen = {from_id}
        frontier: deque[str] = deque([from_id])
        while frontier:
            current = frontier.popleft()
            for link in self._outgoing.get(current, []):
                if link.target in seen:
                    continue
                seen.add(link.target)
                parent[link.target] = link
                if link.target == to_id:
                    nodes = [to_id]
                    edges: list[GraphLink] = []
                    while nodes[-1] != from_id:
                        edge = parent[nodes[-1]]
                        edges.append(edge)
                        nodes.append(edge.source)
                    nodes.reverse()
                    edges.reverse()
                    return PathOutcome(status="found", nodes=tuple(nodes), edges=tuple(edges))
                frontier.append(link.target)
        return PathOutcome(status="no_path")

    def explain_neighborhood(self, node_id: str) -> NeighborhoodExplanation:
        """Incident links of one node, sorted, with per-relation counts.

        This is a *relational* explanation — which typed relations connect
        this node to which peers — not a document subtree (that is
        :meth:`BudgetedTraversal.traverse`'s job).
        """
        outgoing = tuple(self._outgoing.get(node_id, ()))
        incoming = tuple(self._incoming.get(node_id, ()))
        counts: dict[str, int] = {}
        for link in (*outgoing, *incoming):
            counts[link.relation] = counts.get(link.relation, 0) + 1
        return NeighborhoodExplanation(
            outgoing=outgoing,
            incoming=incoming,
            relation_counts=tuple(sorted(counts.items())),
        )

    def affected(self, node_id: str, *, depth: int, max_nodes: int) -> AffectedReport:
        """Reverse BFS: the nodes that depend on *node_id*, bounded by *depth*.

        In-links are walked transitively up to *depth* hops (``[1,6]``, the
        same bound as the traversal contract); the seed is never re-reported
        through a cycle, and breadth/depth caps surface as typed guard names
        in ``guards_hit`` rather than silently truncating.
        """
        if not 1 <= depth <= 6:
            raise ValueError("depth must be within [1,6]")
        if max_nodes < 1:
            raise ValueError("max_nodes must be >= 1")
        seen = {node_id}
        hits: list[AffectedHit] = []
        guards: set[str] = set()
        frontier: deque[tuple[str, int]] = deque([(node_id, 0)])
        exhausted = False
        while frontier and not exhausted:
            current, level = frontier.popleft()
            if level >= depth:
                if any(link.source not in seen for link in self._incoming.get(current, [])):
                    guards.add("depth")
                continue
            for link in self._incoming.get(current, []):
                if len(hits) >= max_nodes:
                    guards.add("max_nodes")
                    exhausted = True
                    break
                if link.source in seen:
                    continue
                seen.add(link.source)
                hits.append(AffectedHit(node_id=link.source, depth=level + 1, relation=link.relation, via=current))
                frontier.append((link.source, level + 1))
        return AffectedReport(hits=tuple(hits), guards_hit=tuple(sorted(guards)))

    def god_nodes(self, *, limit: int) -> tuple[tuple[str, int], ...]:
        """The top hub nodes by measured degree: ``(-degree, node_id)`` order.

        Degree counts every link row touching the node (in and out), so the
        ranking is a deterministic measurement of the published artifact —
        never a centrality approximation and never a model call.
        """
        if limit < 1:
            raise ValueError("limit must be >= 1")
        ranked = sorted(self._degree.items(), key=lambda item: (-item[1], item[0]))
        return tuple(ranked[:limit])
