# SUB-79 — Perbaikan kualitas CDLP/LPA (kernel cdlp benchmark institusional)

Tanggal: 2026-10-27 · Pelaksana: SUB-79 · Scope: `src/know_your_soul/analytics/communities.py`, `tests/test_analytics_communities.py`, `docs/recovery/`

## Milestone board

- [08:05 UTC] SUB-79 | cdlp fix | RED-CONFIRMED | benchmark institusional gagal direproduksi di luar harness: `two_triangles_bridge` ours=1 komunitas vs networkx asyn_lpa(seed=0) `{a,b,c}|{d,e,f}`; `cliques_4x5` sudah equivalen | bukti: probe `run.build_datasets()` + `nx.community.asyn_lpa_communities(seed=0)`
- [08:05 UTC] SUB-79 | cdlp fix | DESIGN | kandidat diuji empiris via prototipe scratch pada 11 dataset benchmark: LPA async kanonik + self-vote (bobot 1 pada hop sendiri) + hop attenuation δ (kontribusi tetangga `w·(1−δ·hop)`, gaya HANP/Leung et al. 2009). Hasil: δ∈{0.1,0.25,0.4} + self-vote → exact-cover di semua dataset, EQUIV=True pada kedua dataset `unambiguous-partition`, konvergen ≤5 iterasi; tanpa self-vote → bridge tetap menyatu (EQUIV=False) + non-konvergensi di cycle/cliques. Kedua mekanisme load-bearing. Default dipilih δ=0.25 (titik tengah rentang stabil (0, 0.5)) | bukti: scratch/lpa_variants.py run, semua dataset `all ok: True`
- [08:10 UTC] SUB-79 | cdlp fix | RED | 7 test baru (bridge split, K4-K4 bridge, δ=0 merge pins mekanisme, validasi δ, pelaporan δ, konvergensi cycle, property exact-cover) gagal pada kode lama: ImportError DEFAULT_HOP_ATTENUATION | bukti: pytest tests/test_analytics_communities.py → collection error
- [08:20 UTC] SUB-79 | cdlp fix | ITERATION | self-vote murni memecah bridge TAPI membekukan leaf (path3 terbelah {a,b}|{c} — regresi vs referensi networkx yang merge di semua seed 0..9). Bukti mengarah ke leaf-rule: node degree-1 tidak punya pilihan komunitas → ikuti satu-satunya tetangga (hop+1). Prototipe ulang: semua dataset PASS + path3 merge + K4-K4 split + δ=0 tetap merge | bukti: scratch/lpa_variants.py v2, nx asyn_lpa path3 seeds 0..9 = merge
- [08:25 UTC] SUB-79 | cdlp fix | GREEN-UNIT | 42/42 tests/test_analytics_communities.py hijau (35 lama tanpa diubah + 7 baru); full suite 3149 passed; ruff check+format bersih; pyright 0 errors 0 warnings | bukti: pytest/ruff/pyright runs
- [08:31 UTC] SUB-79 | cdlp fix | GREEN-BENCH | `bench/institutional/run.py --repeats 3 --strict` → **8/8 available kernels PASS** (pagerank, wcc, bc, **cdlp**, lcc, tc, louvain, cycles; bfs+sssp SKIP jujur), exit 0; cdlp: two_triangles_bridge `pass` + EQUIV vs networkx asyn_lpa(seed=0), cliques_4x5 `pass`, semua dataset exact-cover + deterministic=yes | bukti: bench/institutional/results/institutional-latest.{md,json} digenerate ulang
- [08:31 UTC] SUB-79 | cdlp fix | DONE | catatan usang "known divergence ... FAIL" di `_kernel_cdlp` (run.py) diperbarui agar laporan yang digenerate tidak memuat klaim salah; TIDAK ada commit (aturan 2) | bukti: git status — hanya communities.py, test, run.py (note string), results/, docs/recovery/sub79-*

## Ringkasan akhir

**Masalah**: LPA sinkron/polos menggabung `two_triangles_bridge` menjadi 1 komunitas; referensi networkx asyn_lpa(seed=0) dan Louvain (Q=5/14) memisah di bridge → kernel cdlp FAIL (7/8).

**Perbaikan** (tetap label propagation, BUKAN Louvain; deterministik penuh, tanpa random/deps baru):
1. **Hop attenuation** (HANP/Leung et al. 2009): vote tetangga = `w·(1−δ·hop)`, δ default 0.25 (titik tengah rentang stabil (0, 0.5)). Label yang baru diadopsi tak bisa langsung mengalahkan label asli node → label tak menyeberangi bridge.
2. **Self-vote**: label node ikut voting untuk dirinya (bobot 1 pada hop sendiri) → anchor yang mencegah rotasi/oscilasi label pada mode async (tanpa ini: bridge tetap menyatu + non-konvergensi di cycle/cliques — terbukti di prototipe).
3. **Leaf-follow**: node degree-1 mengikuti satu-satunya tetangga — mencegah self-vote membekukan pendant chain sebagai singleton (path3 tetap merge, sama seperti referensi di semua seed 0–9).

Hop adoption = min-hop kontributor + 1; keep mempertahankan hop; tie → label terkecil; konvergensi jujur (`converged`, `iterations_used`); `hop_attenuation` dilaporkan di output; validasi `ValueError` di luar [0, 1]. API publik hanya **ditambah** keyword-arg `hop_attenuation` (default memperbaiki perilaku) + konstanta `DEFAULT_HOP_ATTENUATION` — pemanggil lama (exporters, generation_diff, review_signals, benchmark) tak berubah.

**Verifikasi**: 7 test baru (RED→GREEN) + 35 test lama hijau tanpa diubah; full suite 3149 passed; ruff + format + pyright bersih; benchmark institusional 8/8 PASS `--strict` exit 0.
