# ATM Mega-Wave 2 — integrasi jalur produksi (SUB-80)

Tanggal: 2026-10-27 ~08:58–09:2x UTC. HEAD `afbf8ad` + file wave-2 uncommitted
(milik SUB-74/75/76/77 — tidak satu pun disentuh kecuali tidak sama sekali).
TIDAK ada commit (aturan). Scope yang disentuh: `exporters/builtin.py`,
`ops/export_cli.py` (docstring saja), `cli.py`, `tests/test_builtin_integration.py`
(expected-set diperluas), `tests/test_wave2_integration.py` (BARU, 13 test),
`docs/{operations.md,verification.md}`, `README.md`, `README.id.md`.

## Apa yang di-wire

**`exporters/builtin.py`** — registry kini menghasilkan **48 artifact (22
format + 26 analytics)**:

- Format baru (SUB-75, self-register): `exports/graph.gexf`,
  `exports/cytoscape.json`, `exports/d3-force.json`, `exports/adjacency.csv` +
  `exports/edges.txt`, `exports/graph.tgf`. Alias: `gexf`, `cytoscape`,
  `d3force`, `adjacency`, `tgf`.
- Analytics baru: `analytics/graph-summary.json` (SUB-75, alias
  `graph-summary`); suite SUB-74 via `registry.register_json` —
  `hits`/`eigenvector`/`katz`/`clustering`/`louvain`/`reachability`/`motifs`/
  `assortativity`; `analytics/paths.json` = **probe Yen deterministik**
  (endpoint = node id terkecil→terbesar; <2 node → `status:
  insufficient_nodes` yang jujur, bukan raise); `analytics/aggregates.json`
  (SUB-76 `aggregate_nodes(group_by="domain")`);
  `analytics/near-duplicates.json` (SUB-76, `proposal_only`/`auto_merge:
  false` sebagai data).
- Cap per-artifact eksplisit (`_JSON_SMALL` 2 MiB / `_JSON_LARGE` 4 MiB);
  semua producer murni atas read model.

**`cli.py`** — tiga subcommand OPS baru (bukan knowledge ops; 14 operasi beku
utuh; tidak masuk MCP/HTTP):

- `config describe|effective|dump` + `--config FILE`: resolusi 4 lapis
  (default < TOML < env `KNOW_YOUR_SOUL_*` < CLI). Flag CLI eksplisit
  (`--store`, `--output-root`, `--actor`, `--ops-root`) masuk sebagai layer
  `cli` hanya bila berbeda dari default-nya. `ConfigError` → exit 2.
- `metrics [--format json|markdown] [--stale-after S]`: metrik operasional
  read-only dari store + proyeksi aktif.
- `diagnose [--format json|markdown] [--no-permissions] [--stale-after S]`:
  11 check fail-closed; **exit code = `exit_code(report)`** (1 iff ada finding
  severity error).

## Bukti gate (fresh)

- Full suite: **3165 passed, 0 failed** (334.58s) — melampaui baseline 3149.
- `ruff check` + `ruff format --check` src+tests: bersih. `pyright`: 0/0/0.
- `uv lock --check`: OK. `python scripts/license_guard.py`: OK.
- `uv run python bench/run.py check`: **OK**.
- E2E NYATA di store dari `bench/corpus` (rebuild penuh; workspace
  `/root/.hermes/cache/scratch/sub80-e2e`):
  - `export --formats all` → exit 0, 22 artifact; `analytics --modules all` →
    exit 0, 26 artifact; **48/48 file di disk re-hash cocok dengan sha256
    summary** (round-trip sudah dipaksa oleh `produce_exports`).
  - `config effective|describe|dump` → exit 0; `store.path` tercatat
    `source: cli`; `serve.api_key` ter-redaksi.
  - `metrics` json+markdown → exit 0 (`counts.domain_events: 71`,
    `health.score: 85`, `projection.present: true`).
  - `diagnose` → exit 0 (0 error, 9 warning permission/kapasitas — jujur);
    setelah tamper 1 byte pada `graph.json` generasi aktif → **exit 1**,
    finding `ARTIFACT_HASH_MISMATCH` + `VALIDATOR_FAILED` (file dipulihkan).

## Catatan

- `tests/test_builtin_integration.py` mem-pin himpunan nama registry secara
  eksak — expected-set diperluas dengan 18 artifact baru (disengaja; kontrak
  registry memang berubah oleh integrasi).
- Probe `paths` pada korpus bench: `path_count: 0` (sumber tak terhubung ke
  target) — hasil jujur yang deterministik, bukan kegagalan.
- Determinisme byte artifact baru dijamin oleh modul masing-masing (dipin
  property test mereka) + `test_cli_export_is_byte_deterministic_across_invocations`.
