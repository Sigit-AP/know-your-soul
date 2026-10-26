# Retrieval Verbs Contract (G1)

Contract version: 2.0.0 (addendum)
Status: ACTIVE (2026-10-26)
Authority: operator ruling 2026-10-26, recorded in
`.superpowers/sdd/2026-10-24-superpower-knowledge-soul/progress.md` ("permukaan
operasi diperluas dari tepat-sepuluh menjadi sepuluh + verb retrieval G1
(path/explain/affected/god_nodes) sebagai kontrak v2").
Scope: this addendum **extends** the knowledge surface from the frozen ten v1
operations (spec section 8) to fourteen. The v1 contracts in this directory —
including the frozen Task 3A contracts (scoring, measurement, access,
recommendation, relearn, token-budget) — are **unchanged**; the ten v1
operations keep their exact semantics. Feature-matrix row 37 (query / path /
explain retrieval parity with Graphify) is served by this addendum.

Any change to the verb semantics below requires a new contract version and
operator approval.

## Shared guarantees

All four verbs are **reads** over the published projection artifact
(`graph.json`), identical in discipline to `get_node`/`get_subtree`:

- exactly one final audit event per invocation (`knowledge.path`,
  `knowledge.explain`, `knowledge.affected`, `knowledge.god_nodes`), successes
  and typed failures alike;
- read linearization at invocation start (`required_knowledge_watermark`), the
  shared audit/projection metadata envelope, and the same stale-projection
  repair rule (one synchronous repair, then the typed retryable
  `STALE_PROJECTION`);
- the frozen token-budget semantics: `token_budget` below 128 is
  `BUDGET_TOO_SMALL`, above the configured maximum is `BUDGET_EXCEEDS_MAX`,
  and a response that cannot fit is never returned — list payloads truncate
  deterministically from the tail with `truncated: true`; a payload that
  cannot truncate (see `path`) fails typed with `METADATA_EXCEEDS_BUDGET`;
- an absent `node_id`/endpoint is the typed `NODE_NOT_FOUND` error;
- all algorithms are deterministic and model-free: BFS over the **links list**
  (never a collapsed simple-graph edge view), with adjacency sorted by
  `(relation, peer)`; rankings are pure functions of the link rows.

## `knowledge.path(from_id, to_id, token_budget=1024)`

The shortest **directed** path between two nodes: BFS from `from_id` over the
sorted `(relation, target)` adjacency with first-discovery parents, so the
returned path is a deterministic function of the graph artifact.

- `status: "found"` — `nodes` lists the path in order, `edges` the traversed
  typed links, `hops = len(edges)`. A self-path (`from_id == to_id`) is
  `found` with the singleton node and zero edges.
- `status: "no_path"` — the target is a known node but no directed route
  exists. This is a typed **result**, never an exception.
- An unknown endpoint (either side) is `NODE_NOT_FOUND`.
- The path is **atomic**: it is never truncated. If the complete response
  exceeds `token_budget`, the call fails typed with `METADATA_EXCEEDS_BUDGET`.

## `knowledge.explain(node_id, token_budget=1024)`

A **relational** explanation of one node — distinct from `get_subtree`, which
returns a bounded document subtree. The response carries the node view, the
`outgoing` links sorted by `(relation, target)`, the `incoming` links sorted
by `(relation, source)`, and `relation_counts` (per-relation incident count,
outgoing and incoming combined). Link lists truncate tail-first under budget
pressure (longer list first; ties drop an incoming edge); the node view never
truncates away.

## `knowledge.affected(node_id, depth=2, max_nodes=256, token_budget=1024)`

Reverse traversal: the nodes that **depend on** `node_id` — transitive
in-links walked breadth-first up to `depth` hops (`depth` in `[1,6]`,
`max_nodes` in `[1,1024]`). Each hit carries `node_id`, `depth`, the
`relation` of the matched in-link, and `via` (the node through which the
dependency reaches the seed). The seed is never re-reported through a cycle.
Breached caps surface as typed guard names in `guards_hit` (`"depth"`,
`"max_nodes"`). Hits truncate tail-first under budget pressure.

## `knowledge.god_nodes(limit=10, token_budget=1024)`

The top hub nodes by **measured degree**: every link row touching the node
counts (in and out; parallel typed relations count separately, because the
links list is the source of truth). Ordering is `(-degree, node_id)` — total
and deterministic; `limit` is bounded to `[1,64]`. Nodes carry `node_id`,
`summary`, `degree`. An empty graph returns an empty list. The list truncates
tail-first under budget pressure.

## Adapter mapping

| Verb | CLI subcommand | HTTP route | MCP tool |
|------|----------------|------------|----------|
| `path` | `know-your-soul path` | `POST /v1/knowledge/path` | `knowledge_path` |
| `explain` | `know-your-soul explain` | `POST /v1/knowledge/explain` | `knowledge_explain` |
| `affected` | `know-your-soul affected` | `POST /v1/knowledge/affected` | `knowledge_affected` |
| `god_nodes` | `know-your-soul god-nodes` | `POST /v1/knowledge/god_nodes` | `knowledge_god_nodes` |

MCP schemas are closed (`additionalProperties: false`) with the same ranges as
the service: `depth` `[1,6]`, `max_nodes` `[1,1024]`, `limit` `[1,64]`,
`token_budget` `[128, max]`. No schema carries any path-like property; the
graph root stays fixed at startup.
