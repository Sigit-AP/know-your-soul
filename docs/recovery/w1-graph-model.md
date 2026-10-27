# W1 — Graph Model v2 (SUB-84)

Scope: fondasi Universal Knowledge Graph — modul domain murni, tanpa I/O, tanpa
AI, deterministik penuh. TIDAK menyentuh `store.py` (SUB-85), kontrak/guard
(SUB-86), atau kontrak beku v1/v2.

## Artefak

| File | Status | Isi |
|---|---|---|
| `src/know_your_soul/graph_model.py` | BARU (901 baris) | model v2 lengkap |
| `tests/test_graph_model.py` | BARU (1403 baris, 153 `def test_`, 276 item) | unit + property tests |
| `docs/recovery/w1-graph-model.md` | BARU | laporan ini |

## Keputusan desain

1. **Node kinds universal, open-forward.** `CoreNodeKind` (StrEnum, beku):
   hierarki pengetahuan (`root/primary/child/branch/leaf`) + kinds kode
   (`file/module/class/function/method/variable/constant/interface/type/import`)
   + kinds dokumen (`document/section/page/paragraph`) = 19 kinds inti.
   `document` ditambah sebagai akar sumber non-kode (amanat: hierarki berakar
   di "file/dokumen"). Ekstensi ke depan lewat `register_node_kind()` —
   append-only, tervalidasi (`[a-z][a-z0-9_]{0,31}`), idempoten; iterasi
   `all_node_kinds()` selalu terurut. Edge kinds mengikuti pola yang sama:
   `CoreEdgeKind` = `contains`, `bridges` + 13 relasi codegraph
   (`calls/imports/exports/extends/implements/references/type_of/returns/
   instantiates/overrides/decorates/navigates`).

2. **Node id stabil gaya codegraph, byte-kompatibel.**
   `make_node_id(path, kind, name, line=None, col=None)` →
   `kind:sha256(path:kind:name:line)[:32][:col]`. Vektor pertama test
   mereproduksi persis contoh terdokumentasi codegraph
   (`sha256("src/utils.ts:function:normalizePath:12")` →
   `function:c1a23b8765bedac66f0d6e29332c2041`). `line=None` (node pengetahuan)
   menghilangkan segmen line dari preimage. Root file memakai bentuk native
   codegraph `file:<path>`. Fail-closed: kind tak terdaftar, name kosong,
   line<1, bool-sebagai-int, col tanpa line → `NodeKindError`/`NodeIdError`.

3. **Hierarki = edge `contains`, bukan kolom.** Depth arbitrer muncul dari
   rantai edge (test: rantai depth 50). Invariant ditegakkan **eagerly** di
   `add_edge`, fail-closed: single-parent (`MultipleParentsError`), acyclic
   (`ContainsCycleError`, self-loop = siklus), endpoint harus ada
   (`UnknownNodeError`). `validate()` memeriksa ulang forest secara global
   (bukti, bukan perbaikan). Query: `contains_parent/children`,
   `subtree_ids` (BFS deterministik), `depth`, `contains_root`.

4. **Node primary (ibu) first-class.** `GraphNode` kind `primary` wajib satu
   `domain` + satu `purpose` non-kosong (single-responsibility, divalidasi
   model). Antar-primary terhubung via `add_bridge(a, b, weight, reason)`:
   simetris (disimpan kanonik `source<target`, `SYMMETRIC_EDGE_KINDS`),
   berbobot Decimal [0,1] (float ditolak), reason wajib, self-loop dan
   endpoint non-primary → `BridgeError`. Bridge TIDAK menciptakan parent
   contains.

5. **Auto-detect dekomposisi (proposal-only).**
   `propose_primary_splits(graph, root_id, SplitThresholds)` — root wajib
   primary (`PrimaryError`). Tiga sinyal deterministik: `subtree_size`
   (>max_subtree_nodes, default 500), `domain_heterogeneity`
   (>max_distinct_domains, default 3), `kind_heterogeneity`
   (>max_distinct_kinds, default 8). Heterogen → satu proposal per domain
   non-root (grup root tertahan); homogen tapi oversize → first-fit packing
   atas subtree anak langsung (urut `(-size, id)`, kapasitas threshold).
   Output `SplitProposal` (nama deterministik, member terurut, reasons
   terurut, metrik). Graph TIDAK pernah dimutasi (dipin test: canonical dump
   sebelum/sesudah identik).

6. **Sinyal review per subtree (proposal-only).**
   `review_candidates(graph, root_id, ReviewPolicy)` — root dieksklusi,
   keturunan dinilai tiga sinyal: `stale` (watermark None atau < watermark
   kebijakan), `low_score` (min(P,M,E) < min_score, Decimal, default 0.2),
   `content_drift` (content_hash vs reviewed_hash keduanya ada dan berbeda).
   Output `ReviewCandidate` terurut by node_id, reasons urutan beku
   (`content_drift < low_score < stale`), details membawa bukti (watermark,
   min_pme terkuantisasi 12dp, hash). Tanpa `now()` — staleness berbasis
   watermark yang diinjeksikan, jadi deterministik.

7. **Kanonik & determinisme.** `canonical_dump()` = JSON kanonik
   (sort_keys, Decimal→fixed-point 12dp via `storage_payload` milik domain
   v1, datetime→ISO) dengan nodes terurut by id dan edges terurut
   `(source,target,kind,line,col)`. Invariant di bawah permutasi urutan
   insert node/edge (unit + hypothesis) dan stabil lintas
   `PYTHONHASHSEED=0/1/42` (test subprocess nyata). `GraphNode` menegakkan
   **konsistensi id**: `node_id` harus sama dengan id deterministik dari
   field-nya — identitas tidak bisa dipalsukan.

## Property tests (hypothesis, derandomized sesuai konvensi repo)

- id deterministik + roundtrip `parse_node_id` untuk input acak;
- id sensitif terhadap line (stability per lokasi);
- forest acak (attach di bawah node yang ada) tidak pernah siklik dan
  `contains_root` selalu benar;
- penutupan siklus ke root selalu `ContainsCycleError`; ke ancestor
  ber-parent selalu `MultipleParentsError` (kedua cabang invariant);
- canonical dump invariant di bawah permutasi (seed acak);
- review candidates proposal-only + predikat stale eksak;
- split proposals: member men-cover tepat sekali (disjoint + lengkap) bila
  oversize, kosong bila tidak.

## Angka gate (nyata, exit code)

- `pytest tests/test_graph_model.py` → **276 passed, exit 0** (2 run,
  4.0s/5.4s — hasil identik).
- Full suite `pytest tests/` → **3631 passed, 1 failed, exit 1** — kegagalan
  tunggal = `tests/test_store_v3.py::test_property_name_tail_...` milik
  SUB-85 (file WIP sibling, 0 referensi ke `graph_model`; terisolasi 26/26
  passed — flaky di bawah full suite, di luar scope SUB-84).
- `ruff check` + `ruff format --check` kedua file → **exit 0**.
- `pyright graph_model.py test_graph_model.py` → **0 errors, 0 warnings**,
  exit 0.
- Determinisme: subprocess `PYTHONHASHSEED=0/1/42` byte-identik (dipin test).

## Batasan / handoff ke W1 berikutnya

- Model murni in-memory; persistensi/migrasi schema v3 = SUB-85.
- Konsistensi id berlaku untuk node yang dibuat lewat model ini; importer
  kode/dokumen (W2/W3) wajib memakai `make_node_id`/`file_node_id`.
- `bridges` dibatasi primary↔primary; relasi lain tetap lewat edge kinds
  codegraph. Tidak ada auto-apply: split/review murni proposal.
