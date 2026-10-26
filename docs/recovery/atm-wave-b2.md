# ATM Wave B bagian 2 — F-B7 viz/layout + F-B4 exporters/graphml_svg (SUB-66)

Tanggal: 2026-10-26. Repo: `/root/know-your-soul` @ `main` (HEAD 6272fe6 saat mulai). TIDAK di-commit (sesuai scope). 0 file existing diubah; 6 file baru.

## Apa yang dibangun

| File | Plan | API publik | Isi |
|---|---|---|---|
| `src/know_your_soul/viz/__init__.py` | F-B7 | re-export | Marker paket + re-export API layout. |
| `src/know_your_soul/viz/layout.py` | F-B7 | `layout(read_model, *, seed, iterations, width, height, mode) -> LayoutResult`; `LayoutError`; konstanta `DEFAULT_SEED`, `DEFAULT_ITERATIONS`, `MAX_LAYOUT_NODES`, `MAX_LAYOUT_ITERATIONS`, `QUANTUM` | Mesin layout deterministik murni-Python. Dua mode: **radial-tree** (forest: root = derajat tertinggi tie-break id terkecil per komponen, BFS terurut, alokasi sudut proporsional jumlah daun subtree, ring per depth, komponen di-packing grid sel deterministik) dan **force** (spring ala Fruchterman-Reingold: inisialisasi spiral golden-angle + jitter `random.Random(seed)` dalam urutan node ter-sortir, repulsi all-pairs + atraksi edge dalam urutan kanonik, temperatur mendingin linear, iterasi berjumlah tetap tanpa early-exit, fit bbox ke frame, tanpa upscale). Mode `auto` = keputusan deterministik dari bentuk graph: forest (`m ≤ n − components`) → radial, lebih rapat → force. Koordinat terkuantisasi ke grid `QUANTUM=0.001` (pembulatan tetap, -0.0 dinormalisasi). Cap fail-closed typed (`LayoutError`): frame non-positif/non-finite, iterations di luar `[0, 10_000]`, node > 4096, mode tak dikenal. |
| `src/know_your_soul/exporters/graphml_svg.py` | F-B4 | `to_graphml`, `to_svg`, `validate_graphml_bytes`, `validate_svg_bytes`, `domain_palette`; typed errors `GraphExportError` / `GraphExportCapExceededError` / `GraphExportRoundTripError` | **GraphML**: XML 1.0 legal (`_xml10_clean` membuang C0 control, lone surrogate, U+FFFE/U+FFFF — pelajaran "1 label buruk membatalkan seluruh ekspor" ditutup dengan strip, bukan abort), namespace kanonik graphdrawing, key d_label/d_domain/d_kind/d_version/d_relation + count d_node_count/d_edge_count di level graph, node sorted-by-id, edge sorted `(source, relation, target)` dengan id sintetik stabil `e000001…`, escaping via saxutils. **SVG statis**: tanpa script/CDN; edge garis, node lingkaran berwarna per domain (palet terang milik kita P1–P7 + ramp fallback deterministik by sorted-domain), label terbatas 48 char dengan elision **bertanda** (`…`, teks penuh di `<title>`), legenda domain ter-sortir ter-bounding (`+N more…`). Posisi dari `viz.layout`. Cap eksplisit: 4096 node / 16384 edge / 256 KiB per artifact; producer menjalankan round-trip validator sendiri sebelum return (fail-closed). |

## Keputusan desain

- **Determinisme (plan §5.2)**: iterasi selalu atas tuple ter-sortir; set hanya untuk membership; RNG lokal ber-seed eksplisit hanya dipakai untuk jitter inisial mode force (urutan draw = urutan node ter-sortir); tanpa wall-clock; kvantisasi menghilangkan noise float sub-piksel. Dibuktikan: 3 run byte-identik + **3 proses terpisah dengan `PYTHONHASHSEED=0/1/42` menghasilkan sha256 identik** untuk GraphML, SVG, dan posisi layout.
- **Round-trip validator** (pola F-B8): `validate_graphml_bytes` parse ET, cek root namespace, id node unik, endpoint edge ada, C0 scan byte-level, dan **count deklaratif vs aktual** (d_node_count/d_edge_count) — defect apa pun → `GraphExportRoundTripError`. `validate_svg_bytes`: UTF-8 + C0 scan + root `{svg}svg`. Validator adalah fungsi murni terpisah sehingga bisa dipakai orchestrator SUB-63/integrator wave B sebagai `roundtrip` registry entry tanpa impor siklik.
- **Sanitasi**: label/relation lewat `security.sanitize_text` (strip Cc/Cf) + `_xml10_clean` + cap char; node id yang berubah saat sanitasi **ditolak sebagai malformed** (id adalah system-generated; konsistensi referensi edge↔node terjaga). Link endpoint-unknown/self-loop/duplikat dibuang deterministik (pola `_prepare_graph` wave A).
- **Koordinasi dengan `tools/generate_visual_assets.py`**: script itu mempertahankan radial placer pinned-nya sendiri (aset README frozen, byte-locked oleh `--check`); modul ini adalah mesin umum untuk keluarga exporter. Tidak ada kode yang diduplikasi — algoritma di sini general (multi-komponen, BFS wedge allocation) vs script yang hard-coded KROOT/P1..P7. Palet SVG sengaja **berbeda** (tema terang untuk dokumen vs tema gelap aset README) dan merupakan pilihan milik repo ini.
- **Clean-room**: struktur mengikuti spesifikasi publik GraphML/SVG 1.1; tidak ada template string, nama helper, atau palet Graphify yang diangkat.
- **Tanpa deps baru**: stdlib saja (`math`, `random`, `xml.etree`, `xml.sax.saxutils`) + modul repo sendiri.

## Bukti gate

- RED teramati: 2 collection errors (`ModuleNotFoundError` viz.layout / exporters.graphml_svg), pytest exit=2 — dicatat di board.
- `pytest tests/test_viz_layout.py tests/test_exporters_graphml_svg.py -q` → **43 passed** (21 layout + 22 exporter), termasuk: determinisme 3-run byte-identik (layout radial+force, GraphML, SVG), invariansi permutasi input, hypothesis property (`derandomize=True`, 25 contoh: permutasi-invarian + dalam frame untuk graph acak ≤24 node), koordinat dalam frame & terkuantisasi, XML valid ET-parse, C0 strip, elision bertanda, cap typed.
- Probe determinisme lintas proses: `PYTHONHASHSEED ∈ {0,1,42}` → sha256 GraphML/SVG/layout identik; mode auto = radial pada topologi star-of-stars 7 domain.
- `ruff check` + `ruff format --check` pada src+tests repo-wide: **0 issue**.
- `pyright` pada 6 file baru: **0 errors, 0 warnings**.
- Full suite: **1904 passed, 0 failed** (341s) dengan `--ignore` pada `tests/test_analytics_{generation_diff,timeline}.py` — dua file itu **WIP sibling wave C** (test tanpa modul, RED state mereka; terlihat untracked di `git status`). Diverifikasi bukan milik SUB-66: modul saya tidak diimpor file mana pun di luar 2 test saya.

## Catatan untuk integrator

- Untuk registry F-B8: `ExportFormat(name="exports/graph.graphml", kind="format", producer=to_graphml, roundtrip=validate_graphml_bytes, max_bytes=GRAPHML_MAX_BYTES)` dan pasangan SVG-nya — producer sudah `read_model -> bytes` persis sesuai kontrak orchestrator; kedua validator signature `bytes -> None`.
- HTML viewer (F-B5) harus memakai `viz.layout` untuk posisi (seed pinned) agar konsisten dengan SVG.
- `LayoutResult.metadata` membawa mode/seed/iterations/frame/counts/quantum — cukup untuk mereproduksi atau mengaudit penempatan di artifact manapun.
