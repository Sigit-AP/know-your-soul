# W7 — Superpower Server (SUB-100)

Daemon multi-agent di atas store pengetahuan, dibangun untuk **melebihi daemon codegraph**
(RESEARCH-ARCH §1b) pada lima sumbu yang diminta operator: kapasitas eksplisit 50 agent
dengan backpressure typed, read pool read-only WAL vs single writer, arbitrase tulis dengan
dedup idempotency, audit per-agent, dan scope enforcement ketat (14 ops beku + ingest v3).

## Arsitektur

```
host agent (MCP stdio)
   │  newline JSON-RPC 2.0
   ▼
serve/proxy.py  (1 per host; handshake lokal identik; PPID watchdog; auto-spawn)
   │  Unix socket (0600, path AF_UNIX-cap-safe)
   ▼
serve/superpower.py — SuperpowerDaemon (1 per store; detached double-fork/setsid)
   ├─ acceptor thread: kapasitas max_agents=50; overflow → SERVER_BUSY + retry_after_ms
   ├─ session threads: parse envelope (fail-closed) → dispatch
   ├─ READ lane: _ReadPool = N=8 worker, masing-masing ReadOnlyEventStore (mode=ro)
   │    + KnowledgeService dengan audit forwarded; projector read-only (repair → ProjectorBusy)
   │    + staleness: nudge writer → tunggu publish berbatas → retry (bukan gagal)
   └─ WRITE lane: _WriteArbiter = 1 writer thread
        ├─ audit lane internal (prioritas; jurnal read lane lewat sini)
        ├─ antrian tulis berbatas (256) + dedup in-flight (verb, actor, idem-key)
        └─ publish proyeksi per batch (repair_if_stale) — satu-satunya publisher
```

Keputusan kunci:

- **Read-only ditegakkan mesin, bukan disiplin**: koneksi pool dibuka `mode=ro`; percobaan
  tulis apa pun gagal di level SQLite. Skema diverifikasi sama dengan writer saat konstruksi
  (`ReadOnlyStoreError` bila beda). Tidak masuk registry `_LIVE_STORES` (bukan writer).
- **Satu penulis untuk hash chain**: semua append event_log (domain + audit read lane)
  diserialkan lewat thread writer tunggal; `_ForwardingAuditLog` menjembatani begin/complete
  read lane ke antrian audit writer (audit lane didrain dulu setiap iterasi → read tidak
  pernah tertahan antrian tulis klien).
- **Idempotency dua lapis**: in-flight `(verb, actor, key)` berbagi satu eksekusi; replay
  selesai ditangani store (`idempotent_replay`), reuse konflik → `IdempotencyConflict` typed.
- **Freshness tanpa stampede**: reader yang menemukan proyeksi stale (balapan dengan commit)
  tidak pernah rebuild — ia men-nudge writer (publisher tunggal) dan menunggu watermark
  bergerak, batas `read_publish_wait_seconds` (default 30s), lalu typed STALE_PROJECTION.
- **Scope**: dispatcher hanya mengenal 16 tool beku + `initialize`/`ping`/`tools/list`/
  `resources/*` + `daemon.status|shutdown` (socket lokal); envelope dengan key asing →
  -32600; tool tak dikenal → -32004; tanpa `_meta.actor` → -32003; method `daemon.*` dari
  sisi proxy → -32002. Satu-satunya argumen ber-path (ingest v3) tetap terkurung pada root
  startup oleh `run_ingest_verb` yang sudah ada.
- **Supervisi**: lockfile `daemon.lock` O_CREAT|O_EXCL (isi pid+nonce, kepemilikan diverifikasi
  sebelum cleanup), pidfile, ready marker, `daemon.exit.json` (alasan: idle/operator/sigterm),
  idle reaper (refcount 0 + timeout), liveness watchdog (heartbeat acceptor; stall → exit 70),
  PPID watchdog proxy (early-ppid capture ala #1185 + ppid==1 → orphan).
- **AF_UNIX cap**: bila `runtime/daemon.sock` > 100 byte, soket direlokasi deterministik ke
  `$TMPDIR/kys-<sha256(root)[:16]>/` (lock/pid/ready/exit tetap di samping store).

## CLI

```
python -m know_your_soul.serve --daemon|--proxy|--daemon-foreground --db … --graph-root …
python -m know_your_soul.serve daemon status|stop --db … --graph-root …
know-your-soul serve --daemon | --proxy      # wiring operator (additive di cli.py)
know-your-soul daemon status|stop
```

`KNOW_YOUR_SOUL_DAEMON_INTERNAL=1` menolak spawn rekursif (pola CODEGRAPH_DAEMON_INTERNAL).
Tanpa flag superpower, `serve` tetap API HTTP/socket lama (tidak berubah; teruji).

## Perbaikan terukur atas codegraph

| Sumbu | codegraph | superpower |
|---|---|---|
| Kapasitas | tak eksplisit; overload → risiko crash | 50 agent + antrian berbatas; SERVER_BUSY + retry_after typed |
| Reads vs writes | 1 koneksi SQLite per daemon (query pool offload thread) | 8 koneksi ro WAL vs 1 writer; engine menolak tulis di pool |
| Dedup tulis | writer-lock file | antrian single-writer + dedup in-flight + replay store |
| Audit | per request (MCP session) | per request, actor wajib, read lane ikut ter-jurnal via writer |
| Idle/reap | refcount + idle timeout | sama + exit.json ber-alasan + stale-lock recovery terbukti kill-9 |
| Handshake | proxy lokal identik (#172) | sama, diverifikasi byte-parity vs server referensi |

## Bukti gate (nyata, direproduksi)

- `pytest tests/test_serve_*.py -q` → **160 passed** (protokol 34, config 17, readstore 9,
  daemon 26, arbiter 8, spawn 10, proxy 14, concurrency 4, cli 7 + helper).
- Storm: 50 agent × 7 invokasi (2 rewrite pool bersama + 1 shared-key + 3 read) →
  111 domain events tepat (10 seed + 100 rewrite + 1 shared), `verify_chain().ok`,
  350 jurnal `complete` terkelompok 7/actor × 50 actor.
- Banjir tulis (queue=2, delay hook 0.3s, 16 penyerbu) → ≥1 SERVER_BUSY typed dengan
  retry_after_ms > 0; daemon tetap menjawab ping; chain valid sesudahnya.
- kill -9 host → proxy mati oleh PPID watchdog (stdin dipegang pihak ketiga — bukan EOF).
- kill -9 daemon → proxy respawn otomatis dan call berikutnya sukses.
- Daemon idle 1s → exit bersih, artefak terhapus, exit.json reason=idle.
- Race 8 thread spawn → tepat 1 pid daemon; semua caller sukses.
- Konflik idempotency 10 penyerbu → 1 pemenang + 9 IdempotencyConflict typed; 1 event.
- Paritas wire: initialize + tools/list proxy == server referensi `mcp_server` (parsed JSON).
- Klien MCP resmi (ClientSession stdio) → handshake, 16 tools, query + get_node sukses.

## SUB-104 — penyelesaian & gate SUPERPOWER-ARCHITECTURE.md

Perbaikan sumber (bukan workaround):

1. **Lane re-partisi berdasar semantik tulis** — `knowledge_evaluate` (materialisasi skor)
   dan `knowledge_review` (record keputusan) memanggil `store.commit`; keduanya salah jalur
   di read pool (koneksi `mode=ro` menolak → INTERNAL_ERROR). Sekarang WRITE 7 / READ 7 /
   HEAVY 2. Ditemukan oleh probe 16-verb; kontrak 16-tool beku tak disentuh.
2. **Group commit: ack hanya setelah COMMIT** — future klien dulu di-resolve di dalam
   transaksi batch → kill -9 menghilangkan tulisan yang sudah di-ack (ditemukan test
   crash-safety: 4 tulisan ack hilang). Sekarang outcome ditangkap per-member dan
   `_resolve_mutation` berjalan setelah `transaction()` exit; resolusi tanpa dedup-lock
   (lock-order deadlock dengan sharer `_in_flight` ditemukan dan diperbaiki).
3. **Fairness batch-gather** — `_FairQueue.get(exclude=…)`: maksimal 1 item per actor per
   batch selama lane lain berisi (work-conserving fallback bila hanya lane itu yang berisi).
   Tanpa ini, pola fase pasca-commit (semua klien batch resubmit serentak) memberi spammer
   beberapa slot per batch.
4. **Drain beradab nyata** — drain thread menunggu queue KOSONG **dan** batch writer yang
   sedang dieksekusi (`arbiter.executing`); `_finalize` menghentikan lane SEBELUM menutup
   socket session dan menunggu `_inflight` dispatch mencapai 0 (batas drain_timeout) → nol
   request terputus tanpa respons. `shutdown(force=True)` skip drain, exit code 2;
   `daemon stop --force` di `__main__` + `cli.py`.
5. **SocketGuard uninstall** — `mcp_server.main` dulu menginstall SocketGuard seumur proses
   tanpa uninstall saat return; di proses test (anyio.run di-monkeypatch) guard bocor dan
   menolak loopback untuk test berikutnya. Sekarang `try/finally: guard.uninstall()` saat
   main() keluar — proteksi selama serving tak berubah.
6. **2 test backpressure ditulis ulang** — actor berbeda per flooder + `per_actor_write_cap`
   ≤ `write_queue_size` (validasi config fail-closed SUB-100 menolak kombinasi lama).

Gate (tests/test_serve_gates.py, 9 test, semua hijau):

| Klaim | Bukti nyata |
|---|---|
| 50 agent × 16 verb campuran | 734 invokasi, 57.9s, nol kegagalan, nol timeout internal; jurnal per actor tepat (16 lifecycle / 13 pool), chain `verify_chain().ok`; latency client-side per jalur: **read** p50/p95/p99 = 226/3657/3993 ms (target 6000), **write** 5264/11942/13762 ms (target 18000), **heavy** 3206/22027/22507 ms (target 30000). Persentil didominasi queue-wait pada writer tunggal tersaturasi — angka jujur, bukan target kosmetik |
| Overload aman | queue penuh → SERVER_BUSY typed + retry_after_ms, daemon tetap menjawab, chain valid (test_serve_arbiter/test_serve_concurrency) |
| Fairness | spammer 10 koneksi menawarkan 466 writes (≥10× beban sopan) → 9 agent sopan menyimpan **85.8%** throughput (15.0/s → 12.9/s); gate <20% loss |
| Group commit | A/B 240 writes/arm: off 20.42s (12/s) vs on 2.90s (83/s) → **7.04×** (klaim ≥3× TERLAMPAUI); kedua arm chain-valid, event count identik |
| Read-your-writes | tulis→baca lintas lane via nudge writer + watermark (test_serve_concurrency) |
| Crash safety | daemon proses nyata di-kill -9 di tengah burst (12 writes ter-ack) → respawn (stale-lock recovery) → **0 hilang**, chain ok, 0 jurnal in_progress tak-terekonsiliasi |
| Session leak | lease 0.5s, max_agents=1: slot di-reap terukur (agent kedua masuk setelah kedaluwarsa) |
| Drain | graceful: 3 tulisan (1 eksekusi + 2 antri) semua terjawab sukses, kerja baru ditolak SHUTTING_DOWN typed, exit 0; force: exit 2 cepat, reason `operator-force` |
| Heavy lane | 6 ingest sukses sementara read lane terus menjawab (p95 < target); histogram lane `heavy` tercatat terpisah |
| Deadline | tulis dengan `_meta.deadline_ms=150` antri di belakang mutasi 0.4s → DEADLINE_EXCEEDED typed, tak pernah dieksekusi (node absen dari store) |

Catatan kejujuran angka: storm dirancang saturasi-penuh (734 ops serentak); write p95 ~12s
adalah antrian di writer tunggal, bukan service time (~13ms solo). Ini batas arsitektur
single-writer + jurnal wajib; codegraph pada beban sama tak punya admission control sama
sekali (antrian tak berbatas → risiko crash, bukan angka).

## Penyimpangan & catatan integrasi

1. **Kolisi namespace dengan SUB-101**: sibling memakai `src/know_your_soul/serve/` untuk
   webview (`__init__.py`, `webview.py`). Modul saya diberi nama tak-ambiguitas
   (`rpc.py`, `daemon_config.py`, `superpower.py`, `proxy.py`, `__main__.py`); `__init__.py`
   milik sibling tidak disentuh. Integrator: gabungkan ekspor di `__init__.py` bila perlu.
2. `cli.py` disunting additive (flag `serve --daemon/--proxy/--daemon-foreground`,
   `--max-agents/--read-pool-size/--idle-timeout`, subcommand `daemon`) — bersinggungan
   dengan edit sibling (`webview` subcommand); kedua perubahan hidup berdampingan, teruji.
3. `mutation_delay_seconds` di DaemonConfig adalah seam uji backpressure terdokumentasi
   (default 0.0, produksi tidak terpengaruh).
4. Publish-capacity guard (F-3, artifact 64 KiB) membatasi ~40 node kecil per store uji —
   storm dirancang padat-operasi (rewrite pool 20 node), bukan padat-node, sesuai kontrak.
5. `python -m know_your_soul.cli` tanpa main-guard (pra-ada) exit 0 diam-diam — entry
   operator yang benar: `python -m know_your_soul` / console script. Bukan scope saya.
