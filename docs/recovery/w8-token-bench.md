# W8 — token-reduction benchmark (SUB-108)

Tanggal: 2026-10-30. Scope: `bench/tokens/` (baru), `tests/test_tokens_bench.py`,
dokumen ini. `src/` dan extraction TIDAK disentuh (milik SUB-106).

## Tujuan

Mengukur reduksi token produk secara jujur pada korpus campuran comparable
dengan klaim Graphify (71.5x pada korpus 52 file milik mereka), dengan
metodologi yang lebih ketat dari harness mereka.

## Apa yang dibangun

`bench/tokens/` — suite kelima, pola `bench/codebase`:

- **Korpus**: 52 file vendored offline di `bench/tokens/corpus/`, sha256 per
  file di `manifest.json`. Campuran: kode multi-bahasa (Python/JavaScript/C
  dari repo ter-pin `bench/codebase` — requests v2.32.4, chalk v5.6.2,
  tree-sitter-python v0.23.6), markdown (docs requests/chalk/tree-sitter +
  5 file SOUL dari `bench/corpus`), teks (corpus tree-sitter), dan 5 PDF
  "paper notes" kecil yang digenerate deterministik byte-identik oleh
  `run.py::make_pdf_bytes` (minimal PDF 1.4, tanpa timestamp/id; terverifikasi
  terbaca pypdf 6.21.3 dengan text layer).
- **Tier bersarang**: small 6 ⊂ medium 26 ⊂ large 52 file — reduksi naik
  dengan ukuran korpus, jadi dilaporkan per ukuran, termasuk small yang
  memang rendah (Graphify sendiri melaporkan ~1x untuk 6 file).
- **Pertanyaan beku** (`questions.json`): 6/10/16 per tier, tiap tier punya
  1 kontrol negatif (harus 0 hit). Rubric = istilah yang wajib ada di
  konteks terakit (nama simbol/heading/path — produk memang mengembalikan
  identitas + provenance, bukan body mentah).
- **Estimator beku**: `ceil(utf8_bytes/4)` — formula kontrak
  (`contracts.estimated_tokens_of_payload`), dipakai untuk file mentah
  (byte) dan konteks terakit (render). Paritas di-pin di test.
- **Pengukuran produk apa adanya**: `IngestionPipeline.ingest_path` +
  `resolve_pending`, lalu per pertanyaan `SymbolLookup.search` (top-10) +
  `assemble_context` (budget 2000 token = default MCP Graphify).

## Perbedaan metodologi vs harness Graphify (terdokumentasi di ledger)

1. Estimator korpus: mereka `words*133/100`; kita `ceil(bytes/4)` pada byte
   nyata.
2. Kontrol negatif (no-match) **TERMASUK** dalam rata-rata biaya query kita;
   harness mereka mengecualikannya (`benchmark.py:83-92`) — mengecualikan
   mengangkat rasio.
3. Jawaban mereka adalah teks subgraph BFS (~1.7k token rata-rata); jawaban
   produk kita adalah entri simbol terskeletonisasi + provenance (~300-an
   token). Ini perbedaan desain produk yang nyata, dilaporkan apa adanya.

## Hasil run nyata (ledger-20261030T194355Z, exit 0)

| Tier | Files | Raw tokens | Mean query tokens | Reduction | Saving | Rubric | Recall |
|---|---|---|---|---|---|---|---|
| small | 6 | 8,940 | 321.0 | 27.9x | 96.41% | 1.00 | 5/5 |
| medium | 26 | 62,442 | 319.5 | 195.4x | 99.49% | 0.87 | 9/9 |
| large | 52 | 151,606 | 325.1 | 466.4x | 99.79% | 0.97 | 15/15 |

Referensi terdokumentasi (korpus BERBEDA — bukan perbandingan like-for-like):
Graphify 71.5x pada korpus 52 file milik mereka. Angka kita lebih tinggi
karena konteks terakit produk jauh lebih ramping (skeletonisasi); klaim
perbandingan head-to-head sengaja TIDAK dibuat — ledger hanya mencatat
referensi dengan caveat lengkap.

## Temuan jujur (bukan gate failure)

- **Cakupan rubric parsial nyata** pada 3 pertanyaan: q-m4 "cookie jar
  merge" 0.33 (hanya 1 dari 3 simbol cookie masuk top-10), q-m5 "prepared
  request encoding mixin" 0.5, q-l5 "grammar tests comprehension" 0.5.
  Semua istilah rubric ADA sebagai simbol di store (diverifikasi dari dump
  simbol) — retrieval men ranking mereka di bawah top-10. Keterbatasan
  produk yang diukur, bukan disembunyikan: mean coverage dilaporkan apa
  adanya (0.87/0.97).
- **Rust kernel menolak 4 file requests** (`from . import x` — nama kosong,
  fallback ke walker Python; temuan lama F-SUB94-1 yang residunya dicatat
  SUB-95/SUB-97, bridge menutup dengan benar; hasil ekstraksi identik).
- Small tier 27.9x (bukan ~1x seperti baris 6-file Graphify): konteks
  terakit adaptif terhadap jumlah hit, tidak memaksakan ~2k token per
  jawaban.

## Gate (semua hijau pada run nyata)

1. verify korpus vs manifest — OK;
2. determinisme: 2 pass ingest byte-identik per tier (`canonical_ingestion_dump`)
   + 2 pass query identik per pertanyaan — OK;
3. recall retrieval: positif ≥1 hit, negatif 0 hit — OK (5/5, 9/9, 15/15);
4. rubric mean ≥ 0.5 per tier — OK (1.00/0.87/0.97).

`tests/test_tokens_bench.py`: 13 test — self-checks, paritas estimator
kontrak, integritas question-set (nesting, sumber dalam tier, kontrol
negatif), determinisme 2 pass + 2 invokasi `measure()`, recall, rubric ≥
floor, struktur ledger (JSON round-trip, referensi 71.5x dengan label
"different corpus"), render report per tier.

## Perintah reproduksi

```bash
uv run python bench/tokens/run.py check    # exit 0
uv run python bench/tokens/run.py verify   # exit 0
uv run python bench/tokens/run.py run      # exit 0, ledger + report baru
uv run pytest tests/test_tokens_bench.py -q
```

## Catatan koordinasi

- TIDAK commit (aturan wave).
- `bench/README.md` ditambah 1 baris tabel suite (preseden SUB-94) — doc-only,
  di luar scope ketat `bench/tokens/`; revertible kalau koordinator keberatan.
- Sibling SUB-106 memegang `src/` + extraction — 0 file mereka disentuh.
