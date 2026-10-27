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
| path3 | 3 | 2 | pass | yes | 3.97e-13 | 1.097 | 0.649 | 0.884 | 1.2x | 0.7x |
| diamond | 4 | 4 | pass | yes | 4e-13 | 1.545 | 0.581 | 0.561 | 2.8x | 1.0x |
| two_triangles_bridge | 6 | 7 | pass | yes | 4.01e-13 | 2.385 | 0.889 | 0.866 | 2.8x | 1.0x |
| star9_bidir | 9 | 16 | pass | yes | 1.09e-05 | 3.868 | 1.409 | 3.111 | 1.2x | 0.5x |
| directed_cycle6 | 6 | 6 | pass | yes | 3.33e-13 | 2.236 | 0.973 | 0.070 | 31.9x | 13.9x |
| two_cycles_shared_node | 5 | 6 | pass | yes | 1.86e-06 | 1.959 | 1.595 | 3.323 | 0.6x | 0.5x |
| disconnected3 | 6 | 3 | pass | yes | 3.97e-13 | 2.008 | 0.829 | 0.541 | 3.7x | 1.5x |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 12.655 | 4.896 | 0.121 | 104.6x | 40.5x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.88e-13 | 54.791 | 15.283 | 1.299 | 42.2x | 11.8x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 4.97e-13 | 427.919 | 69.649 | 4.341 | 98.6x | 16.0x |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | 5e-13 | 1130.856 | 149.680 | 11.222 | 100.8x | 13.3x |

## wcc

- public entrypoint computes the full connectivity suite (weak+strong+articulation+k-core); ratio_kernel isolates the WCC pass.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.058 | 0.015 | 0.039 | 1.5x | 0.4x |
| diamond | 4 | 4 | pass | yes | — | 0.056 | 0.017 | 0.022 | 2.5x | 0.7x |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.080 | 0.027 | 0.024 | 3.3x | 1.1x |
| star9_bidir | 9 | 16 | pass | yes | — | 0.110 | 0.067 | 0.037 | 3.0x | 1.8x |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.065 | 0.021 | 0.024 | 2.6x | 0.9x |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.057 | 0.019 | 0.021 | 2.8x | 0.9x |
| disconnected3 | 6 | 3 | pass | yes | — | 0.071 | 0.019 | 0.022 | 3.2x | 0.9x |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.305 | 0.105 | 0.100 | 3.0x | 1.0x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 1.004 | 0.287 | 0.240 | 4.2x | 1.2x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 4.992 | 1.445 | 1.238 | 4.0x | 1.2x |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 11.523 | 3.164 | 2.665 | 4.3x | 1.2x |

## bc

- correctness runs with betweenness_samples >= n, forcing exact Brandes (default 32 samples is an estimator); ratio_kernel compares exact Brandes vs exact Brandes.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | 0 | 1.080 | 0.057 | 0.065 | 16.7x | 0.9x |
| diamond | 4 | 4 | pass | yes | 0 | 1.510 | 0.072 | 0.057 | 26.4x | 1.3x |
| two_triangles_bridge | 6 | 7 | pass | yes | 5.55e-17 | 2.154 | 0.105 | 0.084 | 25.6x | 1.2x |
| star9_bidir | 9 | 16 | pass | yes | 0 | 4.084 | 0.313 | 0.283 | 14.4x | 1.1x |
| directed_cycle6 | 6 | 6 | pass | yes | 0 | 3.721 | 0.155 | 0.108 | 34.5x | 1.4x |
| two_cycles_shared_node | 5 | 6 | pass | yes | 1.11e-16 | 1.849 | 0.106 | 0.084 | 22.0x | 1.3x |
| disconnected3 | 6 | 3 | pass | yes | 0 | 1.957 | 0.081 | 0.108 | 18.2x | 0.7x |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 10.852 | 0.574 | 0.365 | 29.7x | 1.6x |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.16e-17 | 65.032 | 13.100 | 7.206 | 9.0x | 1.8x |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 2.78e-17 | 864.647 | 324.305 | 158.503 | 5.5x | 2.0x |

## cdlp

- LPA partitions are only well-defined on unambiguous structure; ambiguous datasets are scored on the exact-cover property only (Graphalytics itself validates CDLP exactly on fixed inputs).
- CDLP variant: canonical-order asynchronous LPA with hop attenuation (delta=0.25, HANP-style) + self-vote + leaf-follow rule, fully deterministic. The previous plain LPA merged two_triangles_bridge into one community; the attenuated variant splits at the bridge, matching networkx asyn_lpa(seed=0) and the Louvain-pinned partition (Q=5/14).

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.086 | — | — | — | — |
| diamond | 4 | 4 | pass | yes | — | 0.068 | — | — | — | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.116 | — | 0.241 | 0.5x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.178 | — | — | — | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.153 | — | — | — | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.136 | — | — | — | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.104 | — | — | — | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.700 | — | 0.652 | 1.1x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 2.241 | — | — | — | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 17.769 | — | — | — | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 49.619 | — | — | — | — |

## lcc

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | 0 | 0.041 | — | 0.100 | 0.4x | — |
| diamond | 4 | 4 | pass | yes | 3.33e-13 | 0.045 | — | 0.058 | 0.8x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | 3.33e-13 | 0.114 | — | 0.129 | 0.9x | — |
| star9_bidir | 9 | 16 | pass | yes | 0 | 0.088 | — | 0.126 | 0.7x | — |
| directed_cycle6 | 6 | 6 | pass | yes | 0 | 0.067 | — | 0.072 | 0.9x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | 3.33e-13 | 0.052 | — | 0.067 | 0.8x | — |
| disconnected3 | 6 | 3 | pass | yes | 0 | 0.045 | — | 0.090 | 0.5x | — |
| cliques_4x5 | 20 | 80 | pass | yes | 0 | 0.222 | — | 0.655 | 0.3x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | 4.44e-13 | 1.630 | — | 1.777 | 0.9x | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | 4.87e-13 | 6.076 | — | 9.651 | 0.6x | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | 4.87e-13 | 10.704 | — | 27.630 | 0.4x | — |

## tc

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.070 | — | 0.076 | 0.9x | — |
| diamond | 4 | 4 | pass | yes | — | 0.042 | — | 0.047 | 0.9x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.121 | — | 0.090 | 1.3x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.144 | — | 0.121 | 1.2x | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.104 | — | 0.059 | 1.8x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.103 | — | 0.035 | 2.9x | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.054 | — | 0.084 | 0.6x | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.480 | — | 0.153 | 3.1x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 0.713 | — | 0.760 | 0.9x | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 5.960 | — | 5.936 | 1.0x | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 15.983 | — | 8.243 | 1.9x | — |

## louvain

- modularity of OUR partition is recomputed with networkx.community.modularity and compared to ours (12-decimal) at abs<=1e-9.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.079 | — | — | — | — |
| diamond | 4 | 4 | pass | yes | — | 0.127 | — | — | — | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.277 | — | 0.500 | 0.6x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.167 | — | — | — | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.191 | — | — | — | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.139 | — | — | — | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.134 | — | — | — | — |
| cliques_4x5 | 20 | 80 | pass | yes | — | 0.511 | — | 1.064 | 0.5x | — |
| erdos_n64_m192_s11 | 64 | 192 | pass | yes | — | 6.422 | — | — | — | — |
| erdos_n256_m1024_s12 | 256 | 1024 | pass | yes | — | 87.492 | — | — | — | — |
| erdos_n512_m2048_s13 | 512 | 2048 | pass | yes | — | 188.759 | — | — | — | — |

## cycles

- bounded to small datasets (n<=12): simple-cycle counts explode combinatorially.

| Dataset | n | m | Correctness | Deterministic | Max abs err | Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |
|---|---|---|---|---|---|---|---|---|---|---|
| path3 | 3 | 2 | pass | yes | — | 0.063 | — | 0.220 | 0.3x | — |
| diamond | 4 | 4 | pass | yes | — | 0.029 | — | 0.073 | 0.4x | — |
| two_triangles_bridge | 6 | 7 | pass | yes | — | 0.046 | — | 0.079 | 0.6x | — |
| star9_bidir | 9 | 16 | pass | yes | — | 0.082 | — | 0.489 | 0.2x | — |
| directed_cycle6 | 6 | 6 | pass | yes | — | 0.050 | — | 0.224 | 0.2x | — |
| two_cycles_shared_node | 5 | 6 | pass | yes | — | 0.045 | — | 0.532 | 0.1x | — |
| disconnected3 | 6 | 3 | pass | yes | — | 0.059 | — | 0.180 | 0.3x | — |
