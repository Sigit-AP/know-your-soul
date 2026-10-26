# ATM Wave C — SUB-62: F-C3 report_v2, F-C4 analytics/health, F-C5 analytics/query_trace

Tanggal: 2026-10-26 · Pelaksana: SUB-62 · Status: GREEN (38/38 test baru; ruff + pyright bersih pada file milik SUB-62)

## Ruang lingkup

Tiga modul baru, murni fungsi atas data read-model, tanpa menyentuh file existing (integrasi = SUB-63):

| Fitur | File | Kanal |
|---|---|---|
| F-C3 | `src/know_your_soul/report/__init__.py`, `src/know_your_soul/report/report_v2.py` | REP + ART |
| F-C4 | `src/know_your_soul/analytics/health.py` | ART + REV |
| F-C5 | `src/know_your_soul/analytics/query_trace.py` | ART + OPS |
| Tests | `tests/test_report_v2.py`, `tests/test_analytics_health.py`, `tests/test_query_trace.py` | — |

## Keputusan desain

### F-C3 — report_v2 (kejujuran metrik)

- **Satu predikat** `is_real_node` (dict + id string non-kosong + label non-blank) dipakai untuk SEMUA hitungan: total, per-domain, distribusi confidence, thin/shown komunitas. Invarian dipin sebagai test: `thin + shown == total komunitas` dan `thin_nodes + shown_nodes + unassigned_nodes == nodes_real` (pelajaran bug Graphify #3548: dua definisi "real node" → thin+shown undercount).
- Distribusi confidence: edge per label (EXTRACTED/INFERRED/AMBIGUOUS) hanya atas edge yang KEDUA endpoint-nya real; node per bucket (high ≥ 0.7, medium ≥ 0.4, low).
- Ringkasan per-domain: mean P/M/E/U dalam Decimal prec 34, kuantisasi 12dp ROUND_HALF_EVEN (string fixed-point; tidak ada float bocor).
- Staleness: `source_knowledge_high_watermark` manifest vs watermark store yang dibaca caller (read-only); `lag != 0` → stale (manifest di depan store = stale juga, pola "lying manifest" F-2). Tanpa watermark → `{"known": false}`.
- Review signals diterima sebagai mapping polos (`code`/`severity`/`subject`) — **loose coupling**: tidak ada import ke health/review_signals; urut (severity, code, subject), cap top-N dengan hitungan `omitted` eksplisit.
- **Tanpa path absolut di output**: guard `_ABS_PATH_RE` (bentuk sama dengan validator) atas canonical JSON dict dan atas markdown hasil render; pelanggaran → `ReportIntegrityError` (fail-closed).
- `render_markdown` menolak dict yang bukan schema v2.

### F-C4 — analytics/health (audit-style)

- Temuan: `junk_node` (critical, alasan dari `ReadModelValidator._junk_node_reasons` — satu classifier dengan publish gate, tidak bisa drift), `dangling_arc` (critical), `duplicate_label` (warning, normalisasi casefold+whitespace), `evidence_without_provenance` (warning, provenance list kosong pada node real), `contradiction_without_resolution` (warning; ter-resolve bila ada arc `supersedes` yang menarget salah satu endpoint), `empty_community` (warning; dari mapping eksplisit atau turunan `community_sig`, nol anggota real).
- Skor: 1 − Σ deduksi (critical 0.25, warning 0.05, info 0.01), floor 0, string 12dp. `ok == False` bila ada critical (fail-closed untuk temuan berat).
- Evidence per temuan di-cap (default 5) dengan `omitted` eksplisit; temuan terurut (severity, code, evidence) → byte-stabil terhadap urutan input.

### F-C5 — analytics/query_trace (observability retrieval)

- `build_query_trace`: input = purpose/context_tags/domain + route outcome + kandidat + kuota + traversal + rejection (semua mapping primitif; `service.py` tidak berubah). Term diekstrak via `query_terms` (termasuk fold plural, mis. `bayes`→`baye`); IDF dihitung dari `LexicalIndex` yang diserahkan caller (tanpa index → `idf: null`, bukan dilarang).
- Kandidat terurut `(-score, -field_rank, node_id)` — persis ordering `LexicalIndex.rank`; semua angka string 12dp; dict siap-artifact (canonical JSON, tanpa timestamp).
- `aggregate_traces`: metrik kualitas matcher — `no_match_rate`, `ambiguous_rate`, `mean_terms`, `mean_candidates`, `by_status`, `rejection_reasons`; nol-aman pada himpunan kosong.

## Bukti gate

- `pytest tests/test_report_v2.py tests/test_analytics_health.py tests/test_query_trace.py -q` → **38 passed**.
- `ruff check` + `ruff format --check` pada 7 file milik SUB-62 → bersih.
- `pyright` pada file milik SUB-62 → 0 errors, 0 warnings.
- Full suite: 1660 passed / 40 failed — **kegagalan pre-existing, terbukti bukan milik SUB-62**: semua berakar pada distribusi `matplotlib`/`pillow` yang terinstal di venv (kontaminasi lingkungan dari pekerjaan visual-assets HEAD a7f331a) yang memicu `runtime_guard.forbidden_distributions`. Diverifikasi dengan meminggirkan sementara semua file SUB-62 dan mengulang test gagal (`test_mcp_server.py::test_capabilities_are_exactly_tools_and_resources`, `test_dependency_policy.py`) → tetap FAILED tanpa file SUB-62. Collection error `test_analytics_bridges.py`/`test_analytics_review_signals.py` = milik agen wave-A lain (modul belum ada di tree bersama).

## Risiko / catatan untuk SUB-63 (integrasi)

1. `report_v2` membaca `graph.json`+manifest sebagai mapping; caller menyuplai `store.knowledge_high_watermark()` (read-only) — modul tidak pernah membuka SQLite sendiri.
2. `health` menginstansiasi `ReadModelValidator()` sekali (module-level) demi classifier junk yang identik dengan publish gate.
3. Kapasitas artifact: dict hasil belum di-cap byte; orkestrator (F-B8) yang menulis artifact wajib menegakkan cap per-artifact.
4. Kegagalan gate lingkungan (matplotlib/pillow) harus diselesaikan di luar scope SUB-62 (pemilik visual-assets mengisolasi deps atau guard diperbarui oleh operator).
