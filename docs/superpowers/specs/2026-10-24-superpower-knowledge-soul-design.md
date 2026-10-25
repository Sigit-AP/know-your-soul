# Super Power Knowledge SOUL Graph — Design Specification

Date: 2026-10-24
Status: Waiting for user approval before implementation

## 1. Intent and final goal

Build a standalone, local-first knowledge and learning system for Hermes Agent. It converts actions, decisions, outcomes, and lessons into a versioned knowledge graph with auditable dynamic scoring and controlled improvement. It is not a code-search product and is not part of the Hermes Agent repository.

The system must be deterministic. It must not load model assets, provider dependencies, or model credentials; make model/embedding/vector calls; generate outbound provider traffic; or register MCP sampling handlers/capabilities. Hermes Agent is an external consumer that supplies its own model and calls the system's tools. The enabled runtime surface is protected by locked dependencies, forbidden-import and entry-point guards, a scrubbed environment, and outbound-socket tests.

## 2. Verified references

- Graphify repository: https://github.com/Graphify-Labs/graphify
- Graphify inspected commit: `92741d162b2094ffc2fed58cfbd8ee823e9da62c`
- Graphify inspected package version: 0.9.95, branch `v8`, Python library and CLI, NetworkX node-link graph, deterministic query/path/explain, confidence-tagged edges, MCP stdio/HTTP, caching, visualization, and local-first operation.
- Hermes repository: https://github.com/NousResearch/hermes-agent
- Hermes inspected commit: `a480a27e76dcad8da7665408e54c6ec270c10f14`
- Hermes native MCP supports first-class direct tools over stdio and Streamable HTTP. MCP sampling is optional and will not be implemented or advertised by this system.
- Host Graphify is 0.9.91. It must remain unchanged; the project uses an isolated environment pinned to 0.9.95.

The 90% target means at least 90% of the approved, applicable positive Graphify capability inventory is adopted or wrapped. It does not mean copying 90% of the source code. Code-only AST language extractors and AI-only semantic backends are outside the applicable inventory and must be explicitly marked excluded with reasons. The initial matrix assigns every applicable feature to an owning implementation task and a named contract test. A feature counts as adopted only after its adapter and contract test pass. Coverage is calculated as `verified_adopted_applicable_features / total_applicable_features`. No coverage claim is allowed without the feature matrix and fresh test evidence.

## 3. Users and value

Primary user: Hermes Agent and its operator.

Before: lessons and operating knowledge are dispersed across raw documents and histories, requiring repeated reading and offering no consistent quality measurement or improvement lifecycle.

After: a caller can use a fixed tool contract to retrieve a scoped primary-node context, record events and outcomes, inspect evidence and scores, and manage versioned improvements without an internal model call or a whole-corpus read.

## 4. Non-negotiable constraints

1. New standalone repository at `/root/superpower-knowledge-soul`; never edit Hermes source, the existing SOUL corpus, or the global Graphify installation.
2. No model assets, provider dependencies, credentials, model/embedding/vector calls, outbound provider traffic, or MCP sampling handlers/capabilities. Graphify's installed distribution may contain dormant optional provider-integration source, but it is outside the enabled runtime surface: provider extras and entry points are forbidden, forbidden imports fail closed, the process environment is scrubbed, and outbound sockets are denied in verification. If this operational boundary is unacceptable, replace Graphify with a reviewed deterministic-only distribution before implementation rather than silently weakening the rule.
3. All scoring, routing, retrieval, logging, scheduling, validation, and status transitions are deterministic and reproducible.
4. Every tool action and decision—including queries, evaluations, mutations, and rollbacks—has an append-only audit event and returns its audit event ID.
5. Every node has provenance, evidence references, plus/minus assessments, P/M/E scores, uncertainty, version, status, and relations.
6. Quality is never traded away for speed or token savings. Promotion is blocked below the configured professional-quality gate.
7. Semantic interpretation or rewriting, when wanted, happens in Hermes outside this system. Hermes submits explicit structured content or patch proposals through tools; this system validates and stores them.
8. The global plan remains stable during defect repair. A failed audit creates a local recovery plan linked to the same global objective and acceptance criteria.
9. Implementation uses TDD, fresh execution evidence, independent audit, regression tests for every fixed defect, and tested rollback.

## 5. Architecture decision

Use a Python 3.14 modular monolith with one package and one database. Reuse `graphifyy[mcp]==0.9.95`; do not fork it. Use SQLite in WAL mode as the source of truth for immutable events, node revisions, evidence, scores, and improvement records. Project Graphify-compatible `graph.json`, `graph.html`, and `GRAPH_REPORT.md` as read models using atomic replacement.

Python is selected because Graphify's supported library, query engine, MCP implementation, NetworkX, NumPy, and the official Python MCP SDK directly cover the required workload. Adding Rust or TypeScript now would increase operational and interface cost without measured benefit. A native extension is allowed later only after a profile and benchmark demonstrate a material bottleneck.

Three access paths share the same application service:

1. CLI: `soul-knowledge` for direct local commands.
2. Local API: persistent Starlette/Uvicorn service, Unix socket by default and loopback HTTP as an option.
3. MCP: official Python MCP SDK over stdio, with Streamable HTTP as an option. No sampling capability and no model configuration.

Docker is packaging only, not a fourth access path and not required for local use.

## 6. Knowledge model

Hierarchy:

`Knowledge Root -> Primary Node -> Child Node -> Branch Node -> Leaf Knowledge Node`

Exactly seven primary domains:

- P1 Pembelajaran
- P2 Gagasan
- P3 Tindakan
- P4 Keputusan
- P5 Pemikiran/Penalaran
- P6 Disiplin
- P7 Level Profesional

Every primary node owns its subtree's integrity and aggregates its own evidence plus descendants' contributions. Aggregation must not hide low-scoring or contradictory children.

Supported relations include `parent_of`, `derived_from`, `supports`, `contradicts`, `causes`, `learned_from`, `applied_in`, `supersedes`, `duplicates`, and `depends_on`.

Minimum node fields: stable ID, parent ID, node kind, primary domain, payload, summary, provenance, context tags, scope, created/updated/last-used timestamps, evidence references, plus/minus entries, P/M/E, confidence/uncertainty, professional level 1–5, version, lifecycle status, and relations.

Store concise auditable rationales, assumptions, alternatives, and outcomes. Do not store secrets or require raw private chain-of-thought.

## 7. Event and projection flow

1. After envelope parsing, the service creates a durable invocation journal with a new `invocation_id` and `audit_event_id`, operation, request hash, actor, idempotency key, and `started_at`. The journal is operational recovery state, not an append-only audit event.
2. Authorization, domain validation, node references, idempotency, and expected versions are evaluated. Every valid invocation, including one that fails validation or projection, receives exactly one final append-only audit event.
3. A mutation transaction appends the domain event and updates revision metadata. Domain events and audit completion events share the global monotonic `event_seq`; only knowledge-affecting domain events also receive a monotonic `knowledge_seq`. Audit-only/read events never advance `knowledge_seq`.
4. A single serialized projector consumes knowledge-affecting events in `knowledge_seq` order. It publishes a content-addressed generation and manifest containing `projection_version`, `source_knowledge_high_watermark`, and graph content hash; an atomic `CURRENT` pointer activates it only after all files and the manifest are durable. Older knowledge watermarks cannot replace newer ones.
5. At invocation start, a read captures `required_knowledge_watermark` from the latest committed `knowledge_seq`; this is its linearization point. A mutation uses its assigned `knowledge_seq`. Later mutations do not retroactively make an earlier read stale, while self-auditing reads never change the target.
6. Retrieval accepts only an active manifest whose graph hash validates and whose source knowledge watermark meets the request target. Responses carry `required_knowledge_watermark`, `source_knowledge_high_watermark`, `projection_version`, and `stale`. A stale read triggers synchronous repair and one retry; if still stale it returns a retryable unavailable error.
7. On success or failure, one transaction appends the invocation's single final audit event containing outcome/result references, error if any, projection outcome, duration, and correlation ID, then marks the journal complete. Response `audit_event_id` was allocated at start and identifies this final record.
8. On crash recovery, incomplete journals are reconciled using the idempotency key and domain records. The system completes or repairs projection, then appends exactly one final success/failure audit event before marking the journal complete. Journal replay never duplicates domain events or audit events.
9. Every mutation response contains the original `domain_event_id`, new invocation `audit_event_id`, final audit `event_seq`, original `node_version` and `knowledge_seq`, active `projection_version`, and `source_knowledge_high_watermark`. Every read/evaluation response contains its audit ID and projection metadata.
10. The graph can be rebuilt from zero from SQLite and immutable source imports. Atomic rename alone is not considered a consistency guarantee.

Idempotent retry deduplicates only the domain mutation. It returns the original `domain_event_id`, `node_version`, and `knowledge_seq`, with `idempotent_replay=true`; it does not create a new knowledge event or advance the knowledge watermark. Each retry is still a new invocation with a new `audit_event_id` and final audit `event_seq`, linked to the original domain result. Its projection metadata identifies the active generation after confirming that generation covers at least the original `knowledge_seq`.

The existing `/root/.hermes/knowledge/soul` corpus is a read-only optional seed source. Structured imports require explicit domain mappings. No semantic AI extraction is used.

## 8. Retrieval and direct tools

Required tools:

- `knowledge.query`
- `knowledge.get_node`
- `knowledge.get_subtree`
- `knowledge.log_event`
- `knowledge.record_outcome`
- `knowledge.evaluate`
- `knowledge.review`
- `knowledge.propose_improvement`
- `knowledge.apply_improvement`
- `knowledge.rollback`

A query supplies purpose, optional primary domain, context tags, and token budget. A supplied domain routes directly to its stable primary node. Otherwise deterministic lexical ranking selects candidate roots using Graphify query. Traversal is relevance- and dependency-bounded, deduplicated, and budget-limited. Responses include provenance, confidence, node versions, `audit_event_id`, projection high-water metadata, and truncation status.

The dependency-free budget unit is `estimated_tokens = ceil(utf8_byte_length / 4)`; it is not claimed to equal any provider tokenizer. The budget covers the complete canonical JSON `QueryResponse`, including provenance, audit/projection metadata, and truncation fields. CLI stdout, API JSON, and MCP tool content carry the same canonical payload; transport envelopes are excluded. Budgets below 128 estimated tokens return `BUDGET_TOO_SMALL` without knowledge payload. If mandatory metadata cannot fit, the result is an error rather than an over-budget success. Every successful response must satisfy the budget under this exact algorithm.

On completion of every valid tool invocation, including query/get/evaluate/review and failure paths, the service appends the invocation's single final audit event containing the operation, request hash, actor, started/completed timestamps, result references/outcome or error, projection outcome, duration, and correlation ID. Read audits are excluded from knowledge evidence so auditing cannot recursively change scores or knowledge watermarks.

A scheduler scans due evaluation metadata, not the agent. It emits candidates through `knowledge.review`; it never calls a model and never silently rewrites knowledge.

## 9. Mathematical scoring contract

Scores P, M, and E use decimal strings in `[0,1]` and remain separately visible. All calculations use Python `Decimal` with precision 34 and no binary-float conversion. Intermediate comparisons use full precision; serialized scores are quantized to 12 decimal places with `ROUND_HALF_EVEN`. Formula and parameter changes require a new scoring-contract version.

Each evidence item contains: `evidence_id`, `node_id`, `source_uri`, `observed_at`, `context_tags`, `independence_key`, `outcome` in `[-1,1]`, `correctness`, `reliability`, `reproducibility`, `scope_fit`, `successful_use`, `task_coverage`, `quality_at_use`, and `contradiction` in `[0,1]`; `utilization_u` in `{0.1,0.2,...,1.0}`; and `latency_ms > 0`, `compute_ms > 0`, and `output_chars >= 0`. Evidence without valid provenance is unscorable and reported separately.

For dimension `d`, evidence weight before normalization is:

`q_j,d = reliability_j * exp(-ln(2)*age_days_j/h_d) * context_relevance_j * independence_j / (1 + depth_j)`

`age_days_j` is the nonnegative integer microsecond difference between UTC `at` and `observed_at`, converted with `Decimal(microseconds)/(86400*1000000)`; evidence from the future is invalid. Defaults: `h_P=90`, `h_M=30`, `h_E=7` days. `depth_j=0` for the scored node and increases toward descendants. `independence_j=1/sqrt(group_size)` for evidence sharing an `independence_key`; exact duplicates are represented once. `context_relevance=1` for an empty query context, otherwise the Jaccard similarity between evidence and query context tags.

Before scoring, deduplicate exact duplicates by canonical content hash, retaining the lexicographically smallest UTF-8 `evidence_id`. Sort retained evidence by ascending UTF-8 `evidence_id` and use that single order for every Decimal sum, weighted sum, primary aggregation, and replay. For P and E, normalize with `w_j,d=q_j,d/sum(q_d)` over all eligible items for that dimension. Never replace missing evidence with a fabricated prior score.

P is the weighted mean of:

`p_j = 0.30*correctness_j + 0.25*((outcome_j+1)/2) + 0.20*reproducibility_j + 0.15*scope_fit_j + 0.10*(1-contradiction_j)`

Thus usage frequency alone cannot increase P. Contradictory, correlated, stale, and out-of-context evidence affects P through the defined terms rather than undocumented penalties.

M means maximum effective utilization under the quality gate, not raw usage count. For each utilization bin `u`, select only evidence in that bin with `q_j,M>0` and normalize locally: `w_j,u=q_j,M/sum(q_M in bin u)`. Then `Q(u)=sum(w_j,u*quality_at_use_j)` and `S(u)=sum(w_j,u*successful_use_j*task_coverage_j)`. The independent-item count is the number of distinct `independence_key` groups remaining after exact deduplication in that bin. A bin is eligible only with at least three distinct independence groups, nonzero local weight, and `Q(u)>=0.70`.

`M = max over eligible u of (u * S(u))`; if no bin is eligible, `M=0` and `selected_m_bin=null`. Ties are resolved toward the smaller `u`, then the lexicographically smallest contributing evidence-ID list.

`task_coverage` is `covered_required_task_items / total_required_task_items` for the same versioned task rubric; an empty denominator makes the evidence invalid. The selected bin and its evidence count must be returned.

E is quality-adjusted efficiency. For each evidence item:

`benefit_j = successful_use_j * task_coverage_j`
`cost_j = 0.4*min(2, latency_ms_j/1000) + 0.4*min(2, output_chars_j/4000) + 0.2*min(2, compute_ms_j/1000)`
`e_j = benefit_j/(benefit_j+cost_j)`, or zero if both are zero.

E is the weighted mean of `e_j`. Baselines are milliseconds and UTF-8 characters per completed knowledge operation, versioned by workload class; the defaults above are the initial `default` class. E never overrides the P gate.

For a dimension with no valid provenance-bearing evidence or with `sum(q_d)=0`, that dimension is treated as having zero eligible weighted evidence: its score is 0, `U_d=1`, and no weighted mean is evaluated. For M this also forces `selected_m_bin=null`; for P it forces professional level 1. The response reports separate counts for missing provenance, disjoint/out-of-context evidence, zero reliability, and other exclusions with reason `ZERO_TOTAL_WEIGHT` when applicable. If all three dimensions have zero total weight, `P=M=E=0` and overall uncertainty is 1. Otherwise scores and uncertainty remain independent per dimension. For nonzero total weight, `U_d=1/sqrt(1+sum(q_d))`. Overall uncertainty is `max(U_P,U_M,U_E)` and confidence per dimension is `1-U_d`, not an AI probability.

Primary aggregation recomputes the same formulas over the union of the primary node's own evidence and descendant evidence, deduplicated by `evidence_id`, with the depth factor above. Child scores are never averaged directly and low children remain inspectable.

Professional level starts from P: level 1 below 0.30; 2 at 0.30; 3 at 0.50; 4 at 0.70; 5 at 0.85. Evidence counts below mean distinct `independence_key` groups with `q_P>0` after exact deduplication. Caps then apply: fewer than 5 groups caps at 2; fewer than 10 caps at 3; fewer than 20 caps at 4. `U_P>0.70` caps at 2, `U_P>0.50` caps at 3, and `U_P>0.30` caps at 4.

The optional composite is `G=0` when `P<0.70`; otherwise `G=P^0.50 * M^0.30 * E^0.20`. Promotion additionally requires at least 10 distinct independence groups with `q_P>0` and `U_P<=0.50`. G never replaces the three primary scores.

The contract requires at least 30 versioned fixtures, including independently hand-calculated cases, and property tests for bounds, deterministic replay, recency, context relevance, duplicate/correlation resistance, contradiction handling, zero evidence, disjoint context, all-zero reliability, depth discount, M-bin local denominators, correlated-group thresholds, bin boundaries, E units, level caps, and the P gate. Permuting input evidence must produce byte-identical 12-decimal output after canonical sorting. Exact expected output uses the 12-decimal serialization rule.

## 10. Improvement lifecycle

Statuses: `maintain`, `observe`, `needs_improvement`, `ready_to_promote`, `merge_or_deduplicate`, `deprecate`, and `rollback`.

Transitions consider score trends, evidence count, uncertainty, severity, utilization, cost-benefit, regression risk, cooldown, and hysteresis. One event cannot cause promotion.

Flow: `detect -> propose -> review -> test -> version -> apply -> measure -> retain/rollback`.

A proposal is an explicit structured diff with expected version and rationale. Validation, regression checks, and approval policy run before apply. Every revision is immutable and reversible. High-impact changes require operator approval. Automatic semantic rewriting is out of scope.

## 11. Acceptance criteria

1. All seven domains support create/import, query, subtree retrieval, outcome recording, evaluation, improvement, and rollback.
2. Every valid invocation, including validation/projection failure and crash recovery, produces exactly one final audit event with outcome and duration; idempotent retries create new linked audit IDs without duplicating domain events.
3. Graph and audit data survive restart; projection rebuild reproduces the current state.
4. P/M/E formulas pass unit, property, replay, calibration, and boundary tests.
5. The locked transitive environment contains no provider/vector/model packages; runtime guards reject forbidden imports/entry points and provider credentials, scrubbed-process tests make no outbound sockets, and MCP exposes no sampling handler/capability.
6. CLI, local API, and MCP expose exactly ten knowledge operations through the same contract and produce exactly one final audit event per valid invocation.
7. Hermes discovers and calls MCP tools without changes to Hermes source and without model credentials in the MCP configuration.
8. Successful query responses satisfy the frozen UTF-8/4 budget for the complete canonical payload and include provenance, audit/projection metadata, and truncation status; undersized budgets return the specified error.
9. Applicable Graphify feature coverage is at least 90%, with exclusions and calculation shown.
10. Benchmarks report cold/warm latency, throughput, memory, output tokens, and quality/regression results without invented targets or results.
11. Backup, restore, migration, concurrent access, stale projection recovery, and rollback are tested.
12. Final release has zero unresolved launch-blocking audit findings; lower-severity residual findings are listed explicitly.

## 12. Global delivery and rebuild loop

The global sequence is research/specification -> core -> retrieval -> scoring -> improvement -> access paths -> integration -> audit -> release.

After each build increment: run tests, then an independent audit/review before the next task starts. If a defect or gap is found, write a local recovery plan containing evidence, severity, root cause, affected global acceptance criteria, smallest corrective change, regression probe, and rollback. Rebuild and repeat build-audit-review. A repaired launch blocker can close only after independent re-verification of its regression probe and finding closure. Do not replace the global objective or silently narrow acceptance criteria.

Stop a repair branch and escalate if three iterations produce no new verified improvement, a requirement conflict appears, or the repair would require changing Hermes source or adding an internal model. Do not loop indefinitely.

## 13. Deep-research adaptation contract

The four cited research artifacts under `docs/research/` are the evidence base for this section. Applicable Graphify feature coverage is 92.5% (37/40 after mandatory adapters), with three explicit partials: benchmark scope, merge/publication integrity, and schema migration. Product adapters below are mandatory, not optional.

### 13.1 Retrieval and access

- A supplied primary domain routes directly to its stable root. There is no global keyword fallback when the root is missing.
- Traversal has an explicit relation allowlist plus maximum nodes, edges, and depth. Graphify's render budget alone does not bound traversal.
- Ranking uses stable node IDs, aliases, summaries, and context tags. Source paths and provenance contribute zero relevance. IDF is computed over every searchable knowledge field.
- Ambiguity and no-match are typed results; a tie is never resolved to the first graph-order node.
- The adapter loads through the pinned Graphify compatibility helpers, but never shells out to `graphify query` and parses rendered text.
- Graph caches key on graph content hash plus projection version, never `(mtime_ns, size)`.
- The MCP graph root is one startup-configured allowlisted root. Caller-supplied `project_path` is rejected. Every path resolves inside that root, symlinks are re-verified, and cross-tree reads fail closed.
- Tool schemas reject unknown properties, missing keys, wrong types, string booleans, out-of-range depth/mode/budget, and return `ToolError` with `isError:true`.
- Traversal depth is `[1,6]`, mode is `{bfs,dfs}`, and budget is an integer `[128, configured_max]`.
- The complete canonical payload is sanitized: control, bidi, and zero-width characters are stripped, lengths and resource sizes are capped, and output is JSON-encoded. Raw Graphify reports are never exposed.
- MCP exposes tools and resources only. Capabilities are exactly `tools` and `resources`; sampling, prompts, logging, completions, PR tools, `gh`, provider imports, and DB push surfaces are forbidden.

### 13.2 Conditional Hermes access and recommendation contract

Conditional access is a hash-pinned local policy artifact, so deciding whether to call the system never costs a model call or a graph call. L1 surfaces (greeting, one fact lookup, one command, a file read, a status check) never call the system. Ambiguous cases may call `knowledge.query` with `access_only=true`, capped at 160 estimated tokens with zero knowledge payload. No eleventh public tool is added.

When knowledge is needed, `knowledge.query` and `knowledge.review` return a recommendation block with exactly three lists: `best_actions`, `recommendations`, and `unnecessary_actions`. Every item carries source node IDs and versions, evidence IDs, learned class, reason codes, and expiry. The response never returns raw SOUL or corpus text. One observation can never promote an action or a node.

### 13.3 Source versions and relearn

A source-version registry stores SHA-256, byte/char/line counts, manifest hash, parser/normalization/engine/scoring versions, `superseded_by`, and deletion state. Every node and evidence item carries `source_version_id`. Version identity is hash based; mtime is never a correctness gate.

When SOUL.md or its corpus changes, the system emits a deterministic `RelearnOffer` and sets `RELEARN_PENDING`. It never chooses a mode automatically. No operator decision means no change.

- RESET starts with zero old evidence; P=M=E=0, U=1, level 1, and promotion blocked until new evidence exists.
- ACCUMULATE reuses only nodes whose anchors map deterministically. Evidence is carried only where the approved mapping still holds; unmapped evidence is quarantined from scoring.
- Supersession uses valid-from/valid-to relation fields with audit retention, never deletion. Contradictions are retained in both directions and resolved by the frozen signed recency rule or left `needs_improvement` on a tie.
- Default scoring, recommendation, and promotion exclude evidence incompatible with the active source manifest. A mixed response requires explicit operator approval and `mixed_source_versions=true`; otherwise it fails with `MIXED_SOURCE_VERSIONS`.
- Relearn, source-version, backup, and rollback events carry zero evidence weight. Only `log_event`, `record_outcome`, and `evaluate` can move P/M/E, and relearn can never trigger promotion.
- Every apply takes a verified atomic backup of SQLite, generation directory, manifest, `CURRENT`, source registry, and scoring-contract version. Rollback restores exact pre-relearn hashes, graph bytes, and P/M/E values with exactly one linked audit event.

### 13.4 Token and quality measurement

Token savings are a paired measurement with a quality gate. The estimator is always `ceil(utf8_bytes/4)` over the complete canonical payload. Frozen initial baselines measured from the host are: SOUL.md 31,279 bytes / 7,820 estimated tokens; SOUL corpus 52,604 bytes / 13,151 estimated tokens. No provider-tokenizer equality is claimed. A case that loses quality is rejected even when it saves tokens.

### 13.5 Deterministic generation and lifecycle

The Graphify pipeline is wrapped, not trusted as a publication mechanism. A deterministic local fragment generator replaces model-backed semantic extraction and emits Graphify's schema with file-scoped IDs. One writer process runs per corpus root, `dedup=False` on the guarded build, explicit prune sources, and one pinned partitioner with `PYTHONHASHSEED=0`.

Publication uses a generation directory plus one atomic `CURRENT` manifest binding graph JSON, report, labels, signatures, analysis, corpus high-water, schema version, partitioner version, and content hashes. Non-deterministic fields (dates, commit stamps) are excluded before hashing. Readers fail closed on an unknown schema version. A product-owned read-model validator checks endpoints, file types, confidences, absolute-path leakage, secrets, and junk nodes. The doc-change watcher calls the deterministic update pipeline because stock Graphify only flags document changes.

## 14. Pre-mortem

Tiger — mathematically precise but meaningless scores. Mitigation: calibration fixtures, uncertainty, sensitivity analysis, blind cases, and no promotion on insufficient evidence.

Tiger — stale/corrupt read model during concurrent writes. Mitigation: SQLite WAL, idempotency, expected versions, atomic projection, stale detection, rebuild-from-events tests.

Tiger — Graphify upgrade or coupling breaks the product. Mitigation: pin version, adapter boundary, contract tests, isolated environment, and no global installation changes.

Elephant — the definition of M may not match the operator's intention. Current interpretation is maximum useful utilization under the quality gate; it must be confirmed at approval or adjusted without changing the global architecture.

Paper tiger — using only Python is inherently less professional. The selection is based on verified reuse and lower maintenance cost; a faster language is justified only by measured need.
