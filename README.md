<div align="center">

**🇬🇧 English** | [🇮🇩 Bahasa Indonesia](README.id.md)

</div>

# Know Your Soul

<p align="center">
  <a href="https://github.com/Sigit-AP/know-your-soul/actions/workflows/ci.yml"><img src="https://github.com/Sigit-AP/know-your-soul/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"/></a>
  <a href="https://pypi.org/project/know-your-soul/"><img src="https://img.shields.io/pypi/v/know-your-soul" alt="PyPI"/></a>
  <img src="https://img.shields.io/badge/python-3.14-blue" alt="Python 3.14"/>
  <img src="https://img.shields.io/badge/coverage-99.4%25-brightgreen" alt="Coverage 99.4%"/>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue" alt="License: Apache-2.0"/></a>
</p>

**Know Your Soul** is a standalone, deterministic knowledge and learning graph
for [Hermes Agent](https://hermes-agent.nousresearch.com/docs). It stores what
the agent has learned as an auditable event-sourced graph, scores every node
with exact Decimal arithmetic, and improves itself only through operator-gated
proposals. There is **no internal AI model, model API, embedding, vector call,
provider dependency, or outbound provider traffic** anywhere in the system —
the model stays on the Hermes side; this service is the memory it can trust.

<p align="center">
  <img src="docs/assets/graph-formation.png" alt="Graph formation: real projection of the frozen benchmark corpus — 28 corpus files become 36 nodes and 35 typed edges, coloured by primary domain P1–P7" width="900"/>
</p>
<p align="center">
  <em>How a corpus becomes a graph: the real projection of the frozen benchmark corpus
  (36 nodes, 35 typed edges, coloured by primary domain). Generated from live project
  data — see <a href="docs/assets/README.md">docs/assets</a>.</em>
</p>

## Contents

- [Why Know Your Soul](#why-know-your-soul)
- [Key features](#key-features)
- [Quickstart](#quickstart)
  - [Install](#install)
  - [CLI](#cli)
  - [Local HTTP API](#local-http-api)
  - [MCP server (Hermes)](#mcp-server-hermes)
- [Architecture](#architecture)
- [Benchmark results](#benchmark-results)
- [The learning loop](#the-learning-loop)
- [The fourteen operations](#the-fourteen-operations)
- [Security and the no-AI boundary](#security-and-the-no-ai-boundary)
- [Contracts and operator-gated relearn](#contracts-and-operator-gated-relearn)
- [Development](#development)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License and attribution](#license-and-attribution)

## Why Know Your Soul

Agents re-read the same documents on every session and relearn the same
lessons the expensive way. Know Your Soul gives an agent one local,
deterministic memory:

- **Query instead of re-reading.** Token-bounded retrieval returns exactly
  `best_actions`, `recommendations`, `unnecessary_actions` — never raw corpus
  dumps. Measured mean token saving vs reading the raw corpus: **~94.2%**
  (24/24 frozen benchmark cases accepted).
- **Every answer is auditable.** Every operation — reads included — returns an
  audit event ID into an append-only, hash-chained SQLite event store.
- **Learning is scored, not vibes.** Each node carries calibrated P/M/E scores
  computed with exact Decimal arithmetic under a frozen contract.
- **Nothing improves itself without you.** Improvements are structured diff
  proposals; applying, relearning, and rolling back are operator-gated state
  machines with byte-exact rollback.

## Key features

| Capability | What you get |
|---|---|
| **14 audited operations** | Retrieval, evidence, scoring, review, improvement, and graph verbs — identical on CLI, HTTP, and MCP |
| **Event-sourced truth** | Append-only SQLite WAL event store; every mutation is versioned, idempotent, and hash-chained |
| **Content-addressed projections** | Atomic Graphify read-model generations, validated and activated through one `CURRENT` manifest |
| **Deterministic scoring** | Frozen mathematical contracts; exact Decimal arithmetic; no formula changes without a new contract version |
| **Operator-gated relearn** | Corpus changes emit a deterministic `RelearnOffer` — the system never chooses a relearn mode by itself |
| **Byte-exact rollback** | Every apply takes a verified atomic backup; rollback restores exact pre-apply hashes and scores |
| **Hard no-AI boundary** | Provider/vector/model entry points fail closed; outbound sockets denied; MCP is tools+resources only |
| **Three access paths** | CLI (`know-your-soul`), persistent local API (Unix socket / loopback HTTP), MCP server (stdio / HTTP) |

## Quickstart

### Install

From PyPI (requires Python 3.14):

```bash
pip install know-your-soul
```

Or from source with the bit-exact locked environment:

```bash
git clone https://github.com/Sigit-AP/know-your-soul.git
cd know-your-soul
uv sync --frozen
```

### CLI

Every command below was run against a fresh store and exits 0:

```bash
# Record knowledge (an audited, idempotent node upsert)
know-your-soul --store demo.sqlite3 --output-root graph log-event \
  '{"node_id":"p1-root","kind":"primary","domain":"P1",
    "summary":"P1 root: verification discipline",
    "provenance":["soul://demo"],"source_version_id":"demo-v1"}'

know-your-soul --store demo.sqlite3 --output-root graph log-event \
  '{"node_id":"n1","parent_id":"p1-root","domain":"P1",
    "summary":"Always verify before claiming done",
    "provenance":["soul://demo"],"source_version_id":"demo-v1"}'

# Rebuild the content-addressed projection
know-your-soul --store demo.sqlite3 --output-root graph rebuild
```

```json
{"activated":true,"content_hash":"9728d2…","node_count":1,
 "projection_version":"gen-9728d2000ab6f02b0afc104b", "...":"..."}
```

```bash
# Token-bounded retrieval with provenance, versions and recommendations
know-your-soul --store demo.sqlite3 --output-root graph query \
  '{"purpose":"verify before claiming done","token_budget":400}'
```

```json
{"routing":"ranked","results":[{"node_id":"n1","kind":"leaf",
  "summary":"Always verify before claiming done","...":"..."}],
 "recommendations":{"best_actions":[],"recommendations":["..."],
  "unnecessary_actions":[]},
 "audit_event_id":"706813cd1ed848df8394a0fb0c5693b3","...":"..."}
```

```bash
# Inspect a single node with evidence and projection metadata
know-your-soul --store demo.sqlite3 --output-root graph get-node '{"node_id":"n1"}'

# Liveness: hash chain, migrations, projection watermark — one JSON verdict
know-your-soul --store demo.sqlite3 health
```

### Local HTTP API

The persistent local API serves versioned `/v1` JSON over a Unix socket by
default (loopback HTTP optional, API key mandatory beyond loopback):

```bash
know-your-soul --store demo.sqlite3 --output-root graph serve --socket demo.sock

curl -s --unix-socket demo.sock -X POST http://localhost/v1/ops/health \
  -H 'content-type: application/json' -d '{}'

curl -s --unix-socket demo.sock -X POST http://localhost/v1/knowledge/query \
  -H 'content-type: application/json' \
  -d '{"purpose":"verify before claiming done","token_budget":400}'
```

### MCP server (Hermes)

Connect Hermes with its own MCP command — no config files are hand-edited:

```bash
hermes mcp add know-your-soul \
  --command /root/know-your-soul/.venv/bin/python \
  --args -m know_your_soul.mcp_server \
         --graph-root /root/know-your-soul/.local/graph \
         --db /root/know-your-soul/.local/know-your-soul.sqlite3

hermes mcp test know-your-soul
hermes mcp list
```

Hermes registers the fourteen operations as first-class tools
(`mcp__know-your-soul__knowledge_query`, `…__knowledge_get_node`, …) and calls
them natively. The server refuses to start if provider credentials are present
in its environment. Full setup: [docs/integration/hermes.md](docs/integration/hermes.md).

## Architecture

Python 3.14 modular monolith — three access paths over one service:

<p align="center">
  <img src="docs/assets/architecture.png" alt="Layered architecture: CLI / HTTP / MCP adapters over the KnowledgeService, domain and scoring layer, content-addressed projection, and the append-only event store" width="900"/>
</p>

- **Source of truth:** append-only SQLite WAL event store; every mutation is
  versioned and idempotent, every invocation (reads included) lands as exactly
  one final audit event.
- **Read models:** content-addressed Graphify generations, validated and
  activated atomically through one `CURRENT` manifest; repair re-derives
  torn or corrupted artifacts byte-identically.
- **Determinism:** frozen mathematical contracts ([`docs/mathematics/`](docs/mathematics/),
  [`docs/contracts/`](docs/contracts/)); scoring uses exact Decimal arithmetic;
  no formula or threshold may change without a new contract version.

## Benchmark results

`bench/` is a fully deterministic harness: a sha256-pinned corpus snapshot
(28 files, 52,604 bytes = 13,151 estimated tokens), 24 frozen questions
spanning the seven primary domains, a frozen token-estimator contract, and a
keyword-coverage quality gate — **no model is involved anywhere**. Token
savings never excuse a quality regression: a case is accepted only if it
clears its contract-compliant quality ceiling.

Latest ledger (repeats = 2, `bench/results/ledger-20261026T191540Z.json`):

| Metric | Result |
|---|---|
| Cases accepted | **24 / 24** |
| Mean token saving vs raw corpus | **~94.2%** (min 92.6%, max 96.8%) |
| Raw-corpus baseline | 13,151 estimated tokens per case |
| Median query latency (p50) | ~7.9 ms |
| Token estimator | exact parity (`ceil(utf8_bytes / 4)`) |

<p align="center">
  <img src="docs/assets/bench-impact.png" alt="Benchmark impact: per-case token saving versus the raw-corpus baseline across the 24 frozen questions — mean ≈ 94.2%, all 24 cases accepted by the quality gate" width="900"/>
</p>

Reproduce it yourself:

```bash
uv run python bench/run.py check   # harness self-checks (exit 0)
uv run python bench/run.py run     # paired benchmark, fresh JSON ledger
```

See [bench/README.md](bench/README.md) for the methodology and
[docs/verification.md](docs/verification.md) for the full release-gate evidence.

## The learning loop

Know Your Soul is not a static index — it is a learning graph. Outcomes
recorded against evidence move calibrated P/M/E scores through a review state
machine with minimum evidence, cooldown, and hysteresis. The chart below is
real: the P/M/E evolution of an actual graph node over a deterministic
12-outcome scenario, scored by the frozen `ScoreEngine` at fixed timestamps,
plus event-store growth over a real seed-import replay.

<p align="center">
  <img src="docs/assets/learning-timeline.png" alt="Learning timeline: P/M/E score evolution of a real graph node over twelve deterministic recorded outcomes, plus cumulative event-store growth" width="900"/>
</p>

Lifecycle events (relearn, backup, rollback) carry **zero** evidence weight —
only `log_event`, `record_outcome`, and `evaluate` can move scores, and a
relearn can never trigger promotion.

## The fourteen operations

Exposed identically on all three access paths. The ten v1 operations are
frozen; contract v2 (addendum
[`docs/contracts/retrieval-verbs-v2.md`](docs/contracts/retrieval-verbs-v2.md))
adds the four G1 retrieval verbs:

| # | Operation | Purpose |
|---|-----------|---------|
| 1 | `query` | Token-bounded primary-root retrieval with provenance, versions, confidence, and the three-list recommendation block |
| 2 | `get_node` | Single node with evidence and projection metadata |
| 3 | `get_subtree` | Bounded subtree traversal (depth `[1,6]`, relation allowlist, node/edge caps) |
| 4 | `log_event` | Append an immutable domain event |
| 5 | `record_outcome` | Attach an outcome to evidence |
| 6 | `evaluate` | Calibrated P/M/E scoring, uncertainty, level 1–5, promotion eligibility |
| 7 | `review` | Review state machine with minimum evidence, cooldown, hysteresis |
| 8 | `propose_improvement` | Structured diff proposal (never autonomous rewriting) |
| 9 | `apply_improvement` | Operator-gated apply with expected-version check and regression gate |
| 10 | `rollback` | Byte-exact reversal of an applied improvement or relearn |
| 11 | `path` | Deterministic shortest directed path between two nodes (`no_path` is a typed result) |
| 12 | `explain` | Relational neighborhood explanation: typed in/out links plus per-relation counts |
| 13 | `affected` | Reverse dependency walk: what depends on a node (bounded depth, typed guards) |
| 14 | `god_nodes` | Top hub nodes by measured graph degree (deterministic, bounded) |

Conditional access is hash-pinned: L1 tasks never call the system;
`access_only` returns a zero knowledge payload within 160 estimated tokens.

## Security and the no-AI boundary

Least privilege by default, fail closed everywhere
([docs/operations.md](docs/operations.md)):

- **No-AI boundary, enforced at runtime** — provider/vector/model packages and
  entry points are forbidden and fail closed (`runtime_guard.py`); outbound
  sockets are denied; the MCP server is tools+resources only (no sampling,
  prompts, logging, or completions) and **refuses to start** if provider
  credentials are present in its environment.
- **Bind policy** — Unix sockets and loopback binds need no key; any wider
  bind without a configured API key raises at startup.
- **Request authorization** — Host allowlist enforced on every request
  (loopback included; an empty allowlist fails closed); API keys compared via
  `hmac.compare_digest` over SHA-256 digests.
- **Payload sanitization** — Unicode format/control characters stripped;
  request bodies capped (default 1 MiB).
- **Path containment** — fixed-root resolution that fails closed on `..`
  traversal, absolute escapes, and symlink escapes.
- **Redaction** — known secret shapes and email PII are redacted on write and
  on every read surface that leaves the process; the structured logger redacts
  all fields automatically.

## Contracts and operator-gated relearn

Behaviour that must never drift is pinned by frozen, versioned contracts in
[`docs/contracts/`](docs/contracts/) and [`docs/mathematics/`](docs/mathematics/)
— scoring, token budget, access policy, recommendations, and the relearn
lifecycle. Any change requires a new contract version and operator approval.

When the source corpus changes, the system emits a deterministic
`RelearnOffer` and sets `RELEARN_PENDING`. It **never** chooses a mode
automatically:

```bash
know-your-soul relearn-scan      # detect corpus drift, record the offer
know-your-soul relearn-status    # inspect gate state and pending offer
know-your-soul relearn-preview   # dry-run the relearn plan
know-your-soul relearn-apply     # operator-gated apply (RESET or ACCUMULATE)
```

Every apply takes a verified atomic backup of the store, generation directory,
manifests, and scoring-contract version. Rollback restores exact pre-relearn
hashes, graph bytes, and P/M/E values with exactly one linked audit event.
Full contract: [docs/contracts/relearn-contract.md](docs/contracts/relearn-contract.md).

## Development

```bash
uv sync --frozen                           # pinned, locked environment (Python 3.14)
uv run pytest -q                           # full test suite (1,531 tests)
uv run pytest -q --cov=src/know_your_soul  # coverage (99.4%)
uv run pyright                             # type checking (0 errors)
uv run ruff check                          # lint
uv run ruff format --check src tests bench # format check
uv run python bench/run.py check           # harness self-checks
uv run python bench/run.py run             # paired benchmark (exit 0, 24/24 accepted)
```

Current state on `main` (fresh local runs): **1,531 passed**, coverage
**99.4%** (7,362 statements, 41 missed), ruff and pyright clean. CI runs the
same gates on every push and pull request.

## Roadmap

Near-term, in priority order:

- **Graph analytics** — centrality, community detection, bridges, and cycle
  detection as first-class deterministic operations
- **Richer reporting** — health reports, query traces, and export formats
  built on the same content-addressed projections
- **Broader CI matrix** — multi-OS runners for the full gate suite

The frozen contracts and the no-AI boundary are not on the roadmap — they are
permanent.

## Contributing

Issues and pull requests are welcome at
[github.com/Sigit-AP/know-your-soul](https://github.com/Sigit-AP/know-your-soul).
A few ground rules keep the project deterministic:

- Run the full gate suite before submitting: pytest, pyright, ruff, and the
  benchmark (`bench/run.py run` must exit 0 with 24/24 accepted).
- Never introduce a model, provider, embedding, or vector dependency — the
  runtime guards fail closed and CI will reject it.
- Behaviour pinned by a frozen contract changes only with a new contract
  version.

## License and attribution

Licensed under the [Apache License 2.0](LICENSE).

Know Your Soul includes a design inspired by, and a runtime dependency on,
[Graphify](https://github.com/Graphlet-AI/graphify) (Apache-2.0 OR MIT).
Graphify's graph projection format is consumed via the pinned `graphifyy`
package; feature ideas adapted from Graphify were re-implemented from scratch
under this project's own architecture — no Graphify source code is copied into
this repository. See [NOTICE](NOTICE) for the full attribution.
