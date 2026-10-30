# SUB-107 — W8: composite graph-analysis report (melampaui Graphify GRAPH_REPORT)

Date: 2026-10-30 (UTC). Repo: `/root/know-your-soul` @ `fa62dc7` (main).
Scope: `src/know_your_soul/report/graph_report.py` (BARU),
`src/know_your_soul/ops/report_cli.py` (BARU), wiring `cli.py`
(subcommand `report`, additif), `report/__init__.py` (re-export additif),
`tests/test_report_graph_report.py` (BARU, 117 test), this recovery doc.
Not touched: kontrak beku (14 knowledge ops tetap 14 — dipin test),
`extraction/` + `tests/test_extraction_*` + `bench/tokens/` (milik sibling
SUB-106), `report_v2.py`, semua permukaan MCP/HTTP. No commits (pool rule).

Catatan konteks: `RESEARCH-QUERY.md` / `RESEARCH-ARCH.md` tidak ada di repo;
basis riset yang dipakai: `docs/research/graphify-feature-matrix.md` +
sumber Graphify ter-pin (`graphifyy==0.9.95`, `analyze.py` dibaca langsung).

## 1. Tabel perbandingan mekanisme vs Graphify + test pembuktinya

| Fitur | Graphify (`analyze.py` 0.9.95) | know-your-soul (`report/graph_report.py`) | Test pembukti |
|---|---|---|---|
| **God nodes** | Ranking **degree saja** (`god_nodes` L112-148: `sorted(G.degree())`); noise-filter (builtin/JSON-key/file-hub) tapi tak ada sinyal struktural lain | Ranking **komposit**: `(degree + pagerank + betweenness)/3`, tiap metrik max-normalized `[0,1]`, `Decimal` presisi 34, 12dp string; tiap entri membawa `rank_by_degree_only` sehingga laporan sendiri mendokumentasikan di mana komposit membalik ranking degree-only | `test_composite_beats_degree_only_on_the_degree_trap` (fixture barbell + degree-trap `x` degree 6: Graphify menaruh `x` #1; komposit menurunkan `x` di bawah pasangan bridge `b`/`e`); `test_star_center_ranks_first`, `test_path_middle_ranks_first`, `test_betweenness_component_decides_degree_ties`, `test_pagerank_component_rewards_endorsement` |
| **Surprising connections** | Skor komposit int sederhana (confidence 1-3 + bonus cross-type/repo/community + peripheral→hub) dengan `why` **string datar** | Skor komposit `find_bridges` (community distance dgn edge kandidat dilepas + relation rarity + evidence novelty) **+ penjelasan terstruktur**: daftar berurutan stabil dari 8 kode alasan (`CROSS_DOMAIN`, `CROSS_COMMUNITY`, `SOLE_COMMUNITY_LINK`, `NO_ALTERNATIVE_PATH`, `UNEXPECTED_PATH`, `DEGREE_ANOMALY`, `RARE_RELATION`, `LOW_CONFIDENCE`) masing-masing dengan `detail`, **+ confidence bucket** (`high`/`medium`/`low`, ambang sama dgn report_v2) + `alternative_path_hops` | `test_sole_link_explanation_codes`, `test_unexpected_path_detail_carries_hops`, `test_degree_anomaly_explanation`, `test_rare_relation_explanation`, `test_low_confidence_explanation[AMBIGUOUS/INFERRED]`, `test_confidence_bucket_{high,low}`, `test_sole_link_outranks_redundant_bridge` |
| **Suggested questions** | 4-7 pertanyaan prosa; field `type`/`question`/`why` saja; **tanpa node id bukti, tanpa query** | 5 template deterministik per pola struktur (`hub_dependency`, `bridge_path`, `isolated_community`, `isolated_node`, `ambiguous_relationship`); tiap pertanyaan membawa **`evidence_node_ids`** + **`query` executable** yang dieksekusi `answer_question()` terhadap read model yang sama — "answerable" adalah properti teruji, bukan harapan; interleave round-robin per tipe (pola `zip_longest` Graphify dipertahankan) | `test_every_generated_question_is_answerable` (meta-test: setiap pertanyaan dari fixture kaya dieksekusi dan isi jawabannya cocok per tipe); `test_hub_dependency_question`, `test_bridge_path_question`, `test_isolated_community_question`, `test_ambiguous_relationship_question`, 17 test `TestAnswerQuestion` |
| **Token benchmark** | Diprint setelah `build` selesai (pola CLI) | Dihitung **saat build** dengan estimator yang sama dgn jalur retrieval (`estimated_tokens` = `ceil(utf8/4)` atas kanonik JSON), **tertanam di JSON kanonik + markdown**, diprint ke stderr oleh CLI. Self-reference markdown diselesaikan dgn fixed-point deterministik (≤8 iterasi); `report_json_tokens` diukur atas laporan tanpa bagian benchmark (sirkularitas dihindari eksplisit). Graf kecil dilaporkan jujur (savings bisa negatif) | `test_markdown_tokens_match_rendering` (fixed-point eksak), `test_report_json_tokens_exclude_the_benchmark_itself`, `test_report_is_smaller_than_raw_graph`, `test_empty_graph_benchmark_is_safe`, `test_benchmark_printed_to_stderr` (CLI) |
| **Output** | `GRAPH_REPORT.md` markdown + `analysis.json` (skema implisit) | Markdown terstruktur (5 seksi terurut, dipin test) **+ JSON kanonik** (`schema_version`, `kind`, `parameters`, canonical_json-safe); CLI `report --format json\|markdown`, `--out` menulis `graph-report.json` + `graph-report.md` atomik idempoten, containment fail-closed | `test_sections_in_order`, `test_out_writes_both_artifacts`, `test_out_is_idempotent`, `test_out_escape_is_usage_error`, `test_output_is_canonical_json_safe` |
| **Determinisme** | `sorted()` + seed 42 utk betweenness; sebagian jalur pakai dict/set ordering | Fungsi murni atas konten graph: urutan input irrelevant (dipin dgn permutasi 5 seed + hypothesis), 12dp `Decimal` di mana-mana, tie-break `node_id` eksplisit | `test_input_permutation_invariance[0,1,2,7,42]`, `test_property_permutation_invariance` (hypothesis), `test_repeated_builds_identical`, `test_ties_break_by_node_id` |
| **Kejujuran output** | (pelajaran report.py: undercount silent) | Guard absolute-path fail-closed (`GraphReportError`) seperti report_v2; graph kosong → seksi `none`; fallback `no_signal` eksplisit | `test_absolute_path_labels_fail_closed`, `test_empty_sections_render_honestly`, `test_no_signal_fallback` |

## 2. Keputusan desain

- **Bobot komposit god node: equal thirds (1,1,1).** Dipilih karena paling
  sederhana untuk dipertahankan; uji degree-trap membuktikan betweenness +
  pagerank cukup untuk menurunkan leaf-collector hub di bawah bridge node
  nyata. Normalisasi per-metrik by max (bukan by `n-1`) agar tiga sumbu
  sebanding; graph degenerate (n<2) → semua nol (dipin
  `test_single_node_graph`).
- **Reuse mesin analitik yang ada**: `_prepare_graph` / `_degree_scores` /
  `_pagerank_scores` / `_betweenness_scores` dari `analytics.centrality`
  (preseden import privat: `analytics.paths`), `find_bridges` dari
  `analytics.bridges`, `find_k_shortest_paths` untuk jawaban `path`,
  `estimated_tokens` dari `retrieval`, `CONFIDENCE_BUCKETS` dari
  `report_v2`. Nol logika scoring duplikat.
- **Answerability sebagai kontrak**: generator hanya memunculkan pertanyaan
  `hub_dependency` untuk god node dengan ≥1 incoming link, sehingga jawaban
  `dependents` dijamin non-empty pada graph yang sama; meta-test
  `test_every_generated_question_is_answerable` menegakkan ini untuk semua
  tipe.
- **CLI adalah OPS surface** (pola `export`/`analytics`): bukan knowledge
  op (dipin `test_report_is_not_a_knowledge_or_http_op`), tak pernah
  menyentuh store (dipin `test_report_does_not_touch_the_store`),
  `NO_ACTIVE_GENERATION` → exit 1, path escape → exit 2.

## 3. Bukti gate

- TDD: RED nyata (collection error `ModuleNotFoundError:
  know_your_soul.report.graph_report`, exit 2) → GREEN 117/117.
- Fokus: `pytest tests/test_report_graph_report.py` → **117 passed, exit 0**
  (4.1s).
- Live CLI E2E (store nyata 20 node/17 edge via `EventStore` +
  `GraphProjector.rebuild()`): exit 0; markdown lengkap 5 seksi; god node
  `HUB_A` composite #1 (0.8468) mengalahkan `HUB_B` via betweenness bridge;
  surprising connection `HUB_A —depends_on→ HUB_B` surprise 0.9804
  confidence high dengan 6 kode penjelasan; 8 pertanyaan ter-interleave;
  benchmark stderr: `report ~998 tokens markdown / ~1427 json vs ~2921 raw
  (savings 0.658336186238, 2.926853707415x smaller)`.
- `ruff check` + `ruff format --check` pada 5 file scope: 0 error.
- `pyright` pada 4 file scope (module, CLI glue, `__init__`, tests):
  0 errors / 0 warnings.
- Full suite: **6474 passed, 5 failed, exit 1 (996s)** — ignore-set 9 file
  test extraction sibling SUB-106 (WIP) + deselect 1 pin kontrak yang
  dilanggar file untracked sibling (`docs/contracts/extraction-contract-v4.md`).
  Kelima failure dibuktikan BUKAN milik SUB-107:
  - `test_serve_protocol::test_sixteen_tools_sorted`,
    `test_acceptance_e2e::test_exactly_fourteen_audited_tools_exposed`,
    `test_acceptance_e2e::test_single_allowlisted_graph_root` — ketiganya
    gagal karena tool MCP ke-17 `knowledge_submit_extraction` (permukaan
    sibling SUB-106); dibuktikan tetap gagal dengan file SUB-107
    di-`git stash` (17 tools tanpa perubahanku; `report` TIDAK ada di
    tool listing — OPS-only).
  - `test_serve_gates` storm + fairness — gate timing di bawah beban full
    suite ("polite agents lost 22.8% > 20%", "daemon closed the
    connection"); keduanya hijau standalone dengan perubahanku aktif
    (storm 2/2, fairness 1/1) dan area serve/ memang sedang dimodifikasi
    sibling (superpower.py/webview.py dirty di working tree).
  - 0 kegagalan menyangkut `report/graph_report.py`, `ops/report_cli.py`,
    atau `cli.py`.

## 4. Residual / catatan

- `report_json_tokens` sengaja mengecualikan bagian benchmark
  (self-reference); didokumentasikan di docstring + dipin test.
- Graph sangat kecil melaporkan savings negatif secara jujur (laporan bisa
  lebih besar dari read model mentah); tidak diklem.
- Betweenness memakai sampler fixed-seed `analytics.centrality`
  (`k = min(32, n)`); untuk n ≤ 32 hasilnya eksak Brandes.
- God-node candidate cap untuk pertanyaan hub: 5 teratas komposit; bridge
  question dari 3 surprising teratas; konstan di-level modul
  (`_HUB_QUESTION_CANDIDATES` dsb.).
