# ATM Wave B bagian 3 — eksportir F-B5 (HTML viewer) + F-B6 (book) (SUB-67)

Tanggal: 2026-10-26. Repo: `/root/know-your-soul` @ `main` (HEAD 6272fe6 saat mulai). TIDAK di-commit (sesuai scope). Semua file BARU; nol file existing disentuh.

## Apa yang dibangun

| File | Plan | API publik | Isi |
|---|---|---|---|
| `exporters/html_view.py` | F-B5 | `render_html_view(read_model, *, positions=None, max_nodes=5000, max_bytes=4MiB, title=…)`, `html_view_format()`, `register_html_view(registry, …)` | Viewer HTML statis **satu file** self-contained: pencarian atas index JSON tertanam, panel info node (label, P/M/E + uncertainty/confidence, versi, level, provenance), daftar tetangga (arah + relasi), legenda domain P1–P7 dengan judul spec, tema terang/gelap via CSS custom properties + toggle (localStorage/matchMedia), posisi dari parameter `positions` (hasil mesin layout mana pun) dengan **grid fallback internal** deterministik (row-major atas id ter-sortir, koordinat terkuantisasi 3 desimal). |
| `exporters/book.py` | F-B6 | `build_book(read_model, *, communities=None, evidence_counts=None, max_pages=1024, max_page_bytes=1MiB)`, `cap_filename()`, `book_to_bytes()`, `validate_book()` | Ekspor "buku" markdown: `book/index.md` + satu halaman per domain (`book/P1.md`…; judul domain dari spec §6) + satu halaman per komunitas (`book/C<id>.md`) **hanya bila** data komunitas (output `analytics.communities` atau list mapping) diberikan. Node terurut per id; tiap entri: id, label/ringkasan, skor P/M/E, versi, evidence count (parameter; default 0), provenance, tetangga sebagai link relatif ke halaman domain tetangga. Output = `dict[path → markdown]` siap tulis; daftar halaman = kunci dict, dan `validate_book` menegakkan setiap halaman non-index terdaftar di index. |

## Keputusan desain

- **Lisensi (plan §2 baris F-B5/F-B6).** Desain ulang total: markup, CSS (~60 baris), JS (~90 baris), dan palet 7 warna domain + fallback adalah nilai original yang dipilih di sini; tidak ada `COMMUNITY_COLORS`, `_html_styles`, `_html_script`, template wiki, docstring, atau nama privat Graphify yang dibaca/diangkat — hanya *ide fitur* (satu file, pencarian, panel tetangga, legenda; index + artikel per komunitas) dengan titik masuk ide dicatat di docstring modul.
- **Anti-XSS berlapis.** (1) Data tertanam sebagai JSON kanonik dalam island `application/json` inert dengan escaping `& < > U+2028 U+2029` → `\u00xx`, sehingga `</script>` di label tidak pernah menutup island (test: dokumen selalu punya tepat 2 `</script>`). (2) JS hanya `textContent`/`createElement` di belakang **satu delegated click listener** pada `document`; nol `innerHTML`/`eval`/`fetch`/`document.write`. (3) CSP meta `default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'` menolak semua resource eksternal secara struktural. (4) Atribut `data-node-id` di SVG lewat `html.escape(quote=True)`. (5) Semua teks node lewat `security.sanitize_text` (Cc/Cf dibuang) sebelum serialisasi.
- **Elision bertanda, bukan senyap.** Melebihi `max_nodes` → node pertama `max_nodes` (id ter-sortir) dipertahankan, payload membawa `elided:{cap,total,omitted}` dan banner terlihat di halaman; link ke node ter-elisi dibuang sehingga tidak ada referensi menggantung. Byte cap fail-closed typed (`HtmlViewError`); di seam registry, cap ditegakkan orkestrator sebagai `ExportCapExceededError` (producer sengaja render tanpa cap byte internal agar errornya bertipe orkestrator).
- **Book caps fail-closed.** `max_pages` (halaman = 1 + domain + komunitas) dan `max_page_bytes` per halaman → `BookCapExceededError` typed, bukan truncasi senyap. Filename di-cap **byte** (default 64) dengan suffix hash sha256-8 dari nama asli saat truncate, pemotongan pada batas UTF-8; domain non-aman dinamai `domain-<slug-capped>.md`; tabrakan filename antar domain fail-closed.
- **Markdown inert.** Label/provenance diratakan ke satu baris dan metakarakter `\ [ ] ( ) < > \`` di-escape — payload `[x](javascript:…)` maupun `<script>` tidak pernah jadi markup aktif (dipin test + property hypothesis).
- **Determinisme.** Semua iterasi atas list ter-sortir; tie-break kanonik; tanpa clock/random/global state; `positions` dikuantisasi `Decimal` 3 desimal (`ROUND_HALF_EVEN`). Property test: permutasi input (hypothesis `derandomize`, 25 seed shuffle eksplisit) → byte-identik.
- **Tanpa dependency runtime baru.** Stdlib + `domain`/`security`/`exporters.orchestrator` milik sendiri. `viz/layout` **tidak** diimpor (belum ada) — posisi murni parameter.

## Bukti gate

- RED terverifikasi pre-implementasi: 2 collection errors (`ModuleNotFoundError`), 43 test tertulis.
- GREEN: `pytest tests/test_exporters_html_view.py tests/test_exporters_book.py -q` → **43 passed** (26 viewer + 17 book).
- `ruff check` + `ruff format --check` pada 4 file: **0 issue**.
- `pyright` pada 4 file: **0 errors, 0 warnings**.
- Smoke eksekusi nyata: render graf 4 node → HTML 11.288 byte; parse HTML (html.parser) 0 error/0 tag tak tertutup; island JSON parse OK; JS lolos `node --check` (browser headless tidak tersedia di host ini — libatk hilang — jadi verifikasi runtime browser diganti parse struktural + syntax check). Book 4 domain → 5 halaman, semua link resolve (dipin test).
- Full suite: dijalankan terpisah (lihat board) — modul baru tidak diimpor kode existing, jadi tidak ada jalur regresi; hasil dicatat di board.

## Catatan untuk integrator

- `register_html_view(registry)` mendaftarkan `exports/graph.html` (kind `format`, cap 4 MiB) dengan round-trip validator (doctype + island + schema_version). Wiring ke `default_registry()` = milik task integrasi (SUB berikut), bukan scope ini.
- Untuk buku, tulis via `book_to_bytes(build_book(...))` dan daftarkan tiap path ke manifest (kunci dict = daftar halaman eksplisit); `validate_book` adalah validator fail-closed-nya.
- Ketika `viz/layout.py` (F-B7) landed, panggil `render_html_view(model, positions=layout_positions)` — tanpa perubahan di modul ini.
