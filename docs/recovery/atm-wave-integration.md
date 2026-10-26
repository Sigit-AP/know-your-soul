# ATM Wave — Export/Analytics Artifact Integration (SUB-63, F-B8 + F-D1)

Status: implemented, uncommitted (multi-agent rule). HEAD basis: `a7f331a`.
Scope: `exporters/{__init__,orchestrator}.py`, `ops/{__init__,export_cli}.py`,
bounded edits to `projection.py`, `read_model_validator.py`, `cli.py`, plus
tests. No other module touched; the frozen 14-operation knowledge contract is
unchanged (pinned by `tests/test_export_cli.py::test_export_and_analytics_are_not_knowledge_or_http_ops`).

## 1. What exists now

### `exporters/orchestrator.py` — the registry (F-B8)

- `ExportFormat`: `name`, `version`, `kind` (`"format"` | `"analytics"`),
  explicit `max_bytes` cap, `producer(read_model) -> bytes`,
  `roundtrip(bytes) -> None` (parse-back; raises on defect).
- `ExportRegistry`: the single extension point. `register(spec)` for full
  control; **`register_json(name, fn, *, kind, version, max_bytes)`** is the
  generic seam for analytics modules: any deterministic callable
  `read_model -> Mapping` becomes a capped, round-trip-validated,
  manifest-bound artifact. Round-trip for JSON artifacts is byte-exact:
  parse → require top-level object → re-serialize through `canonical_json`
  must reproduce the bytes.
- `produce_exports(registry, read_model, select=...)`: batch production in
  sorted-name order; cap breach → `ExportCapExceededError`; round-trip defect
  → `ExportRoundTripError`; non-bytes producer → `ExportError`. All typed,
  all fail-closed *before* publish.
- `manifest_entries(registry, artifacts)`: name → `{sha256, size, version,
  kind}`.
- `registry.fingerprint()`: sha256 of the registry shape (name →
  version/kind/cap) — pinned into the generation identity so two writers
  with different registries never collide on one content hash.
- Artifact names are portable relative paths: 1–3 `/`-separated segments of
  `[A-Za-z0-9._-]`, never absolute, never `..` — a registry entry can never
  escape the generation directory.
- `default_registry()`: two real built-ins proving the seam end to end —
  `exports/node-index.json` (kind `format`) and
  `analytics/domain-rollup.json` (kind `analytics`), both derived from the
  projection read model.

### Projection integration (F-B8, task point 2)

`GraphProjector(..., export_registry=registry)`:

- Export artifacts are produced from the same `graph_data` read model during
  `_build_generation`, cap-checked twice (per-format cap, then the publish
  cap backstop), hashed **into the content identity** (`export_artifacts` +
  `export_registry_fingerprint` in the identity payload), written into the
  generation directory (subdirectories supported, fsynced), and listed in the
  manifest under `export_artifacts` / `export_registry_fingerprint`.
- `ARTIFACT_NAMES` and the `artifacts` manifest set are **unchanged** — old
  generations and every pinned test keep their exact semantics; without a
  registry the build is byte-identical to before (proven by
  `test_without_registry_generation_is_unchanged`).
- A defective export raises `ProjectionValidationError` before activation
  (nothing is pointed to; the previous generation keeps serving).
- Constructor wiring: projector-built validators automatically receive
  `export_schemas=registry.schema_view()`; a caller-supplied validator that
  lacks a registered schema is rejected with `ValueError` at construction.

### Read-model validator (F-B8 gate 4)

`ReadModelValidator(..., export_schemas=...)`:

- A manifest `export_artifacts` entry whose name is not a registered schema
  **fails closed** ("unknown export artifact schema").
- Each entry is verified: file present, sha256 match, size match, per-format
  cap, and the format's own round-trip validation from the on-disk bytes.
- The portable-header/secret scan now covers export artifacts too (a
  secret-shaped string in an export artifact fails the generation).

### Ops CLI (F-D1)

`know-your-soul export [--formats a,b] [--out DIR]` and
`know-your-soul analytics [--modules a,b] [--out DIR]`:

- Read the **active** generation's `graph.json` (never a rebuild, never a
  store write — proven by `test_export_and_analytics_do_not_touch_the_store`),
  produce the selected artifacts, write them atomically+idempotently into a
  contained directory (default `<output-root>/exports`), and print a
  canonical-JSON summary (`ok`, `generation`, `registry_fingerprint`,
  `output_dir`, per-artifact `name/sha256/size/version/kind/written`).
- Strict argument validation: unknown/empty format lists → `CLI_USAGE`
  envelope on stderr, exit 2; output paths resolve through
  `resolve_within_root` (`--ops-root` when given, else the output root) —
  escapes fail closed with exit 2; no active generation → exit 1.
- **Not** in `KNOWLEDGE_OPERATIONS`, **not** in `OPS_OPERATIONS`, not
  dispatchable over HTTP or MCP (regression-pinned in tests).

## 2. How SUB-60/61/62 (and later waves) register modules

The registry accepts any module generically — no import of in-flight wave
code was needed, and none is required later:

```python
from know_your_soul.exporters.orchestrator import default_registry

registry = default_registry()
registry.register_json(
    "analytics/centrality.json",
    my_centrality_fn,                 # read_model (graph.json mapping) -> Mapping
    kind="analytics", version=1,      # bump version on any format change
    max_bytes=128 * 1024,             # explicit per-artifact cap, fail-closed
)
```

Rules for registrants:

1. Producers must be deterministic (canonical iteration, no wall-clock, no
   RNG without a pinned seed) — round-trip is byte-exact canonical JSON.
2. Non-JSON formats use `register(ExportFormat(...))` with a custom
   `roundtrip` that parses and asserts structure.
3. To make artifacts ride every generation, construct the projector with
   `export_registry=registry` (the validator schemas follow automatically).
   `build_stack` deliberately does **not** wire a default registry yet —
   flipping it on changes generation identity fleet-wide and is a single
   integration decision for the wave coordinator, not for a parallel subagent.

## 3. Gate evidence (2026-10-26 ~19:5x UTC)

- New tests: `test_export_orchestrator.py` (21), `test_projection_exports.py`
  (11), `test_export_cli.py` (10 incl. 8 parametrized) — **50 passed**.
- Adjacent suites (`test_projection`, `test_property_projection`,
  `test_read_model_validator`, `test_cli`, `test_operations`,
  `test_power_loss_recovery`, `test_wave3_coverage_adapters`, `test_security`,
  `test_mcp_server`): 287 passed; 2 failures **environmental** (see §4).
- `ruff check` + `ruff format --check`: clean. `pyright src tests`: 0 errors.
- `uv lock --check`: resolved/consistent. `bench/run.py check`: OK.

## 4. Hazards found in the shared tree (for the integrator)

1. **matplotlib/pillow**: a parallel agent added them to `pyproject.toml`
   dev-deps + `uv.lock` and synced the shared `.venv`. This trips
   `runtime_guard.assert_no_forbidden_distributions` → 14 failures in
   `test_no_ai_runtime.py` / `test_dependency_policy.py` and 2 in
   `test_mcp_server.py`. Unrelated to this change (verified: failures
   reference only the installed distributions). The wave integrator must
   reconcile: either gate those deps out of the runtime environment/lock or
   amend the guard policy deliberately.
2. `test_audit_final_flow.py::test_reader_storm_under_write_stream_bounds_repairs`
   is flaky at HEAD (fails and passes across runs, with and without this
   change) — pre-existing timing sensitivity, not caused here.
3. The working tree is **shared live** with SUB-58/60/61/62 (their
   `analytics/`, `report/`, assets and test files appeared mid-session) and a
   pre-existing stash `wip-mixed-agent2-residuals` exists. Do not run
   `git stash` casually here; scope any verification to known files.
