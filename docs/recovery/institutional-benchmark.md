# Institutional graph benchmark (SUB-78)

Harness: `bench/institutional/run.py` — menguji kernel graph `know_your_soul.analytics`
melawan referensi pada standar benchmark institusional, dengan kebenaran + performa +
determinisme per kernel. Laporan: `bench/institutional/results/institutional-latest.{json,md}`.

## 1. Riset: standar yang dipakai dan kenapa

Dua standar industri diverifikasi dari sumber resmi:

**LDBC Graphalytics** (ldbcouncil.org/benchmarks/graphalytics, spec
`ldbc_graphalytics_docs/graphalytics_spec.pdf`): benchmark industrial-grade untuk platform
analisis graph (Giraph, Spark GraphX, GraphBLAS). Enam kernel inti: **BFS, PageRank (PR),
WCC, CDLP, LCC, SSSP**. Metodologi validasi (LDBC TUC, G. Szárnyas): *exact match* untuk
BFS/CDLP, *epsilon match* (toleransi relatif 0.01%) untuk LCC/PR/SSSP, *equivalence match*
(kelas ekuivalensi sama) untuk WCC. Dataset diklasifikasikan "T-shirt size" (S/M/L/XL/2XL+)
dengan timeout per skala. Varian PageRank Graphalytics me-redistribusi massa dangling ke
semua vertex — sama dengan skema kita dan referensi.

**GAP Benchmark Suite / GAPBS** (github.com/sbeamer/gapbs, paper arXiv:1508.03619):
enam kernel **BFS, SSSP (delta-stepping), PR, CC (Afforest/Shiloach-Vishkin), BC (Brandes),
TC**, dipilih lewat survei literatur + karakterisasi workload; metodologi pengukuran:
64 trial dari 64 sumber untuk BFS/SSSP, 16 trial CC, 16×4 BC, 3 TC — median/rata-rata
antar-trial. GAPBS juga menekankan *referensi baseline yang dioptimasi* sebagai titik
banding performa.

**Keputusan desain harness** (dan alasannya):

1. *Kernel set* = union LDBC Graphalytics ∪ GAPBS, dipetakan ke kernel kita yang ada
   (lihat tabel di bawah). BFS/SSSP dilaporkan SKIP jujur (belum ada entrypoint publik;
   read-model tidak membawa bobot edge).
2. *Matcher per kernel mengikuti Graphalytics*: exact (cycles, TC), equivalence (WCC,
   partisi komunitas), epsilon (PR/BC/LCC).
3. *Referensi* = **networkx 3.7** (sudah pinned main dependency — tidak perlu menambah
   dev-dep, policy guard tetap hijau). Pengecualian: `networkx.pagerank` memerlukan scipy
   (sengaja bukan dependency) → referensi PR ditulis sendiri dalam numpy, *self-validated*
   (assert residual L1 < 1e-10 dan massa = 1 pada setiap panggilan). Plus cross-check
   kedua: klon float pure-python dari skema fixed-iteration kita (toleransi 1e-9) untuk
   menangkap drift implementasi yang bisa lolos dari toleransi longgar.
4. *Dataset* = kecil-dengan-hasil-diketahui (path3, diamond, two_triangles_bridge,
   star9_bidir, directed_cycle6, two_cycles_shared_node, disconnected3, cliques_4x5) +
   sintetis ter-seed (`random.Random(11/12/13)`, n=64/256/512). Tanpa network, tanpa
   unduhan — murni fungsi kode. Dataset SNAP tidak dipakai: pinning artifact besar
   melanggar "tanpa network di test", dan T-shirt-size LDBC tidak relevan untuk graph
   knowledge sebesar kita.
5. *Pengukuran* = median wall-time dari `--repeats` (default 3) untuk kita dan referensi,
   graf networkx dibangun di luar timing. Dua angka untuk kita: `ours_public_ms` (biaya API
   publik apa adanya) dan `ours_kernel_ms` (kernel terisolasi via helper privat, dilabeli
   jelas) — karena entrypoint publik kita (mis. `compute_centrality`) menghitung seluruh
   suite, yang akan menyesatkan rasio bila tidak dipisah.
6. *Determinisme*: setiap kernel dijalankan dua kali per dataset; output kanonik
   (`json.dumps(sort_keys=True)`) harus byte-identik — kegagalan determinisme = FAIL.
7. *Auto-discovery*: setiap adapter `importlib.import_module` + `getattr` dalam try/except.
   Kernel yang belum landed → `skipped` dengan alasan eksplisit, tidak pernah crash atau
   pass palsu. Terbukti nyata: saat `analytics/algorithms.py` (mega-wave 2) mendarat di
   tengah sesi, LCC/TC/Louvain langsung terdeteksi dan terskor tanpa mengubah harness.

## 2. Pemetaan kernel

| Kernel harness | Standar | Implementasi kita | Referensi | Matcher |
|---|---|---|---|---|
| pagerank | LDBC PR, GAPBS PR | `analytics.centrality.compute_centrality` (fixed 64 iter, d=0.85, dangling redistribute) | numpy self-validated + klon float 1e-9 | epsilon rel≤1e-3 abs≤1e-4 |
| wcc | LDBC WCC, GAPBS CC | `analytics.components.analyze_components` | `nx.connected_components` | equivalence |
| bc | GAPBS BC | `analytics.centrality` Brandes (dipaksa eksak: samples≥n) | `nx.betweenness_centrality` normalized | epsilon rel≤1e-6 abs≤1e-9 |
| cdlp | LDBC CDLP | `analytics.communities.detect_communities` | `nx.community.asyn_lpa_communities(seed=0)` | exact-cover di semua + equivalence di dataset unambiguous |
| lcc | LDBC LCC | `analytics.algorithms.compute_clustering` (auto-discovered) | `nx.clustering` | epsilon abs≤1e-9 |
| tc | GAPBS TC | `analytics.algorithms.compute_clustering.global.triangles` | `nx.triangles` sum/3 | exact integer |
| louvain | project-local (GAPBS-adjacent) | `analytics.algorithms.compute_louvain` | `nx.community.louvain_communities(seed=0)` + cross-check modularitas partisi kita | equivalence + abs≤1e-9 |
| cycles | project-local | `analytics.cycles.find_cycles` | `nx.simple_cycles` (rotasi kanonik) | exact set |
| bfs | LDBC/GAPBS BFS | — belum ada entrypoint publik | — | SKIP jujur |
| sssp | LDBC/GAPBS SSSP | — read-model tanpa bobot | — | SKIP jujur |

Catatan toleransi PR: Graphalytics memakai epsilon relatif 0.01%; PageRank kita fixed
64 iterasi tanpa early-stop (residual terhadap fixed point ≤ d⁶⁴ ≈ 3e-5), jadi gerbang
abs≤1e-4 + rel≤1e-3, dan error terukur dilaporkan apa adanya (terukur ≤ 1.1e-5, mayoritas
~5e-13 — jauh di dalam gerbang Graphalytics).

## 3. Hasil run nyata (2026-10-27, `python bench/institutional/run.py`, exit 0)

**SKOR: 7/8 kernel tersedia PASS kebenaran** (1 FAIL terdokumentasi, 2 SKIP jujur).

| Kernel | Status | Temuan |
|---|---|---|
| pagerank | PASS | max err ~5e-13 vs referensi konvergen (worst 1.1e-5 di star); klon float 1e-9 cocok |
| wcc | PASS | equivalence di 11/11 dataset |
| bc | PASS | Brandes eksak cocok networkx sampai ~5e-17; star hub=1.0 persis |
| cdlp | **FAIL** | LPA sinkron kanonik kita menggabung `two_triangles_bridge` jadi 1 komunitas; referensi (dan Louvain kita sendiri, Q=5/14) memisah di bridge. Divergensi varian LPA yang sah, tapi sinyal kualitas nyata → dilaporkan, tidak disembunyikan |
| lcc | PASS (auto-discovered) | koefisien per-node cocok networkx ≤ 4.9e-13; hand-check diamond avg=7/12 |
| tc | PASS (auto-discovered) | jumlah segitiga integer persis di 11/11 dataset |
| louvain | PASS (auto-discovered) | partisi setara referensi di dataset unambiguous; modularitas partisi kita divalidasi silang oleh networkx ≤1e-9 |
| cycles | PASS | set cycle persis `nx.simple_cycles` di semua dataset kecil |
| bfs | SKIP | kernel belum tersedia (hanya `_bfs_distances` privat) |
| sssp | SKIP | kernel belum tersedia (tanpa bobot edge) |

Performa (median 3 repeat, vs referensi; rasio = kita/referensi):

| Kernel | n=512 rasio public | n=512 rasio kernel | Catatan |
|---|---|---|---|
| pagerank | ~120–143x | **~16x** | entrypoint publik menghitung full centrality suite (incl. n×BFS reachability) — biaya jujur API; kernel terisolasi ~16x di atas numpy dense |
| wcc | ~3.5–4.4x | **~1.2x** | kernel terisolasi hampir setara networkx |
| bc | ~6–11x | **~1.8x** | Brandes eksak vs Brandes eksak; n dibatasi ≤256 (O(n·m) dua sisi) |
| lcc / tc | ~0.5–0.7x | — | kita *lebih cepat* dari networkx di n=512 (algoritma adjacency-set langsung) |
| cycles | ~0.2–1x | — | lebih cepat dari `nx.simple_cycles` di dataset kecil |
| cdlp / louvain | property-only di dataset ambigu | — | performa wajar (louvain 138ms @ n=512) |

**Di atas atau di bawah?** Kebenaran: 7/8 — di atas untuk kernel numerik (PR/BC/LCC/TC
presisi 12-desimal tervalidasi silang), satu gap kualitas nyata di CDLP bridge-merge.
Performa kernel terisolasi: setara–2x untuk WCC/BC, ~16x untuk PageRank (Decimal 34-digit
vs float64 numpy — harga determinisme presisi), lebih cepat untuk LCC/TC/cycles.
Bottleneck terbesar bukan kernel melainkan *packaging API*: `compute_centrality` selalu
menghitung seluruh suite → follow-up yang disarankan: entrypoint per-kernel publik.

## 4. Reproduksi

```bash
.venv/bin/python bench/institutional/run.py            # full, exit 0, tulis results/
.venv/bin/python bench/institutional/run.py --quick    # dataset kecil saja
.venv/bin/python bench/institutional/run.py --strict   # exit 1 bila ada kernel FAIL
.venv/bin/python -m pytest tests/test_institutional_bench.py -q   # 12 passed
```

Exit code: 0 = harness selesai + laporan ditulis (FAIL kebenaran tercatat di laporan);
`--strict` mengubah FAIL menjadi exit 1 untuk dipakai operator/CI.
