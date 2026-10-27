# ATM Wave 2 — perluasan format ekspor (SUB-75)

Tanggal: 2026-10-27. Repo: `/root/know-your-soul` @ `main` (HEAD `86b8d15` saat mulai). **TIDAK di-commit** (aturan multi-agent). Clean-room: semua format ditulis dari spesifikasi publik (GEXF 1.2draft, Cytoscape.js elements JSON, konvensi D3-force node-link, adjacency matrix + edge list, Trivial Graph Format); tidak ada satu baris/konstanta/template Graphify yang disalin. Tidak ada dependency baru, tidak ada AI/network, orchestrator.py tidak disentuh (integrasi = keputusan koordinator wave).

## Milestone board

- [06:16 UTC] SUB-75 | mulai; pola dipelajari (orchestrator/dot/mermaid/tabular/graphml_svg + plan §5); desain 6 format dipin
- [06:30 UTC] SUB-75 | RED: 6 file test ditulis, 6 collection error ModuleNotFoundError teramati
- [06:48 UTC] SUB-75 | GREEN: 6 modul implementasi; 205 test hijau; ruff+pyright 0; determinisme lintas PYTHONHASHSEED 0/1/42 byte-identik (sha256 sama)
- [07:05 UTC] SUB-75 | gates: full suite 2729 passed / 0 failed; recovery doc selesai

## Apa yang dibangun

Enam modul eksportir baru di `src/know_your_soul/exporters/` (total 1.732 LOC src + 1.771 LOC test), masing-masing mengikuti pola orchestrator (F-B8): fungsi murni `read_model -> str|Mapping` kanonik + `produce_* -> bytes` ber-cap eksplisit fail-closed bertipe (`ExportCapExceededError`/`ExportError` dari orchestrator) + `roundtrip_*` (parse-kembali → assert struktur) + `register_*` seam ke `ExportRegistry`.

| File | API publik | Isi & cap |
|---|---|---|
| `gexf.py` (342) | `render_gexf`, `produce_gexf`, `roundtrip_gexf`, `register_gexf`; `GEXF_NAMESPACE`, cap 2 MiB / 4096 node / 16384 edge / 256 char label | GEXF 1.2draft: namespace `http://www.gexf.net/1.2draft`, `<graph mode="static" defaultedgetype="directed">`, `<attributes>` node (domain/kind/version) + edge (relation), `<nodes count>`/`<edges count>` eksplisit, edge id sekuensial `eNNNNNN`. XML-1.0 legal (C0/surrogate/non-character dibuang sebelum escaping `quoteattr`); id yang berubah saat cleaning → node di-drop. Tanpa `<meta>` (creator/date merusak determinisme). Round-trip: ElementTree parse, namespace/version/mode persis, urutan kanonik node/edge, sekuens id edge, endpoint terdeklarasi, count cocok. |
| `cytoscape.py` (289) | `build_cytoscape`, `produce_cytoscape`, `roundtrip_cytoscape`, `register_cytoscape`; cap 4 MiB / 4096 / 16384 / 256 | Cytoscape.js elements JSON: `{"artifact","schema_version","elements":{"nodes":[{"data":{id,label,domain,kind,version}}],"edges":[{"data":{id:eNNNNNN,source,target,relation}}]}}`, canonical JSON + `\n`. Round-trip: byte-exact canonical re-serialisasi + struktur + urutan + endpoint + label bebas Cc/Cf. |
| `d3force.py` (274) | `build_d3force`, `produce_d3force`, `roundtrip_d3force`, `register_d3force`; cap 4 MiB / 4096 / 16384 / 256 | D3-force node-link: `nodes[]` ber-`index` == posisi terurut + `group`==`domain`; `links[]` mereferensikan integer index, terurut atas (source,target,relation) string-id. Round-trip: index==posisi, link index sekuensial, endpoint in-range, label bersih. |
| `adjacency.py` (298) | `render_/produce_/roundtrip_adjacency_csv`, `render_/produce_/roundtrip_edge_list_txt`, `register_adjacency`; matrix cap 16 MiB / 2048 node (kuadratik), edge-list cap 4 MiB / 65536 edge | Dua artifact: `exports/adjacency.csv` (RFC 4180, header `id,<sorted ids>`, sel `"0"/"1"`, self-loop = diagonal 1) dan `exports/edges.txt` (header 3 baris `#`-marker + counts, baris `source\tTARGET\trelation` TAB-separated terurut). Semua field disanitasi + whitespace-control collapse (TAB/CR/LF tak bisa menyelundupkan field). |
| `tgf.py` (233) | `render_tgf`, `produce_tgf`, `roundtrip_tgf`, `register_tgf`; cap 2 MiB / 8192 node / 32768 edge / 80 char label | Trivial Graph Format: id integer `1..N` ditetapkan dalam urutan node-id ter-sortir, label tersanitasi (fallback node id), separator `#`, edge `src dst relation` (relasi kosong → fallback `related`). Round-trip: id sekuensial dari 1, satu separator, edge terurut unik, endpoint ≤ N, tanpa Cc/Cf. |
| `summary.py` (296) | `build_summary`, `produce_summary`, `roundtrip_summary`, `register_summary`; cap 256 KiB, kind=`analytics` | `analytics/graph-summary.json`: counts (nodes/edges/self_loops/isolated/dangling/duplicates — penjatuhan tidak pernah senyap), degree histogram (total degree atas edge ter-dedup, self-loop = 2, konsisten dengan `nodes.csv`), min/max/mean (mean = string 12-desimal via `serialize_score`), breakdown per domain (nodes/internal_edges/boundary_in/out; domain kosong → `unknown`), counts per relasi. Round-trip: canonical byte-exact + invariant struktural (histogram sum == nodes; massa degree == 2×edges; domain nodes sum == nodes; boundary in == out; internal+boundary_out == edges; relasi sum == edges; bentuk mean terkuantisasi). |

## Keputusan desain

- **Error classes**: memakai `ExportError`/`ExportCapExceededError`/`ExportRoundTripError` dari orchestrator (pola dot/mermaid/tabular), bukan kelas baru — satu hierarki bertipe untuk integrator.
- **Sanitasi seragam**: `security.sanitize_text` (strip Cc/Cf/bidi/zero-width) → slice ke `max_chars+1` **sebelum** sanitize (agar label raksasa tidak melempar `OversizedPayloadError`) → collapse `[\t\r\n]+` → spasi → cap char. Id yang berubah saat sanitasi di-drop sebagai malformed (ids adalah slug system-generated).
- **Relasi kosong**: setelah collapse, relasi kosong → fallback konstan `"related"` (TGF/Cytoscape/D3/GEXF) atau edge di-drop (edge-list) — didokumentasikan per modul; round-trip selalu menolak relasi kosong.
- **Self-loop dipertahankan** di semua 6 format (GEXF/TGF/D3/Cytoscape mendukung; matrix = diagonal 1; summary menghitung 2 ke degree) — konsisten dengan dot.py.
- **Determinisme**: semua iterasi atas struktur ter-sortir; dedup via set yang di-sorted sebelum render; tanpa waktu/RNG; dibuktikan byte-identik untuk input ter-permutasi (test unit) + hypothesis `derandomize=True` per modul + proses terpisah dengan `PYTHONHASHSEED=0/1/42` (sha256 gabungan identik).
- **Cap fail-closed bertipe**: byte cap + count cap eksplisit per format; registry mengulang cap-check sebagai backstop; `produce_*` menjalankan roundtrip-nya sendiri sebelum return.

## Bug yang ditangkap test (RED→GREEN nyata)

1. `sanitize_text(raw, max_length=max_chars+1)` melempar `OversizedPayloadError` untuk label 500 char alih-alih mem-cap → slice dulu baru sanitize (4 modul; ditangkap `test_label_char_cap`).
2. Ekspektasi escaping GEXF keliru di test: `quoteattr` beralih ke single-quote dan tidak meng-escape `"` → test dikoreksi ke perilaku XML-legal yang sebenarnya.
3. Edge-list node-count check awal memakai ekspresi set-union yang kacau → disederhanakan ke himpunan endpoint eksplisit.
4. Relasi all-whitespace di TGF menghasilkan baris yang gagal round-trip sendiri → fallback `"related"`.

## Bukti gate

- `pytest tests/test_exporters_{gexf,cytoscape,d3force,adjacency,tgf,summary}.py -q` → **205 passed** (RED teramati: 6 collection errors `ModuleNotFoundError`).
- `ruff check` + `ruff format --check` 12 file baru: **0 error**. `pyright` 12 file baru: **0 errors, 0 warnings**.
- Determinisme lintas proses: `PYTHONHASHSEED=0/1/42` → sha256 gabungan 7 producer identik.
- Full suite: **2729 passed, 0 failed** (329s) dengan `--ignore` pada 4 modul test sibling wave yang implementasinya belum ada (`test_analytics_timeline.py`, `test_analytics_generation_diff.py`, `test_analytics_algorithms.py`, `test_analytics_paths.py` — `ModuleNotFoundError`, di luar scope SUB-75; pendekatan sama dengan SUB-65).

## Integrasi (untuk koordinator wave)

```python
from know_your_soul.exporters import adjacency, cytoscape, d3force, gexf, summary, tgf
gexf.register_gexf(registry)          # exports/graph.gexf
cytoscape.register_cytoscape(registry)  # exports/cytoscape.json
d3force.register_d3force(registry)     # exports/d3-force.json
adjacency.register_adjacency(registry) # exports/adjacency.csv + exports/edges.txt
tgf.register_tgf(registry)             # exports/graph.tgf
summary.register_summary(registry)     # analytics/graph-summary.json (kind="analytics")
```

Semua artifact lolos `produce_exports` (cap + round-trip) dan siap masuk manifest via `manifest_entries`. Mengubah bentuk output = bump `version` di `register_*`.
