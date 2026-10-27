<div align="center">

**🇬🇧 English** | [🇮🇩 Bahasa Indonesia](README.id.md)

</div>

# Know Your Soul

<p align="center">
  <a href="https://github.com/Sigit-AP/know-your-soul/actions/workflows/ci.yml"><img src="https://github.com/Sigit-AP/know-your-soul/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"/></a>
  <a href="https://pypi.org/project/know-your-soul/"><img src="https://img.shields.io/pypi/v/know-your-soul" alt="PyPI"/></a>
  <img src="https://img.shields.io/badge/python-3.14-blue" alt="Python 3.14"/>
  <img src="https://img.shields.io/badge/coverage-97.7%25-brightgreen" alt="Coverage 97.7%"/>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue" alt="License: Apache-2.0"/></a>
</p>

Know Your Soul is a deterministic knowledge and learning graph for
[Hermes Agent](https://hermes-agent.nousresearch.com/docs). It stores what the
agent learns as an auditable, event-sourced graph, scores every node with exact
Decimal math, and changes itself only through operator-approved proposals.
No model, no embeddings, no provider calls anywhere in the runtime. The AI
stays on the Hermes side; this is the memory it can trust.

<p align="center">
  <img src="docs/assets/screenshots/graph-viewer.png" alt="The graph viewer exported by the running system: 36 nodes and 35 typed edges from the real corpus, colored by domain P1–P7" width="900"/>
</p>
<p align="center">
  <em>The graph viewer, exported by the running system. 36 nodes, 35 typed
  edges, colored by domain. Real data, not a mockup.</em>
</p>

## Why

Agents re-read the same documents every session and pay full token price for
it. Know Your Soul gives the agent a local memory instead:

- Query instead of re-reading. Token-bounded retrieval, measured at ~94% token
  saving against reading the raw corpus (24/24 frozen benchmark cases).
- Every call is audited. Reads included. Each operation lands as one event in
  an append-only, hash-chained SQLite store.
- Learning is scored, not vibes. Nodes carry calibrated P/M/E scores under a
  frozen math contract.
- Nothing rewrites itself. Improvements are diff proposals you approve;
  rollback is byte-exact.

## Quickstart

Requires Python 3.14.

```bash
pip install know-your-soul
```

From source, with the locked environment:

```bash
git clone https://github.com/Sigit-AP/know-your-soul.git
cd know-your-soul
uv sync --frozen
```

Record two nodes and query them:

```bash
know-your-soul --store demo.sqlite3 --output-root graph log-event \
  '{"node_id":"p1-root","kind":"primary","domain":"P1",
    "summary":"P1 root: verification discipline",
    "provenance":["soul://demo"],"source_version_id":"demo-v1"}'

know-your-soul --store demo.sqlite3 --output-root graph log-event \
  '{"node_id":"n1","parent_id":"p1-root","domain":"P1",
    "summary":"Always verify before claiming done",
    "provenance":["soul://demo"],"source_version_id":"demo-v1"}'

know-your-soul --store demo.sqlite3 --output-root graph rebuild
know-your-soul --store demo.sqlite3 --output-root graph query \
  '{"purpose":"verify before claiming done","token_budget":400}'
```

A real answer from a real server, with its audit trail:

<p align="center">
  <img src="docs/assets/screenshots/live-api-query.png" alt="A live query response from the running HTTP server: results, provenance, token accounting, and the audit event id" width="900"/>
</p>

The persistent API serves versioned `/v1` JSON over a Unix socket (loopback
HTTP optional, API key mandatory beyond loopback):

```bash
know-your-soul --store demo.sqlite3 --output-root graph serve --socket demo.sock

curl -s --unix-socket demo.sock -X POST http://localhost/v1/knowledge/query \
  -H 'content-type: application/json' \
  -d '{"purpose":"verify before claiming done","token_budget":400}'
```

Hermes registers the system as an MCP server:

```bash
hermes mcp add know-your-soul \
  --command /root/know-your-soul/.venv/bin/python \
  --args -m know_your_soul.mcp_server \
         --graph-root /root/know-your-soul/.local/graph \
         --db /root/know-your-soul/.local/know-your-soul.sqlite3

hermes mcp test know-your-soul
```

Full setup: [docs/integration/hermes.md](docs/integration/hermes.md).

## The graph viewer

`know-your-soul export --formats all` writes a self-contained `graph.html` (no
CDN, no JavaScript dependencies, works offline) plus twelve more interchange
formats: DOT, Mermaid, CSV (nodes + edges), GraphML, SVG, GEXF, Cytoscape.js,
D3-force, an adjacency matrix + edge list, TGF, and a per-domain markdown book.
Every artifact is hashed into the generation manifest and round-trip validated.

`know-your-soul analytics --modules all` adds the deep graph suite over the same
read model: centrality, components, communities, cycles, bridges, HITS,
eigenvector and Katz prestige, clustering, deterministic Louvain, all-pairs
reachability, a 16-class triad census, degree assortativity, a k-shortest-paths
probe, per-domain score aggregates, and proposal-only near-duplicate candidates.
Day-2 operation is covered by three read-only commands: `config describe` /
`config effective` (layered configuration with provenance), `metrics`
(store/latency/capacity/health), and `diagnose` (11 fail-closed self-checks;
exit 1 when anything is broken, so CI can gate on it).

Clicking a node shows its scores, version, and provenance:

<p align="center">
  <img src="docs/assets/screenshots/node-detail.png" alt="Node detail panel of the exported graph viewer: label, id, domain, P/M/E scores, and provenance of a real corpus node" width="900"/>
</p>

Dark theme included:

<p align="center">
  <img src="docs/assets/screenshots/dark-theme.png" alt="The same graph viewer in dark theme" width="900"/>
</p>

## The fourteen operations

Identical on CLI, HTTP, and MCP. The ten v1 operations are frozen; contract v2
([docs/contracts/retrieval-verbs-v2.md](docs/contracts/retrieval-verbs-v2.md))
adds four retrieval verbs.

| # | Operation | Purpose |
|---|-----------|---------|
| 1 | `query` | Token-bounded retrieval with provenance, versions, and recommendations |
| 2 | `get_node` | One node with evidence and projection metadata |
| 3 | `get_subtree` | Bounded subtree traversal |
| 4 | `log_event` | Append an immutable domain event |
| 5 | `record_outcome` | Attach an outcome to evidence |
| 6 | `evaluate` | P/M/E scoring, uncertainty, level, promotion eligibility |
| 7 | `review` | Review state machine: minimum evidence, cooldown, hysteresis |
| 8 | `propose_improvement` | Structured diff proposal |
| 9 | `apply_improvement` | Operator-gated apply with regression gate |
| 10 | `rollback` | Byte-exact reversal |
| 11 | `path` | Shortest directed path between two nodes |
| 12 | `explain` | Relational neighborhood: in/out links plus per-relation counts |
| 13 | `affected` | Reverse dependency walk, bounded depth |
| 14 | `god_nodes` | Top hub nodes by measured degree |

## Benchmark

`bench/` pins a corpus snapshot by sha256 (28 files, 13,151 estimated tokens)
and asks 24 frozen questions. No model involved. Latest ledger: 24/24 accepted,
mean token saving 94.2%, median p50 latency 7.9 ms.

```bash
uv run python bench/run.py check
uv run python bench/run.py run
```

Methodology: [bench/README.md](bench/README.md). Release evidence:
[docs/verification.md](docs/verification.md).

## Security

- The no-AI boundary is enforced at runtime: provider, vector, and model
  packages fail closed, outbound sockets are denied, and the MCP server refuses
  to start if it finds provider credentials in its environment.
- Host allowlist on every request. API keys compared in constant time.
- Request bodies capped. Secret shapes and email PII redacted on write and on
  every read surface. Paths contained to a fixed root.

## Relearn

When the corpus changes, the system emits a `RelearnOffer` and stops. It never
picks a mode on its own:

```bash
know-your-soul relearn-scan      # detect drift, record the offer
know-your-soul relearn-status    # inspect the pending offer
know-your-soul relearn-preview   # dry-run the plan
know-your-soul relearn-apply     # apply, after your approval (RESET or ACCUMULATE)
```

Every apply takes a verified atomic backup first. Rollback restores exact
pre-apply hashes and scores.

## Development

```bash
uv sync --frozen
uv run pytest -q                 # 3,165 tests
uv run pytest -q --cov=src/know_your_soul
uv run pyright                   # 0 errors
uv run ruff check
uv run python bench/run.py run   # 24/24 accepted
```

CI runs the same gates on Ubuntu and macOS, plus bandit, pip-audit, and a
license-attribution guard.

## Contributing

Issues and pull requests welcome. Three rules: run the full gate suite first,
never add a model or provider dependency, and never change frozen-contract
behavior without a new contract version.

## License

[Apache 2.0](LICENSE). Design ideas adapted from
[Graphify](https://github.com/Graphlet-AI/graphify) (Apache-2.0 OR MIT) were
re-implemented from scratch under this project's own architecture. No Graphify
source code is copied. Full attribution in [NOTICE](NOTICE).
