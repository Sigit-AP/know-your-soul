# ADR 0001 — Standalone deterministic modular monolith with no model boundary

- Status: Accepted (Task 1, 2026-10-25)
- Deciders: product owner (operator) + implementation
- Supersedes: none
- Related: ADR 0002 (SQLite event store + Graphify read model), spec `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md` sections 4–5 and 13, plan Task 1.

## Context

The system must turn actions, decisions, outcomes and lessons into a versioned knowledge graph with auditable scoring and controlled improvement. Hermes Agent is the consumer and already supplies its own model. Two properties dominate every other choice:

1. **No AI at runtime.** No model assets, provider dependencies, provider credentials, model/embedding/vector calls, outbound provider traffic, or MCP sampling handlers/capabilities anywhere in this system.
2. **Determinism.** Scoring, routing, retrieval, logging, scheduling, validation and status transitions must be reproducible from the same inputs, byte for byte.

The repository is new and standalone at `/root/superpower-knowledge-soul`. Hermes source, the existing SOUL corpus and the host Graphify installation (`0.9.91`) must not be modified; the project uses an isolated environment pinned to `graphifyy[mcp]==0.9.95`.

## Decision

1. **Language: Python 3.14 only** (`requires-python = ">=3.14,<3.15"`). Graphify's supported library, query engine, MCP implementation, NetworkX, NumPy and the official Python MCP SDK cover the required workload directly. A native extension is permitted later **only** after a profile and benchmark demonstrate a material, measured bottleneck.
2. **Shape: one modular monolith**, one package (`src/soul_knowledge`), one SQLite database, and three thin access adapters (CLI `soul-knowledge`, local Starlette/Uvicorn API on a Unix socket by default with optional loopback HTTP, and MCP over stdio with optional Streamable HTTP) that all call the same application service. Docker is packaging, not a fourth access path.
3. **No model boundary, enforced operationally, fail closed.** The installed Graphify distribution may contain dormant optional provider-integration source; that source is outside the enabled runtime surface. The boundary is enforced by four independent mechanisms, all implemented in `src/soul_knowledge/runtime_guard.py`:
   - **Packaging/lock:** exactly `graphifyy[mcp]==0.9.95`; the mcp extra is the only enabled extra; `uv.lock` pins the whole transitively reachable environment and contains no provider/vector/model distribution at all.
   - **Forbidden imports and entry points:** `graphify.llm`, `graphify.serve`, `graphify.prs`, `graphify.transcribe`, `graphify.ingest`, `graphify.mcp_ingest`, `graphify.exporters*` and the provider/vector module roots raise `ForbiddenImportError` from a meta-path guard. The entry-point policy fails closed on any entry point whose target module is a forbidden import root and on provider/`graphify-mcp` console-script names; **recorded boundary:** the pinned `graphifyy==0.9.95` wheel unavoidably ships `graphify-mcp = graphify.serve:_main`, which the policy therefore rejects. It is not on the product surface and the product never invokes it, but it cannot be removed without forking upstream, so it is reported explicitly as `pinned_entry_point_boundary` by `verify_runtime()` and by `blocking_forbidden_entry_points()`; `assert_no_forbidden_entry_points()` is strict and raises on it. Residual owner: whoever next revisits the Graphify pin (feature matrix §5 and ADR review triggers). A future wheel that ships any *other* forbidden entry point is a hard failure, not a boundary.
   - **Scrubbed process environment:** credentials and model-asset variables (`OPENAI_*`, `ANTHROPIC_*`, `AWS_*`, `HF_*`, `TIKTOKEN_*`, `TRANSFORMERS_*`, `TORCH_*`, `OLLAMA*`, and shape-based `*_API_KEY`/`*_TOKEN`/`*_SECRET`) are dropped by `scrubbed_env()`/`deterministic_env()`, which pins `PYTHONHASHSEED=0` for reproducible runs.
   - **Denied outbound sockets:** AF_INET/AF_INET6 `connect`, `connect_ex`, `sendto`, `sendmsg` and `create_connection` raise `OutboundSocketDenied` while AF_UNIX stays usable for the local API.
   `verify_runtime()` runs every guard, requires **observed** MCP capability evidence (a live server whose registered handlers are read, or an explicit capability snapshot), and returns a JSON report or raises. It fails closed when no capability evidence is supplied rather than checking the allowlist against itself.
4. **Hermes MCP no-sampling contract.** MCP advertises exactly `tools` and `resources`. No `sampling/createMessage`, `prompts/*`, `logging/*` or `completion/*` handler is registered; `observed_mcp_capabilities()` derives the advertised families from a server's real request-handler registry, and `assert_no_sampling_handler()` plus `assert_mcp_capabilities()` enforce the exact surface and fail closed if it cannot be verified. The documented Hermes server entry additionally sets `sampling: {enabled: false}` and `elicitation: {enabled: false}`, but correctness never depends on the client's cooperation. There is no `project_path`, no PR/`gh` surface, no provider configuration and no `env` requirement. Details and Hermes-side evidence: `docs/research/hermes-mcp-contract.md`.
5. **Semantic interpretation stays outside.** When semantic rewriting is wanted, Hermes produces explicit structured content or patch proposals and submits them through tools; this system validates, scores and stores. Automatic semantic rewriting is out of scope.

## Alternatives rejected

| Alternative | Why rejected |
|---|---|
| Rust or TypeScript core "for performance/professionalism" | No measured bottleneck exists; adds interface and operational cost with no benefit for the required workload. Re-open only with a profile/benchmark (paper-tiger risk in the spec pre-mortem). |
| Fork or vendor Graphify, or depend on the host `0.9.91` install | A fork is maintenance without measured benefit; the host install is shared state and must remain untouched. Pinning `0.9.95` in an isolated environment plus a documented adapter boundary is cheaper and testable. |
| Ship the provider extras (`[all]`, `openai`, `anthropic`, `bedrock`, `tiktoken`, …) and "just not call them" | Violates the verifiable boundary: `graphify.llm` has import-time side effects including a tokenizer download attempt and ambient-key auto-detection. Dormant source is acceptable; dormant **installed and importable** code is not. |
| Use an embedding/vector store for retrieval | Deterministic lexical ranking with content-hash caches is sufficient, reproducible and dependency-free; a vector store would add model assets or an external service and non-deterministic state. |
| Enforce the boundary by documentation and code review only | Unverifiable. Guards must be executable, fail closed, and covered by tests that fail in a scrubbed child process as well as in-process. |
| Keep the system inside the Hermes repository | Acceptance requires no Hermes source change and independent packaging; the product is explicitly not a code-search product and not part of Hermes. |

## Consequences, rollback and review triggers

- Positive: reproducible behaviour, no credential surface, no egress, small dependency tree (73 locked packages — 72 registry distributions, all artifact-hashed, plus the editable root project), and a single application service behind three thin adapters.
- Cost: private Graphify primitives are used through the adapter; an upgrade requires re-running golden fixtures. Lexical relevance is not professional quality — P/M/E remain the quality signals.
- **Rollback:** the boundary is additive and reversible per component. If the no-model boundary is unacceptable operationally, the correct action is to **replace the Graphify distribution with a reviewed deterministic-only distribution before implementation**, not to weaken the rule (spec section 4.2). Reverting the packaging/lock (`pyproject.toml`, `uv.lock`) and the guard module returns the repository to the pre-Task-1 state; no other artifact depends on them yet.
- Review triggers: a Graphify upgrade or pin change; a candidate feature that would need any provider/model/vector dependency; a measured performance bottleneck that would justify a native extension; any operator objection to the boundary (escalate before silently relaxing it).