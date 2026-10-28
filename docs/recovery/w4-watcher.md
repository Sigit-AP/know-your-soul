# W4 — Polling Watcher + Sinkronisasi Incremental (SUB-92)

Status: GREEN.
Scope: `src/know_your_soul/ingest/watcher.py` (baru), `src/know_your_soul/ops/watch_cli.py` (baru),
`src/know_your_soul/cli.py` (subcommand `watch` ditambah; 14 ops beku + kontrak v1/v2/v3 tidak disentuh),
`tests/test_ingest_watcher.py`, `tests/test_ingest_watcher_integration.py`, `tests/test_ingest_watcher_cli.py`.
Tidak menyentuh `retrieval_v2` (milik SUB-91).

## Yang dibangun

`IngestionWatcher(root, pipeline, *, config=WatcherConfig(), clock, sleep, logger)` — polling watcher
murni-stdlib (`os.walk` + `os.stat`, **bukan inotify** → portabel + testable; clock/sleep/logger
diinjeksikan sehingga tidak ada test yang tidur sungguhan). Pola diadopsi dari watcher codegraph
(RESEARCH-OPS §1):

1. **Snapshot stat-only**: `scan_stat_snapshot(root)` → `{relpath: (size, mtime_ns)}`; tanpa baca byte
   (konfirmasi konten tetap milik pipeline: pre-filter `(size, mtime)` → sha256). Skip dir sama dengan
   walk pipeline (`.git/__pycache__/node_modules/...`) + store SQLite (`*.sqlite3/-wal/-shm`) otomatis
   dieksklusi (anti feedback-loop bila store di dalam root). Entry tak terbaca di-skip, tidak pernah fatal.
2. **Debounce dapat dikonfigurasi**: quick quiet 0,3 s saat ≤ `quick_max_changes` (2) file pending,
   debounce penuh 2,0 s untuk burst; quiet window re-scan sampai change set stabil, dibatasi
   `max_quiet_rounds` (4) agar churn konstan tidak pernah menstarvasi ingest (busy-loop mustahil:
   interval punya floor keras 0,05 s dan sleep interval jalan di antara siklus, termasuk siklus gagal).
3. **Quick path**: ≤ `full_scan_ceiling` (500) perubahan tanpa removal → tiap file berubah masuk
   `pipeline.ingest_path(file)` individual, urutan bytewise.
4. **Full scan-diff**: di atas ceiling ATAU ada removal (delete = kerja cascade milik walk direktori)
   → satu `ingest_path(root, incremental=True)`; self-heal event yang terlewat.
5. **Auto-degrade**: K (5) kegagalan berturut → warning per kegagalan, error + **stop** pada kegagalan
   ke-K (`status="degraded"`), tidak pernah raise ke host; counter reset pada sukses; baseline tidak
   maju pada siklus gagal (retry dari state benar).
6. **Policy**: `evaluate_watch_policy(path, env, platform_release, mounts_text)` — prioritas
   `KNOW_YOUR_SOUL_NO_WATCH` > `KNOW_YOUR_SOUL_FORCE_WATCH` > WSL `/mnt/<huruf>` (release mengandung
   microsoft/wsl) > remote mount (`/proc/mounts`, fstype nfs/nfs4/cifs/smbfs/sshfs/9p/drvfs/vboxsf/…;
   longest-prefix, unescape `\040`). Semua input injektable → deterministik.
7. **Laporan per siklus deterministik**: `CycleReport` (mode, changes bytewise, counts agregat pipeline,
   duration dari clock injeksi) + `WatchReport` agregat; `to_dict()` stabil.

## CLI

`know-your-soul watch <path> [--interval s] [--once] [--max-cycles N] [--debounce s] [--quick-quiet s]
[--quick-max-changes N] [--full-scan-ceiling N] [--max-failures N] [--force] [--json]` — OPS surface
(seperti `ingest`/`export`): bukan knowledge op, tidak pernah MCP/HTTP. Exit 0 completed,
1 typed failure (`WATCH_DISABLED` oleh policy, atau auto-degraded), 2 usage (knob invalid / root bukan
direktori). `--once` = satu siklus untuk test/CI.

## Bukti gate

- `pytest tests/test_ingest_watcher.py tests/test_ingest_watcher_integration.py tests/test_ingest_watcher_cli.py -q`
  → **113 passed**, exit 0 (6,7 s) — melampaui target 80+.
- Integrasi nyata: tulis/ubah/hapus/rename file → state v3 (`files`/`symbols`/`graph_edges`/
  `unresolved_refs`) diverifikasi; rename = delete+add; ref unresolved ter-resolve lintas siklus;
  konvergensi incremental-watcher ↔ fresh ingest (`canonical_ingestion_dump`, `include_ids=False`)
  identik; dua run watcher byte-identik; `PYTHONHASHSEED` 0/1/42 via subprocess → dump identik;
  lane extractor riil (tanpa fake) hijau.
- Suite penuh: 5309 passed + **2 failed** — keduanya di luar scope SUB-92
  (`tests/test_resolve_resolver.py::TestKindMapping::test_unknown_kind_fails_closed[references]` dan
  `tests/test_ingest_documents_pipeline.py::...test_real_resolver_parks_unknown_kind_deterministically`):
  SUB-91 in-flight mengedit `src/know_your_soul/resolve/resolver.py` (+4 baris, kind `references` kini
  di-resolve) sehingga dua test lama yang menegasikan perilaku fail-closed lama gugur. Nol kegagalan
  di file watcher/CLI. Baseline pra-SUB-92: 4954 passed.
- `ruff check` + `ruff format` bersih; `pyright` 0 error; `bandit` 0 issue pada dua modul baru.

## Keputusan & residual

- Baseline watcher per-proses (tidak dipersist): siklus pertama memperlakukan semua file sebagai
  `added`; pre-filter incremental pipeline membuat ini murah (`skipped`). Persisted baseline sengaja
  dihindari — store v3 sudah menjadi source of truth (codegraph: scan-diff self-heal).
- Removal selalu memaksa full scan-diff walau di bawah quick limit (delete = cascade, bukan per-file).
- WSL/remote-mount → watcher menolak dengan pesan actionable; `--force`/`KNOW_YOUR_SOUL_FORCE_WATCH=1`
  meng-override kecuali `KNOW_YOUR_SOUL_NO_WATCH` (paritas codegraph).
- Tidak ada thread/subprocess: loop polling sinkron; `serve --watch` lama (doc watcher SOUL corpus,
  `source_watcher.py`) tidak disentuh — beda domain.
