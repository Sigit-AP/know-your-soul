# ATM Mega-Wave 2 — suite algoritme graph dalam (SUB-74)

Tanggal: 2026-10-27. Repo: `/root/know-your-soul` @ `main` (HEAD 86b8d15 saat mulai). TIDAK di-commit (sesuai scope). Semua file BARU; nol file existing disentuh (kecuali 2 baris board `.superpowers/collab/subagent-board.md`).

## Apa yang dibangun

Dua modul analitik deterministik murni yang memperdalam `analytics/` (wave A: centrality/components/communities/cycles/bridges), dengan kontrak identik: input = read model `graph.json` (`nodes`/`links`), output = dict kanonik siap-artifact (`schema_version: 1`, `kind: analytics.*`, `parameters`, `guards`, `dropped_links`), semua skor string fixed-point 12 desimal via `scoring.serialize_score`, aritmetika internal `Decimal` precision 34, iterasi hanya atas list ter-sortir.

| File | API publik | Isi |
|---|---|---|
| `analytics/algorithms.py` (1005 LOC) | `compute_hits`, `compute_eigenvector`, `compute_katz`, `compute_clustering`, `compute_louvain`, `compute_reachability`, `count_motifs`, `compute_assortativity`; error bertipe `AlgorithmError`/`GraphCapExceededError(algorithm, cap, actual)` | HITS (update simultan L1-normalized, iterasi tetap, flag `converged` jujur — graf bipartite/degenerate dilaporkan tidak konvergen, bukan dipalsukan); eigenvector centrality (power iteration L2, `Decimal.sqrt` correctly-rounded); Katz prestige terpotong (`beta·Σ α^k (A^T)^k 1`, guard `katz_truncated`); clustering coefficient per-node + average + transitivity; **Louvain deterministik** (fase local-moving urutan kanonik, gain `>0` strict, tie → key komunitas terkecil, agregasi super-node dengan self-loop `2×internal`, cap level/pass dengan guard jujur); reachability all-pairs (BFS per node, akuntansi pasangan `pairs + unreachable == n(n-1)`, densitas, diameter); motif: triangle counting + **triad census 16 kelas MAN**; assortativity degree (Pearson, guard `assortativity_undefined` saat varians nol). Tiga metrik ranking (HITS/eigen/Katz) punya blok `domains` P1..P7 pada subgraph terinduksi — konsisten dengan `centrality.py`. |
| `analytics/paths.py` (168 LOC) | `find_k_shortest_paths(graph, *, source, target, k=8, max_expansions=65536)`; `PathQueryError(AlgorithmError)` | Yen k-shortest simple paths pada directed collapsed view; uniform-cost search dengan heap key `(cost, path_tuple)` → tie-break leksikografis eksak; budget ekspansi global (guard `max_expansions_reached`); endpoint tak dikenal / source==target → fail-closed bertipe. |

k-core **tidak** diduplikasi — tetap di `components.py` (wave A); suite ini komposisi dengannya via konvensi input/output yang sama (dicatat di docstring modul).

## Keputusan desain

- **HITS update simultan** (bukan alternating): menjamin simetri reversal eksak per-iterasi (`auth(G) == hub(G^T)`) dan konvergensi jujur; alternating sempat dipakai lalu diganti setelah property test menangkap asimetri pada graf dengan eigenvalue degenerate (hub≠auth pada graf simetris — legitim secara matematis tapi menyesatkan). Ini bug-desain nyata yang ketangkap property test.
- **Eigenvector honest-nonconvergence**: path/bintang (bipartite) berosilasi selamanya di bawah power iteration dari start uniform — modul melaporkan `converged: false` + `iterations_used == max_iterations` (dipin oleh test), tidak mengklaim konvergensi palsu.
- **Triad census**: klasifikasi dari predikat struktural first-principles (m/a/n + derajat in/out arc asimetris), bukan tabel tricode networkx; konvensi D/U/C didokumentasikan di docstring `_triad_class`; tiap kelas dipin oleh 2 contoh kanonik (32 kasus) + invariant `sum == C(n,3)` + invariant support-triangle == triangle counter.
- **Louvain**: bobot = 1 per pasangan unik (collapsed view, konsisten dengan clustering); modularitas dilaporkan selalu dihitung ulang dari partisi terhadap graph level-0 (eksak, bukan dari super-graph) sehingga `levels[i].modularity` bisa diaudit.
- **Cap fail-closed**: louvain (default 2048 node), reachability (1024), motifs (256) melempar `GraphCapExceededError` (subclass `AlgorithmError` ⊂ `ValueError`) SEBELUM kerja dimulai; algoritme iteratif dibatasi cap iterasi + flag `converged`.
- **Clean-room**: Graphify tidak punya implementasi sendiri untuk algoritme ini (mereka memakai networkx); semua dari literatur standar (Kleinberg, Bonacich, Katz 1953, Watts-Strogatz, Blondel et al., Wasserman-Faust, Newman, Yen 1971) — dicatat di docstring modul.

## Bukti gate (fresh, saat penulisan)

- `pytest tests/test_analytics_algorithms.py tests/test_analytics_paths.py -q` → **309 passed** (201 + 43 `def test_`, sisanya ekspansi parametrize), exit 0.
- Full suite: **3149 passed, 0 failed** (309s). Catatan: satu run intermediate menunjukkan 36 failed — semuanya file sibling SUB-75/76/77/79 yang sedang mid-write (terbukti: file mereka `M`/`??` di git status; run ulang setelah sibling selesai → hijau penuh; nol kegagalan menyentuh modul/test SUB-74).
- `ruff check src tests` → 0; `ruff format --check` → bersih; `pyright` → **0 errors, 0 warnings** (repo-wide, include src+tests).
- Determinisme byte: `PYTHONHASHSEED ∈ {0,1,42}` × 9 output (8 algoritme + Yen) → sha256 canonical_json identik (`c48a1d45b6c7d527`).
- Property tests (hypothesis, `derandomize=True`): determinisme lintas permutasi input untuk SEMUA fungsi; invariant matematis — clustering ∈ [0,1], Σ auth HITS == 1, ‖eigenvector‖₂ == 1 saat konvergen, assortativity ∈ [-1,1], census == C(n,3), `Σ per_node == 3× triangles`, partisi Louvain = exact cover, modularitas ∈ [-0.5, 1] dan non-turun antar level, akuntansi pasangan reachability, Yen: path simple/valid/non-decreasing cost + rank-0 == jarak BFS oracle independen.

## Angka vs target brief

- src: **1.173 LOC** (target brief ±2.500) — sengaja tidak dipadding: plan §5 butir 6 melarang "analitik kosmetik" dan §7.4 melarang penghitungan ganda; semua algoritme yang diminta brief terimplementasi penuh. Selisih vs estimasi dilaporkan apa adanya.
- test: **309 item** (target 300+) ✓.

## Catatan untuk integrator

- Output siap-artifact: serialkan `canonical_json` + masuk manifest dengan cap per-artifact (pola SUB-70 `exporters/builtin.py`); modul ini tidak menulis apa pun.
- `compute_reachability`/`count_motifs`/`compute_louvain` fail-closed pada graf besar — panggil dengan cap eksplisit bila korpus tumbuh melewati default.
- `compute_hits`/`compute_eigenvector`/`compute_katz` membawa blok `domains` P1..P7 — konsumsi report (F-C3) bisa langsung memotret hub/authority per domain.
