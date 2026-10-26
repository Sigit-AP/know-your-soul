# ATM Wave A bagian 1 — modul analitik F-A1 / F-A2 / F-A4 (SUB-60)

Tanggal: 2026-10-26. Repo: `/root/know-your-soul` @ `main` (HEAD a7f331a saat mulai). TIDAK di-commit (sesuai scope).

## Apa yang dibangun

Tiga modul analitik deterministik murni di `src/know_your_soul/analytics/`, masing-masing menerima read model (`graph.json` shape: `nodes`/`links`) dan mengembalikan dict kanonik siap-artifact. Integrasi artifact (manifest/validator/CLI) = milik SUB-63, **tidak disentuh**. File existing tidak ada yang diubah oleh SUB-60.

| File | Plan | API publik | Isi |
|---|---|---|---|
| `analytics/centrality.py` | F-A1 | `compute_centrality(graph, *, betweenness_samples=32, pagerank_iterations=64, closeness_cutoff=6, top=None)` | degree (weak, /(n-1)), betweenness Brandes ter-sampling (seed tetap `0x4B5953`, guard `k=min(k,n)` + flag `betweenness_sampled`), harmonic (forward, /(n-1)), closeness terbatas gaya Wasserman-Faust dengan cutoff, PageRank iterasi tetap (damping 0.85, dangling dibagi rata). Global + per-domain P1..P7 (subgraph terinduksi). |
| `analytics/components.py` | F-A2 | `analyze_components(graph, *, evidence_counts=None)` | komponen lemah (BFS terurut), komponen kuat (Tarjan iteratif), titik artikulasi + bridge (low-link DFS iteratif, undirected collapse), k-core (heap `(degree, node_id)`), `knowledge_islands` = node tanpa relasi valid **dan** tanpa evidence (kandidat review, reason `no_evidence_and_no_valid_relations`). |
| `analytics/cycles.py` | F-A4 | `find_cycles(graph, *, relations=("depends_on","contradicts","supersedes"), max_length=6, max_cycles=256, max_expansions=65536)` | enumerasi siklus sederhana ber-anchor (tiap siklus ditemukan sekali dari node terkecil), bound panjang, stop-early deterministik (`max_cycles`/`max_expansions` → `guards`), dedup rotasi kanonik, flag `has_contradiction` untuk temuan kontradiksi melingkar. |

## Keputusan desain

- **Skor 12-desimal**: semua skor lewat `scoring.serialize_score` (quantize `ROUND_HALF_EVEN` 1E-12, pola spec §9); aritmetika internal `Decimal` precision 34 via `localcontext`, tanpa float biner. Derajat/closeness/harmonic/betweenness/PageRank semuanya string `x.xxxxxxxxxxxx`.
- **Determinisme**: iterasi selalu atas list ter-sortir; tie-break `(-score, node_id)`; sampler betweenness = `random.Random(seed-tetap)` atas **node id ter-sortir** (RNG lokal eksplisit, bukan global — sesuai plan §5 butir 2) sehingga permutasi input → output identik (dibuktikan property test hypothesis `derandomize=True` di ketiga modul).
- **Normalisasi bersama**: `_prepare_graph` (di `centrality.py`, dipakai ulang oleh dua modul lain) menganonikkan read model: node id unik ter-sortir, link dedup `(source, relation, target)`, self-loop/endpoint-unknown/row cacat dibuang dan dihitung di `dropped_links`.
- **Clean-room**: tidak ada kode/komentar/nama privat Graphify yang disalin; hanya ide (degree ranking, sampled betweenness + guard k, enumerasi siklus terbatas + rotasi kanonik). Sumber ide dicatat di docstring tiap modul (`analyze.py:112`, `analyze.py:513-522`, `analyze.py:720-834`).
- **Tanpa dependency baru**: stdlib saja (`decimal`, `heapq`, `collections`, `random` ber-seed lokal). `hypothesis` hanya di test.

## Bukti gate

- `pytest tests/test_analytics_{centrality,components,cycles}.py -q` → **46 passed** (18 + 13 + 15), exit 0.
- `ruff check` + `ruff format --check` pada 6 file baru: **0 error**.
- `pyright` pada 6 file baru: **0 errors, 0 warnings**.
- Full suite pada saat gate: 1684 passed, 33 failed — **semua kegagalan di luar scope SUB-60** dan terdiagnosis: (a) 12× `test_no_ai_runtime.py` + 3× `test_wave3_coverage_units.py` + e2e runtime-guard gagal karena `matplotlib`/`pillow` ter-install di venv (ditambahkan sibling SUB-58 ke `dev` deps untuk generator PNG) → runtime guard `forbidden_distributions` fail-closed; (b) sisanya (`test_dependency_policy`, `test_mcp_*`, `test_acceptance_e2e`, `test_audit_final_flow`, `test_wave3_coverage_adapters`) berkaitan edit in-flight sibling SUB-61/62/63 di `cli.py`/`projection.py`/`read_model_validator.py`/`pyproject.toml`. Tidak satu pun file test yang gagal mengimpor modul SUB-60 (diverifikasi grep); modul SUB-60 tidak diimpor kode existing. **Catatan untuk coordinator: konflik matplotlib/pillow vs runtime_guard perlu diselesaikan (venv terpisah untuk generator PNG atau allowlist dev-only).**

## Risiko / catatan untuk integrator (SUB-63)

- Output ketiga fungsi sudah berbentuk dict kanonik (`schema_version: 1`, `kind: analytics.*`, `parameters`, `guards`, `summary`) — tinggal diserialkan `canonical_json` + `\\n` dan masuk manifest dengan cap per-artifact.
- `analyze_components` membutuhkan `evidence_counts` (node_id → int) dari store read-only untuk deteksi pulau; tanpa itu semua node dianggap 0 evidence (pulau = node terisolasi murni).
- Betweenness per-domain memakai parameter sampling yang sama dengan global; domain kecil otomatis tidak ter-sampling (guard hanya saat `k < n`).
