# Institutional graph benchmark — skor

Referensi: `networkx 3.7 (+ self-validated numpy PageRank reference; networkx.pagerank requires scipy which is intentionally not a dependency)` · repeats=3 · quick=False

**7/8 available kernels PASS correctness** (skipped jujur: 2 — kernel belum tersedia)

| Kernel | Suite | Status | Detail |
|---|---|---|---|
| pagerank | LDBC Graphalytics: PR, GAPBS: PR | PASS | epsilon rel<=0.001 abs<=0.0001 vs self-validated numpy reference (L1 residual < 1e-10) |
| wcc | LDBC Graphalytics: WCC, GAPBS: CC | PASS | equivalence (same weak components as networkx) |
| bc | GAPBS: BC | PASS | epsilon rel<=1e-06 abs<=1e-09 vs networkx Brandes (normalized, directed) |
| cdlp | LDBC Graphalytics: CDLP | **FAIL** | exact-cover everywhere + equivalence vs networkx asyn_lpa(seed=0) on unambiguous datasets |
| lcc | LDBC Graphalytics: LCC | PASS | epsilon rel<=1e-06 abs<=1e-09 vs networkx.clustering (undirected) |
| tc | GAPBS: TC | PASS | exact integer equality vs networkx.triangles sum/3 |
| louvain | project-local (GAPBS-adjacent) | PASS | exact-cover + modularity cross-check vs networkx + equivalence on unambiguous datasets |
| cycles | project-local (no LDBC/GAPBS counterpart) | PASS | exact set match vs networkx.simple_cycles (canonical rotation) |
| bfs | LDBC Graphalytics: BFS, GAPBS: BFS | SKIP | kernel belum tersedia: tidak ada entrypoint BFS publik (hanya analytics.centrality._bfs_distances privat) |
| sssp | LDBC Graphalytics: SSSP, GAPBS: SSSP | SKIP | kernel belum tersedia: graph read-model tidak membawa bobot edge; tidak ada SSSP publik |

## pagerank

- ours runs a fixed 64 iterations (no convergence stop); public entrypoint computes the full centrality suite, so ratio_public overstates per-kernel cost — ratio_kernel isolates PageRank.
- fixed-iteration float clone cross-check OK (max_abs_err=1.11e-16, tol=1e-9)

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | 3.97e-13 | 2.350 | 0.793 | 1.418 | 1.7x | 0.6x |
| diamond | 4 | 4 | pass | yes | 4e-13 | 1.517 | 0.592 | 0.373 | 4.1x | 1.6x |
| two_triangles_bridge | 6 | 7 | pass | yes | 4.01e-13 | 2.413 | 1.365 | 0.963 | 2.5x | 1.4x |
| star9_bidir | 9 | 16 | pass | yes | 1.09e-05 | 5.124 | 1.619 | 3.404 | 1.5x | 0.5x |
| directed_cycle6 | 6 | 6 | pass | yes | 3.33e-13 | 2.215 | 1.015 | 0.072 | 30.6x | 14.0x |
| two_cycles_shared_node | 5 | 6 | pass | yes | 1.86e-06 | 2.415 | 0.704 | 2.851 | 0.8x | 0.2x |
| disconnected3 | 6 | 3 | pass | yes | 3.97e-13 | 2.697 | 1.530 | 0.938 | 2.9x | 1.6x |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 17.549 | 4.991 | 0.126 | 139.7x | 39.7x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.88e-13 | 95.642 | 20.322 | 1.669 | 57.3x | 12.2x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 4.97e-13 | 515.562 | 76.927 | 5.326 | 96.8x | 14.4x |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | 5e-13 | 1238.762 | 146.687 | 10.147 | 122.1x | 14.5x |

## wcc

- public entrypoint computes the full connectivity suite (weak+strong+articulation+k-core); ratio_kernel isolates the WCC pass.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.074 | 0.015 | 0.034 | 2.2x | 0.4x |
| diamond | 4 | 4 | pass | yes | — | 0.057 | 0.016 | 0.023 | 2.5x | 0.7x |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.070 | 0.022 | 0.025 | 2.8x | 0.9x |
| star9_bidir | 9 | 16 | pass | yes | — | 0.103 | 0.033 | 0.037 | 2.8x | 0.9x |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.071 | 0.022 | 0.025 | 2.9x | 0.9x |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.059 | 0.019 | 0.021 | 2.8x | 0.9x |
| disconnected3 | 6 | 3 | pass | yes | — | 0.071 | 0.019 | 0.023 | 3.1x | 0.8x |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.300 | 0.107 | 0.119 | 2.5x | 0.9x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 0.921 | 0.278 | 0.238 | 3.9x | 1.2x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 5.550 | 1.621 | 1.876 | 3.0x | 0.9x |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 12.501 | 3.397 | 2.672 | 4.7x | 1.3x |

## bc

- correctness runs with betweenness_samples >= n, forcing exact Brandes (default 32 samples is an estimator); ratio_kernel compares exact Brandes vs exact Brandes.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | 0 | 1.114 | 0.058 | 0.064 | 17.4x | 0.9x |
| diamond | 4 | 4 | pass | yes | 0 | 1.573 | 0.062 | 0.058 | 27.2x | 1.1x |
| two_triangles_bridge | 6 | 7 | pass | yes | 5.55e-17 | 2.391 | 0.106 | 0.084 | 28.4x | 1.3x |
| star9_bidir | 9 | 16 | pass | yes | 0 | 3.809 | 0.274 | 0.206 | 18.5x | 1.3x |
| directed_cycle6 | 6 | 6 | pass | yes | 0 | 2.125 | 0.140 | 0.161 | 13.2x | 0.9x |
| two_cycles_shared_node | 5 | 6 | pass | yes | 1.11e-16 | 1.974 | 0.124 | 0.086 | 23.0x | 1.4x |
| disconnected3 | 6 | 3 | pass | yes | 0 | 1.915 | 0.149 | 0.189 | 10.1x | 0.8x |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 12.231 | 0.718 | 0.468 | 26.2x | 1.5x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.16e-17 | 71.139 | 12.902 | 7.587 | 9.4x | 1.7x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 2.78e-17 | 890.140 | 241.413 | 140.346 | 6.3x | 1.7x |

## cdlp

- LPA partitions are only well-defined on unambiguous structure; ambiguous datasets are scored on the exact-cover property only (Graphalytics itself validates CDLP exactly on fixed inputs).
- known divergence: our canonical synchronous LPA merges two_triangles_bridge into one community while networkx asyn_lpa splits at the bridge (mega-wave-2 Louvain pins the split as intended) — reported as FAIL there, not hidden.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.093 | — | — | — | — |
| diamond | 4 | 4 | pass | yes | — | 0.040 | — | — | — | — |
| two_triangles_bridge | 6 | 7 | fail | yes | — | 0.050 | — | 0.122 | 0.4x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.074 | — | — | — | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.067 | — | — | — | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.098 | — | — | — | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.061 | — | — | — | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.250 | — | 0.298 | 0.8x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 1.014 | — | — | — | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 5.498 | — | — | — | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 10.771 | — | — | — | — |

## lcc

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | 0 | 0.041 | — | 0.060 | 0.7x | — |
| diamond | 4 | 4 | pass | yes | 3.33e-13 | 0.045 | — | 0.058 | 0.8x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | 3.33e-13 | 0.102 | — | 0.077 | 1.3x | — |
| star9_bidir | 9 | 16 | pass | yes | 0 | 0.113 | — | 0.263 | 0.4x | — |
| directed_cycle6 | 6 | 6 | pass | yes | 0 | 0.054 | — | 0.086 | 0.6x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | 3.33e-13 | 0.111 | — | 0.144 | 0.8x | — |
| disconnected3 | 6 | 3 | pass | yes | 0 | 0.095 | — | 0.083 | 1.1x | — |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 0.266 | — | 0.301 | 0.9x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.44e-13 | 0.689 | — | 1.330 | 0.5x | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 4.87e-13 | 3.667 | — | 6.293 | 0.6x | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | 4.87e-13 | 7.344 | — | 13.523 | 0.5x | — |

## tc

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.036 | — | 0.043 | 0.8x | — |
| diamond | 4 | 4 | pass | yes | — | 0.041 | — | 0.034 | 1.2x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.055 | — | 0.046 | 1.2x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.067 | — | 0.051 | 1.3x | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.076 | — | 0.037 | 2.1x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.048 | — | 0.035 | 1.4x | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.042 | — | 0.030 | 1.4x | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.209 | — | 0.186 | 1.1x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 0.612 | — | 0.481 | 1.3x | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 3.480 | — | 2.560 | 1.4x | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 6.933 | — | 9.551 | 0.7x | — |

## louvain

- modularity of OUR partition is recomputed with networkx.community.modularity and compared to ours (12-decimal) at abs<=1e-9.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.078 | — | — | — | — |
| diamond | 4 | 4 | pass | yes | — | 0.130 | — | — | — | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.159 | — | 0.451 | 0.4x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.133 | — | — | — | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.178 | — | — | — | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.152 | — | — | — | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.135 | — | — | — | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.464 | — | 1.503 | 0.3x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 6.192 | — | — | — | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 50.710 | — | — | — | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 138.482 | — | — | — | — |

## cycles

- bounded to small datasets (n<=12): simple-cycle counts explode combinatorially.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.030 | — | 0.113 | 0.3x | — |
| diamond | 4 | 4 | pass | yes | — | 0.030 | — | 0.075 | 0.4x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.046 | — | 0.081 | 0.6x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.079 | — | 1.032 | 0.1x | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.069 | — | 0.323 | 0.2x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.061 | — | 0.372 | 0.2x | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.036 | — | 0.103 | 0.3x | — |
