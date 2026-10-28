# W5 — Fix F-SUB94-1: relative from-import crash + W5 residuals (SUB-95)

Tanggal: 2026-10-28 · Basis: tree pasca-SUB-93/94 (belum di-commit) · Scope:
`src/know_your_soul/ingest/extractor.py`, `tests/test_ingest_extractor.py`,
`tests/test_kernel_rust_bridge.py`, `tests/test_codebase_bench.py`,
`bench/codebase/baseline.json`, `bench/codebase/ground_truth.json`,
`bench/README.md`, `docs/operations.md`, doc ini. Kontrak beku tidak disentuh.

## Bug (dari docs/recovery/w5-codebase-bench.md, F-SUB94-1)

`from . import x` (relative from-import, module_name dot-only) melempar
`NodeIdError: name must be a non-empty string`. Korban terukur di requests
v2.32.4: `src/requests/api.py`, `__init__.py`, `help.py`, `utils.py`,
`tests/test_requests.py` hilang dari graph (`EXTRACTION_FAILED`).

**Akar masalah** (`extractor.py::_emit_import`): rule Python
`import_from_statement` membaca field `module_name` lebih dulu. Untuk
`from . import x`, tree-sitter mengembalikan `module_name = "."`
(`relative_import`, dot-only). `_name_tail(".")` = `""` → allocator id
menolak nama kosong → crash fail-closed. Varian kedua ditemukan saat fix:
JavaScript `require(".")` (regex path) — bug yang sama via
`tree-sitter-python/bindings/node/binding_test.js`.

## Fix

`_emit_import`: kandidat target dari `target_fields` kini di-strip dulu dan
diterima hanya bila punya name-tail non-kosong (`_has_name_tail` — helper
baru di sebelah `_name_tail`); kandidat dot-only/kosong jatuh ke field
berikutnya (untuk Python: field `name` = nama yang diimpor). Guard yang sama
diterapkan ke target hasil regex (dot-only → tidak ada node, bukan crash).

Perilaku hasil fix:

| Sumber | Sebelum | Sesudah |
|---|---|---|
| `from . import sessions` | crash NodeIdError | import node `sessions`, ref target `sessions` |
| `from .. import sibling` | crash | node `sibling` |
| `from . import a as b` | crash | node `a as b` (bentuk sama dengan `import a as b` hari ini) |
| `from .a import x` | node `a`, target `.a` | **tidak berubah** |
| `require(".")` (JS) | crash | tidak ada node import (tidak ada nama bermakna) |

Semua file yang sebelumnya berhasil diekstrak menghasilkan output
**byte-identik** — perubahan hanya menyentuh kandidat yang dulu crash/dibuang
(dibuktikan oleh determinisme bench + regression gate counts untuk target
yang tidak terkena bug: fixture/chalk tidak berubah).

## Bukti RED → GREEN

- RED: 3 test baru gagal dengan `NodeIdError` (`test_python_relative_from_import_*`;
  `test_..._with_module_unchanged` hijau sejak awal — penjaga regresi untuk
  bentuk yang memang tidak rusak). Repro CLI: `extract_source("x.py",
  "from . import sessions\n")` → NodeIdError (kernel aktif pun sama: kernel
  menolak → fallback Python → crash).
- GREEN: `pytest tests/test_ingest_extractor.py` → **370 passed**.

## Tripwire & baseline (SUB-94 follow-through)

- `tests/test_codebase_bench.py::test_known_extraction_failures_pinned`
  (tripwire) diganti menjadi **assertion sukses**
  `test_known_extraction_failures_fixed`: tidak boleh ada
  `[EXTRACTION_FAILED]` di target manapun, di baseline maupun pengukuran live,
  dan kelima file korban tidak boleh ada di failed list.
- `tests/test_kernel_rust_bridge.py::test_pathological_empty_name_defers_to_reference`
  (pin crash kedua) diganti menjadi
  `test_relative_from_import_succeeds_through_kernel_routing`: dengan kernel
  aktif, caller menerima hasil reference yang sudah diperbaiki (kernel masih
  menolak → bridge fallback), tanpa exception.
- `baseline.json` di-refresh via `run --update-baseline` (run pertama exit 1
  oleh design — regression vs baseline lama; baseline ditulis; run kedua
  **exit 0**: correctness PASS + regression PASS, ledger
  `results/ledger-20261028T053630Z.json`).

Angka baseline baru (sebelum → sesudah):

| Target | Proposed | Nodes | Edges | Refs resolved |
|---|---|---|---|---|
| requests | 45 → **50** | 1345 → **1804** | 1720 → **2832** | 414/1377 (30.1%) → **1163/2887 (40.3%)** |
| tree-sitter-python | 42 → **43** | 1433 → **1439** | 1534 → **1547** | 281 → **288** |
| fixture, chalk | tidak berubah | | | |

## Ground truth dikoreksi (bukan dikorbankan)

Dengan `tests/test_requests.py` kini ter-ingest, hierarchy
`callers(merge_environment_settings)` menemukan caller kedua yang **nyata**:
`test_env_cert_bundles` memanggil `s.merge_environment_settings(...)` di
`tests/test_requests.py:1013` (diverifikasi dengan membaca sumber ter-pin).
Ground truth SUB-94 ("satu-satunya call site sessions.py:579") diverifikasi
di dunia di mana file test itu tidak ter-ingest. `ground_truth.json` kini
memuat kedua caller — graph sesudah fix lebih benar, bukan lebih longgar.

## CI wiring kernel Rust — keputusan: dokumentasi, bukan CI step

CI workflow tidak disentuh. Alasan: langkah maturin di CI tidak bisa
"dibuktikan hijau" dari host ini (tidak ada runner GitHub lokal; macOS hanya
best-effort), dan suite memang dirancang hijau penuh di path Python murni.
Dokumentasi build kernel + fallback otomatis + kill switch ditambahkan di
`docs/operations.md` (bagian "Rust extraction kernel"), termasuk catatan
bagaimana runner yang ingin menguji hot path kernel mem-build-nya sebelum
pytest.

## Residual

1. **kernel-rust (SUB-93) masih menolak** dot-only relative import dengan
   `EXTRACTION_ERROR` — bridge fallback ke walker Python yang sudah diperbaiki,
   jadi hasil benar; hanya file berbentuk itu yang membayar biaya fallback.
   Fix mirror di `kernel-rust/src/walker.rs` (`emit_import` skip kandidat
   tanpa name-tail) untuk wave berikutnya; setelah itu rebuild maturin.
2. `from . import a, b` mencatat nama pertama saja (`a`) — grammar memberi
   satu node per nama tapi walker membaca `name` pertama; konsisten dengan
   perilaku `from .a import x, y` yang juga mencatat target modul saja.
3. Alias `from . import a as b` menghasilkan target `a as b` (sama dengan
   `import a as b` hari ini); pemisahan alias adalah peningkatan terpisah,
   di luar scope.

## Gate (semua dijalankan di .venv proyek)

| Gate | Perintah | Hasil |
|---|---|---|
| Test extractor | `pytest tests/test_ingest_extractor.py` | 370 passed |
| Suite penuh | `pytest tests/ -q` | 5388 passed |
| Lint | `ruff check src tests bench tools scripts` + `ruff format --check src tests bench` | bersih |
| Types | `pyright` | 0 errors |
| Lock | `uv lock --check` | konsisten |
| License | `scripts/license_guard.py` | OK |
| Bench codebase | `python bench/codebase/run.py run` | **exit 0** (correctness+regression PASS; 0 `EXTRACTION_FAILED` di semua target) |
| Bench self-checks | `python bench/run.py check` | exit 0 |
