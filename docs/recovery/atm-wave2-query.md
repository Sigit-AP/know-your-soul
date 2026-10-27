# ATM Wave 2 — SUB-76: F-E1/F-E2 analytics/query_tools (query depth via kanal OPS/ART)

Tanggal: 2026-10-27 · Pelaksana: SUB-76 · Status: GREEN (393/393 test baru; ruff + format + pyright bersih pada 2 file milik SUB-76)

## Ruang lingkup

Satu modul baru, murni fungsi atas read model (`graph.json` ter-parse / iterable node dict), tanpa menyentuh kontrak beku `QueryRequest`/`QueryResponse`, `service.py`, atau file existing lainnya:

| Fitur | File | Kanal |
|---|---|---|
| F-E1 + F-E2 | `src/know_your_soul/analytics/query_tools.py` | OPS/ART (modul analitik; integrasi CLI = pekerjaan orkestrator wave berikutnya) |
| Tests | `tests/test_analytics_query_tools.py` | — |

## Keputusan desain

### Filter terstruktur (fail-closed, additive-only)

- Skema filter ketat dengan allowlist kunci beku: `domains`, `kinds`, `lifecycle_statuses`, `level_min`/`level_max`, `score_ranges`. Key tak dikenal → `QueryToolsError`; tidak ada yang ditebak. `None`/`{}` = match semua (additive-only sesuai F-E1).
- Vocabulary tertutup divalidasi terhadap enum domain (`PrimaryDomain` P1–P7, `NodeKind`, `LifecycleStatus`) — filter tidak bisa drift dari kontrak spec §6.
- Bound skor: hanya string/int/Decimal; **binary float ditolak** (determinisme lintas platform); harus terhingga, dalam `[0,1]`, dan `min ≤ max`.
- Node tanpa id tidak pernah lolos filter (hasil filter harus bisa dialamatkan cursor); nilai hilang/cacat tidak pernah memenuhi predikat atas nilai itu (absen ≠ wildcard).
- Output `filter_nodes` selalu terurut id ascending — filtering saja sudah proyeksi deterministik.

### Sort multi-kunci deterministik

- Allowlist field: `id`, `label`, `domain`, `kind`, `lifecycle_status`, `professional_level`, `version`, `p`, `m`, `e`, `uncertainty`, `confidence`. Sugar string (`"p"`, `"-p"`) dan bentuk mapping `{"field", "direction"}`.
- `id` ascending selalu tie-breaker terakhir (kecuali caller sudah sort by id) → total order, tak pernah tergantung urutan input.
- Nilai hilang/cacat selalu setelah nilai ada, **apapun arah sort** — direction tak pernah membalik aturan "known before unknown". Komparator tiga-arah via `cmp_to_key`.

### Pagination stabil

- Cursor = node id item terakhir halaman sebelumnya; halaman berikutnya mulai tepat sesudahnya. Cursor yang bukan anggota himpunan terurut → `QueryToolsError` (fail-closed: restart diam-diam akan mencampur halaman dari hasil berbeda).
- `limit` default 50, maks 1000 (beku). `next_cursor` = `None` pada halaman terakhir; `has_more` eksplisit.

### Agregasi + histogram berbin beku

- `aggregate_nodes(group_by=domain|kind|lifecycle_status|professional_level)`: per grup `count` + per dimensi (p/m/e/uncertainty/confidence) `min`/`max`/`mean`/`missing` + **histogram 10 bin beku** (`[0,0.1)…[0.9,1.0]`; skor tepat 1 masuk bin terakhir; aritmetika bin murni Decimal).
- Mean dihitung dalam presisi Decimal 34 lalu dikuantisasi 12dp ROUND_HALF_EVEN — permukaan skor identik dengan kontrak §9. Dimensi yang semua anggotanya missing → null min/max/mean + histogram nol (jujur, bukan diam-diam kosong).
- `facet_nodes`: breakdown per field, urut `(-count, value)`.
- Agregasi/facet di `query_nodes` dihitung atas **himpunan terfilter penuh**, bukan halaman terpotong — konsumen terpaginasi tetap melihat count yang benar (persyaratan eksplisit F-E1).

### Traversal allowlist relasi

- `subtree_nodes(graph, root_id, relations=…, max_depth=…)`: BFS atas link set terpublikasi; relasi divalidasi terhadap vocabulary tertutup `RelationKind` (default: seluruh 10 relasi). Tetangga dikunjungi urut target-id ascending → urutan walk murni fungsi konten graph, bukan urutan link di artifact. Siklus terminasi (tiap node sekali, depth terdangkal). Root tak dikenal → fail-closed. `max_depth=0` = hanya root.

### Pencarian lanjutan di atas LexicalIndex

- `search_nodes(nodes, query, node_filter=…, limit=…, index=…)`: ranking tetap milik `LexicalIndex` (`-score, -field_rank, node_id`); filter terstruktur hanya *membuang* hit, tidak pernah me-rerank. Query kosong/stopword-only → nol hit (permukaan sinyal-positif). Skor hit di-render fixed-point 12dp. Index eksternal boleh disuplai (id di luar node set = out of scope, bukan error).

### Near-duplicate detection (F-E2) — proposal-only, fail-closed

- Normalisasi summary: `query_terms` (casefold, stopword dibuang, plural dilipat — normal form yang sama dengan index leksikal); summary yang seluruhnya stopword jatuh ke token mentah agar tetap punya identitas.
- Shingle word 3-gram (beku); dokumen lebih pendek dari ukuran shingle menyumbang satu shingle = seluruh sekuens (dua dok pendek sama hanya jika teks ternormalisasi identik — bacaan konservatif).
- **Jaccard eksak sebagai `Fraction`** (tanpa float), blocking per primary domain — pasangan lintas domain tidak dibandingkan (itu pertanyaan taksonomi, bukan hygiene). Pairs `O(n²)` per blok cukup pada skala ribuan node; tidak perlu LSH.
- Ambang beku `0.8` (`DEFAULT_JACCARD_THRESHOLD`); teks identik → `exact: true`, skor `1.000000000000` (content-hash SHA-256 atas teks ternormalisasi).
- **Tidak pernah auto-merge**: output membawa `disposition: "proposal_only"` dan `auto_merge: false` *sebagai data*, bukan hanya dokumentasi. Merge aktual tetap milik lifecycle `merge_or_deduplicate` lewat kanal improvement ter-audit. Kandidat terurut `(-similarity, node_a, node_b)`; `max_candidates` memotong deterministik dengan flag `truncated`.
- Kebalikan kebijakan Graphify `dedup.py` (auto-merge MinHash/LSH): implementasi eksak-terblokir, original; origin konsep dicatat di docstring modul.

### Komposisi satu panggilan

- `query_nodes(graph, spec)` = filter → sort → page (+ `group_by`/`facets` opsional). Spec ketat (`filter`, `sort`, `page{cursor,limit}`, `group_by`, `facets`; key lain → error). `total_nodes` vs `total_filtered` mengekspos selektivitas filter untuk tooling review.

## Bukti gate

- `pytest tests/test_analytics_query_tools.py -q` → **393 passed** (melebihi target 200+): validasi fail-closed tiap permukaan, inklusivitas bound, mean 12dp (termasuk 1/3 → `0.333333333333`), histogram bin-boundary per skor, determinisme di bawah permutasi input (filter/sort/agregasi/facet/search/near-dup/traversal), cursor walk ujung-ke-ujung, siklus traversal, threshold-boundary Jaccard (tepat 0.8 lolos; 1/3 tidak).
- `ruff check` + `ruff format --check` pada 2 file milik SUB-76 → bersih.
- `pyright` pada 2 file milik SUB-76 → 0 errors, 0 warnings.
- Full suite: lihat papan milestone (dijalankan terakhir, hasil dicatat di sana).
- Uji jarak kontrak: modul tidak mengimpor `service`/`contracts`, tanpa clock/randomness (dipin sebagai test).

## Risiko / catatan untuk integrasi (orkestrator wave berikutnya)

1. **Modul belum ter-wire ke CLI/orkestrator** — sesuai aturan SUB-76 (hanya file baru); integrasi = pekerjaan wave berikutnya (pola persis SUB-68 → SUB-70).
2. Input adalah `graph.json` ter-parse; caller menyuplai read model. Node read model tidak membawa timestamp — filter rentang waktu dari tabel F-E1 sengaja TIDAK diimplementasikan (tidak ada `created/updated/last_used` di permukaan publik; menambahkannya berarti mengubah proyeksi, di luar mandat).
3. Near-duplicate `O(n²)` per blok domain: nyaman sampai puluhan ribu node; bila korpus tumbuh melebihi itu, tambahkan blocking lanjutan (mis. band per token langka) — jangan ganti ke LSH aproksimasi tanpa keputusan eksplisit (kontrak fail-closed lebih menyukai eksak).
4. `search_nodes` menerima `LexicalIndex` eksternal: pemanggil yang memakai index atas node set berbeda bertanggung jawab atas cakupan id-nya (modul menandai `examined`, id asing diskip).
5. Kapasitas output: halaman dibatasi `MAX_PAGE_LIMIT=1000`; agregasi/near-dup belum di-cap byte — penulis artifact menegakkan cap seperti biasa.
