# ATM Wave-2 OPS — konfigurasi, metrik, diagnostik (SUB-77)

- Tanggal: 2026-10-27 ~06:19–07:20 UTC. HEAD awal: `86b8d15`.
- Scope: **hanya file baru** — `src/know_your_soul/ops/{config,metrics,diagnose}.py`,
  `tests/test_ops_{config,metrics,diagnose}.py`, dokumen ini. `cli.py` sengaja
  **tidak disentuh** (wiring subcommand `config describe` / `metrics` /
  `diagnose` adalah pekerjaan integrator). Tidak ada commit (aturan subagent).
- Batas yang dipegang (atm-feature-plan.md §5): tanpa dependency runtime baru
  (`tomllib` stdlib 3.14), deterministik (terbukti byte-identik lintas
  `PYTHONHASHSEED=0/1/42`), fail-closed typed, sanitasi via
  `security.redact_value`, dan **semuanya read-only terhadap store** — dipin
  oleh test `test_metrics_do_not_write_to_store` /
  `test_diagnostics_do_not_write_to_store` (event/audit/watermark sebelum ==
  sesudah).

## 1. `ops/config.py` — konfigurasi terstruktur berlapis (545 LOC)

- `CONFIG_SPEC`: 20 `ConfigField` beku (frozen dataclass, `__post_init__`
  menolak type/key ilegal): `store.path`, `store.migrations_dir`,
  `projection.output_root`, `ops.{root,actor,stale_after_seconds}`,
  `serve.{socket_path,http_bind,api_key,api_key_env,host_allowlist,max_body_bytes}`,
  `watch.{root,interval_seconds,cooldown_seconds,state_path}`,
  `export.output_dirname`, `retention.{keep,pinned}`,
  `diagnose.check_permissions`.
- Lapisan resolusi `load_config(file_path=, env=, cli_overrides=)`:
  defaults → file TOML (tabel nested atau dotted key, diflatten) → env
  (`KNOW_YOUR_SOUL_*`) → CLI override. Provenance per-key
  (`default|file|env|cli`).
- Fail-closed typed `ConfigError(code, message)`: `UNKNOWN_KEY`,
  `CONFIG_FILE_UNREADABLE`, `CONFIG_FILE_INVALID`, `TYPE_MISMATCH`,
  `OUT_OF_RANGE`, `INVALID_VALUE`, `INVALID_SPEC`. Satu-satunya widening =
  `int→float`; `bool` tidak pernah diterima sebagai `int`; env bool hanya
  `1/0/true/false/yes/no/on/off`; float env menolak `nan/inf`.
- Redaksi: field `secret=True` (mis. `serve.api_key`) di-dump sebagai
  `<redacted>` pada `dump()`, `effective()`, dan `describe_config()` kecuali
  `redact_secrets=False` eksplisit; dipin oleh test yang memastikan nilai
  asli tidak muncul di JSON hasil dump maupun teks describe.
- Dokumentasi diri: `describe_spec()` (tabel markdown seluruh field),
  `describe_config(config)` (nilai ter-resolve + source, ter-redaksi),
  `render_example_toml()` (contoh TOML ter-anotasi yang bila di-uncomment
  parse persis ke defaults — dipin test).

## 2. `ops/metrics.py` — metrik operasional (366 LOC)

- `collect_ops_metrics(store, projection_root=, migrations_dir=,
  stale_after_seconds=)` → dict kanonik float-free, `schema_version: 1`:
  - `counts`: events/domain/audit/evidence/source_versions, journal
    complete/in_progress/stale, watermark knowledge+event,
    `latency_rows_excluded` (outcome malformed dihitung, tidak ditebak);
  - `latency_ms`: per-operasi dari `duration_ms` outcome journal audit —
    `latency_summary` integer murni (mean round-half-up, p50/p95
    nearest-rank pada urutan sorted; input non-int/negatif/bool →
    `ValueError`);
  - `publish_capacity`: estimate vs guard (`ARTIFACT_MAX_BYTES ×
    PUBLISH_CAPACITY_RATIO`) vs cap + `utilization_permille` (int) +
    `headroom_bytes`;
  - `projection`: ukuran tiap artifact generasi aktif dari disk, total byte,
    node/edge count, watermark + `watermark_matches_store`; absent →
    `{"present": False}` atau `None` bila root tak diberikan;
  - `health`: skor berbobot deterministik (`HEALTH_COMPONENT_WEIGHTS` jumlah
    tepat 100; komponen proyeksi dikeluarkan dari denominator bila root tak
    diberikan). Setiap pemeriksaan menelan exception → komponen `ok: False`
    (metrik melaporkan keadaan, tidak pernah raise).
- `render_markdown(metrics)` → laporan markdown stabil (urutan sorted).

## 3. `ops/diagnose.py` — diagnostik self-check (470 LOC)

- `run_diagnostics(store, projection_root=, migrations_dir=,
  stale_after_seconds=, validator=, check_permissions=)` →
  `DiagnosticReport(findings, checked)`; `Finding(code, severity, message,
  details)` beku, severity berjenjang `info < warning < error`
  (`SEVERITY_ORDER`); `exit_code(report)` = 0 bila tak ada error, 1 bila ada
  (`EXIT_OK`/`EXIT_ISSUES`).
- 11 pemeriksaan, masing-masing fail-closed (crash → finding
  `CHECK_CRASHED`, tidak pernah raise): `store_select`, `hash_chain`
  (`verify_chain` + anchor), `migrations` (applied == on-disk),
  `stale_journals` (warning), `projection_pointer` (`PROJECTION_ABSENT`
  warning / `CURRENT_POINTER_INVALID` error), `manifest_load`
  (`MANIFEST_UNREADABLE` error), `manifest_vs_disk` (`ARTIFACT_MISSING` /
  `ARTIFACT_HASH_MISMATCH` error via sha256 ulang dari disk;
  `ARTIFACT_UNMANIFESTED` warning untuk file liar), `watermark_consistency`
  (`WATERMARK_MISMATCH` + `PROJECTION_VERSION_MISMATCH`, warning = read model
  basi, bukan korup), `validator` (`ReadModelValidator` default atau injeksi;
  gagal → error dengan daftar errors ter-redaksi), `publish_capacity`
  (est > guard → warning; est > cap → error), `file_permissions`
  (group/other-accessible store/artifact → `PERMISSIONS_TOO_OPEN` warning;
  hilang/tak terbaca → `FILE_UNREADABLE` error; store `:memory:` diskip).
- Semua pesan + details lewat `security.redact_value` — fragmen berbentuk
  secret di dalam string error tidak bisa keluar lewat laporan.
- `render_markdown(report)`: findings diurut error-dulu lalu code; seksi
  `## Checks` mencantumkan pemeriksaan yang berjalan (sorted, unik).

## 4. Test (206 test, 3 file — melebihi target 180)

- `test_ops_config.py` (103): integritas spec, defaults, env per-tipe +
  ejaan bool + invalid, file TOML (nested/dotted/semua tipe/rusak/unknown/
  range), presedensi penuh default<file<env<cli, redaksi di semua permukaan,
  describe/example round-trip, hardening konstruktor.
- `test_ops_metrics.py` (52): `latency_summary` murni, counts store kosong +
  event nyata + journal, latensi per-operasi + baris malformed, kapasitas,
  proyeksi dari generasi NYATA (rebuild store kosong) + pointer rusak,
  health score (100/100, pengurangan bobot, exception containment),
  rendering, kanonisitas tanpa float, read-only, byte-identik.
- `test_ops_diagnose.py` (51): semantik Finding/Report/exit code, semua
  check pass/gagal (chain patah via stub, pointer, manifest, tamper
  artifact, watermark, version, validator pass/gagal/crash, kapasitas
  guard/cap via stub estimate, permission 0644/0640/0600/disable/`:memory:`),
  rendering + determinisme + read-only.

## 5. Gate (fresh, di pohon bersama)

- `pytest` 3 file: **206 passed** exit 0.
- Full suite: **2420 passed** exit 0 (3 file sibling SUB-74
  `test_analytics_{algorithms,paths,query_tools}.py` RED in-flight di-ignore —
  modulnya belum landed; 0 referensi ke modul saya).
- `ruff check` + `ruff format --check` 6 file saya: bersih. `pyright` 6 file:
  **0 error** (3 error repo-wide = import sibling SUB-74 yang belum landed).
- Coverage modul saya: config 99% (sisa = guard spec-integrity module-level),
  metrics 100%, diagnose 98% (sisa = OSError-on-read defensive + os.access
  sebagai root).
- Determinisme: digest sha256 gabungan metrics+render+diagnose+describe
  identik pada `PYTHONHASHSEED=0/1/42` (probe nyata).

## 6. Catatan untuk integrator

- Wiring CLI yang disarankan (bukan bagian scope ini): `config describe`
  (markdown), `config dump` (canonical JSON `OpsConfig.dump()`), `metrics`
  (JSON/markdown), `diagnose` (exit code dari `exit_code(report)`). Semua
  OPS-surface — bukan operasi knowledge ke-15, tidak dibuka ke MCP/HTTP.
- `run_diagnostics(..., validator=)` menerima `ReadModelValidator` hasil
  injeksi bila registry ekspor custom dipakai; default konstruktor standar.
- `diagnose.check_permissions` di spec config memetakan ke argumen
  `check_permissions` pada `run_diagnostics`.
