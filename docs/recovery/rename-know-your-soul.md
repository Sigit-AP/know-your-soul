# Rename: Super Power Knowledge SOUL → Know Your Soul (SUB-55)

- Tanggal: 2026-10-26 ~16:20 UTC
- Repo: `/root/superpower-knowledge-soul`, branch `feature/superpower-knowledge-soul`, base HEAD `dad6e43`
- Sifat: rename brand menyeluruh atas perintah operator. **Tidak ada commit** — commit adalah langkah coordinator. Direktori repo **tidak** di-rename (langkah coordinator).

## Pemetaan old → new

| Lama | Baru | Cakupan |
|------|------|---------|
| Paket Python `soul_knowledge` (`src/soul_knowledge/`) | `know_your_soul` (`src/know_your_soul/`, via `git mv`) | seluruh impor internal, semua tests, `bench/run.py`, `--cov` di CI |
| Nama proyek PyPI `soul-knowledge` | `know-your-soul` | `pyproject.toml`, `uv.lock` (diregenerasi via `uv lock`) |
| Console script `soul-knowledge` | `know-your-soul` | `[project.scripts]`, `prog=` argparse, Docker `ENTRYPOINT` |
| Nama server MCP `soul-knowledge` | `know-your-soul` | `SERVER_NAME` di `mcp_server.py`, `version("know-your-soul")`, config contoh di `docs/integration/hermes.md` |
| Default store `soul-knowledge.sqlite3` | `know-your-soul.sqlite3` | `cli.py` `DEFAULT_STORE_PATH`; juga `know-your-soul-projection`, `know-your-soul.sock` |
| Env `SOUL_STORE` (Dockerfile) | `KNOW_YOUR_SOUL_STORE` | **backward-compat**: CLI membaca `KNOW_YOUR_SOUL_STORE` dulu, fallback `SOUL_STORE` dengan warning deprecate di stderr, lalu default built-in (`cli.py:_default_store_path`, 3 test baru di `tests/test_cli.py`) |
| Judul prose "Super Power Knowledge SOUL" | "Know Your Soul" | `README.md`, `docs/verification.md`, `docs/audit-release.md`, `src/know_your_soul/__init__.py`, judul `docs/integration/hermes.md`, diagram arsitektur README |
| Coverage gate CI `--cov=soul_knowledge` | `--cov=know_your_soul` | `.github/workflows/ci.yml`; image tag docker `know-your-soul:ci` |

## Yang SENGAJA TIDAK diubah (beserta alasannya)

| Token | Lokasi | Alasan |
|-------|--------|--------|
| `SourceKind.SOUL_CORPUS`, "SOUL corpus", "raw SOUL", `soul://` URI scheme, prefix event `knowledge.*` | src, tests, fixtures, bench | Nama input eksternal (korpus SOUL.md), bukan brand; `soul://` ter-pin di fixtures + ledger historis |
| `docs/mathematics/*`, `docs/contracts/{access-policy,recommendation-contract,relearn-contract}.md` | docs beku | Kontrak matematis beku — rekaman historis; hanya `contracts/retrieval-verbs-v2.md` (aktif) yang di-rename |
| `docs/superpowers/` (plan/spec) | docs historis | Plan/spec historis per desain |
| `docs/research/`, `docs/recovery/` (selain file ini) | docs historis | Rekam jejak audit historis |
| `docs/adr/0001,0002` | docs historis | ADR adalah decision record point-in-time; merujuk path plan historis |
| `bench/corpus/`, `bench/results/*.json` lama (8 ledger ter-track) | bench | Korpus beku + ledger historis (byte-pinned) |
| Path refs `docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md`, `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/*` | docstring src/tests, README:8-9, verification/audit-release | File tujuan benar-benar masih bernama demikian — mengubah string memutus referensi |
| Path absolut `/root/superpower-knowledge-soul/...` | `docs/integration/hermes.md` | Direktori repo belum di-rename (langkah coordinator) |
| Thread name `soul-source-watch`, state file `source-watch.json` | `update_pipeline.py`, `http_api.py` | Watcher untuk sumber SOUL corpus; mengganti nama file state memutus kontinuitas on-disk |
| User/group container `soul` | `deploy/Dockerfile` | Identitas infra internal, bukan permukaan brand |
| `tests/fixtures/*.json` (3 file, FROZEN) | tests | `spec_reference` menunjuk path spec historis; fixtures byte-frozen |

## Insiden selama rename (diperbaiki)

`tests/test_property_projection.py::test_source_section_fragments_replicate_the_importer_id_walk`
gagal setelah rename meski diff kode murni identifier. Akar masalah:
hypothesis `derandomize=True` menghitung seed dari `function_digest(test)` —
rename mengubah digest → seed baru → contoh terderandomisasi baru
(`headings=['a ']`) yang mengekspos **bug laten pre-existing**: replikasi
walk di test memakai heading mentah, sedangkan importer/projection mem-key
section dari hasil capture `_HEADING_RE` (whitespace luar ter-strip).
Perbaikan test-only: replikasi kini menurunkan heading dari baris via
`_HEADING_RE` yang sama. Verifikasi baseline HEAD `dad6e43` di worktree
bersih: contoh lama tak pernah memuat heading ber-trailing-space, jadi bug
tidak pernah terekspos sebelumnya.

## Bukti gate (semua di working tree pasca-rename)

| Gate | Perintah | Exit | Hasil |
|------|----------|------|-------|
| pytest penuh | `.venv/bin/python -m pytest tests/ -q` | 0 | **1523 passed, 1 warning** (~4.5 min) |
| ruff check | `.venv/bin/ruff check .` | 0 | All checks passed! |
| ruff format | `.venv/bin/ruff format --check src tests bench` | 0 | (scope CI; offender pre-existing `docs/mathematics/scoring.md` berada di area beku, sengaja tidak disentuh) |
| pyright | `.venv/bin/pyright` | 0 | 0 errors, 0 warnings, 0 informations |
| uv lock | `uv lock --check` | 0 | konsisten dengan pyproject baru |
| uv build | `uv build` | 0 | `dist/know_your_soul-0.1.0.tar.gz` + `.whl` |
| bench check | `.venv/bin/python bench/run.py check` | 0 | OK (cases schema, frozen corpus integrity, estimator parity) |
| bench run | `.venv/bin/python bench/run.py run` | 0 | **24/24 accepted**, ledger `bench/results/ledger-20261026T161746Z.json` |
| CLI baru | `.venv/bin/know-your-soul --help` | 0 | usage tampil; entry `soul-knowledge` hilang dari `.venv/bin` |
| Env backward-compat | `SOUL_STORE=… know-your-soul health` | 0 | warning deprecate di stderr, path dipakai; `KNOW_YOUR_SOUL_STORE` menang bila keduanya ada |

## Sisa kemunculan brand lama (seluruhnya disengaja)

Verifikasi `git grep` pasca-rename: semua sisa `soul_knowledge` /
`soul-knowledge` / `Super Power Knowledge SOUL` / `superpower-knowledge-soul`
berada eksklusif di `docs/{adr,mathematics,recovery,research,superpowers}`,
path references ke file historis yang masih ada, path absolut repo
(coordinator step), atau ledger/fixture beku. Tidak ada sisa di prose docs
aktif maupun identifier mesin.
