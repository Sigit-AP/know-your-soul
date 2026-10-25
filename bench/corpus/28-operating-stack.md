# SOUL 28 — 28-operating-stack

- **Operating stack wajib** — RTK (output terminal) + Headroom (input prompt) + 3 koleksi skill wajib dipakai setiap tugas; ini bukan preferensi, ini aturan.
- **RTK (terminal output)** — `rtk` 1.0.1 di `/usr/local/bin`, plugin Hermes `rtk-rewrite` (`pre_tool_call` hook, `allow_tool_override: true`) menulis ulang `git status` menjadi `rtk git status` secara otomatis.
- **RTK bukti aktif** — `rtk rewrite "git status"` -> `rtk git status`, exit 3 (3 = rewrite, 1 = passthrough); output terminal jadi versi padat.
- **Headroom (prompt input)** — proxy loopback `127.0.0.1:8787`; `model.base_url` menunjuk ke sana; mode `cache`, `disable_kompress=1`, layanan systemd-user `headroom-proxy.service`.
- **Headroom proteksi tool** — tool result dari read_file, search_files, terminal, execute_code, skill_view, skills_list, web_extract TIDAK dikompres lossy; tool lain (mis. web_search) boleh dikompres.
- **Headroom bukti aktif** — `/root/.headroom/logs/proxy-8787.log` baris `PERF ... tok_before/tok_after/cache_hit_pct`; `/health` mengembalikan `status=healthy`.
- **Skill routing superpowers (proses)** — pakai untuk brainstorm -> writing-plans -> executing-plans -> verification-before-completion -> requesting-code-review.
- **Skill routing pm-skills (planning)** — pakai untuk why/what/urutan: sprint-plan, pre-mortem, create-prd, prioritization-frameworks, metrics-dashboard.
- **Skill routing mattpocock (craft kode)** — pakai untuk bentuk kode/repo: codebase-design, code-review, tdd, git-guardrails, setup-pre-commit.
- **Aturan bukti stack** — klaim "RTK/Headroom aktif" wajib disertai bukti CLI atau log, bukan asumsi konfigurasi.
- **Korpus graphify SOUL** — dipanggil on demand lewat skill `graphify-soul-router`; graf tidak disuntik per turn, jadi retrieval bergantung pada skill yang terpicu.
- **Larangan bypass stack** — jangan menghapus `plugins.enabled: [rtk-rewrite]`, jangan mematikan `headroom-proxy.service`, jangan mengarahkan `model.base_url` langsung ke provider tanpa fallback.
- **Skill routing vs identitas** — SOUL.md tetap acuan pokok; ketiga koleksi skill hanya alat eksekusi di bawah aturan SOUL.md (M1-M10 tetap berlaku).
- **RTK cakupan rewrite (terukur)** — `rtk rewrite` menulis ulang perintah tunggal dan compound: "git status" exit 3, "cd X && git status" exit 3, "ls -la" exit 3, "grep -rn" exit 3; tetapi PIPELINE lolos apa adanya: "git status | wc -c" exit 1. Jadi jangan mengukur manfaat RTK dengan menempelkan pipe, dan jangan menempelkan pipe kalau ingin outputnya diringkas.
