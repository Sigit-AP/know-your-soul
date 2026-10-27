<div align="center">

**🇬🇧 English** | [🇮🇩 Bahasa Indonesia](README.id.md)

</div>

# Know Your Soul

[![CI](https://github.com/Sigit-AP/know-your-soul/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/Sigit-AP/know-your-soul/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/know-your-soul)](https://pypi.org/project/know-your-soul/)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue)](https://www.python.org/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue)](LICENSE)

**A memory service for agents.** It keeps what your agent learned in one local,
auditable graph — scored, versioned, and impossible to change without your
approval. No model, no embeddings, no provider calls inside. The AI stays on
the agent side; this is the part that never guesses.

> Agents re-read the same documents every session and pay full token price for
> it. Know Your Soul answers from the graph instead: **94.2% fewer tokens**,
> every answer carrying its provenance and an audit id.

## Install

**Requires:** Python 3.14

```bash
pip install know-your-soul
```

From source:

```bash
git clone https://github.com/Sigit-AP/know-your-soul.git
cd know-your-soul
uv sync --frozen
```

## Usage

```bash
know-your-soul --store demo.sqlite3 log-event \
  '{"node_id":"n1","parent_id":"p1-root","domain":"P1",
    "summary":"Always verify before claiming done",
    "provenance":["soul://demo"],"source_version_id":"demo-v1"}'

know-your-soul --store demo.sqlite3 rebuild
know-your-soul --store demo.sqlite3 query \
  '{"purpose":"verify before claiming done","token_budget":400}'

know-your-soul export --format all        # graph.html + DOT/Mermaid/CSV/GraphML/SVG + book
know-your-soul analytics --modules all    # centrality, communities, cycles, health, timeline
know-your-soul serve --socket demo.sock   # persistent local API
know-your-soul diagnose                   # full self-check, exit code reflects health
```

Hermes registers it as an MCP server; setup:
[docs/integration/hermes.md](docs/integration/hermes.md).

## What you get

**The graph viewer** — `export` writes a self-contained `graph.html`. No CDN,
no JavaScript dependencies, works offline. This is the real thing running on
the benchmark corpus:

<p align="center">
  <img src="docs/assets/screenshots/graph-viewer.png" alt="Graph viewer: 36 nodes, 35 typed edges, colored by domain" width="900"/>
</p>

Click a node, get its scores and provenance:

<p align="center">
  <img src="docs/assets/screenshots/node-detail.png" alt="Node detail: label, domain, P/M/E scores, provenance" width="900"/>
</p>

<p align="center">
  <img src="docs/assets/screenshots/dark-theme.png" alt="Dark theme" width="700"/>
</p>

**Answers with a receipt** — every call, reads included, lands as one event in
an append-only, hash-chained SQLite store. A live query response carries its
audit id:

<p align="center">
  <img src="docs/assets/screenshots/live-api-query.png" alt="Live query response from the running server" width="900"/>
</p>

**Learning with a contract** — nodes carry P/M/E scores under frozen math.
Improvements arrive as diff proposals you approve; rollback is byte-exact.
Corpus changes produce a relearn offer and wait for you.

**Graph analytics** — PageRank, HITS, Katz, communities, Louvain, cycles,
bridges, health checks. Deterministic: same input, same bytes, every run.

**Twelve export formats** — DOT, Mermaid, CSV, GraphML, GEXF, Cytoscape, D3,
SVG, TGF, adjacency, plus a per-domain markdown book.

## Benchmarks

Pinned corpus (28 files, 13,151 tokens), 24 frozen questions, no model
involved:

| Metric | Result |
|---|---|
| Cases accepted | 24/24 |
| Mean token saving vs raw corpus | 94.2% |
| Median query latency (p50) | 7.9 ms |

Memory benchmark on the real LOCOMO dataset (5,882 turns, 1,982 questions):

| Metric | Result |
|---|---|
| recall@10 (product, as-is) | 0.5433 |

Run both yourself: `uv run python bench/run.py run` and
`uv run python bench/memory/run.py run`. Evidence:
[docs/verification.md](docs/verification.md).

## The fourteen operations

Same on CLI, HTTP, and MCP:

| | | | |
|---|---|---|---|
| `query` | `get_node` | `get_subtree` | `log_event` |
| `record_outcome` | `evaluate` | `review` | `propose_improvement` |
| `apply_improvement` | `rollback` | `path` | `explain` |
| `affected` | `god_nodes` | | |

## Security

The no-AI boundary is enforced at runtime: provider and vector packages fail
closed, outbound sockets are denied, and the MCP server refuses to start when
provider credentials exist in its environment. Host allowlist on every
request, constant-time key comparison, capped bodies, redacted secrets,
contained paths.

## Development

```bash
uv sync --frozen
uv run pytest -q                 # 3,165 tests
uv run pyright
uv run ruff check
uv run python bench/run.py run   # 24/24 accepted
```

CI runs the same gates on Ubuntu and macOS, plus bandit, pip-audit, and a
license-attribution guard.

<details>
<summary>Contributing</summary>

Issues and pull requests welcome. Run the full gate suite first. Never add a
model or provider dependency — the runtime guards fail closed. Frozen-contract
behavior changes only with a new contract version.

</details>

## License

[Apache 2.0](LICENSE). Graph projection format consumed via the pinned
`graphifyy` package. Attribution in [NOTICE](NOTICE).
