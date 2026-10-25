# Super Power Knowledge SOUL Graph Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone deterministic knowledge graph, audit, scoring, and controlled-improvement service that Hermes accesses directly without any internal AI model or model API.

**Architecture:** Python 3.14 modular monolith using pinned Graphify for graph retrieval/projection and SQLite WAL as the immutable event and revision source of truth. CLI, persistent local API, and MCP adapters call one application service; graph files are atomically projected read models.

**Tech Stack:** Python 3.14, `graphifyy[mcp]==0.9.95`, SQLite, NetworkX, NumPy, Pydantic v2, Starlette/Uvicorn, official Python MCP SDK, pytest, Hypothesis, Pyright, Ruff.

**Spec:** `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`

## Global Constraints

- Project root is `/root/superpower-knowledge-soul`; never modify Hermes source, the existing SOUL corpus, or global Graphify.
- No model assets, provider dependencies/credentials, model/embedding/vector calls, outbound provider traffic, or MCP sampling handlers/capabilities; dormant optional source in Graphify is excluded by forbidden-import/entry-point guards, a scrubbed environment, and outbound-socket tests. If this boundary is rejected, stop and use a reviewed deterministic-only distribution.
- Seven primary domains and all node/evidence/score/provenance requirements are defined by the spec.
- P, M, and E remain separate decimals in `[0,1]`; P is a hard quality gate.
- Every tool action and decision, including reads and evaluations, is append-only and returns an audit event ID; every mutation is versioned and idempotent.
- The deep-research adaptation contract in spec section 13 is binding: one allowlisted graph root, strict schemas/ranges, bounded traversal, canonical sanitization, no `project_path`, no PR/`gh`/provider surfaces, tools+resources-only MCP.
- Conditional access is hash-pinned; L1 never calls the system and `access_only` returns zero knowledge payload within 160 estimated tokens.
- Recommendation responses contain exactly `best_actions`, `recommendations`, and `unnecessary_actions`, never raw SOUL text, and one observation cannot promote.
- Source identity is content-hash based and every node/evidence item is source-version scoped. SOUL changes produce `RELEARN_PENDING`; RESET or ACCUMULATE is operator-gated and reversible. Relearn events carry zero evidence weight.
- Default reads never mix incompatible source versions; mixed mode requires explicit approval.
- Generation publication binds all artifacts through one atomic `CURRENT` manifest with content hashes, schema/partitioner versions, and a corpus high-water.
- Use TDD and keep each task independently reviewable; every audit fix adds a regression probe.
- Defect repair uses a local recovery plan without changing global acceptance criteria.
- No completion claim without fresh command output and exit codes.

## Review Focus

- Duplicate or replayed event: preserve the original domain result, issue a new linked audit ID, and never double-count evidence; pinned by Task 2 and Task 5 tests.
- Concurrent writers and interrupted projection: preserve event order and recover a current graph after restart; pinned by Task 2 and Task 9 tests.
- Empty/contradictory/correlated evidence: bounded scores, explicit uncertainty, and no promotion; pinned by Task 5 tests.
- Cross-tree/symlink graph read, unknown properties, wrong types, string booleans, `project_path`, or out-of-range depth/budget: all fail closed with typed errors; pinned by Tasks 8 and 9 tests.
- SOUL source change with no operator mode, incompatible source mixing, or relearn attempting promotion: no change, no mixing, no promotion; pinned by Tasks 2 and 6 tests.
- Evidence permutation, junk nodes, torn generation artifacts, or non-deterministic report fields: canonical output or a loud gate failure; pinned by Tasks 3 and 5 tests.
- Ambiguous domain or oversized query: no guessed primary root and no response over budget; pinned by Task 4 tests.
- MCP client sends sampling or provider-like configuration: server neither advertises nor uses it; pinned by Task 8 and Task 10 tests.

## Mandatory Gate for Every Task

After the task's tests pass and before its commit/next task, dispatch an independent reviewer with the spec, task diff, and fresh test evidence. Record findings in `docs/reviews/task-<N>.md`. For every finding, create `docs/recovery/<finding-id>.md`, implement the smallest corrective change, add its regression probe, and rerun the task gate. Close a launch blocker only after a fresh independent reviewer verifies the fix. Three recovery iterations without new verified improvement, a requirement conflict, or a violation of a global constraint stops execution for operator escalation.

---

### Task 1: Reference Matrix and Architecture Contracts

**Files:**
- Create: `docs/research/graphify-feature-matrix.md`
- Create: `docs/research/hermes-mcp-contract.md`
- Create: `docs/adr/0001-standalone-deterministic-monolith.md`
- Create: `docs/adr/0002-sqlite-events-graphify-read-model.md`
- Create: `pyproject.toml`
- Create: `uv.lock`
- Create: `src/soul_knowledge/runtime_guard.py`
- Test: `tests/test_dependency_policy.py`
- Test: `tests/test_no_ai_runtime.py`

**Interfaces:**
- Consumes: the four cited artifacts in `docs/research/`, inspected Graphify commit `92741d162b2094ffc2fed58cfbd8ee823e9da62c`, and Hermes commit `a480a27e76dcad8da7665408e54c6ec270c10f14`.
- Produces: approved 40-feature inventory, applicable-feature denominator of 40, mandatory adapter list, dependency policy, and exact project packaging used by all tasks.

- [ ] Write failing dependency-policy and runtime-guard tests asserting Python 3.14, the pinned locked transitive environment, absent provider/vector/model packages and credentials, forbidden provider imports/entry points, scrubbed process environments, denied outbound sockets, and absent MCP sampling handlers/capabilities.
- [ ] Run `pytest tests/test_dependency_policy.py tests/test_no_ai_runtime.py -q`; expect FAIL because packaging and guards do not exist.
- [ ] Create packaging, lock the transitive environment, and implement the fail-closed runtime guards before any feature adapter is enabled.
- [ ] Create `docs/research/graphify-feature-matrix.md` from the 40-feature inventory: 37 fully covered after mandatory adapters, 3 partial with residual owners, explicit exclusions (code AST beyond markdown, model semantic extraction, DB sinks, PR/gh, hooks/install, code-navigation exports), and per-row contract-test ownership.
- [ ] Record that research depth is 100% on the eight-area rubric and that adoption coverage may not be claimed above the verified contract-test total. Assign every applicable row to Tasks 3, 4, 8, or 9.
- [ ] Record the no-model boundary, Python selection, alternatives rejected, rollback, and Hermes MCP no-sampling contract in the ADRs.
- [ ] Run `pytest tests/test_dependency_policy.py tests/test_no_ai_runtime.py -q`; expect PASS. Do not claim implemented Graphify coverage in this task.
- [ ] Commit `docs: lock reference coverage and deterministic architecture`.

### Task 2: Immutable Event Store and Versioned Domain Schema

**Files:**
- Create: `src/soul_knowledge/domain.py`
- Create: `src/soul_knowledge/events.py`
- Create: `src/soul_knowledge/store.py`
- Create: `src/soul_knowledge/audit.py`
- Create: `src/soul_knowledge/source_registry.py`
- Create: `src/soul_knowledge/migrations/0001_initial.sql`
- Test: `tests/test_domain.py`
- Test: `tests/test_event_store.py`

**Interfaces:**
- Produces: `PrimaryDomain` enum with seven stable IDs; `Node`, `Evidence`, `Event`, and `ScoreVector` Pydantic models.
- Produces: `EventStore.append_domain(event: Event, *, idempotency_key: str, expected_version: int | None) -> DomainReceipt`.
- Produces: `DomainReceipt(domain_event_id, event_seq, node_version, knowledge_seq, idempotent_replay)`; domain replay preserves these original values and never advances `knowledge_seq`.
- Produces: `AuditLog.begin(invocation: Invocation) -> InvocationJournal` and `AuditLog.complete(invocation_id: str, outcome: InvocationOutcome) -> AuditReceipt`, writing exactly one final append-only audit event for success or failure.
- Produces: `WriteReceipt` with the original domain fields, a new per-invocation `audit_event_id`/final audit `event_seq`, active `projection_version`, and `source_knowledge_high_watermark`.
- Produces: `SourceRegistry.observe(path, kind, parser_versions) -> SourceVersion` with SHA-256/manifest hash/counts, `active()`, `supersede()`, `mark_deleted()`, and `detect_change() -> RelearnOffer | None`; sets `RELEARN_PENDING` and never selects a mode.
- Produces: `EventStore.replay_domain() -> Iterator[Event]`, `current_node(node_id: str) -> Node`, `event_high_watermark() -> int`, and `knowledge_high_watermark() -> int`.

- [ ] Write failing tests for seven domains, required node/evidence `source_version_id`, hash-based source versions, manifest hash/count drift detection, `RelearnOffer` on change with `RELEARN_PENDING` and unchanged data, supersede/delete retention, decimal bounds, invalid relations, append-only domain/final-audit history, separate event/knowledge watermarks, one final audit per success/validation failure/projection failure, idempotent domain replay with a new linked audit ID per retry, expected-version conflict, hash-chain integrity, incomplete-journal crash recovery, and concurrent SQLite writers/readers.
- [ ] Run the two test modules; expect FAIL on missing domain/store/audit interfaces.
- [ ] Implement schemas, SQLite WAL migration, immutable domain revisions, durable invocation journals, exactly-once final audit completion, previous-event hashes, hash-based source-version registry with relearn detection, domain idempotency, optimistic concurrency, and transaction boundaries. Store no provider settings.
- [ ] Run `pytest tests/test_domain.py tests/test_event_store.py -q`; expect PASS.
- [ ] Commit `feat: add auditable event and revision store`.

### Task 3: Deterministic Seed Import and Atomic Graph Projection

**Files:**
- Create: `src/soul_knowledge/importer.py`
- Create: `src/soul_knowledge/projection.py`
- Create: `src/soul_knowledge/graphify_adapter.py`
- Create: `src/soul_knowledge/fragment_generator.py`
- Create: `src/soul_knowledge/read_model_validator.py`
- Create: `src/soul_knowledge/source_watcher.py`
- Test: `tests/test_importer.py`
- Test: `tests/test_projection.py`
- Test: `tests/test_fragment_generator.py`
- Test: `tests/test_read_model_validator.py`

**Interfaces:**
- Consumes: Task 2 models/store and read-only structured source files.
- Produces: `SeedImporter.import_file(path: Path, *, domain: PrimaryDomain, source_uri: str) -> ImportReceipt`.
- Produces: `GraphProjector.rebuild() -> ProjectionReceipt` and `GraphProjector.repair_if_stale() -> ProjectionReceipt`.
- Produces: `GraphifyAdapter.generate_fragment(nodes, edges) -> ExtractionFragment` through the deterministic local generator plus Graphify validate/sanitize schema; no model backend is importable or reachable.
- Produces: `ReadModelValidator.validate(generation_dir, manifest) -> ValidationReport` failing closed on endpoints, file types, confidence, junk nodes, absolute paths, secrets, unknown schema, and hash mismatch.
- Produces: `SourceWatcher.poll() -> list[SourceChange]` and deterministic doc-change update triggering rather than Graphify's flag-only behavior.
- Produces: `ProjectionReceipt(projection_version, source_knowledge_high_watermark, content_hash)` and an atomically activated generation manifest.
- Produces: Graphify adapter functions and contract tests for assigned provenance/confidence, validation, cache/incremental update, merge, JSON/HTML/report export, query, explain, path, god-nodes, communities, and reflection semantics.

- [ ] Write failing tests for explicit domain mapping, provenance/source-version preservation, deterministic fragment generation with no model import, canonical Graphify schema/sanitization, duplicate import idempotency, parent/relation integrity, seven primary roots, stable file-scoped IDs, schema/version acceptance, content-hash validation, read-model endpoint/confidence/junk/absolute-path/secret failures, interrupted activation, paused/competing projectors, rejection of older knowledge watermarks, audit-only events leaving the projection target unchanged, doc-change update triggering, stale-read repair, and deterministic rebuild from replay at a fixed knowledge high-water mark.
- [ ] Run the task modules; expect FAIL.
- [ ] Implement deterministic structured import and fragment generation, Graphify schema/validate/sanitize adapters, read-model validator and junk classifier, doc-change watcher, single serialized projector, monotonic high-water publication guard, content-addressed generations, durable manifest, atomic `CURRENT` activation, stale detection/repair, and adapter boundaries. Implement or wrap every matrix feature assigned to this task and name its passing contract test in the matrix. Never invoke Graphify semantic extraction, provider backends, or AI.
- [ ] Run `pytest tests/test_importer.py tests/test_projection.py tests/test_fragment_generator.py tests/test_read_model_validator.py -q`; expect PASS, including crash, torn-publication, and out-of-order publication probes.
- [ ] Commit `feat: project versioned knowledge into graphify`.

### Task 3A: Frozen Mathematical and Measurement Contracts

**Files:**
- Create: `docs/mathematics/evidence-schema.md`
- Create: `docs/mathematics/scoring-contract.md`
- Create: `docs/contracts/access-policy.md`
- Create: `docs/contracts/recommendation-contract.md`
- Create: `docs/contracts/relearn-contract.md`
- Create: `docs/mathematics/token-budget-contract.md`
- Create: `tests/fixtures/scoring_cases.json`
- Create: `tests/fixtures/token_budget_cases.json`
- Create: `tests/fixtures/access_recommendation_relearn_cases.json`
- Test: `tests/test_measurement_contracts.py`

**Interfaces:**
- Consumes: the exact formulas, parameters, units, numeric semantics, M definition, token algorithm, conditional-access policy, recommendation shape, and relearn rules in spec sections 8, 9, and 13.
- Produces: machine-readable evidence schema, access/recommendation/relearn contracts, independent scoring fixtures, cross-adapter token fixtures, and an approval checkpoint. No feature implementation starts until this contract passes review.

- [ ] Write failing contract tests validating evidence fields/units, Decimal ordering, access L1/ambiguous/required classes, `access_only` zero-payload budget, exactly three recommendation lists and item fields, no raw SOUL text, one-observation-no-promotion, source-version scoping, RESET/ACCUMULATE boundaries, mixed-source refusal, zero evidence weight for relearn, canonical fixtures, and paired token/quality measurement.
- [ ] Run `pytest tests/test_measurement_contracts.py -q`; expect FAIL until the contracts and fixtures exist.
- [ ] Encode the spec contracts without changing them, including at least 30 scoring fixtures, 12 complete-payload token fixtures, and access/recommendation/relearn fixtures. Include correlated groups, bin boundaries, zero-total-weight reasons, null M bin, permutation replay, L1 no-call, ambiguous cap, unmapped evidence quarantine, and rollback cases.
- [ ] Run `pytest tests/test_measurement_contracts.py -q`; expect PASS. Any later formula or M-definition change requires a new contract version and operator approval.
- [ ] Commit `docs: freeze mathematical and measurement contracts`.

### Task 4: Primary-Root Retrieval and Token-Bounded Query Service

**Files:**
- Create: `src/soul_knowledge/retrieval.py`
- Create: `src/soul_knowledge/contracts.py`
- Create: `src/soul_knowledge/service.py`
- Test: `tests/test_retrieval.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Produces: `QueryRequest(purpose, domain, context_tags, token_budget, access_only, depth in [1,6], mode, relation_allowlist, max_nodes, max_edges)` and `QueryResponse` with nodes, provenance, versions, confidence, `audit_event_id`, `required_knowledge_watermark`, `source_knowledge_high_watermark`, `projection_version`, `stale`, `truncation`, and the exact three-list recommendation block.
- Produces: `AccessPolicy.decide(task_surface, context) -> NO_CALL | ACCESS_ONLY | QUERY` from the hash-pinned local artifact, plus `RecommendationBlock` items carrying source IDs/versions, evidence IDs, learned class, reason codes, and expiry.
- Produces: `KnowledgeService.query`, `get_node`, `get_subtree`, `log_event`, and `record_outcome`; Task 5 adds `evaluate`, and Task 6 adds `review`, `propose_improvement`, `apply_improvement`, and `rollback` for exactly ten public knowledge operations.
- Produces: deterministic `PrimaryRouter`, `BudgetedTraversal` with explicit node/edge/depth/relation guards, and `EvaluateRequest`/`EvaluateResponse` service contracts; the scoring implementation arrives in Task 5.

- [ ] Write failing tests for direct domain routing, missing-root failure without keyword fallback, ambiguous domain rejection, bilingual aliases, relation allowlist, max node/edge/depth guards, cross-edge inclusion, deduplication, ranking with zero provenance relevance, field-complete IDF, canonical complete-payload budget with `access_only` zero payload and 160-token cap, budgets below 128, metadata overflow, exactly three recommendation lists and item fields, no raw SOUL text, one-observation-no-promotion, audit IDs on every operation, self-auditing reads that do not self-stale, concurrent reader/writer snapshot freshness, stale retry/error, no-match behavior, and no raw-corpus reads.
- [ ] Run both modules; expect FAIL.
- [ ] Implement fixed contracts, hash-pinned access policy, primary-root routing, bounded Graphify traversal, warm content-hash indexes, field-complete deterministic ranking, recommendation block generation without raw text, canonical JSON budget compaction, and audit recording for all service operations. Implement the matrix retrieval features assigned to this task and their named contract tests.
- [ ] Run `pytest tests/test_retrieval.py tests/test_service.py -q`; expect PASS.
- [ ] Commit `feat: add direct primary-node knowledge retrieval`.

### Task 5: Calibrated P/M/E Scoring and Professional Levels

**Files:**
- Create: `src/soul_knowledge/scoring.py`
- Create: `src/soul_knowledge/scoring_config.toml`
- Create: `docs/mathematics/scoring.md`
- Test: `tests/test_scoring.py`
- Test: `tests/test_scoring_properties.py`
- Modify: `tests/fixtures/scoring_cases.json`

**Interfaces:**
- Consumes: immutable evidence, outcomes, context, timestamps, subtree relations, and the frozen Task 3A contract/fixtures; no formula or threshold may be chosen during implementation.
- Produces: `ScoreEngine.score_node(node_id: str, context: Context, at: datetime) -> ScoreAssessment`.
- Produces: `ScoreEngine.aggregate_primary(primary_id: str, context: Context, at: datetime) -> ScoreAssessment`.
- Produces: `KnowledgeService.evaluate(request: EvaluateRequest) -> EvaluateResponse`, containing P/M/E, uncertainty, level 1–5, evidence-weight explanations, selected M bin, promotion eligibility, `audit_event_id`, and projection metadata.

- [ ] Write failing fixtures and properties for `[0,1]` bounds, deterministic replay under evidence permutation, recency decay, reliability/context weighting, diminishing returns, duplicate/correlated-group resistance, contradiction/staleness, zero total weight from disjoint context or zero reliability, null M bin, depth discount, P quality gate, M not equal to frequency, and E never rescuing low P.
- [ ] Run all scoring tests; expect FAIL.
- [ ] Implement exactly the frozen Decimal formulas, evidence schema, recency/context/independence/depth weights, M-bin eligibility and coverage denominator, E units/baselines, uncertainty, primary aggregation, level caps, and gated composite. `KnowledgeService.evaluate` appends its audit event and returns the complete assessment contract.
- [ ] Run `pytest tests/test_measurement_contracts.py tests/test_scoring.py tests/test_scoring_properties.py -q`; expect PASS against all frozen decimal fixtures and properties.
- [ ] Commit `feat: add explainable dynamic knowledge scoring`.

### Task 6: Review State Machine and Reversible Improvements

**Files:**
- Create: `src/soul_knowledge/review.py`
- Create: `src/soul_knowledge/improvement.py`
- Create: `src/soul_knowledge/scheduler.py`
- Create: `src/soul_knowledge/relearn.py`
- Test: `tests/test_review.py`
- Test: `tests/test_improvement.py`
- Test: `tests/test_relearn.py`

**Interfaces:**
- Produces: review statuses and deterministic transition policy with minimum evidence, cooldown, and hysteresis.
- Produces: `KnowledgeService.review`, `propose_improvement`, `apply_improvement`, and `rollback` from the spec.
- Produces: structured diff proposals, expected-version checks, approval records, immutable revisions, due-review scans without model calls, and source-version/relearn offers attached to existing review operations.
- Produces: `RelearnEngine.offer(change) -> RelearnOffer`, `preview(mode) -> RelearnPreview`, `apply(mode, approval) -> RelearnReceipt`, and `rollback(relearn_id)` with verified atomic backup and exact pre-relearn restoration. RESET and ACCUMULATE are operator-gated; mixed-source reads are refused unless explicitly approved.

- [ ] Write failing tests for every status, insufficient evidence, oscillation prevention, proposal validation, high-impact approval, regression-gate failure, apply, retain, deprecate, merge, exact rollback, `RELEARN_PENDING` without auto mode, RESET zero evidence/level 1, ACCUMULATE anchor mapping and quarantine, supersede/contradiction retention, no stale mixing, mixed-source approval, zero evidence weight for relearn, no promotion from relearn, verified backup, and byte-exact rollback with one linked audit event.
- [ ] Run the task modules; expect FAIL.
- [ ] Implement state machine, due scheduler, explicit proposal/diff pipeline, immutable version handling, regression hooks, rollback, and hash-scoped RESET/ACCUMULATE relearn with quarantine and atomic verified backups. Do not implement autonomous semantic rewriting.
- [ ] Run `pytest tests/test_review.py tests/test_improvement.py tests/test_relearn.py -q`; expect PASS.
- [ ] Commit `feat: add controlled knowledge improvement lifecycle`.

### Task 7: CLI and Persistent Local API

**Files:**
- Create: `src/soul_knowledge/cli.py`
- Create: `src/soul_knowledge/http_api.py`
- Create: `src/soul_knowledge/__main__.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_http_api.py`

**Interfaces:**
- Consumes: Task 4–6 `KnowledgeService` methods only.
- Produces: `soul-knowledge` commands for all ten tool operations plus projection rebuild, backup, restore, and health.
- Produces: versioned `/v1` JSON API, Unix-socket default, optional loopback HTTP, health, and consistent error envelopes.

- [ ] Write failing contract tests proving CLI/API expose all ten knowledge operations through the same service, preserve IDs/versions/projection metadata/audit receipts/errors, produce exactly one final audit event per valid invocation, reject invalid input with a completion audit record, enforce loopback/socket boundaries, and survive service restart.
- [ ] Run both modules; expect FAIL.
- [ ] Implement thin CLI and Starlette/Uvicorn adapters with no duplicated business logic, request size limits, timeouts, structured errors, and graceful shutdown.
- [ ] Run `pytest tests/test_cli.py tests/test_http_api.py -q`; expect PASS.
- [ ] Commit `feat: expose direct cli and local api access`.

### Task 8: MCP and Hermes Integration Without Sampling

**Files:**
- Create: `src/soul_knowledge/mcp_server.py`
- Create: `docs/integration/hermes.md`
- Test: `tests/test_mcp_server.py`
- Test: `tests/test_mcp_stdio_e2e.py`

**Interfaces:**
- Consumes: the same `KnowledgeService` contracts.
- Produces: MCP tools named as the ten spec operations over stdio and optional Streamable HTTP, bound to one startup allowlisted graph root; tools+resources-only capabilities and no sampling/prompts/logging/completion registration.
- Produces: strict validation for unknown properties, missing keys, wrong types, string booleans, depth `[1,6]`, mode enum, and budget `[128,max]`; every validation/business failure uses `ToolError` with `isError:true`.
- Produces: sanitized, size-capped resources; raw Graphify reports and PR/`gh` surfaces are absent.
- Produces: Hermes connection instructions with no `env` provider keys, no `project_path`, and no sampling capability/configuration.

- [ ] Write failing MCP discovery/call tests for schemas, exactly ten tools, strict unknown/missing/type/range rejection, absence of `project_path`, fixed-root path/symlink containment, sanitized resources, exact capability equality, tools+resources only, no sampling/prompts/logging/completions/PR/gh, errors, persistence, audit and exactly-one-final-audit-per-valid-invocation parity with CLI/API, canonical payload parity, and forbidden-import/outbound-socket guards in a scrubbed fresh process.
- [ ] Run both MCP modules; expect FAIL.
- [ ] Implement the MCP adapter with a fixed allowlisted root, strict schema/range validation, sanitized resources, exact capability surface, and Hermes setup documentation using the external MCP connection only; never edit Hermes source or hand-edit its config.
- [ ] Run `pytest tests/test_mcp_server.py tests/test_mcp_stdio_e2e.py -q`; expect PASS and verify discovered tools in a fresh MCP client process.
- [ ] Commit `feat: add deterministic hermes mcp integration`.

### Task 9: Operations, Security, Backup, and Recovery

**Files:**
- Create: `src/soul_knowledge/observability.py`
- Create: `src/soul_knowledge/backup.py`
- Create: `src/soul_knowledge/security.py`
- Create: `deploy/Dockerfile`
- Create: `docs/operations.md`
- Test: `tests/test_operations.py`
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: structured metrics/logs, health/readiness, secret redaction on every surface, fixed-root/symlink-safe path resolution, bounded resource sizes, backup/restore, stale projection repair, and optional container packaging.
- Produces: Unix-socket default, loopback-only optional HTTP, API key required beyond loopback, Host allowlisting even on wildcard binds, TLS/reverse-proxy requirement, and no outbound/provider requirement.

- [ ] Write failing tests for secret/PII redaction on write and every read surface, control/bidi/zero-width sanitization, path traversal and symlink escape, cross-tree root rejection, oversized payload/resource, unauthorized or non-loopback bind without key, host allowlist, audit correlation, backup/restore equivalence, migration, concurrent read/write, and corrupt/stale/torn projection recovery.
- [ ] Run both modules; expect FAIL.
- [ ] Implement operational controls, least-privilege defaults, atomic backups, health metrics, deterministic source watch/incremental rebuild, query-audit export, and container packaging without changing the three access-path contracts. Implement every matrix feature assigned to this task and pass its named contract test.
- [ ] Run `pytest tests/test_operations.py tests/test_security.py -q`; expect PASS.
- [ ] Commit `feat: add secure recoverable operations`.

### Task 10: End-to-End Benchmark, Audit, and Release Candidate

**Files:**
- Create: `bench/run.py`
- Create: `bench/cases.json`
- Create: `docs/verification.md`
- Create: `docs/audit-release.md`
- Create: `README.md`
- Test: `tests/test_acceptance_e2e.py`
- Modify: `tests/test_no_ai_runtime.py`

**Interfaces:**
- Consumes: all prior interfaces and the spec acceptance criteria.
- Produces: reproducible benchmark/verification evidence and release candidate, not an unverified completion claim.

- [ ] Write failing acceptance tests for all seven domains, exactly ten audited tools, three access paths, exactly one final audit per valid invocation including recovered failures, idempotent retry linkage, deterministic scoring, rollback, locked no-provider environment, scrubbed runtime, forbidden imports/entry points, denied outbound sockets, absent sampling handlers/capabilities, strict MCP root/schema/range/capability contract, conditional access, recommendation shape, RESET/ACCUMULATE/rollback, no stale mixing, and no Hermes source modification.
- [ ] Extend the benchmark suite with a frozen SOUL corpus, frozen questions, N repeats, p50/p95 latency, token-estimator parity, quality rubric, paired raw-SOUL vs system comparison, and a machine-readable result ledger. Keep the quality gate: token savings never excuse quality regression.
- [ ] Calculate Graphify coverage only from matrix rows whose owning contract tests pass and require at least 90% of the applicable 40-feature inventory; report the three partial residuals explicitly.
- [ ] Run the complete suite with `pytest -q`, type checking, lint, dependency audit, and `python bench/run.py`; record actual outputs and exit codes.
- [ ] Implement only the missing acceptance fixtures, benchmark harness, and user/operations documentation; keep product fixes in the owning task with a regression probe.
- [ ] Run an independent build-audit-review pass. Classify findings as launch-blocking, fast-follow, or track.
- [ ] For every launch blocker, write `docs/recovery/<finding-id>.md` with evidence, root cause, affected global criterion, smallest fix, regression probe, and rollback; rebuild and rerun the full gate without replacing the global plan. Require a fresh independent reviewer to verify both the regression probe and finding closure before marking it closed.
- [ ] Stop and report if three recovery iterations show no verified improvement or a fix violates a global constraint.
- [ ] Release only when the full gate passes and no launch blocker remains; report fast-follow/track items and all unmeasured claims explicitly.
- [ ] Commit `docs: publish verified release evidence` only after the final fresh verification run.
