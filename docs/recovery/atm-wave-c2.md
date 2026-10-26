# ATM Wave C2 — SUB-68: F-C1 analytics/generation_diff, F-C2 analytics/timeline

Tanggal: 2026-10-26 · Pelaksana: SUB-68 · Status: GREEN (54/54 test baru; ruff + format + pyright bersih pada 4 file milik SUB-68)

## Ruang lingkup

Dua modul baru, murni fungsi atas data read-only, tanpa menyentuh file existing (integrasi = pekerjaan orkestrator wave berikutnya):

| Fitur | File | Kanal |
|---|---|---|
| F-C1 | `src/know_your_soul/analytics/generation_diff.py` | ART `DIFF.md` + REP |
| F-C2 | `src/know_your_soul/analytics/timeline.py` | ART `timeline.json`/`TIMELINE.md` + REP |
| Tests | `tests/test_analytics_generation_diff.py`, `tests/test_analytics_timeline.py` | — |

## Keputusan desain

### F-C1 — generation_diff ("apa yang berubah sejak kemarin")

- **Kunci edge kanonik** `(source, target, relation)` — persis bentuk dedup edge di `projection.py` (`_edge`), jadi diff tidak bisa drift dari apa yang benar-benar terpublikasi.
- **Edge berbalik eksplisit, tak terhitung ganda**: pasangan `(u,v,r)` hilang + `(v,u,r)` muncul dilaporkan SEKALI di `edges_reversed` dan dikeluarkan dari `edges_added`/`edges_removed`. Pencocokan one-to-one dalam urutan kanonik. Invarian akuntansi penuh di-pin sebagai property test: `|removed| + |reversed| == |prev-only|` (dan sebaliknya untuk added).
- **Perubahan skor P/M/E per node**: dibaca dari `node["scores"]` (string 12dp read model) sebagai `Decimal`; `delta = after − before` dikuantisasi 12dp ROUND_HALF_EVEN dengan tanda. Tiap entri membawa `version_from`/`version_to` (`node_version`). Perubahan `label` dan `lifecycle_status` juga dilaporkan.
- **Komunitas**: bila caller menyuplai mapping komunitas eksplisit dipakai verbatim (`source: "provided"`); bila tidak, diturunkan dari `community_sig` node (`source: "derived"`); bila tak ada sama sekali → `source: "none"` (jujur, bukan diam-diam kosong). Pairing lintas generasi mendelegasikan ke `communities.remap_communities` (greedy overlap, one-to-one) — satu algoritma remap, tidak diduplikasi. Perpindahan per node dilaporkan terpisah di `community_moves`.
- **Guard path absolut fail-closed** (regex bentuk sama dengan `report_v2`/`health`) atas canonical JSON dict dan atas markdown hasil render → `GenerationDiffError`.
- `render_diff_markdown` menolak dict non-v1.

### F-C2 — timeline (pembelajaran P/M/E dari event log SSOT)

- **Input = record event log** (`{event_seq, event_type, node_id, domain, occurred_at, recorded_at, payload}` = baris SQLite `event_log` yang dibaca caller, `payload` = `payload_json` ter-parse). Modul **tidak pernah membuka store** — loose coupling persis pola SUB-62. Diverifikasi end-to-end dengan probe store riil (in-memory `EventStore`, 1 node + 3 outcome) → seri P/M/E, bin M, domain, sparkline, markdown semuanya keluar benar.
- **Clock = `recorded_at` dari event**, tidak pernah `now()` → artifact reprodusibel dari log saja. Timestamp naive → `TimelineError` (fail-closed). Urutan kanonik = `event_seq` ascending; duplikat seq → error.
- **Skor mengikuti kontrak beku**: tiap titik menghitung ulang evidence terakumulasi node lewat `scoring.score_items` pada `at=recorded_at` — peluruhan (half-life P=90/M=30/E=7 hari) otomatis sesuai kontrak. Konsekuensi kontrak yang di-pin sebagai test: decay uniform membiarkan mean P/M/E invarian tetapi uncertainty tumbuh (`u = 1/√(1+total)`), jadi timeline menampilkan keduanya (`scores` + `uncertainties` per titik).
- **Dedup by context/content**: resep dedup scoring (canonical JSON semua field kecuali `evidence_id`). Replay id yang sama ATAU konten identik dengan id baru → `duplicates_suppressed += 1`, change code `evidence_duplicate`; evidence baru tetap tercatat di `evidence_count` (store memang menyimpan keduanya; dedup hanya menekan bobot skor — jujur terhadap SSOT).
- **Change codes kanonik** (terurut): `evidence_added`, `evidence_duplicate`, `level_up`, `level_down`, `m_bin_changed`, `score_changed`. Titik pertama dibandingkan terhadap baseline zero-evidence (P=M=E=0, level 1, bin null — spec §9) sehingga evidence pertama sudah terbaca sebagai pembelajaran.
- **Seri domain**: mean Decimal atas skor-terukur-terakhir node anggota (urut id; node tanpa evidence dan node retired dikeluarkan), dikuantisasi 12dp; titik domain hanya dipancarkan saat mean atau keanggotaan bergerak. `node.retire` mengeluarkan node dari seri domain dan menandai `retired: true`.
- **Sparkline** (`sparkline`): kuantisasi satu dimensi seri titik ke integer `0..scale` (ROUND_HALF_EVEN), step function (bucket = nilai titik terakhir ≤ tepi kanan bucket; bucket sebelum titik pertama = `null`), window default = timestamp pertama/terakhir seri (override `start`/`end` opsional, ISO). Aritmetika waktu integer mikrodetik — tanpa float.
- Output: dict kanonik (`schema_version: 1`, `contract_version` scoring), `timeline_json` (canonical JSON + newline), `render_timeline_markdown` (TIMELINE.md, menolak skema asing).

## Bukti gate

- `pytest tests/test_analytics_generation_diff.py tests/test_analytics_timeline.py -q` → **54 passed** (termasuk property tests hypothesis: determinisme di bawah shuffle input, anti-simetri diff add/remove + pembalikan reversal, akuntansi penuh tanpa double-count, monotonitas event_seq/at/evidence_count per node, dan invarians decay-uncertainty).
- `ruff check` + `ruff format --check` pada 4 file milik SUB-68 → bersih.
- `pyright` pada 4 file milik SUB-68 → 0 errors, 0 warnings.
- Full suite: **1958 passed** (246s). Satu kegagalan teramati pada run pertama (`test_audit_final_flow.py::test_reader_storm_under_write_stream_bounds_repairs`) adalah flake timing konkurensi pre-existing: test itu PASS standalone (4.96s) dan PASS pada run penuh kedua; modul SUB-68 read-only dan tidak menyentuh store/HTTP.

## Risiko / catatan untuk integrasi (orkestrator wave berikutnya)

1. **Timeline butuh adapter read-only store→records** (SELECT kolom `event_seq, event_type, node_id, domain, occurred_at, recorded_at, payload_json FROM event_log WHERE kind='domain' ORDER BY event_seq`; `payload = json.loads(payload_json)`). Pola persis ini terbukti di probe; modul sengaja tidak memegang koneksi.
2. **Biaya timeline O(evidence × titik)** per node (recompute penuh per titik) — setara `ScoreEngine` yang me-replay per query; cukup untuk korpus saat ini, tapi orkestrator harus sadar ini bukan incremental.
3. **Domain mean = skor terukur terakhir anggota**, bukan re-decay pada waktu titik domain — terdokumentasi di docstring; jangan menafsirkannya sebagai skor live.
4. **Diff membaca dua generasi `graph.json` sebagai mapping**; caller menyuplai generasi previous/current (manifest store). Community remap memakai `community_sig`; generasi tanpa sig menghasilkan `source: "none"` secara jujur.
5. Kapasitas artifact: dict hasil belum di-cap byte; penulis artifact (F-B8) menegakkan cap per-artifact seperti biasa.
