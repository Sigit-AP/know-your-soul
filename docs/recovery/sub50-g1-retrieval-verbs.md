# SUB-50 — G1 retrieval verbs (contract v2): path / explain / affected / god_nodes

Date: 2026-10-26. Scope: `src/soul_knowledge/` (retrieval, service, contracts,
http_api, mcp_server, cli), `tests/`, `docs/contracts/` (new addendum),
`docs/integration/hermes.md`, `README.md`, `docs/verification.md`.

## Authority

Operator ruling 2026-10-26 (recorded in
`.superpowers/sdd/2026-10-24-superpower-knowledge-soul/progress.md`): the
operation surface expands from exactly-ten to ten + the four Graphify-parity
G1 retrieval verbs as **contract v2**. The frozen v1 contracts (spec section
8; the Task 3A scoring/measurement/access/recommendation/relearn/token-budget
contracts) are untouched — no v1 document was edited; the addendum lives at
`docs/contracts/retrieval-verbs-v2.md`. Feature-matrix row 37 (path / explain
retrieval parity) is now served for real.

## What was built

Core (`retrieval.py`, `RelationalGraph` — deterministic, model-free, over the
verbatim `graph.json` links list):

- `shortest_path(from_id, to_id)` — directed BFS with first-discovery parents
  over `(relation, target)`-sorted adjacency; self-path is the singleton node;
  unreachable target is the typed `no_path` outcome, never an exception.
- `explain_neighborhood(node_id)` — incoming links sorted by
  `(relation, source)`, outgoing by `(relation, target)`, plus per-relation
  incident counts. A relational explanation, not a subtree.
- `affected(node_id, depth, max_nodes)` — reverse BFS over in-links, depth
  `[1,6]`, cycle-safe (the seed is never re-reported), typed guards
  (`depth`, `max_nodes`).
- `god_nodes(limit)` — measured degree (every link row counts, in and out),
  ordered by `(-degree, node_id)`, `limit >= 1`.

Service (`service.py`): `path` / `explain` / `affected` / `god_nodes` — each
one audited invocation (`knowledge.<verb>`, exactly one final audit event,
success or typed failure), the shared snapshot/metadata envelope, the frozen
token-budget semantics (`BUDGET_TOO_SMALL` / `BUDGET_EXCEEDS_MAX` /
deterministic tail truncation with `truncated: true`). The path payload is
atomic by contract: it never truncates, so an undersized budget is
`METADATA_EXCEEDS_BUDGET`.

Contracts (`contracts.py`): `PathResponse`, `ExplainResponse`,
`AffectedResponse` (+ `AffectedNode`), `GodNodesResponse` (+ `GodNode`) — all
frozen, `extra="forbid"`, with `canonical_payload()` matching the CLI/API/MCP
wire byte-for-byte (modulo the per-invocation audit id).

Adapters: HTTP `/v1/knowledge/{path,explain,affected,god_nodes}` (payload
models with the contract ranges), MCP tools `knowledge_path` /
`knowledge_explain` / `knowledge_affected` / `knowledge_god_nodes` (closed
schemas, `depth` `[1,6]`, `max_nodes` `[1,1024]`, `limit` `[1,64]`, budget
`[128, max]`), CLI subcommands `path` / `explain` / `affected` / `god-nodes`
(via the shared `KNOWLEDGE_OPERATIONS` loop).

## Pinned-test updates (v1 "exactly ten" → v2 fourteen)

Each updated pin carries a comment naming the operator ruling and the v2
addendum:

- `tests/test_acceptance_e2e.py` — `TEN_OPERATIONS` → `V1_OPERATIONS` +
  `G1_OPERATIONS` + `KNOWLEDGE_OPERATIONS_V2`; `TestAuditedTools` renamed to
  the fourteen-op pin; the every-tool audit test now drives all 14 (14 calls,
  14 distinct audit ids, +14 audit events, +1 for the typed failure);
  `test_single_allowlisted_graph_root` tool count 10 → 14.
- `tests/test_mcp_server.py`, `tests/test_mcp_stdio_e2e.py` —
  `EXPECTED_TOOLS` extended by the four new tool names; counts 10 → 14.
- `tests/test_property_mcp_server.py` — surface pin 10 → 14 with the four
  operations in the bijection set; `_valid_arguments` extended; the pinned
  required-set snapshot extended; name-regex fuzz range 9 → 13.
- `tests/test_final_adapter_fixes.py` — relearn-surface pins updated:
  relearn still rides the ops route and adds nothing to the (now fourteen-op)
  knowledge surface.

## Probes

`tests/test_retrieval_verbs_v2.py` (18 tests): unit probes for tie-break,
directionality, self-path, cycle safety, empty graph, degree ranking and
bounds; service probes for determinism (two runs identical modulo the audit
id), typed `no_path`/`NODE_NOT_FOUND`, budget truncation and atomic-path
failure; audit parity (4 calls → exactly +4 audit events, watermark
unmoved, chain verifies); and all three surfaces probed for all four verbs
(HTTP dispatch, CLI subcommands incl. `god-nodes`, MCP tools incl. strict
schema rejection).

TDD: RED `18 failed`, exit 1 (pre-implementation) → GREEN `18 passed`.
