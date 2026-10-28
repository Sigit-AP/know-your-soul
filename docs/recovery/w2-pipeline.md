# W2 — Ingestion Pipeline (SUB-89)

Status: GREEN.
Scope: `src/know_your_soul/ingest/pipeline.py` (baru), `src/know_your_soul/ingest/__init__.py` (baru),
`src/know_your_soul/ops/ingest_cli.py` (baru), `src/know_your_soul/cli.py` (subcommand `ingest`
ditambah; 14 ops beku tidak disentuh), `tests/test_ingest_pipeline.py`,
`tests/test_ingest_pipeline_properties.py`, `tests/test_ingest_cli.py`, `tests/_ingest_fakes.py`.

## Yang dibangun

`IngestionPipeline(store, *, extractor=None, resolver=None, budget=IngestionBudget(), split_thresholds=None, actor="operator")`
dengan `ingest_path(path, *, incremental=True, recursive=True, resolve=True) -> IngestionReport`:

1. **Path discipline (kontrak v3)**: path hilang → `PathNotFoundError` (`PATH_NOT_FOUND`); tak
   terbaca → `PathNotReadableError`; symlink yang keluar root → kegagalan per-file
   `PATH_OUTSIDE_ROOT` (walk tidak pernah abort). Berkas di atas `max_file_bytes` →
   `FILE_TOO_LARGE`; bahasa tak dikenal → `UNSUPPORTED_MEDIA_TYPE`; extractor crash →
   `EXTRACTION_FAILED` per-file. Kegagalan per-file tidak pernah menggagalkan batch.
2. **Walk kanonik**: `os.walk` tanpa followlinks, direktori skip
   (`.git/__pycache__/node_modules/.venv/venv/dist/build/.mypy_cache/.pytest_cache/.hypothesis`),
   urutan root-relative sorted **bytewise** (`str.encode`). Berkas store itu sendiri
   (`*.sqlite3` + `-wal`/`-shm`) tidak pernah ikut walk (anti feedback-loop).
3. **Per file**: deteksi bahasa (`languages.language_for_path` SUB-87 bila ada, fallback tabel
   lokal yang menambah format W3: md/txt/pdf/Makefile/Dockerfile) → extract via
   `extractor.extract_source(relpath, source, ...)` → tulis dalam **satu transaksi** per file
   (statement identik dengan helper v3 `upsert_file`/`upsert_symbol`/`add_graph_edges`/
   `insert_unresolved_ref`; helper membuka transaksi sendiri sehingga tidak bisa di-nest —
   batch atomic, row `files` ditulis TERAKHIR sebagai commit marker: crash mid-batch →
   run berikutnya re-ingest). Node `file:` yang diproduksi extractor dipartisi keluar dari
   tabel `symbols` (dihitung tepat sekali, tidak mengotori lookup surface).
4. **Inkremental**: pre-filter `(size, mtime, extraction_version)` tanpa baca; mismatch →
   baca + sha256 konfirmasi; hash sama → resync stat saja; hash beda → cascade + re-extract.
   `extraction_version` naik → re-ingest meski hash sama. File terlacak yang hilang dari
   walk (mode incremental) → cascade delete.
5. **Cascade terbatas + resurrect**: hapus edge yang menyentuh node file lama (source atau
   target), hapus ref yang bersumber dari node lama, hapus simbol file. Edge non-`contains`
   dari node yang BERTAHAN menunjuk ke node terhapus → dibangkitkan sebagai
   `unresolved_refs` pending (`target_name` = nama simbol lama), lalu resolve pass berikutnya
   me-rebind (termasuk failed-ref retry saat file baru memperkenalkan `name_tail` yang cocok).
6. **Proposal-only**: nol penulisan ke domain pengetahuan (0 domain events, 0 node_revision);
   ringkasan proposal = counts + `primary_suggestions` dari
   `graph_model.propose_primary_splits` atas UniversalGraph in-memory (root primary sintetik
   `ingest-root`, domain P1, purpose tetap). Apply ke graph utama tetap lewat jalur
   propose/apply operator-gated.
7. **Audit**: tepat satu event audit per invokasi (`knowledge.ingest_file` /
   `knowledge.ingest_directory`, sukses maupun typed failure) via `AuditLog`; membawa
   sha256 input (digest gabungan `relpath\0sha256` per file dalam urutan traversal),
   `extraction_version`, dan counts — tidak pernah konten.
8. **Budget fail-closed**: `IngestionBudget(max_files∈[1,65536], max_nodes, max_edges,
   max_refs, max_file_bytes)`; total terlampaui → `IngestionBudgetError`
   (`INGESTION_BUDGET_EXCEEDED`) + audit failure; batch file saat itu rollback penuh
   (state file lama utuh).

## Fallback sibling (saat modul belum ada)

- `_default_extractor()` → `ExtractorUnavailableError` (typed `NotImplementedError`,
  `EXTRACTOR_UNAVAILABLE`) bila `ingest/extractor.py` belum ada.
- `_default_resolver()` → `None`; `resolve_pending()` menaikkan `ResolverUnavailableError`
  (typed `NotImplementedError`); `ingest_path(resolve=True)` melaporkan
  `resolution_status="unavailable"` tanpa menggagalkan walk.
- Keduanya SUDAH landed selama pengerjaan (SUB-87 `EXTRACTION_VERSION=1`,
  `extract_source`; SUB-88 `ReferenceResolver.resolve_pending -> BatchReport`) dan
  pipeline terintegrasi nyata: `TestRealSiblingIntegration` mengimpor modul riil langsung
  (tanpa skip diam-diam).

## Determinisme

- `unresolved_refs.first_seen` distempel marker provenance deterministik
  (`ingest:<sha256[:16]>` / `resurrect:<relpath>`), bukan wall clock → dua ingest dari nol
  byte-identik penuh pada 4 tabel v3 (`canonical_ingestion_dump`, termasuk AUTOINCREMENT id).
- Konvergensi incremental↔fresh diverifikasi pada bentuk logis (`include_ids=False`):
  rowid AUTOINCREMENT memang sah bernomor ulang setelah cascade.
- `PYTHONHASHSEED` 0/1/42 via subprocess → dump identik.

## CLI

`know-your-soul ingest <path> [--incremental/--no-incremental] [--recursive/--no-recursive]
[--no-resolve] [--max-files N] [--max-nodes N] [--max-edges N] [--max-refs N]
[--max-file-bytes N] [--json]` — output satu baris ringkas
(`ingest: files N (proposed .., skipped .., failed .., deleted ..) | nodes .. | edges .. |
refs parked .., resolved .., failed .. | duration .. ms`) atau canonical JSON.
Exit 0 sukses, 1 typed failure (envelope standar), 2 usage. OPS surface only — bukan
knowledge op, tidak masuk `KNOWLEDGE_OPERATIONS`/`OPS_OPERATIONS`, tidak lewat MCP/HTTP.

## Temuan selama integrasi

- SUB-87 memasukkan node `file:` di `result.nodes` → pipeline memartisi agar tidak masuk
  `symbols` dan tidak double-count.
- SUB-88 me-resolve `from b import beta` menjadi edge self-loop pada node `import:` (target
  = source) — semantik resolver sibling, bukan pipeline; test resurrect pipeline memakai
  ref `calls` yang memang cross-file.
- Store yang hidup di dalam root yang di-ingest terscan sebagai berkas biasa → pipeline
  mengecualikan file store (+ sidecars) dari walk secara deterministik.

## Gate (bukti nyata)

- `pytest tests/test_ingest_pipeline.py tests/test_ingest_pipeline_properties.py
  tests/test_ingest_cli.py -q` → **152 passed**.
- `ruff check` pada semua file yang disentuh → All checks passed.
- `pyright` pada file yang disentuh → 0 errors.
- Suite penuh: lihat MILESTONE-BOARD (dijalankan terakhir sebelum laporan).
