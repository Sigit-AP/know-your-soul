# Connecting Hermes Agent to the Know Your Soul MCP server

This product is a **pure external MCP consumer target**: Hermes supplies its
own model and calls the fourteen knowledge operations as first-class tools
(the frozen ten v1 operations plus the contract-v2 G1 retrieval verbs —
operator ruling 2026-10-26, addendum
`docs/contracts/retrieval-verbs-v2.md`). Hermes
source is never modified, and its configuration is only ever written through
Hermes' own `hermes mcp` commands — never hand-edited.

The server enforces the no-AI boundary itself (ADR 0001, contract
`docs/research/hermes-mcp-contract.md` §3.1):

- capabilities are exactly `tools` + `resources` — no sampling, prompts,
  logging, completions or elicitation handlers exist, so a client request for
  any of them is refused (`-32601`), never answered;
- startup runs `runtime_guard.verify_runtime(..., strict_env=True)` and
  **refuses to start** if provider credentials or model-asset variables are
  present in its environment;
- the outbound socket guard is installed for the process lifetime;
- the graph root is fixed at startup; no tool accepts `project_path` or any
  path, and unknown properties are rejected (`isError: true`, typed
  `INVALID_ARGUMENTS` envelope with an audit event id).

## 1. Prerequisites

An initialized knowledge base: an event store and at least one published
projection under the graph root (see `docs/operations.md`). Example layout:

```
/root/superpower-knowledge-soul/.local/graph       # projection root (allowlisted)
/root/superpower-knowledge-soul/.local/know-your-soul.sqlite3 # event store
```

## 2. Connect Hermes (stdio, recommended)

Use Hermes' own MCP command — no config files are edited by hand:

```bash
hermes mcp add know-your-soul \
  --command /root/superpower-knowledge-soul/.venv/bin/python \
  --args -m know_your_soul.mcp_server \
         --graph-root /root/superpower-knowledge-soul/.local/graph \
         --db /root/superpower-knowledge-soul/.local/know-your-soul.sqlite3
```

Then verify discovery:

```bash
hermes mcp test know-your-soul
hermes mcp list
```

Hermes registers the fourteen operations as
`mcp__know-your-soul__knowledge_query`, `…__knowledge_get_node`,
`…__knowledge_get_subtree`, `…__knowledge_log_event`,
`…__knowledge_record_outcome`, `…__knowledge_evaluate`,
`…__knowledge_review`, `…__knowledge_propose_improvement`,
`…__knowledge_apply_improvement`, `…__knowledge_rollback` — plus the four
contract-v2 G1 retrieval verbs `…__knowledge_path`, `…__knowledge_explain`,
`…__knowledge_affected`, `…__knowledge_god_nodes` — and calls them like
native tools.

**Do not** pass `--env`: the server needs no environment variables, and
Hermes' stdio child filter (plus the server's own strict startup check) keeps
provider credentials out of the server process by construction.

The resulting server entry — written by Hermes, shown here for reference —
needs nothing else:

```yaml
mcp_servers:
  know-your-soul:
    command: /root/superpower-knowledge-soul/.venv/bin/python
    args: ["-m", "know_your_soul.mcp_server",
           "--graph-root", "/root/superpower-knowledge-soul/.local/graph",
           "--db", "/root/superpower-knowledge-soul/.local/know-your-soul.sqlite3"]
    enabled: true
    # no env, no provider keys, no project_path, no url required
```

The server registers no `sampling` or `elicitation` handlers, so those
capabilities are never advertised and any client request for them is
method-not-found; the entry above must not configure them.

## 3. Optional: Streamable HTTP on loopback

For a long-lived local server instead of a per-session stdio child:

```bash
/root/superpower-knowledge-soul/.venv/bin/python -m know_your_soul.mcp_server \
  --graph-root /root/superpower-knowledge-soul/.local/graph \
  --db /root/superpower-knowledge-soul/.local/know-your-soul.sqlite3 \
  --transport http --host 127.0.0.1 --port 8080
```

The bind policy fails closed: only loopback binds need no API key; anything
wider is refused without `--api-key` (see `docs/operations.md`). Then:

```bash
hermes mcp add know-your-soul --url http://127.0.0.1:8080/mcp
```

Keep the loopback/Unix-socket boundary; do not expose the port externally.

## 4. What the tools return

- Success: one `TextContent` whose text is the **canonical JSON payload** —
  byte-identical (modulo the per-invocation `audit_event_id`) to the CLI
  stdout payload for the same operation, including `projection_version`,
  watermarks, `estimated_tokens` and the truncation flag.
- Failure: `isError: true` with a typed envelope
  `{"error": {"code", "message", "audit_event_id"}}` — codes include
  `INVALID_ARGUMENTS`, `BUDGET_TOO_SMALL`, `BUDGET_EXCEEDS_MAX`,
  `NODE_NOT_FOUND`, `STALE_PROJECTION` (retryable), `UNKNOWN_TOOL`. Raw
  exception text is never returned.
- Every invocation — including rejected ones — produces exactly one final
  audit event in the store, so Hermes-side calls are fully traceable.

### The four G1 retrieval verbs (contract v2)

Addendum `docs/contracts/retrieval-verbs-v2.md`. All four are read-only,
deterministic (BFS over the graph's links list, sorted tie-breaks — no model),
token-budgeted, and audited like every other operation:

- `knowledge_path {from_id, to_id, token_budget?}` — the shortest directed
  path; `status: "no_path"` is a typed result, never an error. The path is
  atomic: an undersized budget is `METADATA_EXCEEDS_BUDGET`, never a partial
  path.
- `knowledge_explain {node_id, token_budget?}` — one node's relational
  neighborhood: sorted incoming/outgoing typed links plus per-relation counts
  (not a document subtree — that is `knowledge_get_subtree`).
- `knowledge_affected {node_id, depth?, max_nodes?, token_budget?}` — the
  reverse dependency walk: what depends on this node, transitively, bounded
  (`depth` `[1,6]`), with typed `guards_hit`.
- `knowledge_god_nodes {limit?, token_budget?}` — the top hub nodes by
  measured graph degree (`limit` `[1,64]`, ordered by `(-degree, node_id)`).

## 5. Sanitized resources

Four read-only resources are exposed, all sanitized (control/bidi/zero-width
stripping, secret redaction), canonical-JSON, and capped at 64 KiB:

- `soul://projection/manifest` — the active projection manifest;
- `soul://projection/stats` — node/edge counts, watermarks, content hash;
- `soul://projection/nodes` — bounded node summaries with a truncation flag;
- `soul://relearn/status` — the relearn gate (below).

Raw Graphify reports are never served; there are no PR/`gh` tools.

## 6. Relearn operations (operator-gated, spec 13.3)

When SOUL.md or the corpus changes, the system emits a deterministic
`RelearnOffer` and sets `RELEARN_PENDING`. It never picks a mode itself: the
RESET/ACCUMULATE decision is an explicit operator act. The lifecycle rides
the **ops surface** (CLI subcommands and `POST /v1/ops/...`) — the fourteen
knowledge operations and the fourteen MCP tools are unchanged by it, and no
relearn tool exists on the knowledge surface:

```bash
know-your-soul relearn-scan '{"path": "/path/to/corpus", "kind": "SOUL_CORPUS"}'
know-your-soul relearn-status            # pending offer + offer_id, or null
know-your-soul relearn-preview '{"mode": "RESET"}'       # or "ACCUMULATE"
know-your-soul relearn-apply '{"mode": "RESET", "approval": {"offer_id": "...", "approved_by": "you"}}'
```

`relearn-scan` registers the baseline on first run (offer `null`) and emits
an offer only when the source fingerprint changes. `relearn-apply` is
refused with a typed `OperatorDecisionRequired` unless the approval names
the pending `offer_id` and mode; every apply takes a verified atomic backup
(receipt carries `relearn_id`), and `knowledge_rollback` /
`POST /v1/knowledge/rollback {"relearn_id": "..."}` restores the exact
pre-relearn state.

Over MCP the gate is **read-only**: `soul://relearn/status` returns the
`relearn_state`, the pending offer (including its `offer_id` and the file
diff), and per-mode preview *counts* (`evidence_carried`,
`evidence_quarantined`, `nodes_reused`, `promotion_blocked`). A resource
read never writes. The approval itself is deliberately not an MCP tool —
the operator decides on the CLI/HTTP ops surface, while Hermes (or any MCP
client) can see that a decision is pending and what each mode would do.
