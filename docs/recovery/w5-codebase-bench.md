# W5 — Benchmark codebase nyata: ingestion + retrieval (SUB-94)

Tanggal: 2026-10-28 · Commit basis: `8764e7b` · Scope:
`bench/codebase/` (baru), `tests/test_codebase_bench.py` (baru),
`docs/recovery/w5-codebase-bench.md` (baru). `src/` **tidak disentuh**;
`kernel-rust/` (SUB-93) tidak disentuh — benchmark memakai extractor Python
produksi apa adanya.

## Apa yang diukur

Suite keempat di bawah `bench/` (pola `bench/README.md`): deterministik,
tanpa AI, tanpa network saat `verify`/`check`/`run`.

**Dataset** — 3 repo OSS kecil-terkenal + 1 fixture tulisan tangan, semuanya
di-pin per commit dan di-vendor terkompresi di `bench/codebase/data/`
(total 4,3 MB < 5 MB), manifest per-file sha256 di `manifest.json`:

| Target | Pin | Isi |
|---|---|---|
| `requests` | psf/requests `v2.32.4` (`021dc729`) | Python, 129 file |
| `chalk` | chalk/chalk `v5.6.2` (`51557784`) | JavaScript, 34 file |
| `tree-sitter-python` | tree-sitter/tree-sitter-python `v0.23.6` (`bffb65a8`) | multi-bahasa (C/JS/Python), 75 file |
| `fixture/` | in-repo, ditulis tangan | py+js, anchor ground truth callers/callees |

Tarball codeload GitHub **tidak stabil per-byte**, jadi `fetch`
mengemas-ulang pohon hasil unduh menjadi tar.gz kanonik (entri terurut
bytewise, mtime/uid/gid dinolkan, gzip mtime=0) — arsip vendored
reproducible dari commit ter-pin.

**Ingestion** — `IngestionPipeline` produksi (extractor tree-sitter asli +
resolver dua-fase asli; satu-satunya injeksi = proxy `_TimedExtractor` yang
hanya mencatat wall-clock `extract_source`, produk jalan apa adanya). Per
target: file/s, node/edge, ref resolved %, waktu walk vs resolve, p50/p95
ekstraksi per file, peak memori (tracemalloc), dan determinisme (2 ingest,
`canonical_ingestion_dump` byte-identik).

**Retrieval** — `SymbolLookup` (hit@10 pada repo nyata; top-1 ketat pada
fixture; 1 query negatif harus kosong) dan `CallHierarchy` (callers/callees
harus **persis** himpunan ground truth). Ground truth
(`ground_truth.json`): fixture diverifikasi manual dengan membaca kodenya
(setiap edge expected adalah call nyata di sumber); simbol repo nyata
diverifikasi via grep terhadap pohon ter-pin
(`Session`/`session`/`PreparedRequest`/`merge_environment_settings`/
`HTTPAdapter`/`SessionRedirectMixin`; `createChalk`/`applyOptions`;
`new_delimiter`/`_get_query`; callers `merge_environment_settings` =
`Session.request`, satu-satunya call site, sessions.py:579).

## Hasil nyata (ledger `results/ledger-20261028T032339Z.json`, exit 0)

| Target | Files (prop/fail) | Nodes | Edges | Refs resolved | Walk | Resolve | Extract p50/p95 | Peak |
|---|---|---|---|---|---|---|---|---|
| fixture | 5/0 | 10 | 18 | 8/10 (80%) | 31 ms | 34 ms | 1.6/2.3 ms | 18.7 MB |
| requests | 45/84 | 1345 | 1720 | 414/1377 (30.1%) | 4.9 s | 0.68 s | 5.3/94 ms | 7.5 MB |
| chalk | 23/11 | 252 | 321 | 90/622 (14.5%) | 0.55 s | 0.22 s | 7.4/38 ms | 1.3 MB |
| tree-sitter-python | 42/33 | 1433 | 1534 | 281/2668 (10.5%) | 6.3 s | 1.17 s | 2.8/213 ms | 12.8 MB |

Retrieval: **15/15 lookup hit@10 (15/15 top-1), 7/7 hierarchy exact, query
negatif kosong, determinisme 4/4 byte-identik**. Lookup p50 1–3,5 ms.

Angka "fail" ingestion mayoritas `UNSUPPORTED_MEDIA_TYPE` yang benar (rst,
png, yml, key, …). Ref resolved % rendah di repo nyata jujur dilaporkan —
penyebab dominan: simbol eksternal (urllib3 dsb di luar root) + file yang
gagal ekstraksi (F-SUB94-1 di bawah).

## TEMUAN NYATA — F-SUB94-1 (bug produk, di luar scope, TIDAK difix)

**Extractor kode crash pada `from . import x`** (relative from-import tanpa
nama modul): `extract_source("x.py", "from . import sessions\n")` melempar
`NodeIdError: name must be a non-empty string`. Akibat terukur pada dataset
nyata: `src/requests/api.py`, `src/requests/__init__.py`,
`src/requests/help.py`, `src/requests/utils.py`, dan
`tests/test_requests.py` — semuanya Python valid — gagal dengan
`EXTRACTION_FAILED`. Pipeline tahan (per-file failure tidak menggugurkan
walk, sesuai kontrak), tapi simbol file-file itu hilang dari graph.
Minimal repro lain: `from . import a as b` juga crash; `from .a import x`
dan `import os` bersih. Pemilik: kode SUB-87 (`ingest/extractor.py`) —
**wajib fix wave berikutnya**. Tripwire: daftar failed-file requests dipin
STRICT di `baseline.json` + `test_known_extraction_failures_pinned` —
saat bug diperbaiki, test itu GAGAL dengan jelas dan baseline wajib
di-refresh (perbaikan terdeteksi, bukan lolos diam-diam).

Selama probing awal, ground truth asli memakai `request` @ `api.py`; query
itu diganti `session`/`PreparedRequest` karena `api.py` korban F-SUB94-1 —
bukan untuk menutupi: kegagalan `api.py` tetap terlihat di stats, ledger,
dan tripwire di atas.

## Gate regresi

`baseline.json` (dibuat `--update-baseline` dari run hijau):

* **STRICT (kebenaran)**: `stats.*`, counts node/edge/ref, dan daftar
  `failed_files` per target harus sama persis;
* **LOOSE (waktu)**: `walk_ms`/`resolve_ms`/`extract_p50/p95` gagal hanya
  bila > 10× baseline + 250 ms slack — jitter mesin tak pernah menggagalkan,
  slowdown order-of-magnitude tertangkap.

## Gate

| Gate | Perintah | Hasil |
|---|---|---|
| Harness nyata | `python bench/codebase/run.py run` | **exit 0** (correctness PASS, regression PASS; ledger+report di `results/`) |
| verify/check | `run.py verify` / `run.py check` | exit 0 / exit 0 (offline; `check` jujur melaporkan dataset absen tanpa gagal) |
| Test | `pytest tests/test_codebase_bench.py -q` | **14 passed** (31s); tanpa dataset: 3 passed + **11 skipped jujur**, exit 0 |
| Lint | `ruff check` + `ruff format --check` (run.py, test, fixture) | bersih |
| Types | `pyright bench/codebase/run.py tests/test_codebase_bench.py` | 0 errors |

## Penyimpangan / residual

1. F-SUB94-1 (di atas) — bug extractor nyata, didokumentasikan + di-tripwire,
   tidak difix (src/ di luar scope SUB-94).
2. `fetch` butuh network (satu-satunya subcommand yang boleh); arsip hasilnya
   divalidasi ulang terhadap manifest oleh `verify` sebelum `run`.
3. Peak memori = tracemalloc Python-heap (bukan RSS proses); cukup untuk
   regresi relatif, bukan angka absolut sistem.
4. `bench/README.md` (tabel suite di luar scope SUB-94) belum mencantumkan
   suite ini — untuk coordinator/integrator.
