# W1 — Schema v3: migrasi transaksional + query helpers (SUB-85)

Tanggal: 2026-10-27 · Commit basis: `3b44e72` · Suite: **3633 passed, exit 0**
(baseline sebelum perubahan: 3165 passed; sisanya milik wave paralel).

## Ruang lingkup

- `src/know_your_soul/store.py` — migrasi v2→v3 + tabel baru + helpers.
- `tests/test_store_v3.py` — 26 test (unit + property hypothesis).
- Ledger/kontrak v1–v2 (`schema_migration`, `migrations/*.sql`) tidak diubah.

## Keputusan desain

1. **Migrasi v3 berada di Python, bukan file `.sql`.** Runner file-based
   memakai `executescript` yang melakukan COMMIT implisit — tidak bisa
   rollback-safe. `_apply_versioned_migrations()` mengeksekusi tiap statement
   DDL di dalam satu `BEGIN IMMEDIATE` (via `EventStore.transaction()`), lalu
   mencatat `schema_versions` (pola codegraph: `version, applied_at,
   description`). Riwayat v1/v2 di-backfill dari `schema_migration` sehingga
   `schema_versions` sendirian menjawab "versi berapa DB ini".
2. **Fail-closed dengan typed errors** (semua subclass `StoreError`):
   - `SchemaVersionTooNew` — DB mencatat versi > `SCHEMA_VERSION` (3).
   - `SchemaStateError` — ledger dan objek nyata tidak sepakat (v3 tercatat
     tapi tabel hilang, atau tabel v3 ada tanpa catatan).
   - `MigrationError` — statement gagal → transaksi di-rollback, DB tetap v2,
     retry bersih dimungkinkan (terbukti di test).
   - `UnresolvedRefError` — transisi lifecycle ilegal + input tak
     UTF-8-encodable (lone surrogate; ditemukan hypothesis, `\ud800`).
3. **DDL mengadopsi pola codegraph terverifikasi** (RESEARCH-STORAGE.md):
   - `idx_symbols_lower_name ON symbols (lower(name))` — expression index;
     query wajib berbentuk `lower(name) = lower(?)` (terbukti dipakai via
     EXPLAIN QUERY PLAN di test).
   - `idx_unresolved_pending ... WHERE status = 'pending'` dan
     `idx_unresolved_failed_tail ... WHERE status = 'failed'` — partial index
     (pending = antrian panas; failed = kandidat retry).
   - `idx_graph_edges_identity UNIQUE ON (source, target, kind,
     IFNULL(line,-1), IFNULL(col,-1))` — NULL dilipat ke -1 sehingga
     `INSERT OR IGNORE` benar-benar dedup (pelajaran codegraph #1034).
   - Adjacency memakai komposit `(source, kind)` / `(target, kind)`; index
     kolom tunggal sengaja dihilangkan (left-prefix menutupi, codegraph v4).
4. **Prepared statements**: sqlite3 meng-cache statement per string SQL;
   bentuk `IN (...)` adjacency di-cache per arah+tuple kinds, dibatasi 64
   bentuk (pola `edgeKindStmt` codegraph).
5. **Hierarki = edge**: tidak ada kolom parent/depth; `subtree()` menghitung
   depth saat BFS dengan visited set (aman terhadap siklus), urutan kunjungan
   deterministik (level-order × urutan adjacency `kind, target, line, col`).
6. **Determinisme**: `files` tidak membawa timestamp wall-clock; test
   determinisme byte-identik mengecualikan hanya `schema_versions.applied_at`.

## API baru (EventStore)

- Files: `upsert_file`, `get_file`, `list_files`.
- Symbols: `upsert_symbol`, `symbol_by_node`, `symbols_named`
  (`case_insensitive=True` → seek expression index), `symbols_in_path`.
- Edges: `add_graph_edge(s)` (OR IGNORE + dedup identitas),
  `outgoing_edges`, `incoming_edges` (urutan deterministik + filter kinds),
  `subtree(root, max_depth, kinds)` BFS depth-bounded.
- Unresolved refs: `insert_unresolved_ref` (name_tail = segmen dot terakhir,
  bisa di-override), `pending_unresolved_refs`, `failed_unresolved_refs`,
  `record_resolution_attempt` (pending→resolved|failed, attempts+1),
  `retry_failed_ref` (failed→pending, attempts dipertahankan),
  `unresolved_ref`. Resolved bersifat terminal.

## Verifikasi (bukti nyata)

| Gate | Perintah | Hasil |
|---|---|---|
| Suite penuh | `.venv/bin/python -m pytest -q` | 3633 passed, 1 warning, exit 0 (402s) |
| Test v3 | `pytest tests/test_store_v3.py -q` | 26 passed |
| Lint | `ruff check store.py test_store_v3.py` | All checks passed |
| Format | `ruff format --check` | 2 files already formatted |
| Types | `pyright store.py test_store_v3.py` | 0 errors, 0 warnings |

Property tests (hypothesis, `deadline=None`):
- Migrasi v2→v3 mempertahankan **setiap baris** untuk 0–8 node (dump penuh
  semua tabel dibandingkan tuple-per-tuple; `verify_chain` tetap ok;
  store tetap writable pasca-migrasi).
- `name_tail == target_name.rsplit(".", 1)[-1]` untuk 50 nama arbitrer.
- `attempts` monoton +1 per attempt, tak berubah saat retry (20 skenario).

Test rollback menyuntik statement SQL rusak di tengah migrasi: DB tetap v2
tanpa satu pun tabel v3, retry bersih sukses, chain valid.

## Catatan untuk wave berikutnya

- `graph_model.py` (SUB-84) dan guard/kontrak (SUB-86) tidak disentuh; id
  node extractor W2 tinggal memakai `symbols.node_id` sebagai PK.
- Resolver W2: antrian kerja = `pending_unresolved_refs(limit=...)`;
  parkir gagal = `record_resolution_attempt(..., outcome="failed")`;
  saat file baru muncul, retry via `failed_unresolved_refs(name_tail=...)`
  → `retry_failed_ref`.
- Tidak ada commit yang dilakukan (aturan proyek); semua perubahan working
  tree saja.
