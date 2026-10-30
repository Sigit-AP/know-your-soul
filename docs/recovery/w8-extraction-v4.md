# SUB-106 — W8: extraction contract v4 (`submit_extraction`) — external-agent intake

Date: 2026-10-30. Scope: `docs/contracts/extraction-contract-v4.md` (new,
frozen), `src/know_your_soul/extraction/` (new package: `errors`, `schema`,
`tier`, `corroboration`, `queue`, `engine`), wiring in `http_api.py` /
`mcp_server.py` / `cli.py` / `serve/superpower.py` / `serve/webview.py`,
`tests/` (8 new files + pin updates). Not touched: the v1/v2/v3 contract
documents (byte-identical, pinned by sha256 in both shape suites),
`store.py`, `graph_model.py`, the seventeen existing operations' semantics.

## Authority

Operator dispatch 2026-10-30 (SUB-106): a no-AI product; external agents
(Claude Code and peers, which own vision models) read PDF scans / images
**outside** the process and submit extraction results. The design must be
measurably stronger than Graphify's inline-LLM + `EXTRACTED`/`INFERRED` tag
approach.

## What was built

**Contract v4 addendum** (`docs/contracts/extraction-contract-v4.md`, frozen
upon publication, v2/v3 addendum style): one verb
`knowledge.submit_extraction(source_sha256, payload, method, confidence,
source_path=None)`, proposal-only; the receipt shape; the typed error
vocabulary (12 codes); the `agent-extracted` truth tier (intake cap 0.35,
tier cap 0.65, corroboration step +0.10, contradiction penalty −0.20, all
quantized Decimal); the deterministic corroboration rules (node-key value
digests, edge endpoint pairs, distinct-actor rule, group recompute as a pure
function); the operator-only review surface; the adapter mapping.

**`extraction/` package**:

- `schema.py` — closed-schema intake validation (unknown properties rejected
  at every nesting level), sanitization before hashing/storage (control/bidi
  stripped, HTML escaped), fail-closed caps (text 256 KiB, 4096 nodes, 8192
  edges, 1 MiB total), decimal-string confidence (binary floats rejected),
  content-derived `receipt_id` / idempotency key. Never mutates the caller.
- `tier.py` — the written tier registry; `stamp_node` builds canonical-graph
  nodes carrying `payload.certainty` (tier, caps, source hash, receipt,
  method), provenance (`sha256:<hash>`, `extraction:<receipt>`,
  `agent:<agent>`), the `agent-extracted` context tag, and
  `scores.confidence = effective`, `uncertainty = 1 − confidence`. Applied
  node ids and timestamps are deterministic (`extracted_at`), so a retried
  approve replays through the store's idempotency instead of double-applying.
- `corroboration.py` — `compare_payloads` (pairwise agreements /
  contradictions / overlap ratio) and `recompute_group` (pure over the
  ordered group rows; contradictions flag **both** sides).
- `queue.py` — the durable review queue: a **sidecar** SQLite database
  (`soul.sqlite3` → `soul.extraction.sqlite3`; the frozen store schema gains
  no table), WAL + synchronous FULL, `BEGIN IMMEDIATE` writes, UNIQUE
  idempotency key, fail-closed 65536-row cap, contract review ordering,
  volatile-masked `canonical_dump` for the two-run determinism proof.
- `engine.py` — `submit` (validate → bind → dedupe → recompute → persist;
  exactly one `knowledge.submit_extraction` audit per invocation, success and
  typed failure alike, carrying hashes/counts — never content), `review`
  (read-only listing + per-receipt diff with corroboration peers), `approve`
  (nodes via the existing audited `knowledge.log_event` path, edges via
  audited `knowledge.extraction_approve_edge` relation commits, deterministic
  idempotency keys, one `knowledge.extraction_approve` decision audit),
  `reject` (one `knowledge.extraction_reject` audit; no graph writes).

**Wiring** (the v3 adapter pattern exactly): shared `run_extraction_verb` in
`http_api.py`; HTTP `POST /v1/knowledge/extraction` (fixed route, same
boundary/CSRF discipline, typed status map); MCP `knowledge_submit_extraction`
(closed schema, nested objects closed, v1 vocabularies as enums); CLI
`submit-extraction` plus the operator-only `extraction review|approve|reject`;
superpower `WRITE_TOOLS` (write lane — the per-agent admission token bucket
applies unchanged); webview `GET /api/extractions` (read-only; a missing
sidecar is an empty queue, never created by a read).

## Measurably better than Graphify — mechanism by mechanism

Graphify runs an LLM inline and lets the extraction enter the graph directly,
marked only by an `EXTRACTED`/`INFERRED` tag. Each row below names the
mechanism difference and the tests that prove it.

| Mechanism | Graphify | This contract | Proven by |
|---|---|---|---|
| Provenance binding | tag on the node; no re-verifiable link to source bytes | `source_sha256` re-hashed against the real file at intake when `source_path` is given; mismatch is a typed refusal | `TestProvenanceBinding::test_tampered_hash_is_rejected`, `test_hash_mismatch_is_audited`, `test_source_path_binds_to_a_real_file` (engine); `test_tampered_source_hash_rejected` (MCP); `test_hash_mismatch_maps_to_409` (HTTP); `test_tampered_hash_exits_1` (CLI) |
| Truth tier | none — extracted and curated nodes share one trust level | `agent-extracted` tier with written intake cap 0.35 / tier cap 0.65; P/M/E stay 0; promotion only via the existing operator-gated evidence ops | `TestTruthTier` (engine, 5 tests incl. applied-node tier block and capped confidence); `TestEffectiveConfidence` (corroboration, 6 tests) |
| Corroboration | none | deterministic multi-agent overlap/contradiction engine; corroboration raises effective confidence by rule; contradiction flags both sides | `TestPairwiseCompare` (8), `TestGroupRecompute` (11), `TestOverlapThresholdBoundary` (2), `TestCorroborationLifecycle` (6, incl. both-sides flagging and the self-corroboration ban) |
| Idempotency | re-extraction re-writes | locked key `(source_sha256, actor, method_fingerprint)`; identical resubmission returns the same receipt and writes nothing; same key + different payload is `EXTRACTION_DUPLICATE`; approve retries never double-apply | `TestIdempotency` (engine, 5), `test_duplicate_idem_key_is_a_unique_violation` (queue), `test_submit_duplicate_returns_same_receipt` (MCP), `test_duplicate_conflict_maps_to_409` (HTTP), `test_duplicate_returns_the_same_receipt_over_the_socket` (daemon) |
| Write path | extraction lands in the graph directly | proposal-only queue; apply exclusively through the operator-gated, audited write path (`log_event` + relation commits) | `test_submit_never_touches_the_canonical_graph`, `test_approve_applies_nodes_and_edges_to_the_graph`, `TestApprove` (6), `TestReject` (5) |
| Rollback | none beyond store snapshots | applied extraction nodes are ordinary revisions: the standard byte-exact `knowledge.rollback` restores them | `TestRollback::test_applied_extraction_rolls_back_byte_exact` |
| Audit | — | exactly one final audit per invocation (submit/approve/reject), carrying hashes and counts, never content | `TestSubmitLifecycle` (5), `test_approve_journals_one_decision_audit`, `test_reject_journals_one_decision_audit`, `test_exactly_one_audit_event_per_mcp_call` |
| Sanitization | — | control/bidi stripping + HTML escaping before hashing/storage; queue, review diffs and applied nodes are inert | `TestSanitization` (schema, 6), `test_applied_node_summaries_are_sanitized`, `test_fields_are_already_sanitized` (webview) |
| Quota | — | write-lane admission token bucket per agent on the superpower daemon | `TestPerAgentQuota::test_the_admission_bucket_throttles_submit_extraction`, `TestLanePlacement` |
| Determinism | model-dependent | pure functions everywhere; two identical runs produce byte-identical queues and receipts | `TestDeterminism` (schema + engine), `test_dump_is_byte_identical_across_runs` (queue), `test_compare_is_symmetric/_is_deterministic`, `test_recompute_is_a_pure_function` |

## Probes (TDD)

- RED: `pytest tests/test_extraction_* -q` → ModuleNotFoundError, 7
  collection errors, exit 2 (milestone board).
- GREEN: 290 extraction tests pass; the pinned-surface files
  (`test_property_mcp_server`, `test_adapter_validation_parity`,
  `test_final_adapter_fixes`, `test_retrieval_verbs_v2`, `test_mcp_server`,
  `test_mcp_stdio_e2e`, `test_serve_daemon`, `test_serve_proxy`,
  `test_contract_v3_shape`, `test_acceptance_e2e`, `test_serve_protocol`)
  pass after the measured pin updates below.
- CLI smoke (real processes): submit → replay (`duplicate: true`, same
  receipt) → review → approve → node readable through the frozen `get-node`
  path with the `agent-extracted` tag; tampered `source_sha256` exits 1 with
  `SOURCE_HASH_MISMATCH`; two identical CLI sequences produce byte-identical
  queue databases (volatile columns masked); a second distinct actor moves
  the receipt to `corroborated` with confidence 0.35 → 0.45.
- Gates: `ruff check` and `ruff format --check` clean on src+tests; `pyright`
  0 errors/0 warnings on all touched files; `bandit -ll` clean on the
  touched files.
- Regression: full suite **6782 passed, 2 warnings, exit 0** (1059.9s).
  (An earlier mid-edit run showed four failures — three pin updates not yet
  applied plus one fairness-gate flake under a concurrent sibling suite; the
  pins were fixed and the flake passes in isolation and in the final run.)
- Freeze proof: the five prior contract documents hash to their pinned
  digests; `store.py` / `graph_model.py` untouched.

## Measured pin updates (the addendum mechanism, same as v3)

The tool surface grows 16 → 17 with exactly one verb; every updated pin
records the addendum inline:

- `tests/test_property_mcp_server.py` — surface size 17, bijection 17,
  operations delta gains `knowledge.submit_extraction`, tool-name index
  bound 15 → 16, `_valid_arguments` gains the extraction entry, the pinned
  required-sets snapshot gains
  `("source_sha256", "payload", "method", "confidence")`.
- `tests/test_adapter_validation_parity.py` — MCP-minus-HTTP delta gains
  `submit_extraction`; size 17.
- `tests/test_final_adapter_fixes.py`, `tests/test_retrieval_verbs_v2.py`,
  `tests/test_mcp_server.py`, `tests/test_mcp_stdio_e2e.py`,
  `tests/test_serve_proxy.py` — listing/size pins 16 → 17.
- `tests/_serve_helpers.py` — `EXPECTED_TOOLS_16` renamed
  `EXPECTED_TOOLS_ALL` (+ the new tool); `tests/test_serve_daemon.py` lane
  sizes READ 7 / WRITE 8 / HEAVY 2.
- `tests/test_contract_v3_shape.py` — the contracts-directory pin admits the
  v4 addendum alongside v3 (both pinned by digest in the shape suites).
- `tests/test_acceptance_e2e.py` — the audited-tools pin gains
  `knowledge_submit_extraction`; the MCP surface count 16 → 17.
- `tests/test_serve_protocol.py` — the sorted tool listing 16 → 17.

No existing assertion was weakened; every change widens a surface pin by the
one contract-v4 verb. No commits (pool rule).
