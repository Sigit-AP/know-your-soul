# Graphify lifecycle, clustering, export, validation, and benchmark — deep primary-source review

Research artifact for the Superpower Knowledge SOUL product. Primary source only: the pinned
Graphify clone plus its tests and manual bench scripts. No product code, spec, or plan was
modified; the only file written for this task is this document.

- Clone: `/root/.hermes/cache/scratch/graphify-ref-20261024`
- Commit: `92741d162b2094ffc2fed58cfbd8ee823e9da62c` (`git rev-parse HEAD`, verified clean afterwards;
  package version `0.9.95` at `pyproject.toml:7`)
- Runtime used for the test probes: a scratch venv under `/root/.hermes/cache/scratch/gfy-venv`
  with `pytest networkx numpy rapidfuzz pyyaml tree-sitter tree-sitter-python` and an editable
  no-deps install of the clone.
- Verification executed: **115 passed, 4 skipped** in 12.45 s across
  `tests/test_validate.py tests/test_atomic_writes.py tests/test_cluster.py tests/test_benchmark.py
  tests/test_partial_cache.py tests/test_build_merge_shrink_guard.py tests/test_compact_graph_json.py
  tests/test_merge_graphs_cli.py` plus targeted `tests/test_cache.py`/`tests/test_export.py` cases.
  (The 4 skips are graspologic/native-Leiden-conditional; see `tests/test_cluster.py:141-201`.)
- `tests/test_graph_invariance.py` was reviewed but not executed: it rebuilds the whole
  `tests/fixtures` corpus across all shipped language grammars, and this review did not install the
  full grammar set.

Citations are `path:line` relative to the clone. “Verified” below means the claim is backed by
source code and/or a test in the pinned commit; “upstream claim” means it appears in a shipped
markdown document but was not reproduced here.

---

## 1. Verdict on applicable-feature coverage

Graphify is a code-intelligence graph engine that also has real knowledge-artifact machinery
(documents, caching, communities, reports, exports). For a **deterministic, no-AI, knowledge-SOUL**
product the applicable universe is 40 features after excluding code-language extractors, DB push
sinks, install/skill/hook surfaces, and model-backed semantic extraction:

- **37 of 40 applicable features are fully covered after a specified adapter** = **92.5%**
  applicable-feature coverage. Every one has a source and/or test citation in §3.
- **3 of 40 are partial and carry explicit residual gaps**: benchmark methodology
  (token-only; no latency/quality suite), merge/publication integrity (union merge, no generation
  high-water), and schema/versioning (metadata only; no reader migration).
- **Out-of-the-box, no-adapter coverage is materially lower.** The rows marked *Adapter* are
  mandatory, not optional polish. Two are decisive:
  1. Without an LLM, `graphify update` produces only structural AST nodes for markdown; SOUL concept
     facts require a replacement deterministic fragment generator (upstream workaround documented in
     the product plan; code behavior confirmed at `graphify/watch.py:1828-1835`,
     `graphify/extractors/markdown.py`).
  2. The watcher treats changed documents as “needs_update flag only”, so a doc-only SOUL corpus gets
     no automatic rebuild unless the adapter drives `graphify update` explicitly
     (`graphify/watch.py:2679-2703`, `graphify/watch.py:2709-2748`).
- **Cross-cutting gap (not a row in the 40): atomic *projection sets*.** Graphify makes each file
  write atomic, but a rebuild publishes `graph.json`, `GRAPH_REPORT.md`, labels, signatures and
  analysis as separate writes in a fixed order, with no generation manifest or compare-and-swap
  high-water. A crash between them is a stale/torn read model. See §6 for the exact write order and
  the required adapter.

Coverage claim in one sentence: **Graphify’s lifecycle, cache, cluster, report, export, validation
and determinism surfaces cover ≥90% of the SOUL product’s applicable needs once a deterministic
fragment generator, a doc-change update driver, and a generation-manifest publisher are added; the
remaining gaps are benchmark scope and publication atomicity, not graph functionality.**

---

## 2. Pipeline map (what runs when)

`detect() → extract() → build() → cluster() → analyze helpers → report.generate() → export.to_*()`
(`ARCHITECTURE.md:8-11`). The module table at `ARCHITECTURE.md:17-34` is imported by
`tests/test_architecture_doc.py`, so it cannot silently drift.

The two write paths that matter to the product:

- Full rebuild: `graphify/watch.py:1677 _rebuild_code` → detect (`watch.py:1791`) → extract
  (`watch.py:2126`) → reconcile existing graph (`watch.py:2181`) → build (`watch.py:2360`) →
  topology compare (`watch.py:2362-2396`) → cluster (`watch.py:2398`) → remap (`watch.py:2401`) →
  analyze/report (`watch.py:2403-2518`) → `to_json` to a temp file (`watch.py:2529`) → shrink guard
  (`watch.py:2568`) → atomic replace + report/labels/signature (`watch.py:2588-2619`).
- `graphify cluster-only/label`: loads graph, reclusters, **writes graph.json first** (`cli.py:2697`)
  then report (`cli.py:2716`), analysis (`cli.py:2725`), labels and signature (`cli.py:2733-2744`).
  This is the clearest torn-publication path in the codebase.

---

## 3. Applicable-feature inventory

Verdict legend: **Direct** = usable as-is with configuration; **Adapter** = mandatory product-side
adapter for the no-AI SOUL runtime; **Partial** = usable but incomplete for the requirement.

### 3.1 Incremental extraction, watch and cache

| # | Feature | Evidence | Verdict | Positives | Negatives / gaps | Adaptation for SOUL |
|---|---|---|---|---|---|---|
| 1 | Code AST extraction, deterministic, no LLM | `graphify/extract.py`, `ARCHITECTURE.md:87-93`, `tests/test_no_cluster_run_only_keys.py:47-60` | Direct | Tree-sitter, zero credits; rebuild of unchanged tree is a test invariant (`tests/test_graph_invariance.py:131-133`) | ~40 grammars must be installed for full corpus; optional grammars missing silently reduce extraction (`tests/test_partial_cache.py:153-172`) | Install only the grammar set the SOUL corpus uses; pin versions and add a contract test that every expected grammar imports |
| 2 | Markdown/document structural extraction | `graphify/extractors/markdown.py`, `watch.py:1828-1835` | Direct | Markdown headings and links become nodes/edges without a model | Only structural; no concept facts. A doc already carrying semantic nodes is deliberately **not** quick-scanned (`watch.py:1841-1901`), which is correct but means no AST replacement layer for those docs | Use as the baseline “file/document” layer only; concept facts come from adapter #3 |
| 3 | Model-backed semantic extraction | `graphify/llm.py`, `tests/test_partial_cache.py`, `ARCHITECTURE.md:36-40` | **Adapter** | Produces rich `concept`/`rationale` nodes; cache, sanitizer and partial-retry rails are mature | Needs an API key/provider; forbidden in the deterministic runtime | Replace with a local, deterministic fragment generator that emits the same node/edge schema and ids, then feed it through `build_merge`/`merge-chunks`. Keep the semantic cache namespace so a later real pass stays compatible |
| 4 | Content-hash extraction cache | `graphify/cache.py:512-614`, `tests/test_cache.py:37-51` | Direct | SHA-256 of contents + portable relative-path salt; stat fastpath skips unchanged reads | Stat fastpath depends on a persisted `stat-index.json`; crossing roots in one process emits a warning and loses the fastpath (`tests/test_partial_cache.py` warnings at `cache.py:1266`, #3989) | Use one process per root or accept a slower first pass; never share one process across SOUL generations |
| 5 | AST cache version/schema/grammar namespacing | `graphify/cache.py:40-41,1016-1045`, `tests/test_cache.py:601-716` | Direct | Version bump re-extracts automatically; stale sibling versions are swept best-effort (`cache.py:86-111`) | Sweep is not transactional; a killed sweep leaves stragglers (retried next run) | Pin `_AST_CACHE_SCHEMA` semantics to the generator build; treat a bump as a full corpus re-derivation event and log it |
| 6 | Semantic prompt/mode cache namespaces | `graphify/cache.py:114-163,1031-1044`, tests `1521-1659` | Direct | Prompt fingerprint invalidates only on prompt change; deep mode is isolated; legacy entries are served with a warning | Legacy flat entries are unknowable-vintage and served by default (`cache.py:1106-1109`) | For deterministic fragments, set a stable synthetic prompt fingerprint so every fragment is attributable |
| 7 | Partial/truncated extraction invalidation | `graphify/cache.py:1122-1144,1464-1479`, `tests/test_partial_cache.py:24-129` | Direct | Partial entries self-heal on a complete re-extraction; empty-parse partials still stamped | Requires the producer to mark `_partial` or pass `partial_source_files`; a buggy adapter can silently promote an incomplete entry | Have the fragment generator stamp `partial` if it ever truncates; make the contract test fail if a truncated SOUL file is served from cache |
| 8 | Cache portability across checkouts/hosts | `graphify/cache.py:1237-1266`, `tests/test_cache.py:197-601` | Direct | `source_file` and ids are relativized on write and absolutized on read; symlink identities are preserved | Legacy absolute entries pass through (`cache.py:1151-1163`); cross-root warm hits are guarded but subtle | Keep `root` = corpus root and `cache_root` = product output; test a cache moved between two absolute checkout paths |
| 9 | Semantic cache liveness prune | `graphify/cache.py:1325-1376`, `tests/test_cache.py:1011-1100,1269-1301` | Direct | Unversioned semantic cache is bounded by live content hashes; deep and prompt subdirs swept | AST cache is swept by version, semantic by liveness; two hosts with different prompts keep each other’s entries by design (`cache.py:1346-1354`) | Run prune against the current SOUL corpus each generation; assert entry count is bounded by live docs × fingerprints |
| 10 | Manifest-driven incremental detection | `graphify/detect.py` `detect_incremental`, `watch.py:2322-2337`, `tests/test_incremental_unscanned_placeholders.py:54-118` | Direct | Manifest avoids “phantom deleted” rows (#1908); failed AST sources stay unstamped (#2543) | Manifest anchored elsewhere is treated as no evidence; without a manifest the older eviction behavior returns | Anchor the manifest to the corpus root and assert it exists before trusting incremental eviction |
| 11 | Watch: code changes rebuild immediately | `graphify/watch.py:2751-2857`, `watch.py:2709-2720`, `tests/test_watch.py:77-118` | Direct | Debounced; ignores read-only inotify events; deletions of any watched file trigger eviction | Linux `opened`/`closed_no_write` filter is heuristic (`watch.py:2723-2738`) | Keep the filter; add an integration test on the target OS |
| 12 | Watch: document changes only flag `needs_update` | `watch.py:2679-2703,2741-2748`, `tests/test_watch.py:23-43` | **Adapter** | Correctly refuses to auto-run a model-backed pass | A docs-only SOUL corpus would never rebuild automatically: the flag is a manual `/graphify --update` prompt | Watch the SOUL corpus and on a docs-only batch run the deterministic `graphify update`/fragment pipeline yourself; do not rely on the flag path |
| 13 | Per-repo rebuild lock + pending-change queue | `watch.py:1699-1767,28-104`, `tests/test_watch.py:2415-2660` | Direct | Non-blocking hooks queue instead of dropping work; late arrivals drain up to `_PENDING_DRAIN_MAX_PASSES`; stale markers are avoided | Lock is advisory flock per repo; a killed holder leaves a queued file drained by the next run | Reuse the flock or run a single writer process; never rebuild while a reader is publishing |
| 14 | Graph-invariance contract | `tests/test_graph_invariance.py:1-147` | Direct | One test asserts checkout folder, rebuild, CRLF and edge-endpoint invariants across every fixture language | Not executed in this review (full grammar set); it is a test, not a runtime guard | Run it in product CI with the pinned grammar set; it is the strongest determinism gate Graphify ships |
| 15 | Extraction schema validation | `graphify/validate.py:4-95`, `tests/test_validate.py:15-158` | Direct | Required node/edge fields, allowed file types/confidences, dangling endpoint detection; non-hashable ids reported not raised | It validates extraction, **not `graph.json`**; unknown top-level keys are accepted; dangling endpoint errors are intentionally downgraded by build (`build.py:1356-1358`) | Add a separate read-model schema validator for the published generation; never let build’s “dangling is expected” warning hide a real endpoint loss |
| 16 | Untrusted semantic fragment sanitizer | `graphify/semantic_cleanup.py:29-170` | Direct | Size/node/edge/hyperedge caps, id charset and path-escape rejection, YAML-unsafe values escaped elsewhere | Deliberately not run on the primary build/load path (`semantic_cleanup.py:6-10`), so it protects only the merge-chunks/skill ingest path | Route the deterministic fragment generator through `validate_semantic_fragment` + `sanitize_semantic_fragment`, not just `build_from_json` |
| 17 | Build canonicalization + validation | `graphify/build.py:1270-1404` | Direct | Legacy aliases folded before validation, numeric ids coerced, semantic ids re-derived from `source_file`, dual-slot hyperedges normalized | Warnings, not failures, for many schema issues; id re-key is heuristic | Treat any build warning as a gate failure in product CI |
| 18 | Per-tier replace on re-extraction | `graphify/build.py:2207-2348,2351-2610`, `tests/test_unverified_semantic_shrink.py:54-155` | Direct | A code re-extract does not delete a document’s semantic layer and vice versa; deleted sources pruned separately | Replacement ownership falls back to node `source_file` when provenance is missing, which can wipe unintended cross-file stubs (`build.py:2431-2436`) | Always pass explicit `ast_sources`/extracted-source provenance from the adapter |
| 19 | Shrink guard / explicit-overwrite refusal | `graphify/build.py:2795-2854`, `watch.py:1449-1546`, `tests/test_build_merge_shrink_guard.py:65-237`, `tests/test_watch.py:1763-2050` | Direct | Catches silent losses from failed chunks; excuses losses from re-extracted/deleted sources; refuses unreadable existing graph even with `--force` (`watch.py:2195-2204`) | Under `dedup=True` the build_merge guard is skipped because fuzzy merges legitimately shrink ids (`build.py:2809`); the residual “own-file under-production” case is excused (`build.py:2804-2808`) | Run the product build with `dedup=False` for the guard, then a separate audited dedup pass if needed |
| 20 | Entity/label deduplication | `graphify/dedup.py`, `tests/test_dedup.py`, `tests/test_dedup_shrink_refuses_force_write.py` | Direct | Order-independent survivor selection, path/lifecycle ranking, hyperedge rewiring (#2805/#3102) | Can merge same-label entities if the type/provenance guards do not fire; a dedup-driven shrink can refuse the write unless explicitly allowed | Keep dedup off the default deterministic path; if enabled, pin `allow_dedup_shrink` only after an audit |
| 21 | Orphan/stale-source sweep | `graphify/build.py:2647-2719`, `graphify/cli.py:_stale_graph_sources`, `tests/test_prune_sweeps_orphans.py`, `tests/test_stale_prune.py` | Direct | Removes sourceless external stubs orphaned by a prune; keeps alive-but-unproven files fail-closed; NFC/NFD-safe | Semantic/curated sourceless nodes are only swept when this prune isolated them (`build.py:2685-2689`); pre-existing isolates survive by design | Run a junk-node classifier over the SOUL graph as a final build step (Graphify does not garbage-collect structural markup junk; upstream corpus experience confirms this) |

### 3.2 Graph construction, schema and atomic writes

| # | Feature | Evidence | Verdict | Positives | Negatives / gaps | Adaptation for SOUL |
|---|---|---|---|---|---|---|
| 22 | Graph schema/version provenance | `graphify/export.py:175-186,475-479`; `watch.py:1370-1384`; `tests/test_export.py:34-42` | **Partial** | `graph.schema_version=1` and `graph.graphify_version` written additively; excluded from topology comparison so no false rebuilds | No reader-side migration code: `GRAPH_SCHEMA_VERSION` says “increment when incompatible” but nothing translates old→new. `graphify_version` is omitted when package metadata is unavailable — observed here as `KeyError: 'graphify_version'` before the editable install (`export.py:179-186`). `built_at_commit` and report date are also non-deterministic fields | Pin the accepted `schema_version` range, fail loudly on unknown, and require a generation manifest that includes schema + content hash. Strip `built_at_commit`/date before hashing the read model |
| 23 | Deterministic serialization + atomic writes | `graphify/export.py:374-440,486-489`, `graphify/paths.py:156-255`, `tests/test_atomic_writes.py:16-355`, `tests/test_compact_graph_json.py:59-187` | Direct | Sorted nodes/links/hyperedges, canonical key order, one-line compact JSON (#3413); atomic same-directory temp + `os.replace`, mode/umask preserved, symlink written through, Windows fallback with restore | Explicitly **no fsync**, so not a power-loss durability guarantee (`paths.py:156-165`); each artifact is atomic separately, never as a set | Adapter must publish a whole generation through one manifest pointer; keep fsync if the product claims crash durability |
| 24 | Idempotent export writes | `graphify/paths.py:221-241`, `tests/test_export_idempotent_writes.py:23-114` | Direct | Unchanged pages keep mtime/inode; avoids inotify/re-index churn (#3060) | Only applies to exporters that call `write_text_atomic_if_changed`; report/labels in cluster-only use plain `write_text` | Route every SOUL artifact through the idempotent atomic writer |
| 25 | Protected-artifact backup before overwrite | `graphify/export.py:30-104`, `tests/test_export.py:975-1047` | Direct | Dated backup when a semantic marker or curated labels exist; never blocks a write; env disable | Backup is best-effort and can silently fail with a warning; same-day backup overwritten | Back up the whole generation, not seven loose files; verify the backup before replacing the live pointer |

### 3.3 Clustering and communities

| # | Feature | Evidence | Verdict | Positives | Negatives / gaps | Adaptation for SOUL |
|---|---|---|---|---|---|---|
| 26 | Leiden → graspologic → Louvain fallback | `graphify/cluster.py:23-176`, `tests/test_cluster.py:141-201` | Direct | Native Leiden path avoids graspologic’s 7–19 s import; deterministic fallback chain | Native run uses `randomness=0.001`, `iterations=1`, `trials=1`, seed 42 (`cluster.py:79-88`) — seeded but not provably zero-variance; fallback is order-sensitive Louvain | Pin one partitioner per generation and record which one ran in the manifest; assert community count is within a tolerance across runs |
| 27 | Canonical partition input | `graphify/cluster.py:119-140`, `tests/test_cluster.py:204-233` | Direct | Sorts nodes and canonicalizes edge endpoint pairs before partitioning; fixes hash-order drift (measured 70 vs 69 communities, `cluster.py:124-131`) | Determinism is tested for endpoint orientation, not across all Python hash seeds/machines | Run the SOUL corpus with `PYTHONHASHSEED=0` and the same partitioner; add the cross-seed test |
| 28 | Stable community IDs and remap | `graphify/cluster.py:384-391,434-482`, `tests/test_cluster.py:107-128`; `tests/test_watch.py:1607-1665` | Direct | Total-order size tie-break makes ids reproducible for an identical grouping; remap matches ids by overlap after a change | Identical grouping yes, changed grouping no; cluster-id flap is explicitly handled as a “don’t rewrite” case (`tests/test_watch.py:1607`) | Treat community ids as internal; persist labels + membership signatures, never expose raw ids as stable identifiers |
| 29 | Deterministic hub labels + membership signatures | `graphify/cluster.py:185-230`, `graphify/watch.py:2444-2512`, `cli.py:2539-2620`, tests `tests/test_watch.py:382-676` | Direct | Highest-degree hub, id tie-break; `.sig` sidecar prevents stale labels after re-scoping (#2298/#label-stale); hub fallbacks isolated in `.pending.json` (#3334) | Hub names are readable but not semantic; signature file is a heuristic guard, and no sidecar falls back to a community-count heuristic | For SOUL, use curated labels only; generate hub labels as placeholders and keep them out of the reviewed file as Graphify already does |
| 30 | Hub exclusion + AMBIGUOUS-edge downweight | `graphify/cluster.py:236-363`, env `GRAPHIFY_AMBIGUOUS_SCALE` | Direct | Excludes super-hubs from partitioning and reattaches by majority vote; halves ambiguous edge weight (#4199) | Hub exclusion changes the partition and can strand nodes; env override is process-global | Decide one policy and record it; use ambiguity scaling for low-confidence SOUL relations |
| 31 | Cohesion scoring | `graphify/cluster.py:412-431`, `tests/test_cluster.py:24-81`, `tests/test_report_gap_thresholds.py` | Direct | Self-loops excluded so the score stays in [0,1]; used for split decisions and report | Cohesion is a density ratio, not a semantic quality metric | Use it only as a structural diagnostic, never as “knowledge quality” |

### 3.4 Report, export and retrieval

| # | Feature | Evidence | Verdict | Positives | Negatives / gaps | Adaptation for SOUL |
|---|---|---|---|---|---|---|
| 32 | Human-readable audit report | `graphify/report.py:96-408`, `tests/test_report.py:22-205`, `tests/test_report_gap_thresholds.py:58-233` | **Adapter** | Corpus check, extraction-confidence mix, god nodes, surprising connections, import cycles, hyperedges, community/cohesion table, ambiguous edges, knowledge gaps, suggested questions, work-memory lessons | Report embeds `date.today()` (`report.py:112,129`) and a commit stamp (`report.py:208-215`), so bytes differ by day/repo; token cost is passed in and is `{input:0,output:0}` on code rebuilds (`watch.py:2517`); thin/shown accounting has a long history of count bugs (`report.py:177-196,326-373`) | Strip date/commit/token placeholders before hashing or diffing; recompute thin/shown counts in the product validator against the raw graph |
| 33 | Portable report/export headers | `graphify/report.py:9-30`, `tests/test_report.py:89-113`, `tests/test_export.py:346-367` | Direct | Host absolute paths are stripped from report/HTML titles | Absolute paths can still leak through other exported fields if produced by a bad fragment | Add a “no absolute checkout path” assertion over every published artifact (Graphify tests this only for graph.json in `test_graph_invariance.py:118-128`) |
| 34 | `graph.json` read model | `graphify/export.py:287-489`, `tests/test_export.py:15-201,1064-1233` | Direct | Node `community`/`community_name`/`norm_label`, confidence-score backfill with honest low defaults (#2813), direction restored from `_src`/`_tgt` (#563), multigraph flag consistency (#4500) | Direction lives in arc order/markers, not `directed` semantics (`ARCHITECTURE.md:73-77`); readers must use `paths.load_node_link_graph` or restore markers | Always load through `restore_arc_direction`/`load_node_link_graph`; never trust `u,v` order from a plain `node_link_graph` |
| 35 | HTML/Obsidian/wiki export | `graphify/export.py:760-1113`, `graphify/wiki.py`, `tests/test_export.py:327-1047`, `tests/test_wiki.py`, `tests/test_export_idempotent_writes.py` | Direct | Vis.js pinned with SRI, XSS-safe tooltips, filename caps/case-collision handling, stale community-node dropping, idempotent reruns | HTML is a visualization, not a stable API; wiki article truncation and slug collision rules are heuristic | Publish Obsidian/wiki as derived views keyed by generation; never as source of truth |
| 36 | GraphML/Cypher/SVG/canvas/DB push | `graphify/export.py:552-1440`, `graphify/exporters/graphdb.py`, `tests/test_cli_export.py:188-384` | Direct | Escaped Cypher identifiers/strings, XML-illegal control stripping, native scalar types, parallel-edge GraphML | DB push is an outbound side effect; password handling must use env not argv (`cli.py:3261-3268`) | Excluded from the no-AI/side-effect runtime unless the product explicitly needs a sink; if used, forbid DB push and credentials-on-argv |
| 37 | Query / path / explain retrieval | `graphify/cli.py:1546-2218`, `graphify/serve.py:_query_graph_text`, `tests/test_query_cli.py`, `tests/test_path_cli.py` | Direct | Real deterministic traversal, ambiguity refusal with candidates, budget-bounded output, `--seed`, directed path markers | Query cost scales with graph; the host skill measured ~0.9 s and ~1.2k tokens/query on a small SOUL corpus, while a broad hub query can hit the truncation guard | Keep the graph small and curated; use `query`/`path` rather than broad keyword probes; enforce the product’s own byte/token budget |
| 38 | Token-reduction benchmark | `graphify/benchmark.py:10-121`, `tests/test_benchmark.py`, `tests/test_benchmark_real_query.py`, `tests/test_benchmark_raw_graph.py`; CLI `cli.py:3576-3591` | **Partial** | Measures the *real* MCP answer string, honors the 2000-token budget and truncation, counts no-match as 0 and excludes it, accepts raw `edges` and clustered `links` graphs identically | Only 5 hard-coded sample questions (`benchmark.py:46-52`); corpus words default to `nodes × 50` when `detect` output is absent (`benchmark.py:75-77`); tokens are `len(text)//4` (`benchmark.py:32-33`); averages only matching questions (`benchmark.py:83-92`); **no latency, no quality/accuracy grading, no repeated trials, no statistical interval, no spend ledger in this module** | Extend with the product suite: fixed corpus, frozen question set, N repeats, p50/p95 latency, quality rubric, token estimator parity, and a machine-readable result file |
| 39 | Parallel-edge / multigraph compatibility | `graphify/multigraph_compat.py:1-212`, `graphify/export.py:383-411`, `tests/test_multigraph_diagnostics.py`, `tests/test_cli_export.py:981-1233` | Direct | Behaviour-based capability probe (not version-based); parallel edges survive the JSON round-trip and are declared via `multigraph` | Simple-graph mode is still the default; collapsing parallel edges loses relation multiplicity | Decide explicitly: SOUL concept graph likely uses simple graph; if multiple relations per pair matter, enable multigraph mode and test the probe |
| 40 | Security / path / size / redaction guards | `graphify/security.py:20-66,105-140,330-438`, `graphify/security.py:372-398` | Direct | 512 MiB graph cap (env-tunable), graph paths confined to `graphify-out/`, control-char label sanitization, credential-shaped text redaction before labels reach artifacts | Caps are per-file, not per-generation; redaction is shape-based risk reduction, not a guarantee (`security.py:409-411`) | Keep the cap, confine all product reads to the output root, and run a secret scan over the published generation |

**Excluded from the denominator (not applicable to a deterministic SOUL product):** the ~40
language-specific AST extractors beyond markdown (code-only), MCP/HTTP serving
(`graphify/serve.py`) and the agent-hook/install ecosystem (`graphify/install.py`,
`graphify/hooks.py`, `graphify/skill*.md`), call-flow/tree HTML aimed at code navigation
(`graphify/callflow_html.py`, `graphify/tree_html.py`), transcription/video/PDF semantic
ingestion, database push sinks, and global cross-repo merge for multi-repo codebases. If any of
these become product requirements they move into the denominator and must be re-scored.

---

## 4. Lifecycle deep dive

### 4.1 Incremental update and watch semantics (verified)

- `_rebuild_code` is the single rebuild entry point (`watch.py:1677`). It takes a lock
  (`watch.py:1714-1727`), queues incremental paths or a full marker on contention
  (`watch.py:1721-1727`), drains queued work, and loops late arrivals up to
  `_PENDING_DRAIN_MAX_PASSES` (`watch.py:1754-1766`). Tests cover queue round-trips, lock
  non-clobber, holder draining and full-request upgrade (`tests/test_watch.py:2365-2660`).
- Incremental mode extracts only changed, still-existing files (`watch.py:1915-1971`). Deleted
  paths are evicted via `deleted_source_identities` (`watch.py:1910-1913,1958-1965`).
- A changed→unchanged cross-file `calls` edge would disappear on an incremental run, so the
  rebuild hands `extract()` the persisted AST nodes/edges of unchanged files as **read-only
  resolution context** (`watch.py:1981-2123`). Tests assert incremental/full parity for
  TypeScript, Python, Rust, Ruby and caller/callee edits (`tests/test_watch.py:3760-4531`).
- Failed AST sources are removed from the stamped manifest and their prior stamp blanked, so an
  extractor that failed does not appear “unchanged forever” (`watch.py:2137-2170`, tests
  `tests/test_partial_cache.py:153-172`).
- An existing graph that cannot be read (over cap or bad JSON) refuses the overwrite; `--force`
  does **not** bypass it (`watch.py:2195-2204`; tests `tests/test_watch.py:3549-3614`).
- If topology is unchanged, the rebuild skips clustering/report and only reconciles missing/stale
  HTML (`watch.py:2362-2396`, test `tests/test_watch.py:1640-1665`). If the graph is unchanged but
  the file layout is legacy pretty-printed, it is rewritten compact (`watch.py:2206-2208`, test
  `tests/test_compact_graph_json.py:86-102`).
- Watch dispatch: code changes or any deletion rebuild now; surviving non-code changes only write
  `needs_update` (`watch.py:2709-2748,2841-2852`). Read-only inotify events are filtered to prevent
  self-retrigger loops (`watch.py:2723-2738`, tests `tests/test_watch.py:4656-4730`).

**Product consequence:** a docs-only SOUL corpus needs an explicit update driver; the stock watcher
will not rebuild it. Also, the “topology unchanged” fast path does not re-run the report, so any
report-only adapter output must be published by the product, not expected from Graphify.

### 4.2 Cache and incremental failure modes (verified)

- Key = SHA-256(content) + relative-path salt; markdown body only (frontmatter changes do not
  invalidate), and a stat fastpath skips re-reads for settled files (`cache.py:512-614`; tests
  `test_cache.py:79-190,1732-1786`).
- A corrupt JSON entry is counted and treated as a miss (`cache.py:1110-1121`, tests
  `test_cache.py:1786-1817`).
- A partial/truncated entry is treated as a miss unless explicitly peeked (`cache.py:1122-1132`,
  tests `test_partial_cache.py:24-129`).
- A semantic entry with zero nodes and zero hyperedges is invalid and becomes a miss
  (`cache.py:1133-1144`, tests `test_cache.py:1819-1917`).
- Cross-file targets recorded at write time are re-checked on read; a renamed/deleted target makes
  the entry a miss (`cache.py:1173-1204`).
- Semantic cache is unversioned by design to avoid re-billing LLM calls; orphan growth is bounded
  by `prune_semantic_cache` against live hashes (`cache.py:1325-1376`). AST cache is
  version/schema/grammar namespaced and stale versions are deleted (`cache.py:1016-1045,86-111`).
- Persisted `stat-index.json` is root-scoped; crossing roots in one process loses the fastpath and
  emits a `RuntimeWarning` (observed in the verification run, source at `cache.py:1266`, #3989).

**Product consequence:** use one writer process per corpus root per generation; keep the semantic
namespace for deterministic fragments so a future semantic pass can coexist; run cache prune as a
bounded maintenance step, not on every query.

### 4.3 Merge, prune and dedup semantics (verified)

- `build_merge` is a load-existing + merge, **not a write** (`build.py:2362-2366`). Re-extracted
  sources replace their prior contribution **per tier**; files absent from new chunks are
  preserved; deleted files are pruned via `prune_sources` (`build.py:2414-2531`).
- `merge_raw_extraction` mirrors the same semantics for the raw `--no-cluster` path
  (`build.py:2207-2348`), and `tests/test_build_merge_shrink_guard.py:227-237` pins the parity.
- Shrink guard: the on-disk baseline is diffed by node identity before the replace rebind; only
  losses explained by same-tier re-extraction or an explicit prune are excused; otherwise
  `build_merge` raises `ValueError` (`build.py:2795-2849`; tests
  `test_build_merge_shrink_guard.py:83-105`). `watch._check_shrink` is the parallel implementation
  (`watch.py:1449-1546`) with tests for rebuild/lost-semantic/growth cases
  (`tests/test_watch.py:1763-2050`).
- `merge-graphs` is a **union**: it prefixes every non-external node with `tag::`, normalizes all
  inputs to a plain undirected `Graph`, composes, offsets communities and re-attaches the union of
  hyperedges (`cli.py:3093-3195`, `build.py:2857-2948`). It is not an id-dedupe across graphs:
  merging the same fragment twice doubles its nodes. Same-order merges are byte-reproducible
  (`tests/test_merge_graphs_cli.py:292-306`) but input order is part of the identity.
- Dedup has a large correctness surface (`tests/test_dedup.py`, 2k+ lines) including order
  independence across hash seeds and cross-file entity guards. The build_merge shrink guard is
  deliberately skipped when `dedup=True` because fuzzy merges legitimately collapse ids
  (`build.py:2800-2809`).
- Orphan sweep removes sourceless machine-minted placeholders stranded by a re-extraction, while
  sparing pre-existing isolates, hyperedge members and fresh isolates (`build.py:2647-2719`, tests
  `test_prune_sweeps_orphans.py`). Stale-source pruning is fail-closed for alive-but-unproven files
  (`tests/test_stale_prune.py:43-118`).

**Product consequence:** merge a deterministic fragment onto a fresh AST baseline exactly once;
never merge the same fragment twice; always pass explicit `prune_sources` for deleted SOUL files;
keep `dedup=False` for the guarded build and audit any separate dedup pass.

### 4.4 Clustering and communities (verified)

- `cluster()` accepts directed graphs by converting to undirected, gives isolates singleton
  communities, optionally excludes hubs by degree percentile and reattaches them by majority vote,
  splits oversized communities (>25% of nodes, min 10), and re-splits low-cohesion communities
  (≥50 nodes, cohesion < 0.05) (`cluster.py:233-391`).
- Partition input is canonical: nodes sorted by `str`, edge pairs canonicalized and sorted with a
  JSON-key tiebreak (`cluster.py:119-140`). Tests pin endpoint-orientation invariance and the
  native-vs-wrapper equality (`tests/test_cluster.py:204-233,141-165`).
- Community IDs are re-indexed by size descending with a total-order tiebreak, explicitly to stop
  id permutation being read as “community churn” (`cluster.py:384-391`). `remap_communities_to_previous`
  greedily matches by overlap and assigns fresh ids deterministically (`cluster.py:434-482`).
- Labels: hub labels are deterministic (`cluster.py:185-209`); membership signatures are
  `sha256(sorted member ids)[:16]` (`cluster.py:212-230`); a saved label survives only on an equal
  signature or strict majority overlap of both sets (`cluster.py:485-507`). Watch and cluster-only
  both validate labels and route hub fallbacks to `.graphify_labels.pending.json`
  (`watch.py:2407-2625`, `cli.py:2518-2620,2729-2744`).
- Ambiguous edges are down-weighted 0.5× before partitioning, overridable by
  `GRAPHIFY_AMBIGUOUS_SCALE` (`cluster.py:272-288`). Cohesion excludes self-loops so it stays in
  [0,1] (`cluster.py:412-427`).

**Product consequence:** clustering is reproducible for an unchanged grouping, but community
membership is not a stable identity across corpus edits. Persist labels + signatures as a reviewed
sidecar and treat community ids as internal. Do not promise byte-stable communities across
generations; promise stable *membership signatures* and audited label changes.

### 4.5 Report, export, validation (verified)

- Report generation is pure string construction from graph + analysis (`report.py:96-408`). It
  filters file/concept/rationale nodes from “real” counts with one shared predicate
  (`report.py:156-196`) and tests pin summary/gaps/render agreement
  (`tests/test_report_gap_thresholds.py:58-233`). It embeds `date.today()` and commit.
- `validate_extraction` is strict about required fields, file types, confidences and dangling edge
  endpoints, and turns malformed non-hashable ids into errors rather than crashes
  (`validate.py:10-95`, tests `test_validate.py:111-158`). It is not a published-graph validator.
- `validate_semantic_fragment` adds untrusted-input caps and id/path checks
  (`semantic_cleanup.py:44-170`), but is not on the primary build path by design
  (`semantic_cleanup.py:6-10`).
- Security: URL/path validation, a 512 MiB per-file graph cap with env override, control-char and
  credential redaction (`security.py:20-66,105-140,330-438`).

**Product consequence:** add a product-owned read-model validator that checks the published
generation against a frozen schema, not just the extraction dict; route every untrusted fragment
through `validate_semantic_fragment`.

### 4.6 Schema and versioning (verified)

- `GRAPH_SCHEMA_VERSION = 1` with the comment “Increment when the persisted graph structure changes
  incompatibly for consumers” (`export.py:175-176`). `graph.schema_version` always written;
  `graph.graphify_version` written only when `importlib.metadata.version("graphifyy")` succeeds
  (`export.py:179-186,475-479`).
- Watch excludes both fields from topology comparison so metadata cannot trigger a rebuild
  (`watch.py:1370-1384`; changelog entry at `CHANGELOG.md:321,337` explains the false-rebuild fix).
- AST cache has its own `_AST_CACHE_SCHEMA = 25` plus extractor version and grammar fingerprint
  (`cache.py:40-41,1039-1041`). Semantic cache is deliberately unversioned
  (`cache.py:114-123,1325-1334`).
- **No migration/reader logic exists**: the schema version is provenance, not a compatibility
  mechanism. A product reader must decide supported ranges and fail closed otherwise.

**Product consequence:** the SOUL generation manifest must carry `schema_version`, content hash,
corpus high-water and generator version; readers validate all four before serving.

### 4.7 Benchmark methodology (verified) vs shipped benchmark claims (upstream)

What the code actually measures (`graphify/benchmark.py`):

- Loads the graph the same way MCP does, accepting raw `edges` or clustered `links`
  (`benchmark.py:70-73`, `tests/test_benchmark_raw_graph.py:64-103`).
- Corpus tokens = `words × 100 // 75`; words default to `nodes × 50` if not supplied
  (`benchmark.py:75-79`).
- Query tokens = `max(1, len(answer)//4)` over the **real** `_query_graph_text` answer with BFS,
  depth 3, 2000-token budget; a no-match answer counts 0 and is excluded from the average
  (`benchmark.py:32-43,83-92`; tests `test_benchmark_real_query.py:39-67`).
- Five fixed sample questions (`benchmark.py:46-52`); CLI optionally reads
  `.graphify_detect.json` for a real word count (`cli.py:3581-3590`).
- It rejects oversized graphs before parsing (`tests/test_benchmark.py:190-198`).

What it does **not** measure: latency (no timing), quality/accuracy (no gold set or grader),
repeat runs or variance, retrieval recall, ingest cost, or per-run spend. The manual bench scripts
`tests/bench_extract.py` and `tests/bench_query_scoring.py` measure extraction speedup and query
scoring traversal count respectively, and both explicitly say they are not wired into CI
(`tests/bench_query_scoring.py:19-27`); their docstrings contain example output, not recorded
results.

**Upstream claims that must not be treated as verified product facts:**

- `BENCHMARKS.md` claims LOCOMO recall@10 0.497 / QA 45.3% (n=300), LongMemEval-S 76% (n=50),
  ERPNext key-fact coverage 82.0% vs 70.8% (n=6), ~$1.40 ingest, judge agreement 90.6% / Cohen’s
  κ 0.81 (`BENCHMARKS.md:19-35,97-165,180-203`).
- `README.md` repeats the same headline numbers and “cut agent token spend by 80%”
  (`README.md:31,130-140,466`).
- The harness those claims depend on is **not in this commit**: `BENCHMARKS.md:190-203` tells the
  reader to run `memory/runner.py` and `crosstool/run.py`, but the pinned tree has no `memory/` or
  `crosstool/` directory (verified top-level tree: `docs graphify scripts tests tools worked` plus
  repo files). The numbers are therefore **unreproducible from the pinned artifact** and are
  classified as upstream marketing claims, not evidence.
- The only code-intelligence number with a dated, localized method is the FastAPI INFERRED
  cross-file edge spot-check: 12/12 correct on 2026-10-05, explicitly “a dated manual spot-check,
  not a continuously-measured metric” (`BENCHMARKS.md:152-165`).

**Product consequence:** build the SOUL benchmark from the `benchmark.py` pattern (frozen graph +
frozen questions + real query answer + budget), then extend it with repeats, p50/p95 latency, token
estimator parity and a quality rubric. Do not inherit the LOCOMO/LongMemEval numbers as product
claims.

---

## 5. Failure-mode catalog (code + tests)

1. **Silent graph shrink** — guarded in three places: `to_json` (#479, `export.py:287-366`),
   `watch._check_shrink` (`watch.py:1449-1546`, tests `test_watch.py:1763-2050`) and
   `build_merge` (#2497, `build.py:2795-2849`). Guard is skipped under `dedup=True`
   (`build.py:2800-2809`) and the “own-file under-production” residual is excused
   (`build.py:2804-2808`).
2. **Unreadable existing graph** — refuses overwrite; `--force` does not override
   (`watch.py:2195-2204`, tests `test_watch.py:3549-3614`). `to_json` fails closed on a corrupt
   non-empty file (`export.py:322-336`, test `test_export.py:1150-1169`).
3. **Torn projection** — `graph.json` is written atomically, but report/labels/analysis are
   separate writes; cluster-only writes graph first (`cli.py:2697-2744`). A crash leaves a graph
   that does not match its report/labels. HTML has a stale-marker mitigation
   (`watch.py:2577-2580`, `_reconcile_graph_html` at `watch.py:1595-1676`), but no generation
   pointer.
4. **Stale cache vintage** — handled per key: AST versioned, semantic prompt-fingerprinted, legacy
   flat entries served with a warning (`cache.py:1106-1109,1151-1152`). A prompt-file read failure
   falls back to flat layout with a warning (`cache.py:166-195`, tests `test_cache.py:1684-1731`).
5. **Poisoned/partial cache entry** — partial and zero-node semantic entries are misses
   (`cache.py:1122-1144`); corrupt JSON counted and missed (`cache.py:1110-1121`).
6. **Root mismatch on prune** — `build_merge` derives a root by suffix-matching absolute prunes
   against stored relative source_files and warns on a true zero-match instead of “already clean”
   (`build.py:2548-2568`, tests `test_build_merge_shrink_guard.py:163-224`).
7. **Merge duplication** — `merge-graphs` is a union; merging the same fragment twice doubles
   nodes (`cli.py:3142`, `build.py:2883`). Same-order output is byte-reproducible, different order
   is not (`tests/test_merge_graphs_cli.py:292-306`).
8. **Community id churn / stale labels** — id re-index is total-ordered (`cluster.py:384-391`);
   labels validated by signature/majority overlap (`cluster.py:485-507`); hub fallbacks isolated
   (`watch.py:2407-2519`); flap is treated as “no change” in tests (`tests/test_watch.py:1607-1665`).
9. **Clustering determinism limits** — native Leiden is seeded (`randomness=0.001, seed=42,
   trials=1`, `cluster.py:79-88`); fallback Louvain is order-sensitive even with seed; tests cover
   endpoint orientation but not every hash seed/machine.
10. **Watcher self-trigger** — read-only `opened`/`closed_no_write` events ignored
    (`watch.py:2723-2738`, tests `test_watch.py:4656-4730`).
11. **Watch lock handoff** — queueing plus late drains; tests cover non-clobber, waiter handoff and
    full-request upgrades (`tests/test_watch.py:2415-2660`).
12. **Deleted-cwd rebuild** — recovers via `GRAPHIFY_REPO_ROOT` or fails with a clear message
    (`watch.py:1564-1592`, tests `test_watch.py:1040-1090`).
13. **Windows replace quirks** — `os_replace_with_fallback` handles PermissionError/WinError 17,
    symlink targets, readonly destinations, and restores on final-rename failure
    (`paths.py:84-152`, tests `test_atomic_writes.py:149-355`).
14. **Oversized graph** — per-file 512 MiB cap before parse (`security.py:372-398`); HTML export
    falls back to community view when over cap (`cli.py:3373-3394`); benchmark refuses oversized
    (`tests/test_benchmark.py:190-198`).
15. **Path traversal / credential leakage** — graph reads confined to `graphify-out/`
    (`security.py:330-369`), label sanitization (`security.py:405-438`), credential-shaped
    redaction before labels reach artifacts (`security.py:409-438`).
16. **Non-deterministic report bytes** — `date.today()` and `built_at_commit` are embedded
    (`report.py:112,129,208-215`); a reader that hashes the report across days sees churn.
17. **Missing version provenance in a source checkout** — `graphify_version` omitted when package
    metadata is unavailable (`export.py:179-186`); observed as
    `KeyError: 'graphify_version'` in `test_export.py::test_to_json_includes_graph_schema_metadata`
    before the editable install. A vendored deployment must install or inject the version.
18. **Semantic-source scope escape** — a model can attribute nodes to a file not in the batch,
    which would replace that file’s graph contribution while its manifest says unchanged; guarded
    by `scope_semantic_result` / `allowed_source_files` (`cache.py:1525-1559,1829-1918`, tests
    `test_cache.py:1949-2058`). The deterministic adapter must apply the same scope rule.
19. **Partial-file manifest gap** — truncated files are excluded from the stamped manifest so the
    next run re-queues them (`tests/test_partial_cache.py:132-172`); a producer that does not mark
    truncation loses this protection.
20. **Structural junk / no GC** — `update` mints a node per file and per markdown heading.
    Graphify has no garbage collector for pre-existing junk; the upstream SOUL corpus measured
    145/658 junk nodes before adding a prune step. Build-time orphan sweep handles the
    re-extraction case (`build.py:2647-2719`) but not arbitrary markup noise.

---

## 6. Deterministic updates and atomic projections vs the product requirement

The product requirement is: monotonic source high-water, atomic whole-generation publication,
readers validate hash/high-water, stale reads repaired or a retryable unavailable error.

What Graphify provides:

- Deterministic content keys and idempotent extraction (`cache.py:512-614`,
  `tests/test_graph_invariance.py:131-133`).
- Atomic **per-file** publication (`paths.py:156-255`, `tests/test_atomic_writes.py`).
- A topology-equality short-circuit that avoids rewriting an unchanged graph
  (`watch.py:2362-2396`, `tests/test_compact_graph_json.py:105-117`).
- Provenance fields (`schema_version`, `graphify_version`, `built_at_commit`) but no content hash
  or high-water in `graph.json` (`export.py:475-485`).

What is missing for the product:

1. **No generation envelope.** There is no directory or manifest that binds
   `graph.json + GRAPH_REPORT.md + labels + signatures + analysis` into one immutable generation.
2. **No compare-and-swap high-water.** Two writers can interleave: each write is atomic, the set is
   not. The lock is per-repo and advisory (`watch.py:1699-1710`), and cluster-only can run outside
   it.
3. **No reader freshness contract.** `serve._load_graph`, `benchmark.run_benchmark` and exporters
   read `graph.json` directly; none validates a manifest or repairs a stale read.
4. **No fsync.** Atomic rename protects against process kill/ENOSPC mid-write, not power loss
   (`paths.py:156-165`).

Required adapter (critical): write each generation into
`generations/<monotonic-id>/` with content hashes and the corpus high-water, then publish by a
single atomic pointer/manifest swap. Readers open the manifest, validate schema/hash/high-water,
and on mismatch either re-read the pointer (bounded retries) or return a retryable unavailable
error. Add a paused/competing-projector test, an interrupted-activation test, a stale-read test
and a monotonic-recovery test; the upstream SOUL corpus already treats “never rebuild while a
consumer is querying” as a hard rule, and the code confirms why (separate writes, no CAS).

Also make budgets deterministic: Graphify’s `benchmark.py` uses `len//4`
(`benchmark.py:32-33`) and MCP query has a byte/token budget; the product should measure the
complete canonical JSON payload including provenance and truncation fields, with a
minimum-budget/metadata-overflow error rather than an over-budget success.

---

## 7. Upstream claims vs verified facts

| Claim | Source | Status |
|---|---|---|
| Pipeline and module responsibilities | `ARCHITECTURE.md:8-34` | **Verified** by source and `tests/test_architecture_doc.py` |
| Graph schema/version fields written | `README.md:72-74`, `export.py:475-479` | **Verified**, with the caveat that `graphify_version` needs installed metadata |
| `graph.json` is one compact line | `tests/test_compact_graph_json.py:1-187` | **Verified by execution** |
| Atomic writes preserve existing file on failure | `tests/test_atomic_writes.py:16-355` | **Verified by execution** |
| AST cache is version/schema/grammar namespaced | `cache.py:40-41,1039-1041` | **Verified** |
| Semantic cache deliberately unversioned | `cache.py:114-123,1327-1334` | **Verified** |
| Watch skips clustering when topology unchanged | `watch.py:2362-2396` | **Verified** |
| Merge is a union, not id-dedupe | `cli.py:3142`, `build.py:2883` | **Verified**; also stated in upstream product skill guidance, matching code |
| Shrink guard refuses silent loss | `build.py:2795-2849`, `watch.py:1449-1546` | **Verified by execution** |
| Communities use Leiden with Louvain fallback | `cluster.py:106-176` | **Verified**; native Leiden skipped on this Python (conditional tests) |
| LOCOMO/LongMemEval/ERPNext accuracy and cost | `BENCHMARKS.md:19-35,97-165`, `README.md:130-140` | **Upstream claim, not reproducible** — harness absent from commit |
| Judge blind validation 90.6%, κ 0.81 | `BENCHMARKS.md:80-83` | **Upstream claim, not reproducible** |
| “Cut agent token spend by 80%” | `README.md:31,466` | **Upstream marketing claim**; no measurement in repo ties it to a reproducible suite |
| Graph construction costs zero LLM credits | `BENCHMARKS.md:92-93,180-188` | **Verified for AST-only** (`extract`/`update` make no model calls); semantic pass is excluded from the SOUL runtime anyway |
| FastAPI INFERRED edge precision 12/12 | `BENCHMARKS.md:152-165` | **Dated manual spot-check**, explicitly not continuous |

---

## 8. Verification log

Commands and results (scratch venv, clone unchanged after cleanup):

```bash
/root/.hermes/cache/scratch/gfy-venv/bin/python -m pytest -q -p no:cacheprovider \
  tests/test_validate.py tests/test_atomic_writes.py tests/test_cluster.py tests/test_benchmark.py \
  tests/test_partial_cache.py tests/test_build_merge_shrink_guard.py \
  tests/test_compact_graph_json.py tests/test_merge_graphs_cli.py
# -> 115 passed, 4 skipped, 12 warnings in 12.45s
```

Additional targeted runs during the review:

- `test_validate.py`, `test_atomic_writes.py`, `test_cluster.py`, `test_benchmark.py`,
  `test_partial_cache.py`, `test_build_merge_shrink_guard.py` → 90 passed, 4 skipped.
- `test_export.py` schema/serialization/shrink cases and `test_cache.py`
  version/portability/prompt/prune cases → passed after the package was installed editable so
  `importlib.metadata.version("graphifyy")` resolved; the one failure before that was the
  environment-caused `graphify_version` KeyError documented in §5.17.
- `test_compact_graph_json.py` update/extract cases initially failed only because the Python
  grammar was not installed (`No module named tree_sitter_python`); all three passed after
  installing it.

The clone was returned to a clean tree: generated `__pycache__/`, `graphifyy.egg-info/` and
pytest caches were removed; `git status --short` printed nothing. No source, spec, plan or test
file was edited.

---

## 9. Critical adaptations (ranked)

1. **Deterministic fragment generator replacing the LLM semantic pass.** Emit the Graphify
   node/edge/hyperedge schema and file-scoped ids from the structured SOUL corpus, stamp a stable
   synthetic prompt fingerprint, and feed it through `build_merge` exactly once onto a fresh AST
   baseline. Without this the SOUL corpus degrades to heading/file nodes.
2. **Generation manifest + atomic projection pointer.** One immutable directory per generation,
   content hashes + corpus high-water + schema version, and a single atomic pointer swap; readers
   validate and retry. This is the only way to satisfy the product’s atomic-projection requirement;
   Graphify’s per-file writes are not sufficient.
3. **Doc-change update driver.** Stock watch only flags `needs_update` for surviving documents; the
   product must drive `graphify update`/fragment regeneration on document batches itself, with the
   rebuild lock held and no concurrent readers.
4. **Product read-model validator.** Validate the published generation (nodes/edges/hyperedge
   members, no dangling endpoints, expected file types/confidences, no absolute checkout paths, no
   secrets) rather than relying on `validate_extraction`, which checks only the extraction dict and
   is after the fact.
5. **Extended benchmark suite.** Freeze corpus + questions, use the real query answer, add repeats,
   p50/p95 latency, quality rubric, token-estimator parity and a result ledger; do not inherit
   `BENCHMARKS.md` numbers.
6. **Deterministic artifact normalization.** Strip `date.today()` and `built_at_commit` before
   hashing/diffing report or graph artifacts; run clustering with a pinned partitioner and
   `PYTHONHASHSEED=0`; record partitioner version in the manifest.
7. **Cache and prune operations.** One writer process per corpus root; run semantic-cache liveness
   prune against the live corpus; keep `dedup=False` on the guarded build and audit any dedup pass;
   pass explicit prune sources for deleted SOUL files.
8. **Junk-node hygiene.** Add the product’s own structural junk classifier as the last build step
   (file nodes, headings without facts, zero-degree orphans), because `update` re-mints them and
   Graphify does not garbage-collect pre-existing junk.
9. **Schema compatibility policy.** Pin the accepted `schema_version` range; fail closed on unknown;
   require `graphify_version` (or an injected generator version) in the manifest.
10. **Keep the budget deterministic.** Measure the complete canonical JSON knowledge payload with
    the product’s own `ceil(utf8_bytes/4)` estimator, label it an estimate, and error on
    metadata overflow instead of emitting an over-budget success.

---

## 10. Residual risks and non-goals

- Community membership is not stable across corpus edits; only the signature/label contract is.
- Clustering determinism is seeded but not universally proven; the native Leiden `randomness=0.001`
  and the Louvain fallback’s order sensitivity are real residual variance.
- The graph has no garbage collector for arbitrary pre-existing junk or superseded text; a polluted
  generation stays polluted until rebuilt from zero.
- Semantic cache pruning is best-effort and not transactional; a surviving orphan costs one
  re-extraction, not incorrect output.
- `merge-graphs` union semantics make “merge twice” a correctness hazard by design.
- `graph.json` is not fsync-durable; crash-during-rename semantics are claimed, power-loss
  durability is not.
- Upstream memory/quality benchmark numbers cannot be reproduced from this commit and should not be
  cited as product evidence.
- Graphify’s code-only extractors, MCP/HTTP server, DB sinks, agent hooks/install and video/PDF
  semantic ingestion are explicitly out of the SOUL product’s scope; if any become requirements the
  inventory and coverage denominator must be re-run.