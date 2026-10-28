# W2 — Resolver dua fase: ReferenceResolver (SUB-88)

Tanggal: 2026-10-27 · Commit basis: `d006f70` · Scope: `src/know_your_soul/resolve/`
(baru), `tests/test_resolve_resolver.py` (baru), `tests/test_resolve_properties.py`
(baru). `store.py`, `graph_model.py`, dan kontrak beku **tidak disentuh** —
resolver berdiri murni di atas API publik store v3 (+ `store.read()` untuk dua
query read-only terdokumentasi).

## Kontrak ref mentah (dibagi dengan SUB-87 extractor)

```python
{"source_node_id": str, "target_name": str, "kind": str}
```

- `source_node_id` — node id simbol (`kind:hash[:col]`) atau node file
  (`file:<path>`); harus sudah ada di store.
- `target_name` — nama apa adanya: plain (`"helper"`), dotted/qualified
  (`"pkg.mod.func"`), atau specifier modul (`"./util"`, `"pkg.mod"`). Import
  relatif Python mempertahankan dot di depan (`".mod.func"`, `"..shared.util"`).
- `kind` — `"calls"` / `"imports"` / `"function_ref"` (dipetakan ke edge
  `calls`; padanan `EdgeKind | 'function_ref'` codegraph).

`name_tail`/`first_seen` adalah urusan store (default `insert_unresolved_ref`).
SUB-87 menghasilkan format yang sama; kontrak ini juga terdokumentasi di
docstring `know_your_soul/resolve/__init__.py`.

## Keputusan desain

1. **Tiga tier strategi, urutan beku** (RESEARCH-INGESTION §2e, pasangan
   import-resolver/name-matcher codegraph):
   a. `exact` — `symbols_named(target_name)` (seek `idx_symbols_name`).
   b. `name_tail` — nama == tail ATAU suffix `.<tail>`/`::<tail>`
      (LIKE escaped; dedup per node_id; urutan `(path, line, col, node_id)`).
   c. `import_path` — hanya bahasa module-based (`python`, `javascript`,
      `typescript`, `javascriptreact`, `typescriptreact`): Python dotted
      absolut/relatif (`.py` lalu `/__init__.py`), JS/TS specifier relatif
      (pencarian ekstensi `.ts→.tsx→.js→.jsx` lalu `index.*`). Specifier bare
      JS/TS gagal sebagai `external_module`.
2. **Ambiguitas tidak pernah menebak (fail-closed)**: tier dengan 2+ kandidat
   menangguhkan alasan dan lanjut ke tier berikutnya — import-path bisa
   mendisambiguasi lewat konteks modul (`pkg.mod.func` → simbol `func` di
   `pkg/mod.py`). Bila tak ada tier yang menyelesaikan, ref gagal dengan alasan
   ambiguitas **paling awal**. Kosakata alasan deterministik:
   `unknown_ref_kind`, `unknown_source`, `ambiguous_exact:N`,
   `ambiguous_tail:N`, `ambiguous_module:N`, `ambiguous_module_symbol:N`,
   `symbol_not_in_module:<tail>`, `external_module`, `no_resolution`.
3. **Dua fase read→write per batch**: semua keputusan dihitung dari read murni,
   lalu SATU transaksi `add_graph_edges` (identitas edge OR IGNORE men-dedup),
   lalu satu `record_resolution_attempt` per ref. Crash di antara dua fase
   aman: edge ter-dedup dan ref tetap pending → retry bersih. Cap fail-closed
   via `limit=`/`ResolverConfig.batch_limit` (None = tanpa cap; 0/negatif/bool
   → `ResolverError`).
4. **Edge membawa identitas ref** di `metadata`
   (`canonical_json({ref_id, ref_name, strategy})`) + `provenance
   "resolver:two-phase:v1"` — syarat `resurrectRefFromDroppedEdge` gaya
   codegraph saat file target dihapus kelak.
5. **Retry incremental (#1240)**: `resurrect_failed(store, name_tail=...,
   limit=...)` memindahkan failed→pending via `retry_failed_ref` (attempts
   dipertahankan); pipeline memanggilnya saat file baru mendarat, lalu
   `resolve_pending`.
6. **Invariant eksplisit**: resolver hanya membuat edge yang kedua ujungnya ada
   — source diverifikasi (simbol atau `file:<path>` dengan row `files`),
   target selalu node simbol nyata atau `file:<path>` dari tabel `files`.
   Edge `calls` tidak pernah terikat ke node file.
7. **Bahasa source** dari row `files`; bila tak ada/`unknown` → inferensi
   ekstensi (deterministik, tabel `_EXTENSION_LANGUAGES`).
8. **Integrasi pipeline (SUB-89)**: `resolve_pending(store, limit=None,
   config=None)` (fungsi modul) atau `ReferenceResolver(config)
   .resolve_pending(store)` — satu panggilan per batch; `BatchReport` membawa
   `processed/resolved/failed/pending_remaining/edges_inserted/outcomes`
   (outcomes diurutkan per `ref_id`).

## Verifikasi (bukti nyata)

| Gate | Perintah | Hasil |
|---|---|---|
| Test resolver | `pytest tests/test_resolve_resolver.py tests/test_resolve_properties.py` | **120 passed** |
| Lint | `ruff check src/know_your_soul/resolve tests/test_resolve_*.py` | All checks passed |
| Format | `ruff format` (2 file direformat) + `--check` | bersih |
| Types | `pyright src/know_your_soul/resolve tests/test_resolve_*.py` | 0 errors, 0 warnings |
| Keamanan | `bandit -r src/know_your_soul/resolve` | exit 0 (hanya B101 assert — pola yang sama dengan `store.py`) |
| Suite penuh | `pytest -q --ignore=tests/test_ingest_*` (milik SUB-87/89 yang sedang in-flight) | **3753 passed, 1 warning, exit 0** (385s) — 3633 baseline + 120 resolver, nol regresi |
| Determinisme ganda | 2× `pytest tests/test_resolve_*.py` | 120 passed dua kali, hasil identik |

Property tests (hypothesis, `derandomize=True`, `deadline=None`, 25 contoh):
- **Determinisme lintas urutan insert**: permutasi acak vs dibalik → multiset
  `(source_node_id, target_name, name_tail, kind, status, attempts)` dan
  multiset edge `(source, target, kind)` identik.
- **Idempotensi**: run kedua byte-identik (dump penuh `unresolved_refs` +
  `graph_edges`).
- **Invariant referensial**: tiap edge menghubungkan node yang ada; tiap ref
  resolved punya triple `(source, target, kind)` yang cocok.
- **Komposisi laporan**: `processed == resolved + failed`; cap batch tidak
  pernah dilampaui; `pending_remaining` konsisten.
- **Determinisme lintas instance**: dua store identik → `BatchReport` equal +
  dump equal.
- **Akuntansi retry**: resurrect + re-resolve → attempts +1 tepat; resolved
  tetap terminal.
- **Tail turunan store**: `name_tail == target.rsplit(".", 1)[-1]` untuk nama
  arbitrer.

## Catatan untuk wave berikutnya

- Query suffix tail/module memakai LIKE tanpa index — benar dan terbatas untuk
  W2; bila profil membuktikan bottleneck, tambah kolom/index di wave storage
  berikutnya (kontrak beku, jadi perlu migrasi baru, bukan ubahan v3).
- Resolusi `calls` berbasis import-path hanya berlaku untuk Python target
  dotted/relatif; JS/TS calls mengandalkan exact/tail (extractor memarkir nama
  lokal). Ini disengaja: tanpa tabel import-statement, menebak modul untuk JS
  calls tidak deterministik.
- Edge resolver tidak membawa line/col (kontrak ref mentah minimal); bila
  extractor kelak memarkir posisi, tambahkan ke kontrak + kolom, bukan
  penambahan diam-diam.
- Tidak ada commit yang dilakukan (aturan proyek); semua perubahan working
  tree saja.
