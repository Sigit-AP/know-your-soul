# Connecting Hermes Agent to the SOUL knowledge MCP server

This product is a **pure external MCP consumer target**: Hermes supplies its
own model and calls the ten knowledge operations as first-class tools. Hermes
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
/root/superpower-knowledge-soul/.local/soul.sqlite3 # event store
```

## 2. Connect Hermes (stdio, recommended)

Use Hermes' own MCP command — no config files are edited by hand:

```bash
hermes mcp add soul-knowledge \
  --command /root/superpower-knowledge-soul/.venv/bin/python \
  --args -m soul_knowledge.mcp_server \
         --graph-root /root/superpower-knowledge-soul/.local/graph \
         --db /root/superpower-knowledge-soul/.local/soul.sqlite3
```

Then verify discovery:

```bash
hermes mcp test soul-knowledge
hermes mcp list
```

Hermes registers the ten operations as
`mcp__soul-knowledge__knowledge_query`, `…__knowledge_get_node`,
`…__knowledge_get_subtree`, `…__knowledge_log_event`,
`…__knowledge_record_outcome`, `…__knowledge_evaluate`,
`…__knowledge_review`, `…__knowledge_propose_improvement`,
`…__knowledge_apply_improvement`, `…__knowledge_rollback` and calls them like
native tools.

**Do not** pass `--env`: the server needs no environment variables, and
Hermes' stdio child filter (plus the server's own strict startup check) keeps
provider credentials out of the server process by construction.

The resulting server entry — written by Hermes, shown here for reference —
needs nothing else:

```yaml
mcp_servers:
  soul-knowledge:
    command: /root/superpower-knowledge-soul/.venv/bin/python
    args: ["-m", "soul_knowledge.mcp_server",
           "--graph-root", "/root/superpower-knowledge-soul/.local/graph",
           "--db", "/root/superpower-knowledge-soul/.local/soul.sqlite3"]
    enabled: true
    # no env, no provider keys, no project_path, no url required
```

The server registers no `sampling` or `elicitation` handlers, so those
capabilities are never advertised and any client request for them is
method-not-found; the entry above must not configure them.

## 3. Optional: Streamable HTTP on loopback

For a long-lived local server instead of a per-session stdio child:

```bash
/root/superpower-knowledge-soul/.venv/bin/python -m soul_knowledge.mcp_server \
  --graph-root /root/superpower-knowledge-soul/.local/graph \
  --db /root/superpower-knowledge-soul/.local/soul.sqlite3 \
  --transport http --host 127.0.0.1 --port 8080
```

The bind policy fails closed: only loopback binds need no API key; anything
wider is refused without `--api-key` (see `docs/operations.md`). Then:

```bash
hermes mcp add soul-knowledge --url http://127.0.0.1:8080/mcp
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

## 5. Sanitized resources

Three read-only resources are exposed, all sanitized (control/bidi/zero-width
stripping, secret redaction), canonical-JSON, and capped at 64 KiB:

- `soul://projection/manifest` — the active projection manifest;
- `soul://projection/stats` — node/edge counts, watermarks, content hash;
- `soul://projection/nodes` — bounded node summaries with a truncation flag.

Raw Graphify reports are never served; there are no PR/`gh` tools.
