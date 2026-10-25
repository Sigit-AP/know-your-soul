# Graphify Research Depth Rubric

Purpose: measure research depth for applying Graphify to Super Power Knowledge SOUL Graph. This is not a claim that a percentage of the whole repository was read.

Formula: `(full areas + 0.5 * partial areas) / 8 * 100`.

An area is full only when its primary implementation, official documentation, and relevant failure/verification behavior have been inspected and cited. Partial means some primary sources were inspected but important implementation or failure behavior remains unexamined.

Final verification (2026-10-25): all eight areas are full, backed by four cited research artifacts under `docs/research/`. One artifact also executed 115 Graphify tests (4 skipped) and runtime MCP probes. The score is therefore 100% under this rubric, while explicitly not claiming that every source line in the repository was read.

| Area | Final | Evidence |
|---|---|---|
| Product capabilities and official docs | Full | README, architecture docs, lifecycle/benchmark report |
| Packaging and dependencies | Full | pyproject, provider extras, import/entry-point surfaces |
| Architecture and pipeline | Full | ARCHITECTURE plus owning modules |
| Graph model and confidence | Full | schema, direction, confidence, validation and gaps |
| Query and retrieval internals | Full | matcher, traversal, cache, ambiguity, budget and tests |
| MCP and transport internals | Full | stdio/HTTP, capabilities, cache, runtime probes, conformance tests |
| Security internals | Full | SECURITY, security.py, live cross-tree/symlink/input probes |
| Tests and benchmark methodology | Full | relevant suites, executed 115 passed/4 skipped, upstream-claim audit |

Final score: `(8 + 0*0.5)/8 = 100%` for research depth, with a separate applicable-feature adoption coverage of 92.5% verified by the lifecycle/benchmark report before product adapters are implemented.

| Area | Baseline | Evidence required for full |
|---|---|---|
| Product capabilities and official docs | Full | README, architecture docs, capability exclusions |
| Packaging and dependencies | Full | pyproject, optional/provider surfaces, lock implications |
| Architecture and pipeline | Full | ARCHITECTURE and owning modules |
| Graph model and confidence | Full | graph schema, edge direction, confidence, validation |
| Query and retrieval internals | Partial | query/explain/path/get-node, cache, ambiguity, budgets, tests |
| MCP and transport internals | Partial | stdio/HTTP, capabilities, hot reload, security, conformance tests |
| Security internals | Not deep | SECURITY, security.py, malicious input/path/network tests |
| Tests and benchmark methodology | Not deep | relevant suites, benchmark harness, upstream claims vs local evidence |

Baseline score: `(4 + 2*0.5)/8 = 62.5%`.

Target before implementation: at least 90%, with all security, retrieval, MCP, lifecycle, and benchmark areas full. A score of 100% requires all eight areas full and cited research artifacts; it still does not mean reading every source line.

Additional research tracks required by the product goal:
- Conditional Hermes access and deterministic recommendation contracts.
- Token/latency/quality comparison against raw SOUL reads.
- SOUL change handling with operator-selected RESET or ACCUMULATE relearning.
- Provenance, supersession, contradiction, backup, and rollback for both modes.
