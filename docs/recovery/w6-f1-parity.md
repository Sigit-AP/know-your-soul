# W6 — F1: paritas Rust↔Python diperbaiki (gate jujur + store byte-identik lintas engine)

Tanggal: 2026-10-28 (UTC) · SUB-97 · HEAD awal `12f16db` (main) · tanpa commit (aturan wave).

## Temuan (audit-final-v020.md, F1)

1. `extract_source()` me-route ke kernel Rust bila kernel tersedia
   (`extractor.py:770-774`), sehingga `tests/test_kernel_rust.py` membandingkan
   **kernel dengan dirinya sendiri** — tautologi. Suite hijau tanpa pernah
   membandingkan walker Python.
2. Perbandingan jujur (kill-switch `KYS_DISABLE_RUST_KERNEL=1` vs kernel)
   menunjukkan `ExtractionResult` **mentah berbeda urutan** pada `edges` dan
   `unresolved_refs`: walker Python mengikuti urutan emisi scope, bridge kernel
   mengikuti urutan canonical ter-sort dari artifact JSON.
3. Konsekuensi: pipeline meng-insert dalam urutan emisi → rowid `edge_id`/
   `ref_id` dan `ref_id` yang tertanam di metadata edge hasil resolusi berbeda
   antar engine → store from-scratch **tidak byte-identik** lintas engine
   (audit: `a0471cff…83a` vs `6b4ef3d2…f4de`). Konten logis identik;
   divergensi murni penomoran/urutan.

## Keputusan engineering

**Urutan emisi canonical adalah kontrak kedua engine** (bukan kanonikalisasi
metadata store). Alasan: sumber divergensi adalah urutan emisi mentah; dengan
menjadikan urutan canonical sebagai satu-satunya urutan legal di kedua engine,
(a) `ExtractionResult` mentah — bukan hanya bentuk canonical — identik lintas
engine, (b) urutan insert pipeline menjadi fungsi murni dari byte sumber,
sehingga rowid/ref_id identik dan store byte-identik **dengan id**
(`include_ids=True`), bukan hanya bentuk logis, (c) tidak ada lapisan
normalisasi baru di pipeline/store.

Kunci sort canonical (tidak berubah dari `to_artifact()` yang sudah ada):

- nodes: `(start_line, start_col, node_id)`
- edges: `(source, target, kind)`
- unresolved_refs: `(source_node_id, target_name, kind, line, col)`

Implementasi:

- `extractor.py`: helper `_node_canonical_key`/`_edge_canonical_key`/
  `_ref_canonical_key` (satu sumber kebenaran, dipakai `extract()` dan
  `to_artifact()`); `TreeSitterExtractor.extract()` men-sort tiga koleksi
  sebelum membangun `ExtractionResult`. Sort di `to_artifact()` kini idempoten.
- `kernel-rust/src/walker.rs`: `sort_nodes_canonical`/`sort_edges_canonical`/
  `sort_refs_canonical` (publik, mirror helper Python), dipanggil di akhir
  `extract_source()` dan dipakai ulang oleh `to_artifact()`.
- `kernel_bridge.py`: docstring kontrak; sisi bridge sudah mewarisi urutan
  canonical dari artifact JSON, tidak perlu perubahan perilaku.

## Perbaikan gate (tautologi → jujur)

`tests/test_kernel_rust.py`:

- `_python_walker_reference()` baru: sisi Python = `TreeSitterExtractor(
  get_spec(...)).extract(...)` langsung — tidak pernah lewat routing.
- `test_bridge_matches_python_byte_identical` (12 bahasa): diperbaiki, kini
  assert `rust_result == python_result` pada **dataclass mentah** (urutan
  termasuk) + canonical JSON. Pre-fix: GAGAL pada `[bash]` (edges/refs order)
  — replikasi persis F1. Test lama tidak dihapus; namanya dipertahankan,
  isinya dijujurkan.
- `test_cli_matches_python_byte_identical`: sisi Python dari walker langsung.
- `test_extract_source_property_python_parity`: sisi Python dari walker
  langsung + assert raw equality.
- `test_cross_engine_store_byte_identical` (BARU): ingest penuh
  `tests/fixtures/ingest` (12 bahasa) dua kali from-scratch — kernel aktif vs
  kill-switch — lalu `canonical_ingestion_dump` (rowid termasuk) wajib
  byte-identik.
- `kernel-rust/tests/parity.rs`: assertion `nodes[0]` adalah file node
  diganti membership (sort canonical tidak menjamin file node pertama),
  plus assertion raw nodes/edges/refs tersort canonical per fixture.

## Bukti verifikasi

- RED nyata: `test_bridge_matches_python_byte_identical[bash]` gagal pre-fix
  pada `edges`/`unresolved_refs` (pytest exit=1).
- GREEN: `tests/test_kernel_rust{,_bridge,_registry}.py` 61 passed exit=0;
  `cargo test` 12 passed exit=0.
- Store lintas-engine, **dua proses terpisah** (probe
  `/root/.hermes/cache/scratch/sub97_probe.py`):
  with-kernel sha256 `a0471cfff8e556adbb4e2b84a0fca358db658c2a66ef49c5f9db1e06ca97983a`
  = no-kernel sha256 `a0471cff…983a` (55.413 byte) — identik, dan cocok dengan
  hash with-kernel audit. Divergensi `6b4ef3d2…f4de` hilang.

## Residual terdokumentasi (ditemukan oleh gate jujur)

**Error-recovery parse berbeda pada sumber INVALID.** Pada input yang bukan
Python valid (contoh: `def else():` — keyword sebagai nama), kedua engine
memakai grammar python pada rev yang sama tetapi core tree-sitter berbeda,
sehingga recovery berbeda: walker Python menghasilkan node `function else`
dengan `parse_error=False`, kernel Rust menjatuhkannya dengan
`parse_error=True`. Ini divergensi **konten** pra-eksis yang disembunyikan
gate tautologis (kedua sisi lama ter-route ke kernel). Dampak: file yang
gagal parse dapat menghasilkan store berbeda konten antar engine. Mitigasi
hari ini: kontrak byte-identity berlaku untuk sumber valid (fixture 12 bahasa
+ property test mengecualikan keyword sebagai identifier); recovery pada
sumber invalid tetap deterministik per-engine. Penyelarasan core grammar
(mis. pin core tree-sitter Python ke 0.25.10) adalah kandidat wave berikut.
