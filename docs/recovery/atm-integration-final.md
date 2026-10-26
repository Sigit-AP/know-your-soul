# ATM Integrasi Final — SUB-70: builtin registry + CLI export/analytics penuh

Tanggal: 2026-10-26 · Pelaksana: SUB-70 (integrator) · Basis: HEAD `65bb301` · TIDAK di-commit (aturan multi-agent).

## Ruang lingkup (sesuai brief)

| File | Perubahan |
|---|---|
| `src/know_your_soul/exporters/builtin.py` | **BARU** — `ExportContext`, `gather_context` (adapter store read-only), `build_registry` (30 artifact), `resolve_selection` (alias CLI), `FORMAT_ALIASES`/`ANALYTICS_ALIASES`/`BOOK_PAGES` |
| `src/know_your_soul/ops/export_cli.py` | Wiring penuh: registry = `builtin.build_registry(gather_context(...))`; seleksi via alias/`all`; error konteks → exit 1 typed |
| `src/know_your_soul/cli.py` | Help text `--formats`/`--modules` (alias + `all`); tidak ada logika baru |
| `src/know_your_soul/exporters/book.py` | **Additif**: parameter `include_domains` (halaman domain kosong tetap diterbitkan → page set tetap 8 nama) |
| `src/know_your_soul/exporters/tabular.py` | **Fix integrasi nyata**: `_score` menormalisasi skor read-model (`0E-12`) ke fixed-point 12dp via `scoring.serialize_score`; kolom `confidence` memakai `scores.confidence` numerik (top-level `confidence` di read model adalah LABEL EXTRACTED/INFERRED/AMBIGUOUS, bukan skor) — tanpa fix ini `nodes.csv` gagal round-trip pada read model produksi |
| `tests/test_builtin_integration.py` | **BARU** — 13 test integrasi |
| `tests/test_export_cli.py` | `default_registry()` → `builtin.build_registry()` untuk perbandingan nama (keputusan integrasi yang memang didelegasikan SUB-63, `atm-wave-integration.md` §2) |
| `docs/operations.md` | Seksi "Export and analytics artifacts (ATM)" + "Generation retention" |
| `docs/verification.md` | Update SUB-70 + bukti artifact |

TIDAK disentuh: `ops/retention.py` + `license_guard` (SUB-69), kontrak beku 14 operasi, `projection.py` (lihat keputusan di bawah), `orchestrator.py` (kontrak registry beku sejak SUB-63 terbukti cukup).

## Registry: 30 artifact, semuanya nyata

**Format (16):** `exports/node-index.json`, `exports/graph.dot`, `exports/graph.mmd`, `exports/nodes.csv`, `exports/edges.csv`, `exports/graph.graphml`, `exports/graph.svg`, `exports/graph.html` (posisi dari `viz.layout`, seed pinned — catatan integrator SUB-67 dipenuhi), `book/index.md` + `book/P1.md..P7.md` (page set tetap; halaman komunitas tetap tersedia via API `exporters.book` langsung).

**Analytics (14):** `analytics/domain-rollup.json`, `analytics/centrality.json`, `analytics/components.json`, `analytics/cycles.json`, `analytics/communities.json`, `analytics/bridges.json`, `analytics/review-signals.json`, `analytics/health.json`, `analytics/report-v2.json`, `analytics/graph-report-v2.md`, `analytics/generation-diff.json`, `analytics/DIFF.md`, `analytics/timeline.json`, `analytics/TIMELINE.md`.

Setiap entry: cap byte eksplisit + round-trip validator (JSON kanonik byte-exact untuk `.json`; parser struktural milik format untuk dot/mmd/csv/graphml/svg/html; penanda header + marker untuk markdown; index-marker + link index untuk halaman book). Ketergantungan antar modul dirangkai di dalam producer (bridges memakai membership hasil `detect_communities`; health memakai sig→members yang sama; report-v2 memakai output review-signals yang dipetakan ke bentuk `code/severity/subject`).

## Adapter konteks (read-only)

`gather_context(store, generation_dir=...)` — semuanya `SELECT` di dalam `store.read()`:

- **Timeline** (pola persis `atm-wave-c2.md`): `SELECT event_seq, event_type, node_id, domain, occurred_at, recorded_at, payload_json FROM event_log WHERE kind='domain' ORDER BY event_seq`, `payload = json.loads(payload_json)`.
- **components/book**: `SELECT node_id, COUNT(*) FROM evidence GROUP BY node_id`.
- **review-signals**: `SELECT source_version_id FROM source_version WHERE source_state='CURRENT'` (`None` = tidak diketahui → sinyal stale-evidence mati secara jujur).
- **report-v2**: manifest generasi aktif + `store.knowledge_high_watermark()`.
- **generation-diff**: generasi "previous" = generasi lain di `<output_root>/generations/` dengan `(watermark, nama-hash)` maksimal — deterministik; **bila tidak ada (generasi pertama / retention GC) → hasil sah `status: "no_previous_generation"`**, DIFF.md memuat penjelasan, bukan error.

## Keputusan: projection.py TIDAK diubah (CLI adalah jalur produksi)

`build_stack` tetap tidak menyuntik registry ke `GraphProjector` (fitur opt-in konstruktor dari SUB-63 tetap ada). Alasan keras, bukan kemalasan lingkup: **artifact konteks-dependen (timeline, generation-diff) adalah fungsi dari seluruh event log dan dari keberadaan generasi lain di disk.** Bila mereka masuk identitas content-addressed generasi, maka (a) hash generasi berubah ketika retention meng-GC generasi lama (diff kehilangan basis), dan (b) setiap rebuild membayar replay timeline penuh. Artifact ATM = turunan on-demand dari generasi aktif + store read-only; validator round-trip berjalan di `produce_exports` sebelum satu byte pun ditulis (fail-closed). Keputusan ini dicatat eksplisit karena `atm-wave-integration.md` §2 mendelegasikannya ke koordinator wave.

## Bukti gate (fresh, host ini, 2026-10-26 ~21:2x–21:5x UTC)

- RED teramati: `pytest tests/test_builtin_integration.py` → collection error (`ModuleNotFoundError: exporters.builtin`).
- GREEN: 13/13 test baru passed; `test_export_cli` (10) + `test_exporters_tabular` + `test_exporters_book` tetap hijau (24 passed gabungan saat gate awal).
- **Full suite: 2025 passed, 1 warning, exit 0** (251.62s) — termasuk file test SUB-69 yang untracked.
- `ruff check src/ tests/`: **All checks passed!** · `ruff format --check src/ tests/`: 147 files already formatted · `pyright src tests`: **0 errors, 0 warnings**.
- `bench/run.py check`: **OK** (cases schema, frozen corpus integrity, estimator parity).
- Baseline HEAD sebelum perubahan: 1958 passed (suite yang sama minus test SUB-69/SUB-70).

## Verifikasi end-to-end NYATA (bench/corpus)

Store dibangun dari `bench/corpus` (resep `bench/run.py`: `SourceRegistry.observe` + `SeedImporter.import_file` 28 file → `GraphProjector.rebuild`): **36 node, 35 edge, 71 domain events**, generasi `gen-0a766ee4…`. Lalu:

1. `know-your-soul export --formats all` → exit 0, 16 artifact.
2. `know-your-soul analytics --modules all` → exit 0, 14 artifact.
3. `record-outcome` nyata pada node `06-2d-heuristics-2d-catalog` + `rebuild` → generasi kedua `gen-44272c3b…`; `analytics --modules generation-diff,timeline` → diff **nyata** (`status: "diff"`, 36→36 node, 35→35 edge) dan timeline memuat 1 titik skor nyata (P=1.0, E=0.984251968504, level 2).
4. Re-run `export --formats all` → semua `written: false` (idempoten, byte-identik); `SELECT COUNT(*) FROM event_log` sebelum/sesudah = **74 / 74** (store tidak tersentuh).
5. Isi diverifikasi semantik: `report-v2.json` staleness `{known: true, lag: 0, stale: false}`; `health.json` `{ok: true, score: 1.0, findings: 0}`; `communities.json` 6 komunitas konvergen; `bridges.json` 6 kandidat cross-domain nyata; `timeline.json` `events_processed: 71` (== jumlah domain events store saat itu).

### Tabel artifact (generasi ke-2, setelah outcome; ukuran + sha256 dari file di disk)

| artifact | size (B) | sha256 |
|---|---|---|
| `analytics/DIFF.md` | 806 | `c752fd4ec65397114a571db5fab6c63b60e57cd916270e52b4ed900e8f77e1aa` |
| `analytics/TIMELINE.md` | 5907 | `c398c145c86c605941e4f1265dd9e7cc894f52eb2f4b0c13e9a7093c77886f8a` |
| `analytics/bridges.json` | 1858 | `542b330831aafef012b28f8ccc63df4a2300b6ccb7d3622027e3e37dc7328e49` |
| `analytics/centrality.json` | 22968 | `0d8999b6d6658655a434bcb335947f154e07f59c5bfcaa48a406c347ec110960` |
| `analytics/communities.json` | 1852 | `41d7101408b39370effb4cc9ef4a5524ebf77e9d4b3d7327ba28b6cd512a9c6c` |
| `analytics/components.json` | 7500 | `fb4faf02ba688e7549cde266219d56f115b3c5a5b40879dc0d5be1a2cc51bb74` |
| `analytics/cycles.json` | 338 | `0f5ac4ab09008bf17ac17e0f4b1430dcecf383fc5a647654e48a33d3ce4965ac` |
| `analytics/domain-rollup.json` | 312 | `932b863bdd6673b8a2e2f1ada7a6c57503b53617371c075be284fe1df3fca396` |
| `analytics/generation-diff.json` | 1471 | `2fb8b3020a98f1e793bbd32071abdc012291ae77d4eb8af2ba3b8d6f772e9bd6` |
| `analytics/graph-report-v2.md` | 1158 | `9207e6070d80e50e2c5574eeb44276e88a5e62803e553113c91ac7597aadd519` |
| `analytics/health.json` | 178 | `c37edeeb0233cbae1ff9dae77914950d1183ad86c5d7331f3bf1b969fb8633b8` |
| `analytics/report-v2.json` | 1469 | `8ea741f37652cf30e45c25949091a8f627399cbf03398191e1431bccf6b8aab5` |
| `analytics/review-signals.json` | 192 | `efdce89f49248c5290756b7d41f6997062f2c0a2afbd29c588918ad31c6dd3d3` |
| `analytics/timeline.json` | 11596 | `db26ced79afdf1eda32f220b4d349f661528083b4e39d9fe32054eb302aff2dc` |
| `book/P1.md` | 2396 | `27dd30084b411a098d5e1103587f76e9d14a674ec77ea2ee937b5c375a0d40d2` |
| `book/P2.md` | 1115 | `b4a369b6af1cd7e55bb18d529c91c56406e26d945714772376a1fc6312fc8203` |
| `book/P3.md` | 3574 | `38cb00f6af03ee7b21e6d5c932c9ed5df976e03d429975a233b997859d9f02f8` |
| `book/P4.md` | 2705 | `76fae570d005c1a88f93aeb6acb818b562fd1008f4486374381d4a3508fa1d84` |
| `book/P5.md` | 2747 | `bc6cc6243c3ea31e96d0b5ebb2c92e36a353a5c6f5c6aeb00614a5626f94ad61` |
| `book/P6.md` | 5905 | `c8ed7b83fd995ff22d99a6a897faff08c43fbeef060bc331aa4523910a8014dc` |
| `book/P7.md` | 6752 | `e2848161b7b537f851ce17d08ebc3fde9bb8b7b5ae3bb4580761a1e5f92ccf07` |
| `book/index.md` | 407 | `2b6291ad877e4527552922cdc5b3d71afc2c281bfff609720f421670bc94d4ad` |
| `exports/edges.csv` | 1680 | `0f26f75dbff6fe6f7858cd19595797cf2f0d7f4fa9152d323c49114730cf7a93` |
| `exports/graph.dot` | 6617 | `b00be5d7d7da3974dd0bfa1dadaa7b440b14c85bcb82427cd324ffa3d5fe23ca` |
| `exports/graph.graphml` | 19128 | `f3f02b8769f777ece79b3d4a1bbffbc0d55798344bf62faade881f790a25cd9a` |
| `exports/graph.html` | 47999 | `ee4ea84d42bfe8a72fdee4c41ab30783023b4bd26efd6df394406b16560d126c` |
| `exports/graph.mmd` | 5012 | `85fabf97725446a0a7774465bd28ed33272fba3a7df37214531d8631881c5e3d` |
| `exports/graph.svg` | 20047 | `d4bd991d5f32f8be2b192c1735f6952da54dbad0673d8bfec325c42782fb6a23` |
| `exports/node-index.json` | 2658 | `d64d57f400f7f850df912e044c4f2db2ef8173cd6ddfc4931b6336ef8e001105` |
| `exports/nodes.csv` | 5020 | `f636650d687a1c4609d46d68df2263a0e2ca09d6b9afdb790b73fd2f84ab1dde` |

Catatan: hash book/P2.md berubah antara generasi 1→2 (evidence count 0→1 pada halaman itu) — bukti konteks store benar-benar mengalir ke artifact.

## Bug integrasi nyata yang ditangkap (RED→GREEN)

1. **`nodes.csv` non-quantized score** pada read model produksi: skor `0E-12` (nol eksak notasi ilmiah) + kolom `confidence` berisi label `INFERRED`. Fix di `tabular.py` (normalisasi `serialize_score` + preferensi `scores.confidence`). Tanpa e2e terhadap generasi nyata, ini tidak terlihat oleh test unit wave B (fixture mereka memakai skor 12dp).
2. **Marker round-trip DIFF.md** mismatch (`no_previous_generation` vs teks "no previous generation") — ditangkap test sendiri sebelum e2e.

## Catatan untuk coordinator

- `tests/test_export_cli.py` kini membandingkan nama terhadap `builtin.build_registry()` (bukan `orchestrator.default_registry()` yang minimal). `default_registry()` sengaja dipertahankan apa adanya — test projection SUB-63 (`test_without_registry_generation_is_unchanged`) tetap berlaku.
- `review-signals` tanpa `current_source_versions` (registry tanpa konteks) mematikan sinyal stale-evidence secara jujur; CLI selalu menyuplai set nyata dari store.
- Workspace e2e: `/root/.hermes/cache/scratch/sub70/e2e` (store + graph + 30 artifact on-disk).
