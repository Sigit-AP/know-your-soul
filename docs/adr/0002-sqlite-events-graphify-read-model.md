# ADR 0002 — Immutable SQLite event store as source of truth, Graphify-compatible read model as projection

- Status: Accepted (Task 1, 2026-10-25)
- Deciders: product owner (operator) + implementation
- Related: ADR 0001 (no-model boundary), spec sections 5, 7, 13.5, 13.3.

## Context

The system needs (a) an append-only audit trail in which every invocation produces exactly one final audit event, (b) versioned nodes, evidence, scores and improvement records with exact rollback, (c) deterministic graph retrieval and analysis, and (d) crash safety under concurrent readers and a single writer.

Graphify provides the deterministic graph machinery (canonical JSON, NetworkX node-link graphs, query/path/explain, clustering, reports, exports) but it is not a transactional store and does not publish a whole generation atomically: a rebuild writes `graph.json`, `GRAPH_REPORT.md`, labels, signatures and analysis as separate writes in a fixed order (`graphify/watch.py:2588-2619`, `graphify/cli.py:2697-2744`). Its MCP freshness key is `(st_mtime_ns, st_size)` (`graphify/serve.py:140-199`), which is not a consistency guarantee, and its report bytes contain `date.today()` and a commit stamp (`graphify/report.py:112,129,208-215`), so they are not reproducible.

## Decision

1. **SQLite in WAL mode is the single source of truth** for immutable domain events, node revisions, evidence, scores, improvement records, the invocation journal and the source-version registry. Domain events and audit completion events share one global monotonic `event_seq`; only knowledge-affecting domain events receive a monotonic `knowledge_seq`. Audit-only/read events never advance `knowledge_seq`.
2. **Graphify is a wrapped projection, never the store and never a publication mechanism.** The product owns:
   - a deterministic local fragment generator (replacing model-backed semantic extraction) that emits Graphify's node/edge schema with file-scoped IDs, routed through Graphify's validate + sanitize helpers;
   - one serialized writer process per corpus root, `dedup=False` on the guarded build, explicit prune sources, and one pinned partitioner with `PYTHONHASHSEED=0`;
   - a **generation directory + one atomic `CURRENT` manifest** binding graph JSON, report, labels, signatures, analysis, corpus high-water, schema version, partitioner version and content hashes, with non-deterministic fields (dates, commit stamps) excluded before hashing;
   - a product read-model validator that fails closed on unknown schema, endpoint/file-type/confidence errors, junk nodes, absolute-path leakage, secrets and hash mismatch;
   - reader-side freshness keyed on **graph content hash + projection version**, never `(mtime_ns, size)`.
3. **Publication is compare-and-swap on the knowledge high-water mark.** Artifacts are fsynced, then the manifest is made durable, then `CURRENT` is replaced atomically. An older knowledge watermark can never replace a newer one. Reads capture `required_knowledge_watermark` at invocation start; a read accepts only an active manifest whose graph hash validates and whose source watermark meets the target, otherwise it repairs synchronously once and then returns a retryable unavailable error with `stale=true`.
4. **Rebuild is always possible from SQLite + immutable source imports.** Atomic rename alone is explicitly not treated as a consistency guarantee; the graph can be rebuilt from zero and byte-compared against the current generation.
5. **Determinism at the boundaries.** Canonical one-line JSON with sorted nodes/links/hyperedges (`graphify/export.py:374-440`), the frozen `ceil(utf8_bytes/4)` token estimator over the complete canonical payload, Decimal-34 arithmetic with 12-decimal `ROUND_HALF_EVEN` serialization, and `PYTHONHASHSEED=0` for every deterministic run. Stage 1 delivers the guards and packaging; the store arrives in Task 2 and the projection in Task 3.

## Alternatives rejected

| Alternative | Why rejected |
|---|---|
| Graphify's `graph.json` as the source of truth | No transactions, no append-only audit, no revision history, no idempotency, and no atomic multi-artifact publication. Losing the graph would lose knowledge; the store must be recoverable from events. |
| A general-purpose database (Postgres/Neo4j/FalkorDB) or a hosted graph service | Outbound dependency, credentials, operational cost and non-deterministic external state; the workload is local, single-writer and small. DB push paths remain excluded (feature-matrix row 36). |
| mtime/size freshness or "atomic rename is enough" | Detected stale/torn read models are the pre-mortem "tiger" risk; a same-size rewrite with unchanged mtime would serve old content. Content hash + watermark CAS removes the class of bug. |
| Publishing Graphify's own report/HTML/analysis as the read model | Report bytes vary by day and repository, HTML is a visualization with XSS surface, and Graphify's HTML/report exporters pull model/embedding-oriented code. The read model is canonical JSON with derived views generated by the product. |
| Reader-side migration of old `graph.json` schemas | Graphify ships `GRAPH_SCHEMA_VERSION` but no migration code. The product pins an accepted schema range, fails closed on anything else, and rebuilds from events — which is deterministic and needs no compatibility shim. |
| Letting Graphify's watcher drive document updates | Stock Graphify only sets a `needs_update` flag for document changes (`graphify/watch.py:2679-2703`), so a docs-only corpus would never rebuild. The product owns the update driver. |

## Consequences, rollback and review triggers

- Positive: auditable, append-only history; exact rollback; a read model that is derivable and verifiable; deterministic artifacts that can be hashed and compared; one writer and many readers with WAL.
- Cost: the product owns publication, validation, junk classification, synchronisation and backup/restore. A crash mid-publication is a stale generation to repair, not a silent corruption.
- **Rollback:** the projection can be deleted and rebuilt from SQLite plus immutable source imports at any knowledge watermark; the generation manifest and `CURRENT` pointer are the only publication state, and a rollback restores the exact pre-change hashes, graph bytes and P/M/E values with exactly one linked audit event (Task 6/Task 9). Reverting Stage 1 leaves only documentation, packaging and `src/soul_knowledge/runtime_guard.py`, none of which the projection depends on yet.
- Review triggers: a Graphify schema/partitioner change; any proposal to serve a read directly from Graphify output; a measured concurrency or durability limit of the single-writer model; a requirement to query across multiple corpus roots (which would change the one-root containment contract recorded in ADR 0001 and the Hermes MCP contract).