# Codebase push: CI + security + release automation (SUB-39, G2/G4/G5)

Date: 2026-10-26 · Branch: `feature/superpower-knowledge-soul` · Base HEAD: `51565d3`
Scope: menutup gap parity-assessment G2 (tidak ada CI), G4 (tanpa bandit/pip-audit),
G5 (tanpa release automation). Tidak ada commit (aturan proyek); semua perubahan ada
di working tree.

## Apa yang ditambahkan

### `.github/workflows/ci.yml` (baru) — trigger push + PR + workflow_dispatch

| Job | Gate | Perintah persis |
|---|---|---|
| `lint` | lock konsisten + ruff | `uv lock --check`, `ruff check`, `ruff format --check src tests bench` |
| `types` | pyright | `pyright` (config pyproject: basic, py3.14) |
| `test` | pytest + coverage | `pytest tests/ -q --cov=soul_knowledge --cov-report=term --cov-report=xml --cov-fail-under=93` |
| `security` | bandit + pip-audit | `bandit -ll -r src`, `pip-audit` |
| `bench-check` | self-check ringan | `python bench/run.py check` (bukan `run` — berat) |
| `docker` | image build | `docker build -f deploy/Dockerfile -t soul-knowledge:ci .` |

Setup via `astral-sh/setup-uv@v8.1.0`, Python 3.14, `uv sync --locked` (gagal bila
lock drift — `--locked` menegaskan konsistensi, `--frozen` dipakai di `uv run`).

### `.github/workflows/release.yml` (baru) — trigger tag `v*` + workflow_dispatch

- Guard: tag harus cocok dengan `uv version --short` (mis. tag `v0.1.0` ↔ version `0.1.0`).
- `uv build` → sdist + wheel → `actions/upload-artifact`.
- Publish PyPI via trusted publishing (`id-token: write`, `pypa/gh-action-pypi-publish@release/v1`) —
  mempersiapkan publish; butuh trusted publisher dikonfigurasi di PyPI sebelum rilis nyata.
- Upload `dist/*` ke GitHub Release (`softprops/action-gh-release@v2`, `contents: write`).
- Kedua langkah publish hanya jalan pada push tag (bukan workflow_dispatch).

### Dev deps baru (dependency-groups dev, via `uv add --group dev`)

`bandit==1.9.4`, `pip-audit==2.10.1`, `pytest-cov==7.1.0` (+ transitive: coverage,
cyclonedx, requests, rich, stevedore, dll). Lock diperbarui via uv; `uv lock --check` exit 0.

### `src/` — 3 komentar `# nosec B608` (satu-satunya sentuhan src, comment-only)

Bandit `-ll` menemukan 3 B608 (medium). Ketiganya false positive — diverifikasi dengan
membaca kode:

1. `observability.py:154` — `placeholders` adalah token `"?"` tetap; semua nilai di-bind
   via tuple `ops`.
2. `source_registry.py:345` / `:349` — `pending_exclusion` adalah fragmen SQL konstan
   hardcoded; `source_id` di-bind sebagai parameter.

Suppression sengaja per-baris dengan justifikasi, BUKAN skip global di bandit.toml —
skip global akan melemahkan gate SQL-injection untuk kode masa depan. Catatan teknis:
nosec bandit harus berada dalam *line range* node string yang diflag
(`tester.py::get_nosec` iterasi `context["linerange"]`), jadi komentar ditaruh di akhir
baris string, bukan baris terpisah.

## Pengukuran coverage nyata (2026-10-26, Python 3.14.9)

`pytest tests/ -q --cov=soul_knowledge` → **94.19%** (6477 statements, 376 miss),
732 passed. Threshold CI dipasang **93** (sedikit di bawah hasil ukur, sesuai aturan
"jujur dari pengukuran nyata"). Terendah: `mcp_server.py` 86%, `backup.py` 89%,
`observability.py`/`read_model_validator.py` 90%. Naikkan threshold saat coverage naik.

## Bukti gate lokal (semua dijalankan fresh, exit code nyata)

| Gate | Perintah | Hasil |
|---|---|---|
| test penuh | `pytest tests/ -q` | 732 passed, exit 0 (98s) |
| test + coverage | `pytest tests/ -q --cov=soul_knowledge --cov-fail-under=93` | 732 passed, 94.19% ≥ 93, exit 0 |
| lint | `ruff check` | All checks passed, exit 0 |
| format | `ruff format --check src tests bench` | 98 files already formatted, exit 0 |
| types | `pyright` | 0 errors 0 warnings, exit 0 |
| lock | `uv lock --check` | Resolved 101 packages, exit 0 |
| policy guards | `pytest tests/test_dependency_policy.py tests/test_no_ai_runtime.py -q` | 81 passed, exit 0 |
| bandit | `bandit -ll -r src` | No issues (3 nosec ter-justifikasi), exit 0 |
| pip-audit | `pip-audit` | No known vulnerabilities, exit 0 |
| bench check | `python bench/run.py check` | OK (cases schema, corpus integrity, estimator parity), exit 0 |
| build | `uv build` | sdist + wheel OK, exit 0 |
| YAML workflows | `python -c yaml.safe_load` kedua file | OK |

## Temuan / batasan terdokumentasi

1. **pip-audit `--strict` exit 1** — karena `soul-knowledge` belum ada di PyPI
   ("Dependency not found on PyPI and could not be audited"). CI memakai mode plain;
   setelah rilis pertama, evaluasi ulang `--strict`.
2. **docker build tidak diverifikasi lokal** — docker tidak terpasang di environment ini
   (`docker: command not found`). Job CI membangun `deploy/Dockerfile` apa adanya;
   Dockerfile itu sendiri pre-existing dan tidak diubah.
3. **`ruff format --check` di-scope ke `src tests bench`** — repo-wide gagal pada
   `docs/mathematics/scoring.md:54` (drift pre-existing sejak 724843c, code block di
   markdown; temuan H-8 audit SUB-37c). docs/ milik agen lain; tidak disentuh di sini.
4. **Dependency policy guards tetap hijau** — dev deps baru tidak melanggar
   `test_dependency_policy.py` (dev group subset-check, transitive lock bersih dari
   forbidden distributions) maupun `test_no_ai_runtime.py`; guard tidak dilemahkan,
   tidak perlu allowlist tambahan.
5. SIGKILL transient pada satu run coverage (OOM, auditor paralel berjalan); re-run
   hijau deterministik.

## File yang diubah

- baru: `.github/workflows/ci.yml`, `.github/workflows/release.yml`,
  `docs/recovery/codebase-push-ci-release.md`
- dimodifikasi: `pyproject.toml` (hanya `[dependency-groups] dev`), `uv.lock` (via uv),
  `src/soul_knowledge/observability.py` + `src/soul_knowledge/source_registry.py`
  (komentar `# nosec B608` saja, 0 perubahan perilaku — suite penuh hijau setelahnya)
