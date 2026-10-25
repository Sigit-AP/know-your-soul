# Graphify retrieval internals and SOUL recommendation adaptation

Date: 2026-10-25
Graphify reference: commit `92741d162b2094ffc2fed58cfbd8ee823e9da62c`, clone at `/root/.hermes/cache/scratch/graphify-ref-20261024`.
Scope: `graphify/serve.py`, `graphify/paths.py`, `graphify/analyze.py`, `graphify/querylog.py`, the CLI handlers, and the retrieval tests named below.
Evidence class: primary source and test assertions inspected at the pinned commit. Targeted pytest execution was unavailable in this checkout (no pytest/networkx in its interpreter), so no test-run result is claimed here.

## 1. Decision first

Graphify's retrieval is deterministic, local, read-only, and does not call a model. Its useful core for Super Power Knowledge is the seed matcher, bounded traversal, deterministic ordering, explicit ambiguity, and per-generation warm indexes.

It is **not** a drop-in recommendation engine. The current query surface is a global text renderer for a code corpus:

- it ranks labels, source paths, rationale prose, and attributes; there is no primary-root or domain scope;
- a supplied knowledge domain cannot route directly to a stable root;
- the token budget is approximate (`3 chars/token`) and can be exceeded deliberately;
- its output is text, not a canonical typed response with provenance, versions, audit IDs, or watermarks;
- several analysis filters treat "concept" nodes as noise, which is the opposite of SOUL's knowledge content.

The system should wrap Graphify's pinned private retrieval functions behind an adapter, or reimplement the small deterministic matcher/traversal if the private surface is unacceptable. It must not shell out to `graphify query` and parse its rendered text.

## 2. Query pipeline, exactly as implemented

### 2.1 Graph load and compatibility repair

`serve._read_graph`:

- requires a `.json` suffix, an existing file, and passes a size cap (`graphify/serve.py:44-55`);
- accepts both `links` and legacy `edges`, forces `directed: True` for storage compatibility, repairs a false `multigraph` when parallel links exist, and preserves the logical directed flag separately (`graphify/serve.py:56-85`);
- stashes the build commit and loads a display-only learning overlay from a sidecar (`graphify/serve.py:86-98`).

Positives to reuse: strict file checks, compatibility with both edge key spellings, parallel-edge preservation, and an explicit logical-versus-storage direction distinction (tested at `tests/test_serve.py:1986-2009`).

Risks: the graph path is not sandboxed to a project root; the cap defaults to 512 MiB (`graphify/security.py:31-32`), which is far larger than a token-bounded knowledge projection should ever be. The learning overlay is an unversioned display sidecar and is not part of graph truth (`graphify/serve.py:93-98`, `graphify/serve.py:1128-1174`).

### 2.2 Entry, seeds, and no-match behavior

`_query_graph_text` (`graphify/serve.py:1413-1488`):

1. If explicit `seeds` are supplied, each is resolved through `graphify.affected.resolve_seed`; unresolved values are reported and skipped, and if none resolve the call fails closed instead of falling back to keyword search (`graphify/serve.py:1432-1455`).
2. Without explicit seeds, a query that is itself an exact node name shared by several nodes is refused. Prefix/substring keyword searches are deliberately not refused (`graphify/serve.py:1456-1462`, rationale at `graphify/serve.py:1846-1865`).
3. Query terms are normalized, stopword-filtered, and scored in one pass; per-term best seeds are collected in the same pass (`graphify/serve.py:1463-1484`).
4. No seed means the literal string `No matching nodes found.` (`graphify/serve.py:1410` and `graphify/serve.py:1485-1486`).

`affected.resolve_seed` accepts an exact node id, a unique exact label, an undecorated callable name, an exact source path, or a unique substring label; otherwise it returns `None` (`graphify/affected.py:174-223`). This is a useful fail-closed primitive, but "unique substring" is too permissive for a knowledge API unless stable node IDs are the first-class lookup key.

The CLI maps this behavior to explicit exit codes: `0` answer, `3` ambiguous name, `4` no matching node (`graphify/cli.py:88-91`). The tests pin:

- query exact-name tie exits `3` with no node body (`tests/test_lookup_exit_codes.py:131-136`);
- ambiguous seed exits `3`, missing seed exits `4` (`tests/test_lookup_exit_codes.py:139-150`);
- a prefix/substring keyword searches rather than refusing (`tests/test_lookup_exit_codes.py:153-160`);
- no result exits `4` (`tests/test_lookup_exit_codes.py:124-128`).

Positives: refusals are distinguishable from answers; ambiguity is not silently resolved to the first graph-order node. Adaptation: carry these as typed service errors (`AMBIGUOUS`, `NOT_FOUND`) rather than CLI-only exit statuses, and return candidate IDs.

### 2.3 Lexical matcher and ranking

The scorer is a bag-of-terms matcher, not a semantic retriever.

- Tokenization lowercases, strips diacritics, strips punctuation, and treats `_` as a separator (`graphify/serve.py:214-233`).
- Query terms split on whitespace/regex, segment Chinese when jieba is available, drop a hard-coded multilingual question/stopword set, and fall back to unfiltered terms when everything was a stopword (`graphify/serve.py:236-331`).
- Tier constants: exact `1000`, prefix `100`, substring `1`, source path `0.5`, rationale `0.75`, attribute `0.75` (`graphify/serve.py:334-346`).
- The full-query tier multiplies exact/prefix by `10 * joined_w`, where `joined_w` is the maximum IDF among constituent terms (`graphify/serve.py:653-667`).
- Exact/prefix label tiers are multiplied by `(matched_terms / n_terms)^2`; source, rationale, and attribute hits add score but do not count toward term coverage (`graphify/serve.py:668-717`, `graphify/serve.py:745-746`).
- Final order is `(-score, label length, node id)` (`graphify/serve.py:749-751`).
- IDF is computed only over `norm_label`; rationale and attribute terms are not counted in document frequency (`graphify/serve.py:377-399`).
- A trigram index over label, tokenized label, node id, source path, source tokens, rationale, and attributes supplies a candidate superset; it falls back to a full scan when a needle is short or too common (`graphify/serve.py:402-543`, `graphify/serve.py:620-635`).

Tested properties worth reusing:

- exact multi-word labels dominate supersets (`tests/test_serve.py:109-138`);
- squared coverage stops one generic exact hit from burying a multi-term match (`tests/test_serve.py:140-199`);
- common terms are down-weighted (`tests/test_serve.py:818-866`);
- trigram filtering is byte-identical to a full scan (`tests/test_serve.py:352-412`);
- short English terms are filtered but short non-English terms survive (`tests/test_serve.py:501-526`);
- rationale can seed a "why" question when the label shares no token (`tests/test_serve.py:1860-1910`).

Critical risk for SOUL: IDF is computed from labels only. A term that appears only in rationale or attributes has document frequency zero in `_compute_idf`, therefore maximum IDF, even if it is common in knowledge summaries. If SOUL projects summaries/context tags into `attributes`, a single rationale hit can receive disproportionate weight. A knowledge adapter must either compute IDF over the same searchable fields or perform its own ranking.

Other direct-use risks:

- A source-path hit is a positive score, but source paths are provenance, not recommendation relevance.
- The hard-coded stopword list and code verbs are English/German/Romance-centric; Indonesian aliases must be explicit nodes or alias attributes.
- There is no stemming or synonym expansion (`graphify/serve.py:214-233`); plural/word-form variants must be materialized as aliases.
- Exact-match dominance (`1000` versus `1`) is tuned for identifiers, not natural-language purposes.

### 2.4 Seed selection and diversification

`_pick_seeds` (`graphify/serve.py:787-882`):

- starts from the ranked scorer output;
- caps seeds at `max_k=3`;
- stops when a later score is below `top_score * 0.2`;
- deduplicates seeds by normalized label so homonymous generic labels do not fill every slot;
- if supplied the per-term winners, guarantees at least one seed per distinct query term that matched, ordered strongest term match first (`graphify/serve.py:857-882`, `tests/test_query_seed_order.py:27-52`).

Relational-intent verbs (`call`, `uses`, `imports`, etc.) are demoted from the per-term seed guarantee so a verb cannot seat a decoy root; they can still win a seat on score (`graphify/serve.py:885-905`, `graphify/serve.py:1471-1484`; tests `tests/test_serve.py:1034-1090`).

Positives: combining a dominant exact match with a per-term recall guarantee is directly reusable. Risks: the thresholds are fixed; code verbs are irrelevant; the per-label dedup key can collapse two distinct knowledge roots that share a display label. For SOUL, deduplicate by stable node ID and root domain, not label.

### 2.5 Traversal, context filtering, and fan-out

- `_traversal_view` builds an undirected view once per graph object and stashes `_src`/`_tgt` for rendering (`graphify/serve.py:1360-1408`).
- `_filter_graph_by_context` copies every node and keeps only edges whose `context` is in the requested set (`graphify/serve.py:994-1008`). Nodes with no surviving edge remain in the graph.
- BFS and DFS compute a hub threshold from the p99 degree but floor it at `50`; non-seed nodes at or above the threshold are not expanded (`graphify/serve.py:1062-1117`).
- `_complete_induced_edges` adds edges between visited nodes that traversal did not discover, preserving traversal order first and sorting completions for determinism (`graphify/serve.py:1011-1059`).
- Tests pin seed-to-seed edges, triangle cross-edges, hub-to-hub edges, no duplicates, context-filter respect, no self-loops, and mutual directed edges (`tests/test_query_induced_edges.py:42-197`).

Critical risks:

1. The token budget does not bound traversal. BFS/DFS can visit a large subgraph before rendering, and only then is text cut (`graphify/serve.py:1076-1090`, `graphify/serve.py:1248-1307`). There is no max visited-node or visited-edge count in the traversal.
2. The hub floor of `50` is a code-graph assumption. A small SOUL corpus may never suppress a hub even when one node connects most of the graph.
3. Context filtering is edge-only. A primary root can remain as an isolated node with no children, producing a misleading single-node answer unless the caller checks edge count (`tests/test_query_seeds.py:104-109` shows a relation filter removing a report node).
4. Traversal has no relation allowlist. A knowledge query could wander through provenance, supersession, or audit edges if projected into the same graph.

### 2.6 Rendering and token budget

`_subgraph_to_text` (`graphify/serve.py:1120-1308`):

- converts `token_budget` to `token_budget * 3` characters;
- renders seed nodes first, then non-seed nodes by `(hop distance, -degree, node id)`, then edges;
- sanitizes every label, caps attributes to 10 pairs, each value to 60 characters, and the summary to 200 characters (`graphify/serve.py:1167-1207`);
- appends a display-only `learning=` status suffix when a sidecar entry exists (`graphify/serve.py:1169-1174`);
- if over budget, cuts at a line boundary and adds a top truncation notice and bottom marker; the seed block is never cut (`graphify/serve.py:1248-1307`);
- if all nodes fit but edges do not, it returns the complete graph anyway, with a "Complete answer over budget" notice (`graphify/serve.py:1275-1289`).

Tests pin that the seed survives a tiny budget and appears first (`tests/test_serve.py:1483-1503`), truncation is announced at the top (`tests/test_serve.py:1506-1511`), and the complete-answer edge overflow can be 4-6x the requested budget (`tests/test_serve.py:1547-1572`; source comment says so at `graphify/serve.py:1268-1275`). The full-answer budget hint is exact under the 3-char rule (`tests/test_truncation_full_answer.py:68-91`).

What to reuse: seed retention, top-of-output truncation, omission counts, and an exact "budget needed for full answer" calculation are good UX patterns.

What to reject: `3 chars/token` is not the frozen SOUL budget; wrapper notices sit outside the budget; a complete answer may exceed the budget by several times; text output cannot include the required canonical metadata. SOUL requires `ceil(utf8_byte_length / 4)` over the complete canonical JSON, no over-budget success, and `BUDGET_TOO_SMALL` below 128 (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:114-116`).

### 2.7 MCP entry arguments and output

The MCP `query_graph` schema defaults to BFS, depth 3, budget 2000, and labels depth `1-6` in prose, but the handler only applies `min(depth, 6)`; it does not reject zero or negative depth or budget (`graphify/serve.py:2300-2320`, `graphify/serve.py:2457-2464`). The CLI hard-codes depth 2 (`graphify/cli.py:1654-1667`).

`get_node` returns label, ID, source, definition source, type, community, degree, and a JSON attributes line truncated to 1000 characters; it does not return relations; `get_neighbors` is a separate call (`graphify/serve.py:2487-2534`). This is insufficient for the SOUL node contract, which requires provenance, evidence, P/M/E, uncertainty, version, status, and relations (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:76-80`).

## 3. Explain, get-node, and path semantics

### 3.1 Shared single-node resolver

`_find_node_tiers` returns `source_exact`, `exact`, `prefix`, `substring` in precedence order (`graphify/serve.py:1629-1735`). `_resolve_single_node`:

- refuses if the winning tier has multiple nodes across different files, or multiple nodes in one file;
- accepts a prefix shorthand;
- treats a substring-only hit as a near miss with up to five suggestions, not as the answer (`graphify/serve.py:1817-1843`, `graphify/serve.py:1786-1814`).

Tests pin cross-file ambiguity, node-order independence, same-file ambiguity, substring near-miss, prefix acceptance, and candidate limits (`tests/test_explain_cli.py:280-348`, `tests/test_serve.py:1761-1834`).

This is a strong pattern: **never take `matches[0]` from a tie**. SOUL should reuse the tier concept but scope tiers to the requested primary domain and prefer stable node ID over label.

### 3.2 Path

CLI `path` is directed by default, can opt into undirected, materializes a sorted graph for deterministic equal-length route selection, and reports the stored relation(s) and direction rather than fabricating `calls` (`graphify/cli.py:1912-2082`; test `tests/test_path_cli.py:148-163`). Direction is recovered from `_src`/`_tgt` when the undirected storage reorders endpoints (`graphify/cli.py:2016-2039`, tests `tests/test_path_cli.py:235-270`).

`_shortest_path_text` mirrors this for MCP, defaults `max_hops=8`, adds an implied reverse hop for `contains` edges so file-to-symbol routes can continue, and returns a clear "No directed path" message (`graphify/serve.py:2018-2148`). A route that does not exist is an answer, exit `0`, not a miss (`tests/test_lookup_exit_codes.py:88-93`). Both endpoints resolving to the same node is treated as ambiguous (`tests/test_lookup_exit_codes.py:103-107`).

Reusable: directed-by-default, `_src`/`_tgt` truth, sorted canonical route selection, parallel-relation rendering, and the distinction between "no node" and "no route".

Not reusable as-is: CLI `path` has no `max_hops` guard and can materialize a whole rank; the `contains` reverse hop is code-schema-specific; there is no relation allowlist. SOUL should permit only declared knowledge relations, default to directed dependency/supersession paths, and return `NO_PATH` as a successful negative result with the queried endpoints.

## 4. Caches and freshness

Graphify uses five relevant caches:

| Cache | Location | Scope / key | Invalidation |
|---|---|---|---|
| IDF per term | `G.graph["_idf_cache"]` | graph object | new graph object; no in-place invalidation (`graphify/serve.py:377-399`) |
| Trigram postings | `G.graph["_trigram_index"]` | graph object | new graph object (`graphify/serve.py:470-491`) |
| Undirected traversal view | `G.graph["_traversal_view"]` | graph object | new graph object (`graphify/serve.py:1390-1408`) |
| Path search graphs | `G.graph["_path_search_graphs"][undirected]` | graph object + direction | new graph object (`graphify/serve.py:2018-2064`) |
| MCP graph contexts | `_GraphContextCache` | resolved path, key `(st_mtime_ns, st_size)` | file stat change; LRU evicts non-default contexts (`graphify/serve.py:140-199`) |

The MCP cache pins the configured default graph, keeps up to `GRAPHIFY_MAX_CONTEXTS` project graphs (default 8, minimum 1), and warms the trigram index on load (`graphify/serve.py:123-169`, `graphify/serve.py:2251-2294`). Tests prove LRU promotion, default pinning, eviction, and that bad/corrupt project graphs become tool errors without killing the server (`tests/test_serve_http.py:236-303`). Tests also prove cache reuse, answer equivalence in any call order, and isolation between old and new graph objects (`tests/test_mcp_graph_view_cache.py:60-124`).

Risks for SOUL:

- Caches key on object identity, not content hash. Any in-place mutation leaves stale IDF/trigram/traversal/path structures. The MCP path avoids this only by replacing `G` on a stat change.
- `(mtime_ns, size)` is not a content identity. An atomic same-size rewrite can preserve both, and the code has no hash check. The SOUL design explicitly requires a graph hash and source watermark, not a stat tuple (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:87-89`).
- `GRAPHIFY_OUT` is read at import time (`graphify/paths.py:30-53`), so ambient process state can silently choose the corpus. That is acceptable for a developer CLI, not for an authoritative knowledge service.
- `project_path` lets an MCP caller resolve an arbitrary directory plus `graphify-out/graph.json` (`graphify/serve.py:2267-2274`), widening the read surface beyond the single SOUL projection.

Adaptation: hold exactly one immutable generation object per active manifest; key any cache by `(projection_version, content_hash)`, never by path alone; validate hash and watermark before serving; discard the whole object on activation. Reuse the warm-index and pinned-context mechanics, not the stat-key freshness rule.

## 5. `paths.py`: reusable filesystem mechanics

`graphify/paths.py` is mostly infrastructure, but three pieces matter:

- `_out_from_env` rejects blank, `.`, and `..`-only output values so `GRAPHIFY_OUT=` cannot make output land in the project root (`graphify/paths.py:30-53`).
- `_atomic_replace` writes a temp file in the same directory, resolves a symlink target, preserves mode, and `os.replace`s into place; it explicitly disclaims fsync/power-loss durability (`graphify/paths.py:156-255`).
- `is_absolute_any_platform`, `nfc`, and `load_node_link_graph` provide portable stored-path judgment, NFC normalization, `links`/`edges` compatibility, and `_src`/`_tgt` restoration (`graphify/paths.py:478-510`, `graphify/paths.py:562-571`, `graphify/paths.py:574-621`).

Positives to reuse: atomic visibility, symlink-safe writes, mode preservation, NFC path identity, and the dual graph edge-key loader. Reject: import-time global output selection as the authority for SOUL, and the no-fsync claim as sufficient for acceptance criterion 3 (data survives restart). SOUL should fsync the generation files and directory, then publish `CURRENT` with a compare-and-swap watermark check. Atomic rename alone is explicitly not a consistency guarantee in the spec (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:87-93`).

## 6. `analyze.py`: useful graph measures, wrong ontology

### 6.1 God nodes

`god_nodes` ranks raw degree, optionally suppresses nodes above a degree percentile, and filters out:

- file nodes and method stubs (`_is_file_node`, `graphify/analyze.py:66-92`);
- concept nodes, defined as empty `source_file` or a source path with no dot in its final segment (`_is_concept_node`, `graphify/analyze.py:183-199`);
- JSON key noise and built-in/mock labels (`graphify/analyze.py:95-109`, `graphify/analyze.py:11-29`);
- nodes above the hub percentile (`graphify/analyze.py:112-148`).

Tests confirm file nodes and JSON keys are excluded (`tests/test_god_nodes_cli.py:39-46`, `tests/test_analyze.py:525-616`) and that percentile suppression leaves non-hubs (`tests/test_god_nodes_exclude_hubs.py:39-64`).

Critical mismatch: a SOUL primary node or concept may have `source_file` empty or a `soul://...` URI without a dot, so `_is_concept_node` returns true and `god_nodes` silently excludes it. All seven primary roots could vanish from the ranking. Reuse the degree/percentile idea only after removing the concept filter and replacing raw degree with typed knowledge degree (children, evidence, dependency links). Gate by lifecycle status and evidence count; do not use code-file heuristics.

### 6.2 Surprising connections

`surprising_connections` selects cross-file edges in a multi-source graph and scores confidence, cross-file-type, cross-repo, cross-community, semantic-similarity, and peripheral-to-hub relationships (`graphify/analyze.py:151-321`). Concept nodes and file hubs are excluded (`graphify/analyze.py:324-384`). For single-source graphs it falls back to cross-community edges or betweenness (`graphify/analyze.py:387-472`).

Useful idea: rank bridge edges by multiple explicit reasons and return a `why`. Not directly usable: `AMBIGUOUS` confidence is treated as more noteworthy than `EXTRACTED` (weight 3 versus 1, `graphify/analyze.py:260-284`); code/doc/paper categories and repo boundaries have no SOUL meaning; concept nodes are excluded. For SOUL, define "insight candidate" as a cross-primary `supports`, `contradicts`, `causes`, or `depends_on` edge with an explicit reason and evidence references, not as a code-confidence surprise.

### 6.3 Suggested questions

`suggest_questions` emits deterministic question records from AMBIGUOUS edges, high-betweenness bridge nodes, god nodes with INFERRED edges, isolated nodes, and low-cohesion communities; it interleaves types before applying `top_n` and returns a `no_signal` record when empty (`graphify/analyze.py:475-618`). Tests pin diversity, required keys, empty graph behavior, and low-confidence tagging (`tests/test_analyze.py:654-709`, `tests/test_suggest_questions_ambiguous.py:41-69`).

For SOUL this is a review-candidate generator, not a recommendation engine. Adapt types to `contradiction`, `weak_evidence`, `orphan_knowledge`, `stale_review`, and `cross_domain_bridge`; emit structured candidates with node IDs, reasons, and evidence, not question strings. `question=None` for `no_signal` is a poor typed API; return an empty list plus a reason code.

### 6.4 Graph diff and import cycles

`graph_diff` compares node/edge sets, treats undirected edge keys canonically, and reports reversed edges only when both snapshots carry `_src`/`_tgt` (`graphify/analyze.py:621-717`). Reuse this logic for projection regression tests and manifest diagnostics.

`find_import_cycles` is code-only (`graphify/analyze.py:720-834`). Reject for SOUL.

## 7. `querylog.py`: logging, not audit

`querylog` is opt-in and default-off. `GRAPHIFY_QUERY_LOG_DISABLE` wins, an explicit path or `GRAPHIFY_QUERY_LOG_ENABLE=1` enables it; full responses require another opt-in (`graphify/querylog.py:15-35`). It writes JSONL with timestamp, kind, question, corpus, inferred node count, result length, duration, and arbitrary extra fields. It infers `nodes_returned` from rendered text with `(\d+)\s+nodes?\s+found` (`graphify/querylog.py:12`, `graphify/querylog.py:38-40`, `graphify/querylog.py:43-78`).

Tests pin append behavior, default-off, disable precedence, opt-in response logging, parent creation, and never raising (`tests/test_querylog.py:14-223`).

Reusable: JSONL line shape, explicit opt-in for raw response content, and best-effort non-blocking diagnostics.

Not reusable as SOUL audit: fail-silent loss (`except Exception: pass`, `graphify/querylog.py:79-80`), regex-derived counts, no audit event ID, no outcome, no request hash, no transaction, no fsync, no hash chain. SOUL requires exactly one final audit event per valid invocation and an audit ID in every response (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:84-92`, `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:118`). Keep raw questions/responses off by default; log request hashes, node IDs, counts, outcome, duration, and audit correlation in a durable append path.

## 8. Exact SOUL retrieval adaptation

### 8.1 Root routing before any lexical query

Do not pass `purpose` alone into Graphify's global scorer.

1. If `domain` is supplied, map it to the stable primary node ID and verify it exists in the active generation. A missing root is `INVALID_DOMAIN`/`PROJECTION_INCOMPLETE`, never a fallback global search.
2. Build a candidate subgraph from that root: root + descendants + only the declared relation allowlist (`parent_of`, `derived_from`, `supports`, `contradicts`, `causes`, `learned_from`, `applied_in`, `supersedes`, `duplicates`, `depends_on`). Do not include provenance or audit edges in traversal.
3. If no domain is supplied, use deterministic lexical scoring only to choose candidate primary roots, then repeat step 2. A tie among primary roots is `AMBIGUOUS` with all root IDs; never take graph order.
4. If the purpose matches the root label exactly, route directly without scoring.

This follows the design requirement that a supplied domain routes directly to its stable primary node and otherwise lexical ranking selects candidate roots (`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md:114`; Task 4 at `docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md:130-148`).

### 8.2 Ranking inside the root

Use Graphify's tier logic only as a candidate generator, with these changes:

- stable node ID exact match first; exact domain-qualified label second; alias exact third; label prefix fourth; summary/context-tag match last;
- IDF computed over the same fields that are searchable, per generation, not labels only;
- source URI gets zero or negative relevance;
- explicit aliases are real nodes/attributes; no stemming or synonym inference;
- deduplicate by stable node ID and primary root, not label;
- deterministic final tie-break: lexical score, lifecycle status rank, `node_version`, UTF-8 node ID; P/M/E and uncertainty are returned, not allowed to silently erase a lexically exact but low-scored node.

### 8.3 Traversal limits

Before rendering, enforce all of:

- maximum depth (knowledge-specific default, not 2/3 code defaults);
- maximum visited nodes and maximum visited edges;
- relation allowlist;
- primary-root boundary (except explicitly requested cross-domain dependency);
- deterministic expansion order;
- no expansion through a node whose status is `deprecated` or whose version is not active, unless the caller requests history.

Graphify's token budget only limits rendering (`graphify/serve.py:1248-1307`); it is not a fan-out guard. A token-bounded service must stop traversal early, then serialize.

### 8.4 Mandatory response fields and budget

Return one canonical `QueryResponse` containing selected nodes with summary, provenance, evidence references, P/M/E, uncertainty, version/status, relation edges, audit ID, required watermark, source watermark, projection version, stale flag, truncation status, and omitted counts. Use `ceil(utf8_bytes/4)` over the entire canonical JSON payload, including metadata. Reject budgets below 128 with `BUDGET_TOO_SMALL`; if mandatory metadata cannot fit, return an error, never an over-budget success. Do not reuse the 3-char renderer or its edge-overflow exception.

### 8.5 Freshness and caches

- Immutable graph object per active generation; no in-place mutation.
- Cache key includes `projection_version` and graph content hash.
- Validate hash and source watermark on each read; stale read triggers one synchronous repair then a retryable unavailable error.
- No ambient `GRAPHIFY_OUT` or unrestricted `project_path`; resolve only the configured generation root.
- Replace Graphify's `(mtime_ns, size)` freshness test with hash + watermark. Keep mtime only as a cheap pre-check.

### 8.6 Error contract

Map Graphify's semantics to typed outcomes:

| Situation | Graphify behavior | SOUL outcome |
|---|---|---|
| exact name shared by multiple nodes | `Ambiguous:` text, exit 3 | `AMBIGUOUS`, candidate IDs, no nodes |
| no lexical match | `No matching nodes found.`, exit 4 | `NO_MATCH`, no fabricated root |
| unresolved explicit seed | fail closed, exit 4 | `INVALID_NODE_REFERENCE` |
| path endpoints exist, no route | "No directed path", exit 0 | successful `NO_PATH` result |
| path endpoint missing | exit 4 | `NOT_FOUND` |
| prefix/substring keyword | searches, not ambiguous | keep as search, but only within root scope |
| invalid/negative budget | not rejected by Graphify | `INVALID_BUDGET` / `BUDGET_TOO_SMALL` |

## 9. Adoption / reject / adapt table

| ID | Decision | Graphify behavior and primary evidence | Exact SOUL action | Verification |
|---|---|---|---|---|
| R1 | **Adopt** | Tiered lookup: source exact, exact, prefix, substring; near-miss instead of substring answer (`graphify/serve.py:1629-1843`) | Reuse tiers for aliases/labels, scoped to primary root; stable ID first | Ambiguity, prefix, near-miss tests |
| R2 | **Adopt** | Exact-name tie refuses rather than guessing (`graphify/serve.py:1846-1865`; `tests/test_lookup_exit_codes.py:131-136`) | Typed `AMBIGUOUS` with IDs; never graph-order pick | Two same-label roots test |
| R3 | **Adapt** | IDF counts labels only (`graphify/serve.py:377-399`) | Compute per generation over labels + aliases + summary + context tags, or use own ranker | Common summary term must not outrank exact alias |
| R4 | **Adopt** | Trigram prefilter is complete and falls back when unselective (`graphify/serve.py:402-543`; `tests/test_serve.py:352-412`) | Reuse as accelerator only; assert byte-identical ranking vs full scan | Property test on random graphs |
| R5 | **Adapt** | Score weights tuned for identifiers; exact 1000 vs substring 1 (`graphify/serve.py:334-346`, `graphify/serve.py:653-717`) | Knowledge-specific weights; source URI zero relevance; aliases explicit | Rank fixtures for purpose queries |
| R6 | **Adopt** | Gap-based seeds plus one seed per matched term (`graphify/serve.py:787-882`; `tests/test_query_seed_order.py:27-52`) | Keep dominant + per-term recall, but dedupe by node ID/root and cap by visited budget | Seed determinism and recall tests |
| R7 | **Reject** | Code relational-intent verbs hard-coded (`graphify/serve.py:885-905`) | Do not apply to knowledge retrieval; define no verb blacklist | Code verb absent from adapter tests |
| R8 | **Adopt** | BFS/DFS records induced cross-edges deterministically (`graphify/serve.py:1011-1117`; `tests/test_query_induced_edges.py:42-197`) | Reuse completion idea on the root-scoped graph | Cross-edge and parallel-edge tests |
| R9 | **Adapt** | Hub threshold floor 50; no visited-node/fan-out cap (`graphify/serve.py:1062-1117`) | Knowledge fan-out cap, typed degree, relation allowlist; stop before render | Hub graph cannot exceed max visited |
| R10 | **Adapt** | Context filter keeps all nodes and only filters edges (`graphify/serve.py:994-1008`; `tests/test_query_seeds.py:104-109`) | Filter nodes by domain/status first; detect seed with zero surviving edges | Isolated-root test |
| R11 | **Adopt** | Explicit seeds resolve file/path/label defensively (`graphify/serve.py:1432-1455`; `tests/test_query_seeds.py:33-64`) | Accept stable node ID; if label, exact/unique only; fail closed | Given-root and bad-seed tests |
| R12 | **Reject** | Budget is `3 chars/token`, wrappers outside budget, edges can stay complete at 4-6x (`graphify/serve.py:1120-1126`, `graphify/serve.py:1268-1307`) | Frozen `ceil(utf8/4)` canonical payload; no over-budget success; no edge exception | 12+ payload fixtures and boundary tests |
| R13 | **Adopt** | Seed-first rendering and top truncation notice (`graphify/serve.py:1156-1159`, `graphify/serve.py:1297-1307`; `tests/test_serve.py:1483-1511`) | Root and direct answer node survive truncation; show omitted counts | Tiny-budget root-preservation test |
| R14 | **Adapt** | `get_node` prints code metadata and 1000-char attrs, no relations (`graphify/serve.py:2487-2518`) | Own typed node projection with provenance/evidence/scores/version/status/relations | Contract parity across CLI/API/MCP |
| R15 | **Adopt** | Directed path by default with `_src`/`_tgt`, sorted route (`graphify/serve.py:2018-2148`; `tests/test_path_cli.py:148-270`) | Directed knowledge relations by default; sorted deterministic route | Hash-seed determinism test |
| R16 | **Adapt** | CLI path has no max hops; `contains` reverse hop is code-specific (`graphify/cli.py:2005-2039`) | Enforce max hops/visited; no implicit reverse knowledge edges | Long-path refusal test |
| R17 | **Adopt** | No-route is a successful answer, not a miss (`tests/test_lookup_exit_codes.py:88-93`) | `NO_PATH` success with endpoints and search mode | Distinct from `NOT_FOUND` |
| R18 | **Adapt** | Per-graph caches auto-invalidate only on new object (`graphify/serve.py:377-491`, `graphify/serve.py:1390-1408`) | Immutable generation object; caches keyed by version + hash | No stale read after activation |
| R19 | **Reject** | MCP freshness key is `(mtime_ns, size)` (`graphify/serve.py:180-199`) | Hash + watermark validation; stat only pre-check | Same-size rewrite/stale-generation tests |
| R20 | **Adapt** | MCP pinned default + LRU contexts, `project_path` arbitrary (`graphify/serve.py:140-199`, `graphify/serve.py:2267-2274`) | One configured projection; no project_path surface | Path-escape/arbitrary-root tests |
| R21 | **Adopt** | Graph size cap and label/attr sanitization (`graphify/security.py:31-32`, `graphify/security.py:405-451`) | Reuse sanitation; set much smaller cap for knowledge projection | Control-char, oversize, 256-char tests |
| R22 | **Adapt** | `paths.py` atomic replace, symlink handling, NFC, dual edge keys (`graphify/paths.py:156-255`, `graphify/paths.py:562-621`) | Reuse atomic write; add fsync and CAS manifest publication | Crash/interrupted activation tests |
| R23 | **Reject** | `GRAPHIFY_OUT` read at import, global output choice (`graphify/paths.py:30-53`) | Explicit generation root from config; stale env ignored | Env scrubbing test |
| R24 | **Adapt** | `god_nodes` excludes concept/file nodes and uses raw degree (`graphify/analyze.py:66-148`) | Keep concept knowledge nodes; rank typed hubs with status/evidence gates | Seven-root non-empty ranking test |
| R25 | **Reject** | Surprise scoring treats AMBIGUOUS confidence as high signal and uses code file categories (`graphify/analyze.py:260-321`) | No code-confidence ranking; use contradiction/dependency evidence instead | Contradiction candidate fixture |
| R26 | **Adapt** | `suggest_questions` deterministic diversity, AMBIGUOUS/isolated/cohesion signals (`graphify/analyze.py:475-618`) | Emit structured review candidates, not questions; no `question=None` | Diversity and empty-list tests |
| R27 | **Adopt** | `graph_diff` direction-aware projection diff (`graphify/analyze.py:621-717`) | Use for rebuild/manifest diagnostics and regression probes | Reversed-edge/empty-diff tests |
| R28 | **Reject** | `find_import_cycles` is code-only (`graphify/analyze.py:720-834`) | Omit from SOUL inventory with reason | Matrix exclusion row |
| R29 | **Adapt** | Query log is opt-in, fail-silent JSONL, regex node counts (`graphify/querylog.py:12-80`) | Durable audit event with ID/outcome/request hash; explicit counts; no silent loss | Audit failure and replay tests |
| R30 | **Adopt** | Explicit CLI exit contract 0/3/4 (`graphify/cli.py:88-91`; `tests/test_lookup_exit_codes.py:58-160`) | Keep distinctions as typed service error codes | Cross-adapter error parity |

## 10. Minimum regression probes for the adapter

1. **Root isolation:** a query in P4 never returns a P2 node unless a declared cross-domain relation is explicitly requested.
2. **Direct root:** a supplied valid domain bypasses lexical scoring and returns that root even when its label shares no token with `purpose`.
3. **Invalid root:** a missing primary root is an error, not a global fallback.
4. **Exact ambiguity:** two nodes with the same label but different IDs produce `AMBIGUOUS` and both candidate IDs.
5. **Budget:** every successful canonical response is `<=` budget under `ceil(utf8/4)`; below 128 is `BUDGET_TOO_SMALL`; metadata overflow is an error.
6. **Fan-out:** a high-degree hub cannot make visited nodes exceed the configured cap; truncation occurs after bounded traversal, not instead of it.
7. **Freshness:** same-size graph replacement with unchanged mtime cannot serve old content; hash/watermark mismatch repairs or fails.
8. **Cache isolation:** an old in-flight read finishes against its old generation; a new read never sees mixed caches.
9. **Path semantics:** missing endpoint is `NOT_FOUND`; existing endpoints with no allowed relation is `NO_PATH` success.
10. **Truncation:** the primary root and the highest-ranked direct answer survive a minimum feasible budget, and the response reports omitted counts.
11. **Sanitization:** control characters and overlong labels cannot enter the canonical response.
12. **No text scraping:** adapter tests fail if the implementation invokes the CLI and parses its human-readable output.

## 11. Residual risks and open decisions

- **Private API dependency.** The clean query primitives are private (`_query_graph_text`, `_score_query`, `_resolve_single_node`, `_shortest_path_text`). Pinning `graphifyy==0.9.95` makes this viable, but upgrade tests must compare golden fixtures. If private imports are rejected by the runtime guard, reimplement the matcher/traversal rather than scrape CLI text.
- **Semantic quality.** Lexical matching cannot establish that a knowledge node is professionally relevant; P/M/E are quality signals, not query relevance. Keep lexical retrieval and quality assessment separate in the response contract.
- **Cross-domain knowledge.** The design has seven roots but no explicit cross-domain query policy. Decide whether cross-root edges are included only for `depends_on`/`contradicts`, or are opt-in per request. The default should not leak.
- **MCP surface.** Do not expose Graphify's `project_path` or its code-oriented `list_prs`/PR tools. The SOUL MCP server has exactly ten knowledge operations.
- **Learning overlay.** Do not let the unversioned `.graphify_learning.json` sidecar influence ranking or responses. If outcome-derived hints are wanted, project them as versioned node evidence, not display suffixes.
- **Source path.** Keep provenance in the response, but do not let it affect ranking. A path is evidence of origin, not evidence of usefulness.

## 12. Bottom line

Adopt Graphify as a deterministic graph utility: tiered matching, IDF/trigram candidate generation, per-term seed guarantees, induced-edge traversal, deterministic path selection, atomic writes, and warm per-generation indexes.

Reject its top-level query response as the SOUL recommendation contract: the global scope, label-only IDF, code-tuned weights, approximate budget, text serialization, concept-node filters, stat-based freshness, and fail-silent query log are all incompatible with primary-root knowledge retrieval.

Adapt the middle: build a root-scoped candidate graph, use Graphify's matcher/traversal ideas, then serialize a canonical, audited, versioned, token-bounded knowledge response owned by the SOUL service.
