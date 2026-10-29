# ATM Wave B bagian 1 — eksportir DOT / Mermaid / CSV (SUB-65, F-B1/F-B2/F-B3)

Tanggal: 2026-10-26. Repo: `/root/know-your-soul` @ `main` (HEAD `6272fe6` saat mulai). **TIDAK di-commit** (aturan multi-agent). Clean-room: format ditulis dari spesifikasi publik (Graphviz DOT language, Mermaid flowchart syntax, RFC 4180); tidak ada satu baris/konstanta/template Graphify yang disalin — palet warna adalah pilihan milik kita sendiri.

## Apa yang dibangun

Tiga modul eksportir baru di `src/know_your_soul/exporters/`, masing-masing mengikuti pola orchestrator (F-B8, SUB-63): fungsi murni `read_model -> str` kanonik + `produce_* -> bytes` ber-cap eksplisit fail-closed + `roundtrip_*` (parse-kembali → assert struktur) + `register_*` seam ke `ExportRegistry`. **Tidak ada file existing yang diubah** (orchestrator tidak disentuh; integrasi default registry = keputusan koordinator wave).

| File | Plan | API publik | Isi |
|---|---|---|---|
| `exporters/dot.py` | F-B1 | `render_dot`, `produce_dot`, `roundtrip_dot`, `register_dot`; `DOMAIN_COLORS`, `FALLBACK_COLOR`, `DEFAULT_DOT_MAX_BYTES=2 MiB` | DOT kanonik: node terurut by id, edge terurut `(source,target,relation)` ter-dedup, label lewat `security.sanitize_text` + collapse whitespace control + cap 80 char + escape `\`/`"`, fillcolor per domain P1-P7 dari palet milik kita (7 warna heks berbeda, fallback `#808080`), self-loop dipertahankan, edge ber-endpoint tak dikenal dibuang. Round-trip: parser regex penuh — header/footer kanonik, urutan ketat node/edge, warna harus anggota palet, label bebas Cc/Cf, edge hanya ke node terdeklarasi. |
| `exporters/mermaid.py` | F-B2 | `render_mermaid`, `produce_mermaid`, `roundtrip_mermaid`, `register_mermaid`; `DEFAULT_MERMAID_MAX_NODES=200`, `DEFAULT_MERMAID_MAX_BYTES=1 MiB` | `flowchart LR` + subgraph per domain P1-P7 (terurut, judul `P1 · Pembelajaran` dst dari `PrimaryDomain.title`). ID Mermaid deterministik: sanitasi `[^A-Za-z0-9_]→_`, prefix `n_`, cap 48 char, kolisi → suffix `_2/_3` sesuai urutan kanonik id. Label disanitasi + `"`→`#quot;`. **Elision eksplisit**: node > `max_nodes` → alokasi round-robin antar domain (tak ada domain kelaparan), marker `%% elided: K nodes (cap N); M edges dropped` + node `ELIDED["K nodes not shown (cap N)"]` — tak pernah senyap. Round-trip memvalidasi struktur subgraph/edge/marker termasuk konsistensi angka elision. |
| `exporters/tabular.py` | F-B3 | `render_nodes_csv`, `render_edges_csv`, `produce_*`, `roundtrip_*`, `register_tabular`; `NODE_COLUMNS`/`EDGE_COLUMNS` pinned, cap 4 MiB per file | RFC 4180 via stdlib `csv` (`lineterminator="\r\n"`, QUOTE_MINIMAL → kutip didobel, field ber-koma/CRLF ter-quote). Kolom node: `id,domain,kind,status,level,p,m,e,u,confidence,version,degree,community_sig` — skor P/M/E/U/confidence sebagai string 12-desimal (spec §9), `degree` = in+out atas edge ter-dedup (self-loop = 2). **Tanpa field rahasia/free-text by construction**: label, summary, source_file, provenance, context_tags tidak ada di kolom. Edge: `source,target,relation,confidence`. Round-trip: UTF-8 ketat, tanpa bare LF, header persis, lebar baris persis, urutan kanonik, bentuk field skor/integer. |

## Keputusan desain

- **Palet milik kita** (`DOMAIN_COLORS`): P1 `#1B6CA8`, P2 `#2E8B57`, P3 `#E8A33D`, P4 `#C0392B`, P5 `#7D5BA6`, P6 `#5D6D7E`, P7 `#B03A5B` — dipilih untuk proyek ini, bukan dari daftar warna upstream mana pun (kepatuhan §2 plan: baris palet).
- **Determinisme**: semua iterasi atas struktur ter-sortir; dedup edge via set ter-sortir; tanpa waktu/RNG; output byte-identik untuk input ter-permutasi (dipin oleh test unit + property hypothesis `derandomize=True` di ketiga modul).
- **Elision round-robin** (B2): antrian per domain terurut, satu node per domain bergiliran sampai budget habis — elision tidak pernah menghapus satu domain utuh; jumlah node terelisi + edge ter-drop selalu tertulis eksplisit.
- **Sanitasi label** identik di ketiga format: `sanitize_text` (strip bidi/zero-width/Cc-Cf) → collapse `[\t\r\n]+`→spasi → cap char → escape spesifik format.
- **Degree** dihitung dari edge ter-dedup ber-endpoint dikenal, sehingga konsisten dengan isi `edges.csv`.
- **CSV tanpa BOM** (opsi RFC; determinisme byte diprioritaskan).
- Cap breach → `ExportCapExceededError` bertipe (dari orchestrator) di dalam producer; registry mengulang cap-check sebagai backstop.

## Bug yang ditangkap property test (RED→GREEN nyata)

1. Lambda late-binding di `render_mermaid` (`id_map[str(node.get("id"))]` memakai `node` dari loop luar, bukan parameter `item`) → urutan node dalam subgraph salah untuk id non-ASCII (`µ` → `n__` < `n_a`); hypothesis menemukannya dengan contoh minimal `['a','µ']`. Diperbaiki + urutan node dalam subgraph didefinisikan atas **Mermaid id** (bukan node id) agar round-trip konsisten.
2. Dua kesalahan test sendiri (ekspektasi cap label, baris terlalu panjang) — diperbaiki di test.

## Bukti gate

- `pytest tests/test_exporters_{dot,mermaid,tabular}.py -q` → **83 passed**, exit 0 (RED teramati sebelumnya: 3 collection errors `ModuleNotFoundError`).
- `ruff check` + `ruff format --check` pada 6 file baru: **0 error**.
- `pyright` pada 6 file baru: **0 errors, 0 warnings**.
- Full suite @ shared tree: **1904 passed, 0 failed** (298s) dengan `--ignore` pada dua modul test sibling wave-C yang belum ada implementasinya (`test_analytics_timeline.py`, `test_analytics_generation_diff.py` — `ModuleNotFoundError`, di luar scope SUB-65; koleksi suite mentah terhenti oleh kedua file itu). Termasuk 83 test baru saya.

## Integrasi (untuk koordinator wave / SUB-B8)

```python
from know_your_soul.exporters import dot, mermaid, tabular

dot.register_dot(registry)  # exports/graph.dot
mermaid.register_mermaid(registry)  # exports/graph.mmd (max_nodes=200)
tabular.register_tabular(registry)  # exports/nodes.csv + exports/edges.csv
```

Semua artifact lolos `produce_exports` (cap + round-trip) dan siap masuk manifest via `manifest_entries`. Mengubah bentuk output = bump `version` di `register_*`.
