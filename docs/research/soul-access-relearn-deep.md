# Conditional SOUL access, action-recommendation contracts, token-savings measurement, and RESET vs ACCUMULATE relearning

Status: research/design only. No product code, no spec edit, no plan edit, no corpus edit.
Date: 2026-10-25 (UTC).
Scope: deterministic adaptations for Hermes consuming the Super Power Knowledge SOUL Graph.
Citation bases:
- Product spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md` (cited as `spec:<line>`).
- Implementation plan: `docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md` (cited as `plan:<line>`).
- Graphify reference clone: `/root/.hermes/cache/scratch/graphify-ref-20261024` at commit
  `92741d162b2094ffc2fed58cfbd8ee823e9da62c`, `graphifyy 0.9.95` (verified with
  `git rev-parse HEAD`, `git describe --tags --always`, `pyproject.toml`; the same commit/version
  are the inspected references at `spec:15-16`).
- Host SOUL corpus: `/root/.hermes/knowledge/soul/*.md` (cited as `<file>:<line>`).
- Host SOUL identity: `/root/.hermes/SOUL.md` (cited as `SOUL.md:<line>`).

All contracts below are deliberately model-free: no semantic extraction, no embedding call, no
provider traffic, no MCP sampling (`spec:10`, `plan:16`). Where Graphify implements a behaviour with
an LLM-backed semantic stage, this design adopts only the deterministic half and marks the rest
excluded.

## 0. Measured evidence baseline

These are fresh measurements taken for this document, not corpus assertions.

| Item | Measured value | How measured |
|---|---|---|
| `SOUL.md` bytes / chars / lines | 31,279 / 24,686 / 386 | `wc -c -m -l`, `Path.stat()` |
| `SOUL.md` SHA-256 | `ad1677d3f2dac77442bfdd19ab8a58b6a5efee4535d606d53f04705bf3829261` | `sha256sum` |
| `SOUL.md` estimated tokens (`ceil(utf8_bytes/4)`) | 7,820 | doc algorithm from `spec:116` |
| SOUL corpus files / bytes / chars | 28 / 52,604 / 51,492 | `wc`, Python |
| SOUL corpus total labels (`- **Label** — …`) | 381 | Python regex count over the 28 files |
| SOUL corpus manifest hash | `96429eea4560cc5630db9f92881101f9f8282660e23a688e0c41f759efc3d982` | SHA-256 over sorted `relative_path\0sha256\n` |
| SOUL corpus estimated tokens | 13,151 | `ceil(utf8_bytes/4)` |
| Host `graphify-out/graph.json` | 525 nodes / 647 links / 12 hyperedges, 500,875 bytes, SHA-256 `90968f5243a3d3dd410f4332c5e3cb3c8acbda463ed471987367e26c1b8d826d` | Python/JSON |
| Host `graph.json` keys | `directed, graph, hyperedges, links, multigraph, nodes` | Python/JSON |
| Host `LESSONS.md` | 866 bytes, 217 estimated tokens | `wc`, `ceil(bytes/4)` |
| Host `.graphify_learning.json` | 1,252 bytes, 313 estimated tokens, 2 overlay nodes | Python/JSON |
| Graphify reference version | 0.9.95, commit `92741d1…` | `git`, `pyproject.toml` |

### 0.1 Concrete source-version drift already present

The host corpus asserts older source facts than the live files:

- `soul/01-identity-compliance.md:3` asserts `/root/.hermes/SOUL.md` is "21631 chars".
- `soul/25-measured-facts.md:3` asserts "23148 chars / 29733 bytes".
- The live file is 24,686 chars / 31,279 bytes (measurement above; version string at
  `SOUL.md:13-15`).
- `soul/25-measured-facts.md:6` asserts graphify 0.9.91, while the inspected reference is 0.9.95
  (`spec:15-16`).
- `soul/25-measured-facts.md:14` asserts 524 nodes / 646 links after prune; the live host
  `graph.json` is 525 nodes / 647 links / 12 hyperedges.
- `CHANGELOG.md:337` states `graph.json` carries `schema_version` and `graphify_version`; the live
  host graph carries neither key.

This is the strongest local justification for fingerprint-based source versions and for an explicit
relearn-offer step: "the corpus says X" cannot be treated as "the source currently says X". The
existing corpus rule that asserted counts rot is already recorded in the host skill
(`graphify-soul-router` SKILL.md "Asserted counts rot"); this design turns that rule into a
contract.

## 1. Primary-source findings

### 1.1 Graphify reflection is already deterministic, no-LLM work memory

- `graphify/reflect.py:1-27` defines `graphify reflect` as a deterministic work-memory reflection
  over `graphify-out/memory/`: preferred sources, tentative, contested, dead ends, corrections. It
  states "no LLM, stable sort orders, byte-stable output", that source nodes are scored with a
  signed time-decayed value (`useful` positive; `dead_end`/`corrected` negative) with a half-life,
  that a node is promoted only when corroborated by enough distinct results, and that nodes no
  longer in the graph are dropped.
- `graphify/reflect.py:51-52` fixes the two scoring defaults: 30-day half-life and
  `min_corroboration=2`.
- `graphify/reflect.py:275-285` implements the decay `0.5 ** (age_days / half_life_days)`; undated
  signals keep full weight, future-dated ones clamp to age 0.
- `graphify/reflect.py:310-347` records a signed weighted score plus distinct positive/negative
  result counts, then splits into `preferred` (positive-only at or above corroboration),
  `tentative` (positive-only below it), and `contested` (both signs, with "recency decides").
- `graphify/reflect.py:350-361` deduplicates dead ends/corrections by question with the latest
  write winning, and orders deterministically by `(date, question)`.
- `graphify/reflect.py:397-412` maps outcomes to signs (`useful=+1`, `dead_end/corrected=-1`) and
  collects dead ends and corrections.
- `graphify/reflect.py:489-521` renders `LESSONS.md`; the header explicitly says "verify before
  relying" and the dead-end section says "led nowhere; don't re-derive" — the artifact is
  orientation, not authority.
- `graphify/reflect.py:133-156` and `tests/test_reflect.py:127-139` make the input order
  deterministic: `(date, filename)`.
- `tests/test_reflect.py:371-379` asserts byte-stable output built twice from scratch;
  `tests/test_reflect.py:433-457` is the second-session benefit case; `tests/test_reflect.py:183-187`
  proves one save cannot promote a node; `tests/test_reflect.py:195-208` proves recency decides a
  contested verdict; `tests/test_reflect.py:220-226` proves corroboration counts distinct documents,
  not repeated citations inside one document; `tests/test_reflect.py:211-217` proves citations to
  nodes absent from the graph are dropped; `tests/test_reflect.py:401-412` proves `LESSONS.md`
  cannot be re-ingested as a memory document.

### 1.2 `save-result` is the append-only outcome record

- `graphify/ingest.py:296` fixes the outcome vocabulary to exactly
  `("useful", "dead_end", "corrected")`.
- `graphify/ingest.py:299-317` writes a Q&A memory doc that the next `--update` extracts into the
  graph; outcome/correction are written both to frontmatter (for `reflect`) and to an `## Outcome`
  body section (for graph round-trip).
- `graphify/ingest.py:319-320` rejects an unknown outcome; `graphify/ingest.py:331` makes each save
  its own file (`uuid4` suffix) so two saves in one second cannot silently overwrite each other.
- `graphify/ingest.py:333-346` writes bounded, hand-built YAML: `source_nodes` is capped at 10 nodes.
- `graphify/cli.py:1824-1854` is the CLI path; `graphify/cli.py:1835` is the argparse `choices`
  guard.

Adaptation consequence: outcome records are append-only and one save can never promote a lesson.
That matches the product rule that "one event cannot cause promotion" (`spec:170`) and the product
requirement that every tool action returns an audit event (`spec:37`).

### 1.3 Reflection output is a derived sidecar, not durable structural truth

- `graphify/reflect.py:42-48` states the learning layer is a sidecar
  (`.graphify_learning.json`) kept separate from `graph.json`; no `learning_*` field is stamped into
  the graph itself.
- `graphify/reflect.py:613-619` repeats the deliberate split: graph.json is durable structural
  truth; the overlay is merged only at display time.
- `graphify/reflect.py:758-821` builds the overlay from preferred/tentative/contested (not dead
  ends, which stay query-scoped), resolves citations to one canonical node id, and skips ambiguous
  or stale citations.
- `graphify/reflect.py:824-837` writes the sidecar with sorted keys and `indent=2` so identical
  input plus a fixed `now` is byte-identical.
- `graphify/reflect.py:717-738` fingerprints each node by the SHA-256 of its `source_file` content
  only (not path), and deliberately over-flags: any edit to the file marks every node in it stale.
- `graphify/reflect.py:740-755` caps provenance at 5 most-recent `(question, date, outcome)` items
  per node.
- `graphify/reflect.py:840-865` loads the overlay and recomputes a per-entry `stale` flag;
  `graphify/reflect.py:868-882` marks stale when the hash differs or the file vanished, and returns
  false when there is no file to track.
- Read surfaces merge the overlay: `graphify/cli.py:2132-2137` (node display) and
  `graphify/report.py:40-46, 375-380` (report section, omitted entirely when absent).

Adaptation consequence: learned status must be a derived read model keyed to a content fingerprint,
never a destructive rewrite of the durable knowledge record. Staleness must be over-flagging, not
under-flagging.

### 1.4 Freshness and update are two different mechanisms

- `graphify/reflect.py:527-561` implements `--if-stale` with **mtime**: the output is fresh if it
  is at least as new as every memory doc and graph/sidecar input. `graphify/cli.py:1871-1873,
  1892-1895` expose and honour the flag. This is only a skip gate; it does not decide content.
- By contrast, staleness of the *learned overlay* is **content-hash** based
  (`graphify/reflect.py:717-738, 868-882`).
- `graphify/hooks.py:248-258` and `graphify/hooks.py:323-332` refresh `LESSONS.md` after
  post-commit/post-checkout rebuilds, best-effort, never failing the hook.
- `graphify/watch.py:2751-2761` watches for changes, debounces, rebuilds code-only changes with
  no LLM, and only writes a `needs_update` flag for doc/paper/image changes that would need
  LLM-backed re-extraction.
- `graphify/watch.py:2709-2720` rebuilds for code changes or any deletion; a doc-only modification
  does not rebuild by itself. `tests/test_watch.py:77-100` pins exactly that.
- `graphify/watch.py:2726-2738` filters read-only filesystem events; `graphify/watch.py:2841-2852`
  dispatches after debounce.
- `graphify/cli.py:3897-3915` implements incremental mode from an existing `graph.json` and a
  manifest; `graphify/cli.py:3904-3911` explicitly preserves the semantic layer when the manifest
  is missing.
- `graphify/__main__.py:725` pins `PYTHONHASHSEED` for `update`, `extract`, `cluster-only`, and
  `label` so community detection output is run-to-run deterministic.
- Failure behaviour is also pinned: incremental builds match full builds
  (`tests/test_graph_invariance.py:131`), checkout path does not change the graph
  (`tests/test_graph_invariance.py:114`), deleted sources are evicted
  (`tests/test_watch.py:1091`), the rebuild lock is serialized and race-tested
  (`tests/test_watch.py:1529, 1552, 1592`), and a no-op rebuild is idempotent
  (`tests/test_watch.py:1607, 1640`).

Adaptation consequence: use hash equality for **version identity and stale mixing**, use a
monotonic knowledge/version high-water for **linearization** (`spec:84-95`), and never use mtime as
the correctness gate.

### 1.5 Existing SOUL corpus is already in a deterministic node-ready shape

The corpus is 28 files (`soul/01-…` through `soul/28-…`), each one topic per file, each fact on a
`- **Label** — fact` line (381 labels total). Representative sections this design depends on:

- Effort classification and activation sets: `soul/11-effort-router.md:3-6, 7-10, 11-15, 20-23`.
- L1 is "sapaan, one fact lookup, single command, a file read, a status check" and must not trigger
  process ceremony: `soul/11-effort-router.md:3, 16`; the playbook repeats "Do NOT query for L1
  tasks" at `soul/22-query-playbook.md:21`.
- Automatic trigger rule: process/depth/profession/verification/communication decisions trigger a
  query before answering (`soul/22-query-playbook.md:20`).
- Work-memory feedback loop and reflect-before-start: `soul/22-query-playbook.md:15, 18`.
- The corpus is on-demand, not injected every turn: `soul/28-operating-stack.md:13`.
- Since SOUL.md is resident but the corpus is not (`soul/01-identity-compliance.md:6`,
  `soul/25-measured-facts.md:5`), the graph indexes depth; it does not substitute identity.
- Quality gates that token savings must not erase: pre-mortem/steelman before delivery
  (`soul/09-premortem-steelman.md:3-7`), completion criteria (`soul/21-completion-criteria.md:3-10`),
  independent verification (`soul/21-completion-criteria.md:10`).
- Kill conditions that stop churn: measurable stop condition, anti-loop after 3 iterations,
  token-budget stop at 2x estimate (`soul/10-kill-criteria.md:3-8`).
- Evidence-based level changes and "update on new evidence":
  `soul/07-calibration-bayes.md:6-8, 12`, `soul/24-metrics-and-levels.md:10`.

### 1.6 What Graphify does not give us (must be adapted)

- Graphify has no notion of a source document version that can invalidate a whole learned overlay.
  Its staleness unit is a file hash against a node's `source_file`
  (`graphify/reflect.py:717-738, 868-882`), and `reflect` never removes superseded corpus text
  from an already-merged graph.
- Graphify's `reflect` confidence is a corroboration count plus signed decay, not the product's
  P/M/E quality gate (`graphify/reflect.py:51-52, 310-347` vs `spec:124-166`).
- Graphify has no operator-gated reset/accumulate migration; `reflect` simply regenerates from
  whatever memory docs exist (`graphify/reflect.py:564-610`).
- Graphify `watch` writes a flag for doc changes rather than executing semantic re-extraction
  (`graphify/watch.py:2751-2761`), which is the correct model here: **the system never performs AI
  extraction; Hermes/operator submits structured changes** (`spec:40`, `spec:97`).

Therefore every adaptation below uses the deterministic patterns (content hash, signed decay,
corroboration threshold, sidecar overlay, monotonic high-water, append-only outcomes) and adds the
product-specific missing pieces (P/M/E gate, source-version scoping, operator-gated relearn).

---

## 2. Conditional access contract

### 2.1 Goal

Hermes consults the system only when task context warrants it; L1 work never pays for a graph
query. The access decision must itself be cheap and deterministic. To avoid the circular cost of
"call the system to decide whether to call the system", the primary decision is a local,
hash-pinned rule artifact. The system optionally answers an `access_only` query for ambiguous
cases, with no knowledge payload.

### 2.2 Access policy artifact: `contracts/access_policy.v1.json`

Exact shape:

```json
{
  "schema_version": "soul.access_policy.v1",
  "policy_version": "1.0.0",
  "min_system_version": "0.1.0",
  "effort_source": "soul/11-effort-router.md",
  "effort_source_sha256": "<current hash of that corpus file>",
  "default_budget_estimated_tokens": 1024,
  "access_only_budget_estimated_tokens": 160,
  "l1_surfaces": ["greeting", "one_fact_lookup", "single_command", "file_read", "status_check"],
  "hard_consult_triggers": [
    "explicit_protocol_question",
    "explicit_depth_question",
    "explicit_profession_question",
    "explicit_metric_question",
    "explicit_verification_question",
    "explicit_stop_condition_question",
    "explicit_delivery_format_question",
    "explicit_learn_request",
    "irreversible_action",
    "critical_or_high_error_cost",
    "more_than_one_system_or_person",
    "production_change",
    "migration",
    "architecture_decision",
    "multi_component_design",
    "novel_and_irreversible",
    "multi_file_change_with_tests",
    "document_draft_with_quality_bar"
  ],
  "never_consult_surfaces": ["greeting", "one_fact_lookup", "single_command", "file_read", "status_check"],
  "tie_breaker": "HIGHER_LEVEL",
  "ambiguous_policy": "CONSULT_ACCESS_ONLY",
  "policy_unavailable_policy": "CONSERVATIVE",
  "user_override": "USER_COMMAND_SUPREME",
  "raw_source_reads_allowed": false,
  "source_manifest_hash_required": true,
  "sha256": "<hash of this file, excluding this field>"
}
```

Rules and precedence (deterministic; first match wins):

1. **User override first.** If the user explicitly says do not consult (or explicitly asks for a
   query), that wins; user command is supreme (`soul/01-identity-compliance.md:8`).
2. **Explicit request.** Any explicit protocol/depth/profession/metric/verification/stop/delivery/
   learn question -> `consult=true`, `reason_code=EXPLICIT_REQUEST`.
3. **Stakes.** Irreversible, critical, high cost of error, production, migration, architecture,
   multi-component, multi-system/person -> at least L3 -> `consult=true`; these track
   `soul/11-effort-router.md:5, 11-15`.
4. **Scope/novelty.** Multi-file change with tests or a quality-bar document -> at least L2 ->
   `consult=true`; novel + irreversible -> L3+.
5. **L1 surfaces.** If the task is only a greeting, one fact lookup, a single command, a file read,
   or a status check -> `consult=false`, `reason_code=L1_DIRECT`; this is the explicit
   "over-thinking is waste" rule (`soul/11-effort-router.md:16`, `soul/22-query-playbook.md:21`).
6. **Tie.** If two levels fit, choose the higher level but only its artefacts; do not inflate
   ceremony (`soul/11-effort-router.md:15`) -> `consult=true`.
7. **Ambiguous.** Call the system access-only (2.3) if remote policy is enabled; otherwise apply the
   conservative default: consult when any L2+ trigger is present, never for pure L1.
8. **Policy unavailable/hash mismatch.** Fail conservative: never read raw source; consult only for
   explicit process questions or L3+ stakes; record `POLICY_UNAVAILABLE` in the audit/recommendation
   metadata.

### 2.3 Access-only response (inside the frozen ten-tool surface)

Do not add an 11th public tool: the spec fixes exactly ten knowledge operations (`spec:101-112,
183`) and the plan fixes MCP discovery to exactly ten tools (`plan:225`). Conditional access is
therefore either:

- decided locally from 2.2, with no service call; or
- for ambiguous cases only, requested through `knowledge.query` with an access purpose and a
  160-token budget, returning **no knowledge nodes** and only this contract.

Exact additive response block (canonical JSON; counts against the budget per `spec:116`):

```json
{
  "access": {
    "schema_version": "soul.access.v1",
    "decision_id": "uuid",
    "audit_event_id": "uuid",
    "context_hash": "sha256",
    "consult": true,
    "reason_code": "EXPLICIT_REQUEST|L3_PLUS|SCOPE_L2|AMBIGUOUS|L1_DIRECT|POLICY_UNAVAILABLE|USER_OVERRIDE",
    "effort_level": "L1|L2|L3|L4|null",
    "activation": {
      "professions": ["P1"],
      "heuristics": ["Occam", "KISS"],
      "metrics": [],
      "output_contract": "BLUF one-liner"
    },
    "budget": {"estimated_tokens": 160, "response_ceiling": 160},
    "source_state": {
      "source_manifest_hash": "sha256",
      "required_knowledge_watermark": 0,
      "source_knowledge_high_watermark": 0,
      "projection_version": "string",
      "stale": false
    },
    "raw_source_payload_included": false
  }
}
```

### 2.4 Invariants

- No `knowledge.query` call for an L1 surface unless the user explicitly asks.
- The access-only response contains no node payload and no raw corpus text; it is metadata only.
- Every access decision, including local decisions that are later attached to a query, is
  reproducible from `(policy_hash, context_hash, task_surface)`.
- The policy artifact hash is recorded in every recommendation response; a stale policy is visible.
- Subagents never receive SOUL.md (`soul/01-identity-compliance.md:7`, `soul/25-measured-facts.md:10`);
  a subagent may consult the system for task process only when explicitly delegated to, and the
  result is task guidance, never identity. (Operator decision: whether to allow subagent access at
  all; default deny is safest.)

### 2.5 Access tests

File: `tests/test_access_policy.py`.
Fixture: `tests/fixtures/access_cases.json` (fields: `case_id`, `task_surface`, `expected_consult`,
`expected_reason`, `expected_effort`).

| ID | Setup | Expected |
|---|---|---|
| A1 | "What effort level is this migration?" | consult true, EXPLICIT_REQUEST, L3 |
| A2 | "halo" | consult false, L1_DIRECT |
| A3 | "What is the capital of France?" | consult false, L1_DIRECT |
| A4 | `git status` | consult false, L1_DIRECT |
| A5 | read one file | consult false, L1_DIRECT |
| A6 | irreversible production change | consult true, L3_PLUS, L3 |
| A7 | multi-system refactor | consult true, L3_PLUS, >=L3 |
| A8 | schema migration | consult true, L3_PLUS, L3 |
| A9 | novel + irreversible | consult true, L3_PLUS, >=L3 |
| A10 | multi-file change with tests | consult true, SCOPE_L2, L2 |
| A11 | two levels fit (tie) | consult true, higher level only |
| A12 | user says "do not query" | consult false, USER_OVERRIDE |
| A13 | missing policy file | conservative result, no raw read, POLICY_UNAVAILABLE |
| A14 | policy artifact hash mismatch | conservative result, no raw read |
| A15 | ambiguous remote access-only | consult true, AMBIGUOUS, budget 160 |
| A16 | access-only response | contains zero nodes, `raw_source_payload_included=false`, fits 160 |
| A17 | policy replay | same `(policy_hash, context_hash)` yields byte-identical decision |
| A18 | access audit | exactly one final audit event with `audit_event_id` (`spec:37, 118`) |

---

## 3. Action-recommendation contract

### 3.1 Goal and placement

When the access decision is `consult=true`, Hermes calls `knowledge.query` (one of the ten tools).
The response keeps the existing `QueryResponse` fields (`spec:114`) and adds a deterministic
`recommendation` block that returns exactly three action classes from learned SOUL:

- `best_actions` — safe to do now;
- `recommendations` — do only conditionally or after verification;
- `unnecessary_actions` — do not do; keeps the agent from re-deriving dead ends or over-processing
  L1 work.

No raw SOUL text is returned. Every item cites node ids, node versions, source versions, and
evidence ids; the caller can always ask `knowledge.get_node` for a bounded node payload.

### 3.2 Exact response contract

```json
{
  "recommendation": {
    "schema_version": "soul.recommendation.v1",
    "recommendation_id": "uuid",
    "audit_event_id": "uuid",
    "context_hash": "sha256",
    "effort_level": "L1|L2|L3|L4",
    "active_professions": ["P1", "P4"],
    "active_metrics": ["coverage percent", "repeat findings"],
    "best_actions": [
      {
        "action_id": "stable-string",
        "action": "short imperative",
        "why": "short deterministic rationale",
        "source_class": "protocol|preferred|tentative|contested",
        "source_node_ids": ["node-id"],
        "source_node_versions": [12],
        "evidence_ids": ["ev-id"],
        "evidence_groups": 4,
        "learned_score": "0.999999835",
        "gate": {"P": "0.82", "M": "0.71", "E": "0.64", "professional_level": 4, "promotion_eligible": true},
        "conditions": [],
        "expires_at": "RFC3339 or null"
      }
    ],
    "recommendations": [
      {
        "action_id": "stable-string",
        "action": "short imperative",
        "why": "short deterministic rationale",
        "condition": "text",
        "source_class": "tentative|contested",
        "source_node_ids": ["node-id"],
        "evidence_ids": ["ev-id"],
        "evidence_groups": 1,
        "verify_before_use": true
      }
    ],
    "unnecessary_actions": [
      {
        "action_id": "stable-string",
        "action": "short imperative",
        "why_unnecessary": "short deterministic rationale",
        "reason_code": "DEAD_END|CORRECTED|OVER_THINKING|GATE_NOT_MET|STALE_SOURCE_VERSION|REDUNDANT|TOKEN_BUDGET|NO_EVIDENCE",
        "suppressed_by_node_ids": ["node-id"],
        "risk_if_done": "short text",
        "last_observed_at": "RFC3339 or null"
      }
    ],
    "forbidden_actions": [
      {"action_id": "stable-string", "action": "short imperative", "reason_code": "RAW_SOURCE_READ|MODEL_CALL|SAMPLING|OUTBOUND_PROVIDER|OTHER"}
    ],
    "source_state": {
      "source_manifest_hash": "sha256",
      "active_source_version_ids": ["sv-id"],
      "required_knowledge_watermark": 0,
      "source_knowledge_high_watermark": 0,
      "projection_version": "string",
      "stale": false,
      "mixed_source_versions": false
    },
    "confidence": {
      "basis": "evidence_count_and_uncertainty",
      "value": "Decimal string in [0,1]",
      "uncertainty": "Decimal string in [0,1]",
      "what_would_change_it": "short text"
    },
    "limits": {
      "raw_source_payload_included": false,
      "estimated_tokens": 0,
      "truncated": false,
      "empty_vocabulary": false
    }
  }
}
```

Field semantics:

- `source_class=protocol` means the item is an extracted identity/protocol node from the active
  corpus (extraction confidence `EXTRACTED`); it is not learned preference.
- `source_class=preferred` requires positive corroboration at or above the configured
  `min_corroboration` with zero contradictory evidence, mirroring
  `graphify/reflect.py:327-347` and `tests/test_reflect.py:183-187`.
- `source_class=tentative` is positive below the threshold; it must be marked
  `verify_before_use=true` and must never be in `best_actions`.
- `source_class=contested` has both signs; it stays visible, with a deterministic verdict from
  signed decay and recency, never silently dropped (`graphify/reflect.py:335-346`,
  `tests/test_reflect.py:195-208`).
- `unnecessary_actions` can only cite negative evidence, a gate failure, a stale source version, a
  token-budget kill condition, or an L1 over-processing rule. It must not be a personality claim.
- `evidence_groups` counts distinct `independence_key` groups, not raw event count
  (`spec:160`; the Graphify analogue is distinct-doc corroboration at
  `graphify/reflect.py:296-297, 220-226`).

### 3.3 Deterministic derivation algorithm (no AI)

1. Resolve the query as in `spec:114`: domain first if supplied; otherwise deterministic lexical
   ranking over the project's read model (Graphify query semantics are upstream; this project owns
   its own adapter per `spec:46`, `plan:106`).
2. Build the candidate set from: effort-router nodes, active profession cards, heuristic catalog,
   metric index, mitigation playbook, kill criteria, completion criteria, and any learned nodes
   attached to them. This is bounded traversal under the request budget.
3. For each candidate action node compute:
   - `learned_status` ∈ `preferred|tentative|contested|dead_end|none`;
   - `learned_score` with Decimal and the adapted signed decay
     `sum(sign_j * reliability_j * 0.5^(age_days/half_life))`, quantized to 12 decimals, ties
     broken by lexicographic node id. Graphify's algorithm is the reference
     (`graphify/reflect.py:275-285, 310-347`); the product's Decimal/precision rules are
     `spec:124`.
   - `evidence_groups` after `independence_key` deduplication.
4. Eligibility:
   - `best_actions`: `source_class=protocol` and relevance above threshold, or
     `source_class=preferred` with `P>=0.70`, compatible source version, and no unresolved
     contradiction. P is the hard gate (`spec:39, 140, 162`).
   - `recommendations`: `tentative` or `contested`, never auto-promoted; one event cannot promote
     (`spec:170`).
   - `unnecessary_actions`: negative-only nodes, corrected items, over-processing rules for the L1
     surface, and actions whose only support is a stale source version.
5. Sort each list deterministically: `(-Decimal(score), action_id)`; the final canonical JSON must
   be byte-stable under input permutation (`spec:134, 164`).
6. Never mutate P/M/E. This block only reads scores and evidence. Evolution happens only through
   `knowledge.log_event`, `knowledge.record_outcome`, and `knowledge.evaluate` (`spec:105-108`),
   and only evidence can move P/M/E (`spec:124-166`). In particular, a source-version change or an
   operator relearn decision is **not evidence** and cannot raise a score.
7. An empty result is valid: return empty lists with `empty_vocabulary=true`; do not synthesize an
   action (`soul/22-query-playbook.md:6`).

### 3.4 Reason codes (exact enums)

- `best_actions`/`recommendations`: `PROTOCOL`, `PREFERRED`, `TENTATIVE`, `CONTESTED`.
- `unnecessary_actions`: `DEAD_END`, `CORRECTED`, `OVER_THINKING`, `GATE_NOT_MET`,
  `STALE_SOURCE_VERSION`, `REDUNDANT`, `TOKEN_BUDGET`, `NO_EVIDENCE`.
- `forbidden_actions`: `RAW_SOURCE_READ`, `MODEL_CALL`, `SAMPLING`, `OUTBOUND_PROVIDER`, `OTHER`.

### 3.5 Recommendation invariants

- No raw SOUL/corpus file content and no whole-file payload in the response; `limits.raw_source_payload_included=false`.
- No `best_action` from a single observation; no `best_action` from `tentative` or `contested`.
- Every listed item carries at least one source node id and, when learned, at least one evidence id.
- No recommendation can be emitted from an evidence item whose `source_version_id` is not in
  `active_source_version_ids`; instead emit `STALE_SOURCE_VERSION` in `unnecessary_actions`.
- A mixed-version response is either labelled `mixed_source_versions=true` or refused with
  `MIXED_SOURCE_VERSIONS`; it is never silently blended.
- The response carries `required_knowledge_watermark`, `source_knowledge_high_watermark`,
  `projection_version`, and `stale` (`spec:89, 92, 114`).
- Read-only recommendation invocations do not affect scores or knowledge watermarks
  (`spec:118`).

### 3.6 Recommendation tests

File: `tests/test_recommendation_contract.py`.
Fixtures: `tests/fixtures/recommendation_cases.json`, `tests/fixtures/learned_outcomes.json`.

| ID | Setup | Expected |
|---|---|---|
| R1 | one `useful` outcome | item is `tentative`, never `best_actions` |
| R2 | two distinct useful outcomes | item becomes `preferred` in `best_actions` |
| R3 | one useful + one dead end | item is `contested`, both signals visible |
| R4 | fresh dead end vs stale useful | contested verdict leans dead end (recency) |
| R5 | repeated citation in one document | counts as one group, still tentative |
| R6 | node no longer in graph | dropped from all source lists |
| R7 | P below 0.70 despite useful evidence | `GATE_NOT_MET` in `unnecessary_actions` |
| R8 | L1 surface | no process action emitted; `OVER_THINKING` for process ceremony |
| R9 | kill criterion token 2x estimate | `TOKEN_BUDGET` in `unnecessary_actions` |
| R10 | evidence from stale source version | `STALE_SOURCE_VERSION`; no `best_action` |
| R11 | unresolved contradiction | `contested`; no automatic winner; both node ids shown |
| R12 | permuted evidence input | byte-identical 12-decimal output (`spec:164`) |
| R13 | empty vocabulary | empty lists, `empty_vocabulary=true`, no fabrication |
| R14 | response payload | no raw source text, bounded size, `estimated_tokens` computed over canonical JSON |
| R15 | recommendation read | does not change P/M/E or knowledge watermark |
| R16 | audit | exactly one final audit event and returned `audit_event_id` |
| R17 | mixed versions with no mapping | `MIXED_SOURCE_VERSIONS` refusal |
| R18 | protocol identity node | `source_class=protocol`, no fabricated evidence ids |

---

## 4. Token-saving measurement contract

### 4.1 Why measurement, not a claim

Quality is never traded for token savings (`spec:39`); the benchmark must report output tokens and
quality results without invented targets (`spec:187`, `plan:269`). Token savings are therefore
recorded as a **paired measurement with a quality gate**, never as a headline number.

### 4.2 Baselines (exact definitions)

- `B1_RAW_SOUL`: read the complete `/root/.hermes/SOUL.md`; current bytes 31,279 -> 7,820
  estimated tokens. This is the identity slot, not the depth corpus; it should normally not be
  re-read at all.
- `B2_RAW_CORPUS`: read all 28 corpus files; current bytes 52,604 -> 13,151 estimated tokens.
- `B3_MIN_RAW_FILE`: read only the one raw corpus file that contains the needed label; measured per
  case, not asserted.
- `C1_QUERY_RESPONSE`: the complete canonical JSON `QueryResponse` including recommendation,
  provenance, audit/projection metadata, and truncation fields (`spec:116`).
- `C2_ACCESS_ONLY`: the access-only response, no knowledge payload; ceiling 160 estimated tokens.

### 4.3 Exact measurement record

```json
{
  "schema_version": "soul.token_measurement.v1",
  "measurement_version": "1.0.0",
  "case_id": "string",
  "task_class": "L1|L2|L3|L4",
  "context_hash": "sha256",
  "source_state": {
    "source_manifest_hash": "sha256",
    "engine_version": "0.9.95",
    "scoring_contract_version": "string",
    "graphify_adapter_version": "string"
  },
  "baseline": {
    "kind": "B1_RAW_SOUL|B2_RAW_CORPUS|B3_MIN_RAW_FILE",
    "paths": ["/root/.hermes/SOUL.md"],
    "utf8_bytes": 31279,
    "estimated_tokens": 7820,
    "latency_ms": 0
  },
  "candidate": {
    "operation": "knowledge.query",
    "canonical_json_bytes": 0,
    "estimated_tokens": 0,
    "latency_ms": 0,
    "budget_estimated_tokens": 1024,
    "truncated": false,
    "includes_metadata": true
  },
  "delta": {
    "estimated_tokens_saved": 0,
    "ratio_candidate_over_baseline": "Decimal string",
    "raw_source_read_required": false
  },
  "quality": {
    "rubric_id": "string",
    "rubric_version": "string",
    "required_facts": 6,
    "facts_present": 6,
    "regression": false,
    "evidence": ["case-specific assertion ids"]
  },
  "decision": "adopt|reject",
  "reject_reasons": ["QUALITY_GATE_FAILED|OVER_BUDGET|MIXED_SOURCE_VERSIONS|STALE_PROJECTION"],
  "tokenizer_claimed": false,
  "replay_sha256": "sha256"
}
```

Rules:

- `estimated_tokens = ceil(utf8_byte_length / 4)` over the canonical payload bytes exactly as
  `spec:116`; it is labelled an estimate and never claimed to be a provider tokenizer.
- The candidate token count includes **all** canonical fields. Transport envelopes (CLI stdout
  wrapping, HTTP headers, MCP framing) are excluded (`spec:116`).
- Budgets below 128 return `BUDGET_TOO_SMALL` with no knowledge payload; metadata overflow is an
  error, never an over-budget success (`spec:116`).
- The same canonical payload must be measured across CLI, local API, and MCP (`spec:116`,
  `plan:225`).
- `decision=adopt` requires `quality.regression=false`; token savings alone can never decide.
- Latency is recorded from real executions; no target or estimate is written as a result
  (`spec:187`, `plan:269`).

### 4.4 Measurement protocol

1. Freeze `case_id`, task text, source manifest hash, engine version, and adapter version.
2. Run the baseline read and record bytes/estimated tokens/latency from the real file.
3. Run the candidate operation and capture the canonical payload bytes.
4. Run the fixed quality rubric with independent expected facts; a failed rubric rejects the case.
5. Replay the identical run; the record must be byte-identical except measured latency.
6. Aggregate only by exact arithmetic over records; never average across different source manifest
   hashes or engine versions.

### 4.5 Token tests

File: `tests/test_token_savings.py`.
Fixtures: `tests/fixtures/token_savings_cases.json` (at least 12 cases, per `plan:126`).

| ID | Setup | Expected |
|---|---|---|
| T1 | byte lengths 0,1,3,4,5,127,128 | `ceil(bytes/4)` exactly |
| T2 | payload with provenance/metadata/truncation | all fields included in count |
| T3 | current SOUL.md fixture | baseline bytes 31279, estimated 7820 |
| T4 | current corpus fixture | baseline bytes 52604, estimated 13151 |
| T5 | budget 127 | `BUDGET_TOO_SMALL`, no knowledge payload |
| T6 | metadata cannot fit | error, not over-budget success |
| T7 | response at exact budget | bytes-derived estimate <= budget |
| T8 | quality regression | `decision=reject` despite token savings |
| T9 | CLI/API/MCP same case | identical canonical bytes |
| T10 | raw read forbidden | no whole-file payload; requesting one returns `RAW_SOURCE_READ_FORBIDDEN` |
| T11 | replay | identical record except latency |
| T12 | no tokenizer claim | `tokenizer_claimed=false` always |
| T13 | B3 raw file baseline | measured per case, not hard-coded |
| T14 | access-only response | <=160 estimated tokens, zero nodes |
| T15 | stale projection | `STALE_PROJECTION` reject reason |
| T16 | measurement across engine versions | aggregation refused or partitioned |

---

## 5. Source versions, supersession, contradictions, and relearn modes

### 5.1 Source-version registry

Immutable table `source_version`:

| Field | Type | Rule |
|---|---|---|
| `source_version_id` | uuid/string | server-generated; never reused |
| `source_id` | enum | `SOUL_MD`, `SOUL_CORPUS`, `OPERATOR_IMPORT` |
| `source_uri` | string | absolute path or URI |
| `content_sha256` | 64-hex | SHA-256 of raw bytes |
| `byte_length` | int | raw bytes |
| `char_length` | int | Unicode chars |
| `line_count` | int | line count |
| `file_manifest` | list | per-file `{path, sha256, bytes, lines}` for multi-file sources |
| `manifest_hash` | 64-hex | SHA-256 over sorted `path\0sha256\n` |
| `observed_at` | RFC3339 | when the system saw it |
| `parser_version` | string | corpus parser version |
| `normalization_version` | string | `- **Label** — fact` normalization version |
| `source_state` | enum | `CURRENT`, `SUPERSEDED`, `DELETED` |
| `superseded_by` | source_version_id or null | set only by an applied relearn |

Every imported node and every evidence item stores `source_version_id`. Retrieval responses carry
the active `source_manifest_hash`; recommendation and scoring only accept evidence whose
`source_version_id` is compatible with the active version (see 5.7).

### 5.2 Change detection

Detection is by **hash equality**, never by a parsed claim inside the corpus:

- `SOUL_MD`: compare current `sha256` and byte length to the current `source_version`.
- `SOUL_CORPUS`: compare `manifest_hash`, file count, and per-file hashes.
- Also trigger an offer when `parser_version`, `normalization_version`, `scoring_contract_version`,
  or `engine_version` changes, because those change the meaning of already-imported nodes.

Graphify reference patterns: content hash is the fingerprint (`graphify/reflect.py:717-738`),
staleness is a mismatch or vanished file (`graphify/reflect.py:868-882`), and mtime is only a
recompute-skip gate (`graphify/reflect.py:527-561`). The product adopts the hash rule and rejects
mtime as correctness evidence.

On mismatch the system must:

1. append a non-knowledge audit event recording old/new source versions and the diff;
2. create a `RelearnOffer` (5.3);
3. set the active generation state to `RELEARN_PENDING` for reads that depend on source-derived
   nodes, or mark those reads `stale=true` with a retryable error (`spec:89`);
4. never auto-apply a mode.

### 5.3 Relearn offer contract

```json
{
  "schema_version": "soul.relearn_offer.v1",
  "offer_id": "uuid",
  "audit_event_id": "uuid",
  "detected_at": "RFC3339",
  "previous_source_version_id": "uuid",
  "current_source_version_id": "uuid",
  "compatibility": {
    "parser_version_changed": false,
    "normalization_version_changed": false,
    "scoring_contract_version_changed": false,
    "engine_version_changed": false
  },
  "diff": {
    "added": ["path"],
    "changed": ["path"],
    "removed": ["path"],
    "unchanged_count": 0
  },
  "reset_preview": {
    "nodes_created": 0,
    "nodes_reused": 0,
    "evidence_carried": 0,
    "evidence_quarantined": 0,
    "scores_recomputed_from_zero": true
  },
  "accumulate_preview": {
    "nodes_reused": 0,
    "nodes_new": 0,
    "nodes_superseded": 0,
    "nodes_contested": 0,
    "evidence_revalidated": 0,
    "evidence_quarantined": 0,
    "unmapped_evidence": 0
  },
  "mode_options": ["RESET", "ACCUMULATE"],
  "default_action": "NONE",
  "requires_operator_decision": true,
  "offer_expires_at": "RFC3339"
}
```

`reset_preview` and `accumulate_preview` are deterministic dry runs and must be byte-stable for the
same input; they are not requests to apply.

### 5.4 Operator decision contract

```json
{
  "schema_version": "soul.relearn_decision.v1",
  "decision_id": "uuid",
  "offer_id": "uuid",
  "mode": "RESET|ACCUMULATE",
  "decided_by": "operator-id",
  "decided_at": "RFC3339",
  "rationale": "short text",
  "expected_manifest_hash": "sha256",
  "approval_token": "opaque",
  "expected_previous_source_version_id": "uuid"
}
```

Rules:

- No decision -> `RELEARN_PENDING`; no automatic default, neither RESET nor ACCUMULATE.
- Decision whose `expected_manifest_hash` no longer matches -> `RELEARN_STALE_OFFER`; regenerate
  the offer.
- High-impact relearn requires explicit operator approval, consistent with
  `spec:174` ("High-impact changes require operator approval").
- The decision is immutable after apply. Changing mode requires a new offer + decision; reversing
  requires `knowledge.rollback` (5.9).

### 5.5 RESET algorithm

RESET = relearn from empty against the new source version only.

1. Validate decision (5.4) and re-read the source version; abort on hash mismatch.
2. Atomic backup (5.9), then append the relearn-started audit event.
3. Mark all previous source-derived nodes and all evidence with the previous
   `source_version_id` as `superseded`, and set `valid_to` to the switch `knowledge_seq`.
4. Import the new source into a **fresh generation**: new node revisions with new
   `source_version_id` provenance, no carried scores.
5. Evidence carry = 0. No old evidence may influence P/M/E; P/M/E are recomputed from zero and
   therefore read 0 with `U=1` until new evidence arrives (`spec:156`).
6. Professional level resets to 1 under the zero-evidence rule (`spec:156, 160`); promotion stays
   blocked until the normal thresholds are met (`spec:160, 162`).
7. Publish a new content-addressed generation and activate `CURRENT` only after all files and the
   manifest are durable (`spec:87`).
8. Append the single final audit event; return the mode, old/new manifest hashes, new generation,
   new source version, and rollback handle.
9. Old revisions remain queryable for audit but are never fed to the new generation's scoring.

### 5.6 ACCUMULATE algorithm

ACCUMULATE = merge new source knowledge with old, under explicit version scoping and a mapping
step; old evidence does not silently survive.

1. Validate decision and source hash; atomic backup.
2. Build a deterministic anchor map from old node anchors to new node anchors:
   - same normalized label + same section anchor -> same logical identity;
   - moves propagate by label + explicit `→ see **Target**` links;
   - ambiguous/multiple targets -> no mapping (fail closed).
3. Reuse old nodes whose logical identity maps and whose content hash is unchanged; create new
   revisions for changed payloads using the standard immutable revision rule
   (`spec:174`).
4. Relations:
   - same identity with changed payload -> add `supersedes` from new revision to old revision
     (`spec:76`);
   - same scope/label but contradictory payload -> keep both, add `contradicts` in both directions;
   - canonical content hash identical -> `duplicates`;
   - unchanged -> no relation.
5. Evidence rebinding:
   - evidence whose old anchor maps to a new node and whose `scope_fit` still holds is carried with
     `valid_from` preserved and the new `source_version_id` recorded as the *applicable* version
     only when the operator approves the mapping;
   - evidence whose anchor does not map is `quarantined=true` with `reason=UNMAPPED_SOURCE_ANCHOR`
     and excluded from scoring;
   - evidence marked superseded by a later correction stays superseded even if the file is unchanged
     (`graphify/reflect.py:350-361` dedupe-by-question recency analogue).
6. Recompute P/M/E deterministically over only eligible, version-compatible evidence with the
   frozen formulas (`spec:124-166`). A source-version change itself contributes no evidence term.
7. Publish new generation and manifest; `mixed_source_versions` is true only if the operator
   approved a mapping that leaves old-version evidence eligible, and every response must label it.
8. Append final audit event; return counts of reused/new/superseded/contested/quarantined.

### 5.7 Supersession, contradiction, duplicates, no stale mixing

Exact invariants:

- **Supersession is a relation, not a deletion.** Old revisions remain immutable and inspectable;
  retrieval marks them `superseded_by=<new-revision>` and excludes them from default scoring.
- **Contradictions are retained.** Both items keep provenance and evidence; scoring uses the
  existing `contradiction` term in P (`spec:126, 138`) and the deterministic contested verdict
  adapted from signed decay plus recency (`graphify/reflect.py:327-347`). A tie renders `even`
  and sets review status `needs_improvement`; no AI adjudication, no silent winner.
- **Duplicates are content-addressed**, matching `spec:134`; the lexicographically smallest
  `evidence_id` survives.
- **No stale mixing:** every node/evidence carries `source_version_id` and `valid_from/valid_to`.
  A response may include old-version items only when explicitly labelled
  `superseded`/`quarantined` for audit, or when an operator-approved mapping declares them eligible
  under `mixed_source_versions=true`. Default scoring, recommendation, and promotion exclude any
  evidence whose version is not compatible.
- A read that cannot satisfy the active source version returns a labelled stale/retryable error
  (`spec:89`) rather than blending versions.
- Quarantined evidence is reported separately with reasons, never folded into a score
  (`spec:126` requires unscorable provenance-missing evidence to be reported separately).

### 5.8 P/M/E evolution rule

- Only evidence records can change P/M/E. Allowed inputs: `knowledge.log_event`,
  `knowledge.record_outcome`, `knowledge.evaluate` (`spec:105-109`).
- A source-version change, a file hash change, an operator decision, a relearn offer, a backup, and
  a rollback are audit/administrative events; they have zero evidence weight.
- RESET does not fabricate a prior; zero evidence means zero score and maximum uncertainty
  (`spec:134, 156`).
- ACCUMULATE may only carry evidence the operator's mapping declares still valid. Unmapped or
  stale evidence is quarantined, so the score cannot rise from stale mixing.
- Promotion additionally requires the post-relearn evidence thresholds and uncertainty caps
  (`spec:160, 162`); a relearn can never trigger promotion.

### 5.9 Backup and rollback

Before any relearn apply:

1. Take a consistent backup of SQLite (SQLite backup API or `VACUUM INTO`, not a live file copy).
2. Snapshot the graph generation directory, its manifest, `CURRENT`, the source-version registry,
   and the active scoring-contract version.
3. Write a backup manifest with content hashes and the pre-relearn `knowledge_seq` /
   `source_manifest_hash`.
4. Verify the backup by restoring it into a scratch location and validating hashes before touching
   live state.

Rollback (`knowledge.rollback`, `spec:112`):

- Restore the exact pre-relearn generation and pointer atomically; the old generation is never
  deleted by apply until the new generation is validated.
- Restore the source-version pointer to the previous version and mark the applied version
  `superseded_by` the rollback record.
- Append exactly one audit event for the rollback with the reason and correlation id (`spec:37, 90`).
- Test byte-equivalence of restored `graph.json`, manifest hash, node revisions, and P/M/E values.
- Crash-after-backup, crash-after-SQLite-commit, crash-after-graph-publish, and crash-before-final-
  audit are separate test cases; recovery must complete or roll back, never halve the state
  (`spec:90-91`, `plan:243-248`).

### 5.10 Relearn tests

File: `tests/test_relearn_modes.py`; source-version unit tests in
`tests/test_source_versions.py`; fixtures `tests/fixtures/relearn_reset_cases.json`,
`tests/fixtures/relearn_accumulate_cases.json`, `tests/fixtures/rollback_cases.json`.

| ID | Setup | Expected |
|---|---|---|
| V1 | SOUL.md unchanged | no offer; same manifest hash |
| V2 | SOUL.md hash changed | offer with diff, `RELEARN_PENDING`, no auto mode |
| V3 | corpus file changed | offer lists path in `changed` |
| V4 | corpus file deleted | offer lists path in `removed` |
| V5 | parser/normalization/scoring version changed | offer with `compatibility.*_changed=true` |
| V6 | no operator decision | `RELEARN_PENDING`; queries never silently use mixed versions |
| V7 | RESET applied | evidence carried 0; P=M=E=0; U=1; level 1 (`spec:156, 160`) |
| V8 | RESET response | all nodes carry new source version; old revisions inspectable but superseded |
| V9 | ACCUMULATE applied, mapped anchors | unchanged nodes reused; changed nodes superseded; scores recomputed |
| V10 | ACCUMULATE, unmapped evidence | quarantined; excluded from P/M/E; reported separately |
| V11 | duplicate payload | `duplicates` relation; lexicographically smallest evidence id survives |
| V12 | contradictory payload | both retained with `contradicts`; no AI winner |
| V13 | contested tie | review status `needs_improvement`; no promotion |
| V14 | source change alone | P/M/E unchanged pre-relearn; no score movement from the hash |
| V15 | unmapped mixed versions | `MIXED_SOURCE_VERSIONS` refusal, not a blended success |
| V16 | approved mixed mapping | response labelled `mixed_source_versions=true`; old items labelled valid |
| V17 | promotion attempt during relearn | blocked below thresholds (`spec:160, 162`) |
| V18 | stale offer hash | `RELEARN_STALE_OFFER`; no apply |
| V19 | wrong mode | deterministic error; no partial apply |
| V20 | interrupted apply after backup | current generation unchanged; retry idempotent |
| V21 | interrupted apply after SQLite commit | recovery completes projection or rolls back; one final audit |
| V22 | rollback | exact pre-relearn manifest hash + graph bytes + P/M/E restored |
| V23 | rollback audit | exactly one final audit event, linked to original apply |
| V24 | dry-run previews | byte-identical on repeated runs |
| V25 | RESET rollback | previous generation restored, RESET generation still inspectable |
| V26 | ACCUMULATE rollback | mapping metadata preserved for audit; scores restore exactly |
| V27 | no model imports | forbidden-import/outbound-socket guard stays green during both modes (`spec:10, 182`) |
| V28 | every invocation | exactly one final audit event, including failed apply (`spec:85, 90, 179`) |

---

## 6. Mapping onto the frozen ten-tool surface

No 11th public tool is proposed. The contracts attach to existing operations:

| Contract | Existing operation | Attachment |
|---|---|---|
| Access decision | `knowledge.query` | optional `access_only` purpose; no node payload; <=160 tokens |
| Action recommendation | `knowledge.query` | additive `recommendation` block in canonical `QueryResponse` |
| Learned status read | `knowledge.query` / `knowledge.get_node` | provenance/version fields only; bounded node payload on explicit request |
| Source-version registry | `knowledge.evaluate` / `knowledge.review` | read-only source/version metadata |
| Relearn offer | `knowledge.review` | additive `relearn_offer` block; non-mutating |
| Propose relearn | `knowledge.propose_improvement` | structured proposal `kind=relearn_reset|relearn_accumulate`, expected manifest hash, rationale |
| Apply relearn | `knowledge.apply_improvement` | requires operator approval; atomic apply + audit |
| Rollback | `knowledge.rollback` | exact generation restore (5.9) |
| Outcome/evidence | `knowledge.log_event` / `knowledge.record_outcome` | the only inputs that can move P/M/E |
| Audit/read metadata | every response | `audit_event_id`, watermarks, projection version, stale flag |

If the operator wants dedicated `knowledge.access` / `knowledge.relearn` tools, the spec's fixed
ten-operation surface (`spec:101-112, 183`) and MCP discovery contract (`plan:225`) must be
re-approved first; that is a spec change, out of scope for this research note.

## 7. Fixtures and test-matrix requirements

Recommended fixture files (exact names) and minimum contents:

- `tests/fixtures/access_cases.json`: >=20 cases; every L1 surface, every hard trigger, user
  override, missing/mismatched policy, replay.
- `tests/fixtures/recommendation_cases.json`: >=18 cases; one-useful, corroborated, contested,
  recency flip, duplicate-in-doc, stale node, gate failure, L1, token-budget kill, stale version,
  contradiction, permutation, empty vocabulary, raw-read refusal, mixed-version refusal.
- `tests/fixtures/token_savings_cases.json`: >=12 complete-payload cases (per `plan:126`);
  includes boundary bytes, metadata overflow, BUDGET_TOO_SMALL, quality regression, cross-adapter.
- `tests/fixtures/source_versions.json`: current and synthetic old/new SOUL.md + corpus manifests
  with hashes and byte/char/line counts; must include the observed drift case (01/25 asserting old
  sizes).
- `tests/fixtures/relearn_reset_cases.json`, `tests/fixtures/relearn_accumulate_cases.json`: the V7-V19
  cases with exact old/new anchors, evidence, and expected score vectors.
- `tests/fixtures/rollback_cases.json`: pre/post hashes, crash points, expected restored hashes and
  P/M/E vectors.

Every implementation task that adopts one of these contracts should add its regression probe to the
same task's suite, consistent with `plan:34` and the product's no-unverified-completion rule
(`spec:189`, `plan:22`).

## 8. Residual risks and operator decisions

1. **Decision required:** exact `min_corroboration` and half-life for learned actions. The Graphify
   defaults are 30 days and 2 distinct results (`graphify/reflect.py:51-52`), but the product's
   quality gate is P>=0.70 with its own evidence rules (`spec:140, 162`). Recommendation: start
   with 30 days / 2 groups for orientation, but never let learned status replace the P gate.
2. **Decision required:** whether the access policy is local-only or may call an access-only query
   for ambiguous cases. Recommendation: local-first with access-only fallback; the fallback is
   capped at 160 tokens and returns no knowledge payload.
3. **Decision required:** whether subagents may consult the system. Default recommendation: deny
   unless explicitly delegated; SOUL identity is never inherited by children
   (`soul/01-identity-compliance.md:7`, `soul/25-measured-facts.md:10`).
4. **Decision required:** ACCUMULATE mapping approval granularity (per file vs per node anchor vs
   per evidence group). Recommendation: per anchor with deterministic ambiguity fail-closed.
5. **Risk:** the host graph is already out of step with the corpus (525/647 vs asserted 524/646)
   and carries no `schema_version`/`graphify_version`; the new system must not infer source or
   engine version from the read model. Put `engine_version`, `scoring_contract_version`, and
   `source_manifest_hash` in the generation manifest.
6. **Risk:** mtime-based freshness (`graphify/reflect.py:527-561`) can skip recomputation after a
   restore; the product uses content hashes for correctness and treats mtime only as a heuristic.
7. **Risk:** a relearn applied while a reader is mid-flight. The monotonic high-water + stale-read
   repair rule (`spec:87-89`) covers it; tests V20-V22 pin the crash cases.
8. **Risk:** treating Graphify's `watch` doc-change flag (`graphify/watch.py:2751-2761`) as a
   content update. The product never performs AI extraction; only explicit structured imports and
   operator decisions change knowledge (`spec:40, 97`).

## 9. Citation index (file:line)

Spec:
- `spec:8, 10, 34-42, 46` architecture and no-AI boundary.
- `spec:76-80` relations, node fields.
- `spec:84-95` event/projection/watermarks/audit.
- `spec:97` corpus as read-only seed, no AI extraction.
- `spec:101-112, 183` the ten tools.
- `spec:114-118` query/budget/audit rules.
- `spec:124-166` Decimal, evidence, P/M/E, level caps, composite.
- `spec:168-174` improvement lifecycle, operator approval, no semantic rewrite.
- `spec:179-189` acceptance criteria.
- `spec:201-203` pre-mortem stale/mathematics risks.

Plan:
- `plan:15-22` constraints, no AI, audit, evidence.
- `plan:58` feature matrix incl. watch/cache/incremental.
- `plan:104-106` projection tests, no semantic extraction.
- `plan:124-126` token/measurement contracts and fixtures.
- `plan:144` no raw-corpus reads.
- `plan:166` scoring properties.
- `plan:186-188` improvement/rollback.
- `plan:225` exactly-ten MCP tools.
- `plan:243-248` backup/restore/corruption recovery, source watch.
- `plan:267-269` acceptance evidence and no invented results.

Graphify reference (`/root/.hermes/cache/scratch/graphify-ref-20261024`):
- `graphify/reflect.py:1-27, 42-52, 104-156, 211-237, 275-285, 296-347, 350-361, 364-438,
  489-561, 564-619, 717-738, 740-755, 758-837, 840-882`.
- `graphify/ingest.py:296-370`.
- `graphify/cli.py:1824-1911, 2132-2137, 3897-3915`.
- `graphify/watch.py:2705-2761, 2790-2852`.
- `graphify/hooks.py:248-258, 323-332`.
- `graphify/__main__.py:725`.
- `graphify/report.py:40-46, 375-380`.
- `tests/test_reflect.py:127-139, 163-180, 183-187, 195-208, 211-217, 220-226, 333-379,
  394-412, 424-470, 527-700`.
- `tests/test_watch.py:77-100, 1091, 1529-1640`.
- `tests/test_graph_invariance.py:114, 131`.
- `CHANGELOG.md:337`.

Host corpus:
- `soul/01-identity-compliance.md:3, 6-8`; `soul/02-state-ledger.md:3, 10-16`;
  `soul/07-calibration-bayes.md:6-8, 12`; `soul/09-premortem-steelman.md:3-7`;
  `soul/10-kill-criteria.md:3-8`; `soul/11-effort-router.md:3-23`;
  `soul/20-execution-cycle.md:6, 10`; `soul/21-completion-criteria.md:3-10`;
  `soul/22-query-playbook.md:5-6, 15-21`; `soul/24-metrics-and-levels.md:4, 10`;
  `soul/25-measured-facts.md:3-16`; `soul/28-operating-stack.md:13-14`.