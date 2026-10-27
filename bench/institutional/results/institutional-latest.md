# Institutional graph benchmark — skor

Referensi: `networkx 3.7 (+ self-validated numpy PageRank reference; networkx.pagerank requires scipy which is intentionally not a dependency)` · repeats=3 · quick=False

**8/8 available kernels PASS correctness** (skipped jujur: 2 — kernel belum tersedia)

| Kernel | Suite | Status | Detail |
|---|---|---|---|
| pagerank | LDBC Graphalytics: PR, GAPBS: PR | PASS | epsilon rel<=0.001 abs<=0.0001 vs self-validated numpy reference (L1 residual < 1e-10) |
| wcc | LDBC Graphalytics: WCC, GAPBS: CC | PASS | equivalence (same weak components as networkx) |
| bc | GAPBS: BC | PASS | epsilon rel<=1e-06 abs<=1e-09 vs networkx Brandes (normalized, directed) |
| cdlp | LDBC Graphalytics: CDLP | PASS | exact-cover everywhere + equivalence vs networkx asyn_lpa(seed=0) on unambiguous datasets |
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
| path3 | 3 | 2 | pass | yes | 3.97e-13 | 2.449 | 0.894 | 4.103 | 0.6x | 0.2x |
| diamond | 4 | 4 | pass | yes | 4e-13 | 3.889 | 1.750 | 0.491 | 7.9x | 3.6x |
| two_triangles_bridge | 6 | 7 | pass | yes | 4.01e-13 | 5.193 | 0.936 | 0.898 | 5.8x | 1.0x |
| star9_bidir | 9 | 16 | pass | yes | 1.09e-05 | 4.002 | 1.428 | 3.199 | 1.3x | 0.4x |
| directed_cycle6 | 6 | 6 | pass | yes | 3.33e-13 | 2.240 | 0.828 | 0.072 | 30.9x | 11.4x |
| two_cycles_shared_node | 5 | 6 | pass | yes | 1.86e-06 | 2.150 | 0.769 | 3.380 | 0.6x | 0.2x |
| disconnected3 | 6 | 3 | pass | yes | 3.97e-13 | 2.132 | 1.327 | 0.636 | 3.4x | 2.1x |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 15.613 | 4.851 | 0.159 | 98.1x | 30.5x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.88e-13 | 54.385 | 15.964 | 1.282 | 42.4x | 12.5x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 4.97e-13 | 416.268 | 72.044 | 4.595 | 90.6x | 15.7x |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | 5e-13 | 1268.507 | 212.983 | 10.883 | 116.6x | 19.6x |

## wcc

- public entrypoint computes the full connectivity suite (weak+strong+articulation+k-core); ratio_kernel isolates the WCC pass.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.089 | 0.026 | 0.058 | 1.5x | 0.5x |
| diamond | 4 | 4 | pass | yes | — | 0.123 | 0.016 | 0.044 | 2.8x | 0.4x |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.123 | 0.021 | 0.026 | 4.7x | 0.8x |
| star9_bidir | 9 | 16 | pass | yes | — | 0.107 | 0.033 | 0.038 | 2.8x | 0.9x |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.109 | 0.060 | 0.060 | 1.8x | 1.0x |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.116 | 0.032 | 0.023 | 5.2x | 1.4x |
| disconnected3 | 6 | 3 | pass | yes | — | 0.082 | 0.040 | 0.035 | 2.3x | 1.1x |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.445 | 0.216 | 0.103 | 4.3x | 2.1x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 0.977 | 0.292 | 0.308 | 3.2x | 0.9x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 12.983 | 1.379 | 1.259 | 10.3x | 1.1x |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 12.439 | 3.318 | 3.011 | 4.1x | 1.1x |

## bc

- correctness runs with betweenness_samples >= n, forcing exact Brandes (default 32 samples is an estimator); ratio_kernel compares exact Brandes vs exact Brandes.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | 0 | 1.060 | 0.072 | 0.066 | 16.1x | 1.1x |
| diamond | 4 | 4 | pass | yes | 0 | 1.517 | 0.062 | 0.104 | 14.5x | 0.6x |
| two_triangles_bridge | 6 | 7 | pass | yes | 5.55e-17 | 2.195 | 0.107 | 0.084 | 26.1x | 1.3x |
| star9_bidir | 9 | 16 | pass | yes | 0 | 4.176 | 0.379 | 0.269 | 15.5x | 1.4x |
| directed_cycle6 | 6 | 6 | pass | yes | 0 | 2.226 | 0.136 | 0.102 | 21.8x | 1.3x |
| two_cycles_shared_node | 5 | 6 | pass | yes | 1.11e-16 | 1.900 | 0.107 | 0.113 | 16.9x | 1.0x |
| disconnected3 | 6 | 3 | pass | yes | 0 | 2.142 | 0.081 | 0.068 | 31.6x | 1.2x |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 13.140 | 0.512 | 0.509 | 25.8x | 1.0x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.16e-17 | 71.413 | 20.561 | 15.034 | 4.7x | 1.4x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 2.78e-17 | 1026.365 | 355.305 | 159.515 | 6.4x | 2.2x |

## cdlp

- LPA partitions are only well-defined on unambiguous structure; ambiguous datasets are scored on the exact-cover property only (Graphalytics itself validates CDLP exactly on fixed inputs).
- CDLP variant: canonical-order asynchronous LPA with hop attenuation (delta=0.25, HANP-style) + self-vote + leaf-follow rule, fully deterministic. The previous plain LPA merged two_triangles_bridge into one community; the attenuated variant splits at the bridge, matching networkx asyn_lpa(seed=0) and the Louvain-pinned partition (Q=5/14).

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.056 | — | — | — | — |
| diamond | 4 | 4 | pass | yes | — | 0.050 | — | — | — | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.086 | — | 0.165 | 0.5x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.079 | — | — | — | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.146 | — | — | — | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.072 | — | — | — | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.065 | — | — | — | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.400 | — | 0.572 | 0.7x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 1.736 | — | — | — | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 16.343 | — | — | — | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 77.243 | — | — | — | — |

## lcc

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | 0 | 0.041 | — | 0.062 | 0.7x | — |
| diamond | 4 | 4 | pass | yes | 3.33e-13 | 0.045 | — | 0.060 | 0.7x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | 3.33e-13 | 0.070 | — | 0.078 | 0.9x | — |
| star9_bidir | 9 | 16 | pass | yes | 0 | 0.070 | — | 0.114 | 0.6x | — |
| directed_cycle6 | 6 | 6 | pass | yes | 0 | 0.053 | — | 0.071 | 0.7x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | 3.33e-13 | 0.052 | — | 0.069 | 0.8x | — |
| disconnected3 | 6 | 3 | pass | yes | 0 | 0.045 | — | 0.063 | 0.7x | — |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 0.215 | — | 0.306 | 0.7x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.44e-13 | 0.709 | — | 1.197 | 0.6x | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 4.87e-13 | 3.892 | — | 6.725 | 0.6x | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | 4.87e-13 | 9.808 | — | 13.731 | 0.7x | — |

## tc

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.040 | — | 0.045 | 0.9x | — |
| diamond | 4 | 4 | pass | yes | — | 0.041 | — | 0.035 | 1.2x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.055 | — | 0.040 | 1.3x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.069 | — | 0.052 | 1.3x | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.058 | — | 0.037 | 1.6x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.049 | — | 0.035 | 1.4x | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.042 | — | 0.030 | 1.4x | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.207 | — | 0.153 | 1.3x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 0.628 | — | 0.473 | 1.3x | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 3.267 | — | 3.551 | 0.9x | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 7.801 | — | 5.334 | 1.5x | — |

## louvain

- modularity of OUR partition is recomputed with networkx.community.modularity and compared to ours (12-decimal) at abs<=1e-9.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.078 | — | — | — | — |
| diamond | 4 | 4 | pass | yes | — | 0.152 | — | — | — | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.179 | — | 0.433 | 0.4x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.132 | — | — | — | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.170 | — | — | — | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.142 | — | — | — | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.163 | — | — | — | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.423 | — | 1.024 | 0.4x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 5.785 | — | — | — | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 48.669 | — | — | — | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 150.151 | — | — | — | — |

## cycles

- bounded to small datasets (n<=12): simple-cycle counts explode combinatorially.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.030 | — | 0.094 | 0.3x | — |
| diamond | 4 | 4 | pass | yes | — | 0.028 | — | 0.075 | 0.4x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.045 | — | 0.088 | 0.5x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.081 | — | 1.087 | 0.1x | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.049 | — | 0.319 | 0.2x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.049 | — | 0.263 | 0.2x | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.031 | — | 0.072 | 0.4x | — |
