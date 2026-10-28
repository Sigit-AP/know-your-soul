# W4 — Retrieval v2: symbol lookup, call hierarchy, context assembly (SUB-91)

Tanggal: 2026-10-28 · Commit basis: `d838f0f` · Scope:
`src/know_your_soul/retrieval_v2/` (baru), `tests/test_retrieval_v2_*.py` (baru,
8 file), `tests/_retrieval_v2_helpers.py` (baru), `src/know_your_soul/resolve/resolver.py`
(**addendum minimal**: satu entri dict), `tests/test_resolve_resolver.py` +
`tests/test_ingest_documents_pipeline.py` (2 pin perilaku lama disesuaikan —
lihat §5). `store.py`, `graph_model.py`, kontrak beku, dan 14 ops **tidak
disentuh**.

## Modul

| File | Isi |
|---|---|
| `retrieval_v2/query.py` | Parser `kind:/lang:/language:/path:/name:` gaya codegraph `parseQuery`; prefix tak dikenal lolos ke free text; `kind:type` → `type_alias`; tak pernah raise pada string |
| `retrieval_v2/scoring.py` | Konstanta codegraph `query-utils.ts`: `name_match_bonus` (80/60/prefix 10+30·ratio/15/10), `kind_bonus`, `path_relevance` (+10/+5/+3, −15 path test), `bounded_levenshtein` (Damerau, early-exit, cap), `estimate_tokens` (4 char/token) |
| `retrieval_v2/fts.py` | FTS5 external-content di atas `symbols` (kolom name/kind/path) — tabel milik modul (`retrieval_v2_symbols_fts` + meta), fingerprint `count:bytes`, `ensure/rebuild/drop` |
| `retrieval_v2/lookup.py` | Kaskade FTS5 BM25 berbobot kolom (20/5/1) + suplemen exact-name → LIKE (1.0/0.9/0.8/0.5 + 0.05/term) → fuzzy (`0.4/(1+d)`, di bawah LIKE); rescoring multi-sinyal; filter sebagai gate keras terakhir; ordering total `(-score, path, line, col, node_id)` |
| `retrieval_v2/hierarchy.py` | `callers`/`callees` atas edge `calls`/`imports`/`references`, depth 1–8, cycle-safe, output kanonik (definisi diurut `(path,line,col,node_id)`, entry `(depth,path,line,col,node_id)`), marker truncasi `> Showing N of M` |
| `retrieval_v2/context.py` | Token budgeting bertingkat: alokasi proporsional skor (cap `max_share_fraction`), skeletonization bila share < biaya penuh (marker `· skeleton (signature only)`), trim pass deterministik menjamin `total_tokens <= budget`, provenance + node id di setiap entry |

## Keputusan desain

1. **FTS5 tanpa menyentuh store v3** — tabel virtual external-content milik
   modul; kesegaran via fingerprint (`ensure_fts_index` rebuild hanya bila
   fingerprint berubah). Keterbatasan terukur: update yang mempertahankan
   total panjang kolom tidak terdeteksi — pipeline memanggil
   `rebuild_fts_index` eksplisit setelah ingest (terdokumentasi di docstring).
2. **Quirk FTS5 didokumentasikan di kode**: `MATCH` harus menyebut nama tabel
   (bukan alias) + `CROSS JOIN` (persis bentuk codegraph `queries.ts`);
   external-content menolak `DELETE FROM` — rebuild lewat perintah FTS5
   `'rebuild'`.
3. **Tier tidak pernah tercampur**: fuzzy hanya berjalan bila FTS+LIKE kosong
   (skor fuzzy < skor LIKE minimum by construction).
4. **Skeletonization "di atas ambang"**: share proporsional node < biaya bentuk
   penuh → skeleton; trim pass (downgrade tail → pop ke `omitted`) menjamin
   invariant budget. Budget < 16 token (biaya frame) ditolak fail-closed.

## Resolver addendum (temuan SUB-90)

`REF_KIND_TO_EDGE_KIND` mendapat `"references": "references"` (1 baris + komentar).
Ref link markdown/plaintext W3 kini melewati kaskade normal: exact → name_tail
(markdown bukan bahasa modul → tier import_path diskip). Dampak terukur: link
`[x](b.md)` ke dokumen terindeks kini **resolve** (exact-name ke node
`document` bernama `b.md`) alih-alih gagal `unknown_ref_kind`. Dua test pin
lama disesuaikan (lihat §5).

## Property tests (hypothesis, `derandomize=True`)

Determinisme (2 run identik), invariant budget (`total ≤ budget` + render ≤
budget + provenance), stabilitas lintas urutan input (permutasi → output
identik), kontrak Levenshtein (simetri, batas gap panjang, cap → −1), parser
tak pernah raise, `ensure_fts_index` idempoten. Setiap contoh memakai file db
sendiri (`_example_db`, pola SUB-88).

## Gate

| Gate | Perintah | Hasil |
|---|---|---|
| RED | `pytest tests/test_retrieval_v2_*.py -q` | 7 collection errors (ModuleNotFoundError retrieval_v2), exit 2 |
| GREEN | `pytest tests/test_retrieval_v2_*.py -q` | **244 passed**, exit 0 (2 run: 244 passed / 244 passed) |
| Suite penuh | `pytest -q` | **5310 passed**, 1 warning, exit 0 (360s) — dua pin lama disesuaikan (§5), tanpa kegagalan lain |
| Lint | `ruff check` + `ruff format --check` (file W4) | bersih |
| Types | `pyright` (retrieval_v2, resolve, semua test W4) | 0 errors |
| Keamanan | `bandit -ll -r src/know_your_soul/retrieval_v2` | exit 0 (B608 di-nosec-kan per-baris: nama tabel/kolom konstan modul, nilai bound params; B101 Low mengikuti pola store.py/resolver.py) |

## Penyimpangan / residual

1. **2 test pin lama disesuaikan** (bukan dihapus): `test_resolve_resolver.py::TestKindMapping::test_unknown_kind_fails_closed` — `"references"` dikeluarkan dari daftar kind tak dikenal; `test_ingest_documents_pipeline.py` — test parkir `unknown_ref_kind` diganti menjadi `test_real_resolver_resolves_references_kind` yang memverifikasi resolusi nyata link→dokumen. Keduanya adalah residual terdokumentasi SUB-90 (w3-documents.md) yang memang ditugaskan untuk ditutup W4.
2. Fingerprint FTS tidak menangkap update yang mempertahankan `count:bytes` — mitigasi: `rebuild_fts_index` eksplisit pasca-ingest.
3. Lookup filter-only (`kind:function` tanpa free text) mengembalikan semua simbol ter-filter dengan skor 0.0 — kanonik, bukan bug.
