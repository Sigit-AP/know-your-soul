# Super Power Knowledge SOUL

A standalone, deterministic knowledge graph, audit, scoring, and
controlled-improvement service for the SOUL corpus. Hermes accesses it
directly — there is **no internal AI model, model API, embedding, vector call,
provider dependency, or outbound provider traffic** anywhere in the system.

Spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`
Plan: `docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md`

## Architecture

Python 3.14 modular monolith:

```
                 ┌────────────┐  ┌────────────┐  ┌────────────┐
                 │    CLI     │  │ local API  │  │ MCP server │   three access paths
                 │ (soul-     │  │ (Starlette │  │ (stdio /   │   over ONE service
                 │ knowledge) │  │  /uvicorn) │  │  HTTP)     │
                 └─────┬──────┘  └─────┬──────┘  └─────┬──────┘
                       └───────────────┼───────────────┘
                               KnowledgeService          ← ten audited operations
                                       │
        ┌──────────────────────────────┼──────────────────────────────┐
        │                              │                              │
  EventStore (SQLite WAL)      GraphProjector                    ScoreEngine /
  immutable events +           atomic read-model                 Review / Relearn
  revisions = source of truth  generations (Graphify)            state machines
```

* **Source of truth:** append-only SQLite WAL event store; every tool action
  (reads included) returns an audit event ID; every mutation is versioned and
  idempotent.
* **Read models:** content-addressed Graphify generations, validated and
  activated atomically through one `CURRENT` manifest.
* **Determinism:** frozen mathematical contracts (`docs/mathematics/`,
  `docs/contracts/`); scoring uses exact Decimal arithmetic; no formula or
  threshold may change without a new contract version.
* **Boundary:** provider/vector/model packages and entry points are forbidden
  and fail closed (`src/soul_knowledge/runtime_guard.py`); outbound sockets
  are denied; MCP is tools+resources only, no sampling.

## The ten operations

Exposed identically on all three access paths:

| # | Operation | Purpose |
|---|-----------|---------|
| 1 | `query` | Token-bounded primary-root retrieval with provenance, versions, confidence, and the three-list recommendation block |
| 2 | `get_node` | Single node with evidence and projection metadata |
| 3 | `get_subtree` | Bounded subtree traversal (depth `[1,6]`, relation allowlist, node/edge caps) |
| 4 | `log_event` | Append an immutable domain event |
| 5 | `record_outcome` | Attach an outcome to evidence |
| 6 | `evaluate` | Calibrated P/M/E scoring, uncertainty, level 1-5, promotion eligibility |
| 7 | `review` | Review state machine with minimum evidence, cooldown, hysteresis |
| 8 | `propose_improvement` | Structured diff proposal (never autonomous rewriting) |
| 9 | `apply_improvement` | Operator-gated apply with expected-version check and regression gate |
| 10 | `rollback` | Byte-exact reversal of an applied improvement or relearn |

Recommendation responses contain exactly `best_actions`, `recommendations`,
`unnecessary_actions` — never raw SOUL text. Conditional access is
hash-pinned: L1 tasks never call the system; `access_only` returns a zero
knowledge payload within 160 estimated tokens.

## Three access paths

1. **CLI** — `soul-knowledge <operation>` for all ten operations plus
   projection rebuild, backup, restore, and health.
2. **Persistent local API** — versioned `/v1` JSON, Unix socket by default,
   optional loopback HTTP; API key required beyond loopback.
3. **MCP server** — stdio (and optional Streamable HTTP), one startup
   allowlisted graph root, strict schemas/ranges, tools+resources-only
   capability surface, no sampling/prompts/logging/completions. See
   `docs/integration/hermes.md`.

## Install and run

```bash
uv sync                                    # pinned, locked environment (Python 3.14)
uv run pytest -q                           # full test suite
uv run pyright                             # type checking
uv run ruff check                          # lint
python bench/run.py check                  # no-system harness self-checks
python bench/run.py run                    # paired benchmark (exit 3 while integration is pending)
```

## Benchmark and verification

* `bench/` — frozen SOUL corpus snapshot (sha256 manifest, read-only source),
  24 frozen questions spanning the seven primary domains, N repeats, p50/p95
  latency, token-estimator parity against the frozen token-budget contract,
  a deterministic quality rubric, paired raw-SOUL vs system comparison, and a
  machine-readable JSON ledger. Token savings never excuse a quality
  regression. See `bench/README.md`.
* `docs/verification.md` — the evidence tables for the release gate.
* `docs/audit-release.md` — the independent audit and release decision record.
