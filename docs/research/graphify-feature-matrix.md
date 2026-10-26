# Graphify feature matrix — approved 40-feature inventory and adoption ownership

Task 1 artifact for the Super Power Knowledge SOUL Graph
(plan: `docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md:47-71`;
spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md` sections 2 and 13).

- Graphify inspected commit: `92741d162b2094ffc2fed58cfbd8ee823e9da62c`, package version 0.9.95 (`graphifyy[mcp]==0.9.95`, pinned in `pyproject.toml` and `uv.lock`).
- Evidence base: the four cited research artifacts in `docs/research/` (`graphify-lifecycle-benchmark-deep.md` §3 carries the raw 40-row inventory this matrix re-indexes; `graphify-retrieval-deep.md`, `graphify-mcp-security-deep.md`, `soul-access-relearn-deep.md`, `depth-rubric.md`).
- **Status of this document: contracts and ownership only.** No Graphify capability is implemented by Task 1. Implemented coverage is therefore `0/40` until an owning task's named contract test passes; this matrix claims no more than the verified contract-test total.

## 1. Approved inventory, denominator and coverage math

| Item | Value |
|---|---|
| Applicable-feature inventory | **40** features (research artifact §3, rows 1–40) |
| Applicable-feature denominator | **40** |
| Fully covered after the mandatory adapters | **37** (92.5%) |
| Partial rows carrying a residual owner | **3** (rows 22, 23, 38) |
| Coverage formula | `verified_adopted_applicable_features / 40` |
| Coverage ceiling before any implementation | 37/40 = **92.5%** |
| Verified adopted coverage at Task 1 | **0/40 = 0%** — no adapter exists yet |
| Verified adopted coverage at Task 3 | **34/40 = 85%** — rows 1–22, 24, 26–35 and 39 pass their named contract tests (Task 3). Row 23's named test passes, but its residual (whole-set durability / power-loss recovery, owner Task 9) stays open, so the row is not counted |
| Release gate (spec acceptance criterion 9) | ≥ 90% (≥ 36/40) with exclusions and calculation shown |

Rules that bound every later coverage claim:

1. A row counts as adopted **only** after its adapter exists and its named contract test passes with fresh output. A passing contract test for one row never covers another row.
2. Adoption coverage may **not** be claimed above the verified contract-test total: the claim is `rows with a passing named contract test / 40`, never a prose estimate (see plan Task 10 and spec section 2).
3. Research depth is separate from adoption coverage. Research depth is **100%** under the eight-area rubric in `docs/research/depth-rubric.md` — `(8 full areas + 0.5 × 0 partial)/8 = 100%` — and states explicitly that it does not mean every source line was read. Depth is an input to the inventory, not evidence of adoption.
4. If Graphify is upgraded, or a mandatory adapter is dropped, the inventory and denominator must be re-derived before any new coverage claim.

## 2. Mandatory adapter list (not optional polish)

Rows marked **Adapter** cannot be satisfied by configuration. These seven products own the no-AI determinism gap and must exist before any feature adapter is enabled:

| # | Mandatory adapter | Why Graphify alone is insufficient (evidence) | Owning task |
|---|---|---|---|
| A1 | Deterministic local fragment generator replacing model-backed semantic extraction, emitting Graphify's `concept`/`rationale` node-edge schema with file-scoped IDs | Without an LLM, `graphify update` produces only structural markdown nodes (`graphify/watch.py:1828-1835`, `graphify/extractors/markdown.py`) | Task 3 |
| A2 | Doc-change update driver that runs the deterministic pipeline (stock Graphify only sets a `needs_update` flag for document changes) | `graphify/watch.py:2679-2703`, `graphify/watch.py:2709-2748` | Task 3 |
| A3 | Generation-manifest publisher: content-addressed generation directory + one atomic `CURRENT` pointer with schema, partitioner, corpus high-water and content hashes | Graphify writes each artifact atomically but publishes `graph.json`, report, labels, signatures and analysis as separate writes in fixed order, with no generation manifest or compare-and-swap high-water (`graphify/watch.py:2588-2619`, `graphify/cli.py:2697-2744`) | Task 3 |
| A4 | Product read-model validator: endpoints, file types, confidences, junk nodes, absolute-path leakage, secrets, unknown schema, hash mismatch — fail closed | `graphify/validate.py:4-95` validates extraction, not `graph.json`; unknown top-level keys are accepted | Task 3 |
| A5 | Root-scoped retrieval adapter over the pinned Graphify **library** primitives (matcher/traversal/path), never `graphify query` CLI text scraping | Private primitives are the clean interface (`graphify/serve.py:_query_graph_text`, `_score_query`, `_resolve_single_node`, `_shortest_path_text`); CLI output is a rendered, budget-approximate string | Task 4 |
| A6 | MCP adapter: one startup-configured allowlisted root, exactly `tools` + `resources`, no `project_path`, strict schemas/ranges, sanitized capped resources, no sampling/prompts/logging/completions/PR/`gh` | Graphify's server exposes arbitrary `project_path` (`graphify/serve.py:2267-2274`) and unsanitized raw resources (`graphify/serve.py:2711-2772`) | Task 8 |
| A7 | Security/operations guards: single `resolve_within(root, candidate)` containment, size caps, control/bidi/zero-width sanitization, secret redaction on read *and* write, verified atomic backup/restore | Redaction is shape-based and extraction-time only (`graphify/security.py:409-438`); containment and caps are per-file, not per-generation | Task 9 |

Rows 1–21 and 24–40 below are all assigned to exactly one owning task in {Task 3, Task 4, Task 8, Task 9}; the residual gaps of the three partial rows name a residual owner in addition to the row owner.

## 3. Row-by-row inventory, ownership and contract-test naming

Verdict legend: **Direct** = usable as-is with configuration; **Adapter** = mandatory product-side adapter; **Partial** = usable but a residual gap stays with an owner. `Covered?` is `no` for every row at Task 1: the cell states what will be verified, and turns to `yes` only when the named contract test passes.

### 3.1 Incremental extraction, watch and cache

| # | Feature | Verdict | Adaptation for SOUL | Owner | Named contract test | Covered? |
|---|---|---|---|---|---|---|
| 1 | Code AST extraction, deterministic, no LLM | Direct | Markdown/document scope only; language grammars beyond markdown are excluded (§4). Grammar set pinned and asserted importable. Denominator note: the non-markdown grammar sub-surface leaves the denominator exactly once, via §4; row 1 itself stays in the denominator of 40 and is counted covered on its applicable markdown scope — no double counting | Task 3 | `tests/test_fragment_generator.py::test_pinned_grammar_set_imports` | yes |
| 2 | Markdown/document structural extraction | Direct | Baseline file/document layer for imported sources; concept facts come from A1 | Task 3 | `tests/test_fragment_generator.py::test_markdown_structure_nodes` | yes |
| 3 | Model-backed semantic extraction | **Adapter** | Replaced by A1 (deterministic fragment generator), routed through Graphify validate + sanitize; no model backend importable or reachable | Task 3 | `tests/test_fragment_generator.py::test_schema_parity_without_model_import` | yes |
| 4 | Content-hash extraction cache | Direct | Cache keyed on content hash + relative path salt; one process per root, never shared across generations | Task 3 | `tests/test_projection.py::test_cache_keyed_on_content_hash_not_stat` | yes |
| 5 | AST cache version/schema/grammar namespacing | Direct | Cache namespace pinned to the generator build; a bump is a logged full re-derivation | Task 3 | `tests/test_projection.py::test_cache_namespace_pins_generator_version` | yes |
| 6 | Semantic prompt/mode cache namespaces | Direct | Stable synthetic prompt fingerprint so every deterministic fragment is attributable | Task 3 | `tests/test_projection.py::test_fragment_fingerprint_is_stable` | yes |
| 7 | Partial/truncated extraction invalidation | Direct | Generator stamps `partial` on truncation; a truncated source is never served from cache | Task 3 | `tests/test_projection.py::test_truncated_source_is_not_served_from_cache` | yes |
| 8 | Cache portability across checkouts/hosts | Direct | `root` = corpus root, `cache_root` = product output; cache moved between absolute paths still hits | Task 3 | `tests/test_projection.py::test_cache_is_portable_across_roots` | yes |
| 9 | Semantic cache liveness prune | Direct | Prune per generation; entry count bounded by live documents × fingerprints | Task 3 | `tests/test_projection.py::test_cache_prune_bounded_by_live_documents` | yes |
| 10 | Manifest-driven incremental detection | Direct | Manifest anchored to the corpus root and asserted before incremental eviction is trusted | Task 3 | `tests/test_importer.py::test_manifest_anchored_incremental_import` | yes |
| 11 | Watch: code changes rebuild immediately | Direct | Kept as the rebuild trigger for structured sources, with the OS integration probe | Task 3 | `tests/test_projection.py::test_source_change_triggers_rebuild` | yes |
| 12 | Watch: document changes only flag `needs_update` | **Adapter** | A2: product driver runs the deterministic pipeline on a docs-only batch | Task 3 | `tests/test_projection.py::test_doc_change_triggers_deterministic_update` | yes |
| 13 | Per-repo rebuild lock + pending-change queue | Direct | Single serialized writer process; readers never publish; queued work is drained | Task 3 | `tests/test_projection.py::test_single_writer_lock_and_pending_drain` | yes |
| 14 | Graph-invariance contract | Direct | Rebuild invariant across checkout path, CRLF, edge endpoints; run with the pinned grammar set | Task 3 | `tests/test_projection.py::test_rebuild_invariant_across_roots_and_eols` | yes |
| 15 | Extraction schema validation | Direct | Plus A4: product read-model validation of the published generation; build warnings become gate failures | Task 3 | `tests/test_read_model_validator.py::test_rejects_bad_endpoints_and_unknown_keys` | yes |
| 16 | Untrusted semantic fragment sanitizer | Direct | Every generated fragment passes `validate_semantic_fragment` + `sanitize_semantic_fragment` | Task 3 | `tests/test_fragment_generator.py::test_sanitizer_bounds_ids_and_sizes` | yes |
| 17 | Build canonicalization + validation | Direct | Legacy aliases folded, ids re-derived from source file; warnings are gate failures | Task 3 | `tests/test_projection.py::test_build_warning_fails_the_gate` | yes |
| 18 | Per-tier replace on re-extraction | Direct | Explicit `ast_sources`/extracted provenance so a code re-extract cannot wipe the document layer | Task 3 | `tests/test_projection.py::test_explicit_provenance_prevents_cross_file_wipes` | yes |
| 19 | Shrink guard / explicit-overwrite refusal | Direct | `dedup=False` on the guarded build so the shrink guard stays meaningful | Task 3 | `tests/test_projection.py::test_shrink_guard_blocks_silent_loss` | yes |
| 20 | Entity/label deduplication | Direct | Off the default deterministic path; any dedup pass is audited and pinned | Task 3 | `tests/test_projection.py::test_dedup_disabled_on_guarded_build` | yes |
| 21 | Orphan/stale-source sweep | Direct | Plus a product junk-node classifier as the last build step | Task 3 | `tests/test_read_model_validator.py::test_junk_node_classifier` | yes |

### 3.2 Graph construction, schema and atomic writes

| # | Feature | Verdict | Adaptation for SOUL | Owner | Named contract test | Covered? |
|---|---|---|---|---|---|---|
| 22 | Graph schema/version provenance | **Partial** | Accepted `schema_version` range pinned, unknown version fails closed, manifest binds schema + content hash, `built_at_commit`/date stripped before hashing. **Residual:** Graphify ships no reader-side migration code — a future incompatible schema needs a product migration path | Task 3 (row), residual owner Task 3 | `tests/test_read_model_validator.py::test_unknown_schema_version_fails_closed` | yes |
| 23 | Deterministic serialization + atomic writes | **Partial** | Canonical one-line JSON, sorted nodes/links/hyperedges, same-directory temp + `os.replace`, plus fsync and a CAS manifest for the whole generation. **Residual closed 2026-10-25 (SUB-29, Task 9 lineage):** whole-set durability + power-loss recovery — every staged backup file fsynced before activation, two-phase backup activation that never destroys the old backup (trash-sibling + `recover_backup_activation`), crash-temp sweep with live-pid guard; proven by real process-kill probes at every critical point (evidence: `docs/recovery/row-23-whole-set-durability-power-loss.md`, `tests/test_power_loss_recovery.py` 12/12 GREEN) | Task 3 (row), residual owner Task 9 | `tests/test_projection.py::test_atomic_activation_is_fsynced_and_manifest_gated` | yes |
| 24 | Idempotent export writes | Direct | Every product artifact goes through the idempotent atomic writer (unchanged artifacts keep mtime/inode) | Task 3 | `tests/test_projection.py::test_unchanged_artifact_is_not_rewritten` | yes |
| 25 | Protected-artifact backup before overwrite | Direct | Back up the whole generation and verify it before replacing the live pointer | Task 9 | `tests/test_operations.py::test_pre_apply_backup_is_verified` | yes |

### 3.3 Clustering and communities

| # | Feature | Verdict | Adaptation for SOUL | Owner | Named contract test | Covered? |
|---|---|---|---|---|---|---|
| 26 | Leiden → graspologic → Louvain fallback | Direct | One pinned partitioner per generation recorded in the manifest; `graspologic` extras stay out of the locked environment | Task 3 | `tests/test_projection.py::test_pinned_partitioner_recorded_in_manifest` | yes |
| 27 | Canonical partition input | Direct | Canonical node/edge ordering with `PYTHONHASHSEED=0`; cross-seed determinism asserted | Task 3 | `tests/test_projection.py::test_partition_input_canonical_across_hash_seeds` | yes |
| 28 | Stable community IDs and remap | Direct | Community ids stay internal; published membership signatures and labels only | Task 3 | `tests/test_projection.py::test_community_ids_internal_and_signed` | yes |
| 29 | Deterministic hub labels + membership signatures | Direct | Curated labels only; hub labels are placeholders kept out of the reviewed file | Task 3 | `tests/test_projection.py::test_hub_labels_are_placeholders` | yes |
| 30 | Hub exclusion + AMBIGUOUS-edge downweight | Direct | One documented policy per generation, recorded in the manifest; ambiguity scaling for low-confidence relations | Task 3 | `tests/test_projection.py::test_hub_policy_is_recorded` | yes |
| 31 | Cohesion scoring | Direct | Structural diagnostic only, never presented as knowledge quality | Task 3 | `tests/test_projection.py::test_cohesion_is_not_a_quality_score` | yes |

### 3.4 Report, export and retrieval

| # | Feature | Verdict | Adaptation for SOUL | Owner | Named contract test | Covered? |
|---|---|---|---|---|---|---|
| 32 | Human-readable audit report | **Adapter** | Date/commit/token placeholders stripped before hashing; thin/shown counts recomputed by the validator | Task 3 | `tests/test_projection.py::test_report_bytes_are_date_independent` | yes |
| 33 | Portable report/export headers | Direct | No absolute checkout path in any published artifact | Task 3 | `tests/test_read_model_validator.py::test_no_absolute_checkout_paths` | yes |
| 34 | `graph.json` read model | Direct | Always loaded through the pinned Graphify loader so arc direction and confidence backfill are restored | Task 3 | `tests/test_read_model_validator.py::test_direction_and_confidence_restored` | yes |
| 35 | HTML/Obsidian/wiki export | Direct | Derived, generation-keyed views only; never a source of truth | Task 3 | `tests/test_projection.py::test_html_view_is_generation_keyed` | yes |
| 36 | GraphML/Cypher/SVG/canvas/DB push | Direct (DB push excluded) | GraphML/SVG may be published into the generation; DB sinks and credential-on-argv paths are excluded and asserted absent. Denominator note: the DB-push sub-surface leaves the denominator exactly once, via §4; row 36 itself stays in the denominator of 40 and is counted covered on its applicable export scope | Task 9 (co-owner Task 8: MCP parity) | `tests/test_security.py::test_no_db_sink_and_no_credentials_on_argv` | yes |
| 37 | Query / path / explain retrieval | Direct | A5: root-scoped adapter over library primitives; deterministic ranking, ambiguity refusal, bounded traversal, own byte/token budget; never CLI text | Task 4 | `tests/test_retrieval.py::test_bounded_traversal_and_typed_ambiguity` | yes (verified fresh 2026-10-25 @ff06e3d, SUB-33: named test PASSED, exit 0) |
| 38 | Token-reduction benchmark | **Partial** | Frozen `ceil(utf8_bytes/4)` estimator over the complete canonical payload, response truncation and no-match accounting preserved. **Residual:** Graphify's module has no latency, quality, repeat-trial or interval suite | Task 4 (row), residual owner Task 10 | `tests/test_service.py::test_canonical_token_parity` | yes (verified fresh 2026-10-25 @ff06e3d, SUB-33: named test PASSED, exit 0) |
| 39 | Parallel-edge / multigraph compatibility | Direct | Explicit simple-graph decision for the knowledge graph; parallel-edge behaviour probed, not version-guessed | Task 3 | `tests/test_read_model_validator.py::test_multigraph_probe_and_parallel_edges` | yes |
| 40 | Security / path / size / redaction guards | Direct | A7: containment, per-generation size caps, control/bidi/zero-width sanitization and a secret scan over the published generation; redaction treated as defense-in-depth, never a guarantee | Task 9 | `tests/test_security.py::test_containment_caps_and_generation_secret_scan` | yes |

Owner totals (primary owner per row): **Task 3 = 35 rows**, **Task 4 = 2 rows** (37, 38), **Task 9 = 3 rows** (25, 36, 40). Task 8 co-owns three rows as the MCP-parity surface (22, 36, 40) in addition to mandatory adapter A6. Rows 22, 23 and 38 are the three partials: they carry the row owner and an explicit residual owner (Task 3, Task 9, Task 10) and are excluded from the 37 verified rows until the residual is closed and the named contract test passes.

Task 3 verification (2026-10-25): rows 1–22, 24, 26–35 and 39 pass their named contract tests. Row 22's residual is **closed at Task 3**: the event log is the source of truth, so an incompatible future schema is migrated by a deterministic rebuild from zero into a new content-addressed generation (spec section 7.10), while readers fail closed on unknown schema versions. Row 23's residual (whole-set durability and power-loss recovery) remains open with Task 9.

Task 9 verification (2026-10-25, audit wave-1 fix round): rows 25, 36 and 40 now pass their named contract tests (`tests/test_operations.py::test_pre_apply_backup_is_verified`, `tests/test_security.py::test_no_db_sink_and_no_credentials_on_argv`, `tests/test_security.py::test_containment_caps_and_generation_secret_scan`). Row 36 note: `--api-key` (cli.py / mcp_server.py serve flag, docs/operations.md) is the single sanctioned auth-bearing argv option, pinned by the named test; the DB-push credential paths the row excludes remain absent. Row 23's residual stays open with Task 9.

MCP parity rows for Task 8 (the same ten operations through MCP, no extra surface): `tests/test_mcp_server.py::test_exactly_ten_tools_with_strict_schemas`, `tests/test_mcp_server.py::test_fixed_root_containment_rejects_project_path`, `tests/test_mcp_server.py::test_capabilities_are_exactly_tools_and_resources`, `tests/test_mcp_server.py::test_no_sampling_prompts_logging_or_completion_handlers`.

## 4. Explicit exclusions (out of the denominator, with reasons)

Excluded items are **not** counted as covered or uncovered; if any becomes a product requirement it moves into the denominator and must be re-scored.

| Excluded surface | Evidence | Reason |
|---|---|---|
| Code AST extraction for languages beyond markdown (~40 grammars) | `graphify/extract.py`, `tests/test_partial_cache.py:153-172` | The product is not a code-search product (spec section 1); only the markdown/document layer is applicable |
| Model semantic extraction, provider extras and `[all]` (`openai`, `anthropic`, `gemini`, `kimi`, `ollama`, `bedrock`, `tiktoken`, `boto3`) | `graphify/llm.py`, `graphify/pyproject.toml` extras | Requires an API key or downloads model assets; forbidden by the no-model boundary (ADR 0001). Replaced by adapter A1 |
| Transcription/video/PDF semantic ingestion (`faster-whisper`, `yt-dlp`, `pypdf`, `markdownify`) | `graphify/transcribe.py:14,145-151` | Downloads model assets and network media |
| Database push sinks (Neo4j/FalkorDB) and `psycopg` sinks | `graphify/export.py:27`, `graphify/exporters/graphdb.py:1-120` | Outbound external store; SQLite is the source of truth (ADR 0002) |
| MCP PR tools `list_prs`, `get_pr_impact`, `triage_prs`, and the `gh` ecosystem | `graphify/serve.py:2391-2435`, `graphify/prs.py:141-154` | Outbound network, ambient credentials, non-deterministic external state; PR/`gh` surfaces are forbidden (spec section 13.1) |
| Agent hook/install ecosystem (`graphify/install.py`, `graphify/hooks.py`, `graphify/skill*.md`) | research artifact §3 exclusions | Writes host/agent state; the product never modifies Hermes or host Graphify |
| Code-navigation exports (`graphify/callflow_html.py`, `graphify/tree_html.py`) | research artifact §3 exclusions | Aimed at code navigation, not knowledge artifacts |
| Importing `graphify.serve` wholesale; Graphify's HTML/report exporters as the read model; `project_path`; raw `graphify://` resources | `graphify/serve.py:16-31`, `graphify/serve.py:2267-2274`, `graphify/serve.py:2711-2772` | Pulls provider/PR/network modules into the process and exposes unsanitized, uncapped, cross-tree content |
| Global cross-repo merge for multi-repo codebases | research artifact §3 exclusions | Single-writer knowledge corpus; multi-repo merge is out of scope |

## 5. Cross-cutting gap and residual risks

1. **Atomic projection sets (not a row in the 40).** Graphify makes each file write atomic, but a rebuild publishes `graph.json`, `GRAPH_REPORT.md`, labels, signatures and analysis as separate writes. A crash between them is a torn read model. Adapter A3 owns this; the rollback and recovery side is Task 9.
2. **Private Graphify primitives.** Adapter A5 depends on private library functions (`_query_graph_text`, `_score_query`, `_resolve_single_node`, `_shortest_path_text`). The pin makes this viable; an upgrade must re-run golden fixtures, and if the runtime guard rejects private imports the matcher/traversal must be reimplemented rather than scraping CLI text.
3. **Lexical relevance is not professional quality.** P/M/E remain the quality signals; retrieval relevance never substitutes for them (spec sections 8–9).
4. **Coverage claim discipline.** The 92.5% figure is a *ceiling* that requires 37 passing named contract tests plus zero residual-partial closure. Until then the only permitted number is the verified count.
5. **Dormant `graphify-mcp` console script — recorded boundary (Task 1 fix round).** The pinned `graphifyy==0.9.95` wheel ships two console scripts: `graphify = graphify.__main__:main` and `graphify-mcp = graphify.serve:_main`. The runtime entry-point policy now **fails closed** on the second: any entry point whose target module is a forbidden import root (including `graphify.serve`) or whose console-script name is `graphify-mcp` or a provider name is rejected. The wheel cannot be changed without forking upstream, so this one entry point is a *recorded* boundary rather than a silently dropped rule: `verify_runtime()` reports it as `pinned_entry_point_boundary`, `blocking_forbidden_entry_points()` excludes only that exact value, and `assert_no_forbidden_entry_points()` stays strict and raises on it. Residual owner: the next Graphify pin review (ADR 0001 §3 and its review triggers). It is never on the product surface and no row of the 40 depends on it; any *other* forbidden entry point from a future wheel is a hard failure, not a boundary.