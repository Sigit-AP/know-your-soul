# W3 — Ingestion Dokumen (SUB-90)

Status: GREEN. Scope: `src/know_your_soul/ingest/documents.py` (baru),
`ingest/pipeline.py` (penambahan cabang routing dokumen, tanpa mengubah
perilaku jalur kode), `tests/test_ingest_documents{,_pipeline,_properties}.py`
+ `tests/_doc_fixtures.py` (baru). Kontrak v1/v2/v3, `graph_model.py`,
`store.py`, `languages.py`, `extractor.py`, `resolver.py` tidak diubah.

## Yang dibangun

1. **PDF via pypdf** (`_extract_pdf`): text-layer per halaman → `file:<path>`
   → `document` → `page` per halaman berteks → `paragraph` chunk. Id kanonik
   `make_node_id(path, kind, name, line)`; nama chunk `p<halaman>:o<offset>`
   sehingga preimage id membawa path+halaman+offset. PDF tanpa text-layer
   (semua halaman kosong) → node `document` dengan
   `metadata["no_text_layer"]=True` dan 0 anak (TANPA OCR — dilarang).
   Halaman kosong pada PDF campuran tidak mendapat node page.
   PDF rusak → `PdfParseError` (`PDF_PARSE_ERROR`); terenkripsi tanpa
   password kosong → `PdfEncryptedError` (`PDF_ENCRYPTED`).
2. **Markdown structure-aware**: scanner baris (ATX H1–H6, fence ```/~~~,
   list bullet/numerik, paragraf). Heading → `section` berketurunan via
   stack (H2 anak H1 terdekat, level lompat menempel ke heading lebih dangkal
   terdekat, kedalaman arbitrer). Paragraf/list/code-block → `paragraph`.
   Link inline antar-dokumen → `unresolved_refs` kind `references`
   (anchor murni, URL skema, dan gambar dilewati; target `#anchor` dipotong).
3. **Plaintext**: paragraf (pisah baris kosong) → chunk `l<baris>:o<offset>`.
4. **Chunker kanonik** (`split_text_blocks`/`chunk_block`/`chunk_text`):
   split di batas paragraf; chunk ≤ `ChunkingConfig.max_chunk_chars`
   (default 2000); potong di whitespace terakhir dalam window, hard-cut bila
   tak ada; konten non-whitespace terjaga urut tepat sekali (dipropetikan
   hypothesis).
5. **Integrasi pipeline**: cabang tambahan di `_ingest_one` — bahasa dokumen
   (dari `documents.DOCUMENT_LANGUAGES`, fallback tabel lokal pipeline yang
   sudah ada) dilewatkan ke `documents.extract_document` dengan bytes mentah
   (PDF tak pernah di-decode UTF-8); jalur kode byte-identik seperti W2.
   Versi incremental dokumen memakai `DOCUMENT_EXTRACTION_VERSION`
   (bayangan variabel lokal, tak menyentuh jalur kode). Cascade, resurrect,
   budget, audit, dan `canonical_ingestion_dump` bekerja apa adanya.
6. **Primary auto-suggestion**: dokumen besar masuk graf proposal pipeline
   (file root di bawah ingest-root) → `report.primary_suggestions` berisi
   `subtree_size` dst. Level modul: `suggest_primary_splits(result, ...)`
   membungkus pohon dokumen di bawah primary sintetis lalu memanggil
   `propose_primary_splits` — proposal-only, tanpa mutasi.

## Penyimpangan terukur dari brief (dengan alasan)

- **EXTENSION_MAP W2 (`ingest/languages.py`) tidak ditambah.** Test W2
  membekukan `language_for_path("a.md") is None` dan mewajibkan setiap nilai
  EXTENSION_MAP terdaftar sebagai bahasa tree-sitter. Menambah .pdf/.md/.txt
  di sana akan memecah test W2 (modul W2 beku). Sebagai gantinya dokumen
  punya `DOCUMENT_EXTENSION_MAP` di `documents.py`; deteksi bahasa pipeline
  sudah mengenal md/pdf/txt lewat tabel fallback W2 (ditutest W2), jadi
  routing otomatis berjalan. Akar `document` berada di bawah `file:<path>`
  (konvensi W2) agar partisi file-node pipeline + saran primary berfungsi.

## Residual terdokumentasi

- **Resolver W2 belum memetakan kind `references`** (`REF_KIND_TO_EDGE_KIND`
  beku milik SUB-88): link dokumen masuk jalur resolver dan gagal ter-type
  `unknown_ref_kind` (deterministik, tetap terlihat di `unresolved_refs`).
  Dengan resolver yang memetakan `references→references` link ter-resolve ke
  node `document` target (dibuktikan via ToyResolver). Addendum resolver satu
  baris ditunda ke wave berikutnya.

## Bukti gate

- `pytest tests/test_ingest_documents.py tests/test_ingest_documents_pipeline.py tests/test_ingest_documents_properties.py -q`
  → **202 passed** (unit 152, integrasi pipeline 36, property hypothesis derandomize 14).
- Suite W2 ingestion/resolver: `pytest tests/test_ingest_* tests/test_resolve_* -q` → 1119+202 passed.
- Suite penuh `pytest -q` → lihat MILESTONE-BOARD (4752 baseline + 202 baru).
- `ruff check` + `ruff format --check` bersih untuk semua file SUB-90;
  `pyright` 0 error pada file SUB-90.

## Determinisme

- Fixture PDF dibangun via pypdf sendiri (`tests/_doc_fixtures.py`,
  Helvetica Type1 + satu text-op per halaman): dua build byte-identik
  (dipropetikan). Ekstraksi 2 run → `canonical_json()` byte-identik;
  `canonical_ingestion_dump` identik di dua store; node graph dipaku ke
  epoch agar `canonical_dump()` stabil.
