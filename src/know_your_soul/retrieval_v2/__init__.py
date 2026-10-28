"""Retrieval v2 for the universal knowledge graph (wave W4).

Symbol lookup (FTS5 BM25 column-weighted → LIKE → bounded fuzzy),
depth-bounded call hierarchy (callers/callees over calls/imports/
references), and budgeted context assembly with proportional score
allocation + skeletonization. Fully deterministic; no model, provider or
vector calls (MASTER-PLAN boundary).

Modules:

* :mod:`know_your_soul.retrieval_v2.query` — ``kind:/lang:/path:/name:``
  parser (codegraph ``parseQuery``).
* :mod:`know_your_soul.retrieval_v2.scoring` — pure scoring helpers
  (codegraph ``query-utils.ts`` constants).
* :mod:`know_your_soul.retrieval_v2.fts` — module-owned FTS5
  external-content index over the frozen v3 ``symbols`` table.
* :mod:`know_your_soul.retrieval_v2.lookup` — tiered symbol search.
* :mod:`know_your_soul.retrieval_v2.hierarchy` — callers/callees.
* :mod:`know_your_soul.retrieval_v2.context` — token-budgeted assembly.
"""

from know_your_soul.retrieval_v2.context import (
    MIN_BUDGET_TOKENS,
    SKELETON_MARKER,
    AssembledContext,
    ContextConfig,
    ContextEntry,
    ContextError,
    ContextItem,
    assemble_context,
)
from know_your_soul.retrieval_v2.fts import (
    FTS_TABLE,
    META_TABLE,
    FtsUnavailableError,
    build_fts_query,
    drop_fts_index,
    ensure_fts_index,
    fts_available,
    rebuild_fts_index,
)
from know_your_soul.retrieval_v2.hierarchy import (
    DEFAULT_EDGE_KINDS,
    CallHierarchy,
    HierarchyEntry,
    HierarchyError,
    HierarchyReport,
    HierarchyResult,
    NodeDescriptor,
    callees,
    callers,
)
from know_your_soul.retrieval_v2.lookup import LookupConfig, LookupError, ScoredSymbol, SymbolLookup
from know_your_soul.retrieval_v2.query import KNOWN_FIELDS, ParsedQuery, parse_query
from know_your_soul.retrieval_v2.scoring import (
    CHARS_PER_TOKEN,
    KIND_BONUSES,
    bounded_levenshtein,
    estimate_tokens,
    fuzzy_max_distance,
    is_test_path,
    kind_bonus,
    name_match_bonus,
    path_relevance,
    split_identifier,
)

__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_EDGE_KINDS",
    "FTS_TABLE",
    "KIND_BONUSES",
    "KNOWN_FIELDS",
    "META_TABLE",
    "MIN_BUDGET_TOKENS",
    "SKELETON_MARKER",
    "AssembledContext",
    "CallHierarchy",
    "ContextConfig",
    "ContextEntry",
    "ContextError",
    "ContextItem",
    "FtsUnavailableError",
    "HierarchyEntry",
    "HierarchyError",
    "HierarchyReport",
    "HierarchyResult",
    "LookupConfig",
    "LookupError",
    "NodeDescriptor",
    "ParsedQuery",
    "ScoredSymbol",
    "SymbolLookup",
    "assemble_context",
    "bounded_levenshtein",
    "build_fts_query",
    "callees",
    "callers",
    "drop_fts_index",
    "ensure_fts_index",
    "estimate_tokens",
    "fts_available",
    "fuzzy_max_distance",
    "is_test_path",
    "kind_bonus",
    "name_match_bonus",
    "parse_query",
    "path_relevance",
    "rebuild_fts_index",
    "split_identifier",
]
