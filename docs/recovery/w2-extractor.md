# W2 — Extraction Engine Kode (SUB-87)

Tanggal: 2026-10-27. Status: GREEN.

## Lingkup

Extraction engine kode Wave 2: registry bahasa deklaratif + walker tree-sitter
yang menghasilkan node (`CoreNodeKind`, id via `make_node_id`), edge
`contains`, dan ref mentah cross-file untuk resolver dua fase. Pola 90% dari
codegraph (RESEARCH-INGESTION §2, §5), 10% penyesuaian kontrak beku.

## File yang dibuat

| File | Isi |
|---|---|
| `src/know_your_soul/ingest/languages.py` | Registry murni data: `LanguageSpec`, `ImportRule`, `VariableRule` (frozen dataclass, tuple saja), tabel 12 bahasa, `EXTENSION_MAP` (27 ekstensi), `language_for_path` |
| `src/know_your_soul/ingest/extractor.py` | `TreeSitterExtractor` (walker AST + scope stack), `extract_source`, `extract_file`, `detect_generated`, error bertipe, `ExtractionResult` kanonik |
| `tests/fixtures/ingest/<lang>/sample.*` | Fixture kode nyata 12 bahasa (function+class+import per bahasa) |
| `tests/test_ingest_languages.py` | 134 test data-registry (shape, kontrak kind, grammar parity, EXTENSION_MAP) |
| `tests/test_ingest_extractor.py` | ~570 test ekstraksi per bahasa (parametri 12 bahasa) + kasus terarah |
| `tests/test_ingest_files.py` | ~50 test penanganan file (batas 1 MiB, typed error, generated) |
| `tests/test_ingest_properties.py` | ~90 property test (determinisme, contains-forest, id stability, hypothesis fuzz, PYTHONHASHSEED subprocess) |

## Keputusan desain

1. **Registry murni data.** `languages.py` hanya berisi frozen dataclass +
   tuple; tidak ada callable (di-pin oleh test `test_spec_has_no_callables`).
   Menambah bahasa = menambah data, bukan kode walker.
2. **Node id** mengikuti codegraph/W1: `kind:sha256(path:kind:name:line)`;
   suffix `:col` hanya saat dua deklarasi berbagi (path, kind, name, line)
   (`_IdAllocator`, pola `NodeIdAllocator`). File root: `file:<path>`.
3. **Scope stack** menghasilkan edge `contains` (satu-satunya edge yang
   diemit extractor) + `qualified_name` berseparator `::`. `impl_item` Rust
   adalah *transparent container*: menyumbang segmen qualified name dan konteks
   method, tapi tidak membuat node (menghindari banjir simbol, codegraph
   #1093). `package` Java/Go adalah statement header — node `module` dibuat
   tapi bukan scope blok (kelas bukan anaknya).
4. **Method flip**: node bertipe `function` di dalam scope
   class/interface/type (atau container) menjadi `method`
   (`scoped_functions_become_methods` per bahasa; Python/C++/Rust aktif).
5. **Resolusi dua fase dihormati**: calls dan imports keluar sebagai
   `UnresolvedRef{source_node_id, target_name, name_tail, kind, line, col}` —
   cocok 1:1 dengan `store.insert_unresolved_ref`. Extractor tidak menebak
   target cross-file. `name_tail` = segmen `.`-terakhir lalu `::`-terakhir
   (konvensi store).
6. **Penanganan file**: batas 1 MiB diperiksa via `stat` *dan* sesudah baca
   (file yang tumbuh saat dibaca → `FILE_TOO_LARGE`); bahasa dari ekstensi
   (`UNSUPPORTED_MEDIA_TYPE`); decode UTF-8 strict dengan BOM diterima
   (`DECODE_ERROR`); `sha256`/`size` selalu atas byte mentah. Generated =
   hint (nama/banner ≤60 baris/minified JS ≥4KB), bukan filter — persis
   codegraph §5.
7. **Determinisme**: traversal mengikuti urutan anak AST (kanonik), semua
   koleksi output diurutkan di `to_artifact()`, tidak ada iterasi set/dict
   yang bocor ke output. Diverifikasi byte-identik lintas proses dengan
   `PYTHONHASHSEED ∈ {0,1,42,12345}` (16 subprocess test).
8. **Variabel/konstant** hanya di-scope file/module/class-ish (bukan di dalam
   badan fungsi) — kontrol noise ala codegraph value-refs.

## Hasil gate (nyata)

- `pytest tests/test_ingest_{languages,extractor,files,properties}.py -q` →
  **847 passed** (target 200+ terlampaui), exit 0.
- `ruff check` keempat file test + kedua modul → All checks passed.
- `pyright src/know_your_soul/ingest/{extractor,languages}.py` → 0 errors.
- Grammar parity: setiap node-type di registry diverifikasi ada di grammar
  asli via `Language.id_for_node_kind` (12 bahasa).
- Invarian contains-forest divalidasi oleh engine invariant W1
  (`UniversalGraph.validate`) untuk semua fixture + fuzz hypothesis.

## Catatan koordinasi

- `ingest/__init__.py` dan `ingest/pipeline.py` milik SUB-89; file ini tidak
  disentuh SUB-87. Ruff menemukan 5 pelanggaran di `pipeline.py` (milik
  SUB-89, di luar lingkup SUB-87).
- `store.py`/`graph_model.py` tidak diubah; extractor memakai API mereka.
- Tidak ada commit (aturan pool).
