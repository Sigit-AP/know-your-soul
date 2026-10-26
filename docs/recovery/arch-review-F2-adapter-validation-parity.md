# Arch review F-2 — dual adapter validation: parity guard decision

## Evidence

Architecture review (F-2, MEDIUM): the HTTP adapter validates with pydantic
models (`http_api._OPERATIONS[op].parse`); the MCP adapter validates with its
own JSON schemas plus manual `_op_*` request builders. Consistency rested on
a mirror comment and behavior-level parity tests — no proof the two
validators accept/reject the same inputs.

A 204-probe empirical mapping (scratch probe, SUB-54) measured **64
accept/reject divergences** across the 14 knowledge operations, in six
families:

1. HTTP `_Payload` models were non-strict — numeric strings (`depth: "2"`)
   and bools-as-ints (`depth: true`) parsed where MCP rejects them.
2. `_GetSubtreePayload` carried no bounds/enums — `depth: 0/99`,
   `mode: "random"`, unknown relations passed the adapter and raised an
   untyped `ValueError` deep in the traversal (a real 500-on-bad-input bug).
3. MCP `_check_property` returned early on `null` *before* the enum check —
   `mode: null` bypassed the closed vocabulary.
4. MCP `_op_rollback` accepted `{node_id, relearn_id}` both-set and
   `{relearn_id, to_version}` — cross-field rules only lived in the HTTP
   model validator (the service re-validated, so it was layer drift, not a
   silent accept).
5. Length caps / budget bounds / the `at` and `include_due` knobs / the
   approval shape — intentional, documented surface differences (below).
6. (Non-divergent: nested `Node`/`Evidence` models are shared verbatim.)

## Decision: (b) parity guard, not unification

Choice (a) — one shared schema source for both adapters — was rejected on the
measured evidence: the two adapters differ *by design* in transport shape
(`idempotency_key` in-args vs header, `node`/`evidence` nesting, MCP-only
`expected_version`), in client guidance (MCP maxLength/budget caps that guide
LLM callers vs HTTP's frozen typed service errors `BUDGET_TOO_SMALL` /
`BUDGET_EXCEEDS_MAX` / `NODE_NOT_FOUND`), and in exposed knobs (`at`,
`include_due` are HTTP/CLI-only). Unifying means touching frozen contract
models (`contracts.py`, `improvement.py`) and re-shaping the MCP tool
surface — out of scope and high-risk pre-launch for zero behavioral gain.

Instead: fix the genuine drift (families 1-4), and guard parity with a
contract test that *proves* equal accept/reject sets over the shared semantic
input space, with the intentional differences pinned in an explicit table
(spread or flip = build failure).

## Fix applied

- `http_api._Payload` is now `strict=True` (matches `contracts.py` request
  models): wire coercions rejected exactly as MCP's schema layer rejects
  them.
- `_GetSubtreePayload`: `depth [1,6]`, `max_nodes [1,1024]`,
  `max_edges [1,4096]`, `mode: TraversalMode`,
  `relation_allowlist: tuple[RelationKind, ...]` — mirrors `QueryRequest`.
- MCP `_check_property`: the enum check now precedes the null early-return;
  null can no longer bypass a closed vocabulary.
- MCP `_op_rollback`: mirrors `RollbackRequest`'s cross-field validator at
  the adapter (exactly one target; `to_version` only with `node_id`).
- The mirror comment on `_validation_summary` is replaced by a reference to
  the parity test.

## Pinned intentional divergences (in the test)

- **D1 budget bounds** — MCP rejects `token_budget` outside [128, max] at the
  schema; HTTP passes it to the service's frozen typed errors. Both reject
  the invocation overall (asserted end-to-end).
- **D2 length caps** — MCP maxLength guides clients; HTTP's frozen models cap
  only what the spec caps; overlong values fail typed downstream.
- **D3 `at` / `include_due`** — HTTP/CLI-only knobs; MCP rejects as unknown.
- **D4 approval shape** — MCP injects `proposal_id` and defaults
  `decided_at`; HTTP takes a complete `ApprovalRecord`.
- **D5 transport fields** — `idempotency_key`, `expected_version`, and the
  `node`/`evidence` nesting are excluded by the mapping rule.

## Regression probes

`tests/test_adapter_validation_parity.py` (134 tests): ~120 parametrized
probes over all 14 operations (valid bases, unknown/extra keys, coercion
edges, enum/null/bounds, nested model shapes, rollback cross-field,
approval shapes) asserting equal decisions plus the pinned table; structural
guards (operation inventory 1:1, diff-op and relation vocabularies equal the
domain enums, MCP fields ⊆ HTTP fields with the D3/D5 allowances); D1
end-to-end (HTTP `BUDGET_TOO_SMALL`/`BUDGET_EXCEEDS_MAX` vs MCP
`INVALID_ARGUMENTS`).

## Rollback note

The strict/bounds/enum hardening only rejects inputs that previously failed
downstream (typed or as 500s); no valid wire payload changes meaning. To roll
back, revert the five source edits and delete the parity test — the pinned
table documents exactly what reopens.
