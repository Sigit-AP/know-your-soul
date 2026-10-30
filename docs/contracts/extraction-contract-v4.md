# Extraction Contract (v4)

Contract version: 4.0.0 (addendum)
Status: ACTIVE (2026-10-30)
Authority: operator dispatch of 2026-10-30 (SUB-106): "kontrak v4
submit_extraction — jalur masuk ekstraksi agent eksternal" — external agents
(Claude Code and peers, which own a vision model) read PDF scans / images
**outside** this product and submit the extraction result; the graph itself
stays model-free.
Scope: this addendum **extends** the frozen fourteen-operation knowledge
surface plus the two contract-v3 ingestion verbs with **one** operation
(`submit_extraction`), one truth tier (`agent-extracted`), the corroboration
rules, the review surface, and the adapter mapping. The v1 contracts, the v2
addendum and the v3 addendum in this directory are **unchanged** and remain
byte-identical; the seventeen existing operations keep their exact semantics.

Any change to the semantics below requires a new contract version and operator
approval. This document is frozen upon publication.

## Design posture: proposal-only, hash-bound, tier-capped

Graphify's `EXTRACTED`/`INFERRED` approach runs an LLM inline and lets the
extraction enter the graph directly, marked only by a tag. This contract is
strictly stronger, mechanism by mechanism:

- **Provenance is bound to a hash, not a tag.** Every submission carries
  `source_sha256`. When the source file exists under the fixed extraction
  root (`source_path` supplied), the file's bytes are re-hashed at intake and
  a mismatch is the typed `SOURCE_HASH_MISMATCH` failure — the claim is
  re-verifiable at any later time against the same file.
- **A separate truth tier.** Knowledge applied from an external extraction
  carries `certainty.tier = "agent-extracted"` with a written confidence
  ceiling. The tier never promotes itself: corroboration moves the effective
  confidence only up to the tier ceiling, and anything beyond it requires the
  existing operator-gated evidence paths (`record_outcome` / `evaluate`).
- **Corroboration is multi-agent and deterministic.** A second, *distinct*
  `(actor, method)` submission over the same `source_sha256` with sufficient
  overlap and no contradictions raises effective confidence by a fixed rule;
  any contradiction flags **both** submissions for operator review. All of it
  is a pure function of the stored submissions — no model anywhere.
- **Idempotency is locked on `(source_sha256, actor, method_fingerprint)`.**
  Re-submitting the identical payload returns the identical receipt and
  writes nothing; the same key with a *different* payload is the typed
  `EXTRACTION_DUPLICATE` conflict, never a silent overwrite.
- **Nothing here writes the canonical graph.** A submission lands in the
  durable review queue and nowhere else. Application happens exclusively
  through the operator-run `extraction approve` surface, which rides the
  existing audited write path (`knowledge.log_event` / `relation.add`
  commits) — so the standard byte-exact rollback covers every applied
  extraction node.
- **Quota.** On the superpower daemon the verb is a write-lane tool: the
  per-agent admission token bucket applies to it exactly as to every other
  write.

## Shared guarantees

- Exactly one final audit event per invocation (`knowledge.submit_extraction`),
  success and typed failure alike. The event records the receipt id, the
  `source_sha256`, the `payload_sha256`, the node/edge counts and the actor —
  never the submitted content. The review decisions journal exactly one final
  audit event each (`knowledge.extraction_approve`,
  `knowledge.extraction_reject`), plus the per-write audit events of the
  existing apply path.
- Deterministic and model-free: the receipt id, the payload hash, the
  corroboration outcome and the effective confidence are pure functions of
  the stored submissions. The only volatile fields are the per-invocation
  audit id and wall-clock timestamps (the v3 "modulo the per-invocation audit
  id" clause, same shape).
- Fail-closed typed errors, never exceptions on the wire:
  `PAYLOAD_SCHEMA`, `METHOD_SCHEMA`, `CONFIDENCE_NOT_DECIMAL`,
  `CONFIDENCE_OUT_OF_RANGE`, `PAYLOAD_TOO_LARGE`, `SOURCE_HASH_MISMATCH`,
  `SOURCE_PATH_OUTSIDE_ROOT`, `SOURCE_NOT_FOUND`, `EXTRACTION_DUPLICATE`,
  `EXTRACTION_NOT_FOUND`, `EXTRACTION_STATE`, `EXTRACTION_QUEUE_FULL`.
- The v2 no-path-like-property rule is amended for exactly this one verb:
  `submit_extraction` may carry the single optional property `source_path`,
  resolved against the startup-fixed extraction root with the v3 path
  discipline (escapes → `SOURCE_PATH_OUTSIDE_ROOT`, missing →
  `SOURCE_NOT_FOUND`). Every other operation keeps the rule.

## `knowledge.submit_extraction(source_sha256, payload, method, confidence, source_path=None)`

Input (a closed JSON object — unknown properties are rejected):

- `source_sha256` (required): 64 lowercase hex characters — the sha256 of the
  source bytes the external agent read.
- `source_path` (optional): a path resolved against the fixed extraction
  root. When present, the file must exist, be a regular readable file, and
  its sha256 must equal `source_sha256` (`SOURCE_HASH_MISMATCH` otherwise).
  When absent, the hash binds an external source this host never saw (a scan
  read elsewhere); the binding stays verifiable by anyone holding the file.
- `payload` (required, closed object):
  - `text` (required string, ≤ 262144 chars): the extracted plain text.
  - `nodes` (required list, ≤ 4096 items): proposed node records, each a
    closed object: `name` (1..512 chars), `kind` (one of the v1 `NodeKind`
    values, default `leaf`), `domain` (one of `P1`..`P7`), optional `summary`
    (≤ 4096 chars), optional `span` (`line` ≥ 1, optional `col` ≥ 0).
  - `edges` (required list, ≤ 8192 items; may be empty): proposed typed
    links, each a closed object: `relation` (one of the v1 `RelationKind`
    values), `source` / `target` (node `(kind, name)` keys that must exist in
    the same payload's `nodes`), optional `evidence` (≤ 1024 chars).
- `method` (required, closed object): `agent` (1..256 chars — the external
  model/agent id), `extracted_at` (ISO-8601 timestamp), optional `prompt_id`
  (≤ 256 chars), optional `tool` (≤ 256 chars). The method fingerprint used
  for idempotency and corroboration hashes `{agent, prompt_id, tool}` —
  `extracted_at` is recorded but deliberately excluded, so a re-run of the
  same extraction is a replay, not a new entry.
- `confidence` (required): the agent's self-report as a **decimal string**
  matching `0`, `0.x…`, `1` or `1.0…` — never a binary float
  (`CONFIDENCE_NOT_DECIMAL`), always inside `[0, 1]`
  (`CONFIDENCE_OUT_OF_RANGE`).

Sanitization (before anything is stored or hashed): every free-text field is
stripped of control, bidi and zero-width characters and HTML-escaped
(`& < > " '`), so queue rows, review diffs and applied nodes are inert under
rendering. The `payload_sha256` binds the **sanitized canonical** form; the
whole canonical submission is capped at 1 MiB (`PAYLOAD_TOO_LARGE`).

Output — the extraction receipt (a fixed-shape, never-truncated payload):

- `receipt_id`: `ext-` + 32 hex of sha256 over
  `source_sha256 | actor | method_fingerprint | payload_sha256` —
  content-derived, so identical resubmissions identify the same row;
- `status`: `pending_review`, `corroborated` or `contradiction_flagged`
  (see the corroboration rules);
- `duplicate`: `true` on an idempotent replay (same idempotency key **and**
  same `payload_sha256` — nothing is written, the stored receipt is
  returned); the same key with a different payload is `EXTRACTION_DUPLICATE`;
- `source_sha256`, `payload_sha256`, the node/edge counts;
- `confidence_reported` (the agent's decimal self-report, verbatim) and
  `confidence_effective` (the tier- and corroboration-adjusted value, a
  12-decimal fixed-point string);
- `corroboration`: `{corroborators, contradictions, overlap_ratio}`;
- `audit_event_id`: the invocation's single final audit event.

## Truth tier `agent-extracted`

- The tier registry is written, not implicit: `agent-extracted` has
  `intake_cap 0.35` and `tier_cap 0.65`; the deterministic tiers keep cap
  `1.0`. Caps are Decimals, quantized to 12 places, `ROUND_HALF_EVEN`.
- `confidence_effective` at submit time is `min(self_report, 0.35)` adjusted
  by the corroboration rules below, never above `0.65`, never below `0`.
- An applied node carries `payload.certainty = {tier: "agent-extracted",
  intake_cap, tier_cap, source_sha256, receipt_id, method, corroborators}`,
  `provenance` entries `sha256:<source_sha256>`, `extraction:<receipt_id>`,
  `agent:<agent>`, the context tag `agent-extracted`, and
  `scores.confidence = confidence_effective` with
  `uncertainty = 1 - confidence_effective`. P/M/E stay `0`; the professional
  level stays `1`.
- The ceiling is structural: no path in this contract writes a confidence
  above the tier cap. Promotion beyond it rides the existing evidence
  operations (`record_outcome`, `evaluate`) — the same operator-gated way
  every other node earns confidence.

## Corroboration rules (deterministic)

Two submissions over the same `source_sha256` are compared on their stored
canonical payloads:

- **Node keys** are `(kind, name)`; a shared key **agrees** when the
  value digest (sha256 of the canonical `{name, kind, summary, span}`) is
  equal, and **contradicts** when it differs.
- **Edge endpoint pairs** are `(source_key, target_key)`; a shared pair with
  the same `relation` agrees, with a different relation contradicts.
- `shared = |shared node keys| + |shared endpoint pairs|`;
  `overlap_ratio = (agreements) / shared` (`0` when `shared = 0`).
- A submission **corroborates** another when the **actors differ** (an actor
  never corroborates itself, whatever method it reruns), `overlap_ratio ≥
  0.5`, and the pairwise contradiction count is `0`.
- A submission is `contradiction_flagged` when any other submission in the
  group contradicts it; the flag applies to **both** sides.
- Effective confidence: `min(self_report, 0.35) + 0.10 × (distinct
  corroborating actors) − 0.20 × (1 if flagged else 0)`, clamped to
  `[0, 0.65]`, quantized.
- Group state is recomputed over the whole `source_sha256` group inside the
  submit transaction, as a pure function of the ordered stored rows — a late
  corroborator updates the earlier row's status deterministically.

## Review surface (operator-only)

- `know-your-soul extraction review [--status S] [--receipt ID]`: lists the
  queue deterministically (status rank `contradiction_flagged` →
  `pending_review` → `corroborated` → `applied` → `rejected`, then
  `receipt_id`); with `--receipt`, prints the submission's proposed diff
  (sanitized text excerpt, nodes, edges, corroboration peers). Read-only.
- `know-your-soul extraction approve RECEIPT_ID`: applies the proposal to the
  canonical graph through the existing audited write path — one
  `knowledge.log_event` per node (idempotency key
  `extraction:<receipt_id>:node:<key>`, so a retried approve never
  double-applies) and one audited `relation.add` commit per edge
  (operation `knowledge.extraction_approve_edge`, idempotency key
  `extraction:<receipt_id>:edge:<index>`) — then marks the row `applied`. Applied nodes carry the `agent-extracted` truth tier
  exactly as specified above. Approving a `rejected`/`applied` row is the
  typed `EXTRACTION_STATE` failure; an unknown receipt is
  `EXTRACTION_NOT_FOUND`.
- `know-your-soul extraction reject RECEIPT_ID [--note]`: marks the row
  `rejected` with the operator's note; the same typed failures.
- `GET /api/extractions` on the webview: the read-only queue view (counts +
  rows in the review order; sanitized fields only). Never a write.

Because every applied write is an ordinary audited domain event, the standard
byte-exact `knowledge.rollback` restores the exact pre-apply state; the queue
row is review state, not graph state.

## Adapter mapping

| Verb | CLI subcommand | HTTP route | MCP tool |
|------|----------------|------------|----------|
| `submit_extraction` | `know-your-soul submit-extraction` | `POST /v1/knowledge/extraction` | `knowledge_submit_extraction` |

The review surface is CLI + webview only (operator surfaces); it is
deliberately not an MCP tool, so no external agent can approve its own
submission.

The MCP schema is closed (`additionalProperties: false`) with the same ranges
as the service: `source_sha256` exactly 64 hex, `text` ≤ 262144, `nodes` ≤
4096, `edges` ≤ 8192, name ≤ 512, `summary` ≤ 4096, `evidence` ≤ 1024,
`agent`/`prompt_id`/`tool` ≤ 256, `confidence` a decimal string. Exactly one
path-like property (`source_path`) — the confined amendment of the v2
no-path rule; the graph root itself stays fixed at startup. On the superpower
daemon the tool joins the **write** lane; the per-agent admission token
bucket applies unchanged.
