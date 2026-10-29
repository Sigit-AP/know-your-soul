# SUB-103 — W7: synthesis passes + structure-tolerant name-match tier

Date: 2026-10-28 (UTC). Repo: `/root/know-your-soul` @ `8704c87` (main).
Scope: `src/know_your_soul/resolve/name_matcher.py` (BARU),
`src/know_your_soul/resolve/synthesis.py` (BARU),
`tests/test_name_matcher.py` + `tests/test_synthesis.py` +
`tests/test_synthesis_properties.py` (BARU), this recovery doc.
Not touched: `resolve/resolver.py`, `resolve/__init__.py`, `store.py`,
`graph_model.py`, `ingest/` (termasuk `languages.py` — milik sibling),
`serve/`, semua kontrak beku. No commits (pool rule).

Research basis: `RESEARCH-COVERAGE-AUDIT.md` §4.1 (45 `SYNTH_PASSES` +
callback-synthesizer), §4.2 (tier-synthesizer), §4.3 (name-matcher 28K LOC),
`RESEARCH-QUERY.md` §2.1/§2.2 (multi-sinyal rescoring, segment vocabulary).
Aturan port: pola diadopsi, kode ditulis ulang (~90/10). Tidak ada satu baris
pun kode codegraph yang disalin.

## 1. `name_matcher.py` — tier resolver pasca-primer

Pola yang diadopsi dari codegraph `name-matcher.ts` + search layer:

- **Segment vocabulary** (`split_identifier`): identifier dipecah ke segmen
  lowercase — separator (`_ . : - / …`) dulu, lalu batas camelCase
  (`parseHTTP`), acronym tail (`HTTPResponse` → `http`+`response`), dan batas
  huruf↔digit (`utf8Encode` → `utf`+`8`+`encode`). Deterministik, total.
- **Multi-signal rescoring** (`score_name_pair`): skor = sinyal gabungan,
  semuanya `fractions.Fraction` eksak (binary float dilarang di repo ini):
  exact case-insensitive → 1; selain itu `(jaccard + 2·containment)/3` +
  bonus prefix (1/10) / substring (1/20), di-cap 1.
- **Fail-closed selection** (`match_name`): hanya resolve bila tepat satu
  kandidat ≥ `min_score` (default 3/4) dengan margin ≥ `min_margin`
  (default 1/20) atas runner-up; seri/dekat → `None` (ambigu tak pernah
  ditebak).
- **Ubiquity ceiling** (`candidate_cap=500`, pola
  `AMBIGUOUS_NAME_CEILING`): kandidat digenerate via `lower(name) LIKE`
  per-segmen + exact-insensitive; melebihi cap → ref dibiarkan failed.
- **Language-family gating** (pola `gateLanguageMatch`): kandidat wajib satu
  family bahasa dengan file sumber (JS/TS saling melihat; python hanya
  python; bahasa unknown → tidak ada kandidat).

Integrasi kaskade **tanpa menyentuh `resolver.py`**: `NameMatchResolver
.resolve_unmatched(store)` hanya mengambil ref `failed` dengan
`failure_reason == 'no_resolution'` (oldest-first, `LIMIT` fail-closed).
Verdict ambigu primer (`ambiguous_*`) terminal — tier tidak pernah
membukanya. Ref yang tak termatch dibiarkan failed dengan reason asli
(idempoten by construction). Write discipline menyalin resolver: fase baca
murni → satu transaksi `add_graph_edges` → `retry_failed_ref` +
`record_resolution_attempt(resolved)` per hit, sehingga crash di tengah
replay bersih (edge identity dedup). Edge bert provenance
`resolver:name-match:v1` + metadata `{ref_id, ref_name, strategy:
'name_match', score: "p/q"}` — dapat dibedakan dari parse (`ingest:*`) dan
primer (`resolver:two-phase:v1`).

## 2. `synthesis.py` — 4 pass terbatas (subset applicable dari 45)

Pass dipilih yang bisa di-ground-truth-kan di schema v4 (symbols +
graph_edges + unresolved_refs saja; tanpa AST/file content). Semua edge:
`provenance = "synthesis:<pass>"`, metadata `{synthesized_by, confidence,
…}` via `canonical_json`, idempoten (run kedua insert 0), dan **non-overwrite**:
tidak pernah membuat edge kedua bila (source,target,kind) sudah ada —
apapun provenance-nya. Tiap pass bisa dimatikan via
`SynthesisConfig(enabled_passes=frozenset({...}))`. Urutan tetap
`SYNTH_PASS_ORDER` = containment → reexport → type_hint → callback.

| Pass | Pola codegraph | Implementasi | Confidence |
|---|---|---|---|
| `method_class_containment` | fixed step `goMethodContains` | method yatim (tanpa parent `contains` non-synthesis) → owner dari prefix nama terkualifikasi (`Greeter.greet`→`Greeter`, 9/10) atau class terdekat-mendahului di file yang sama (7/10). Skip: ambigu (0/2+ owner, class sebaris), siklus contains (BFS berbatas `ancestor_cap`), file generated (default) | 9/10, 7/10 |
| `reexport_chains` | `module-importers` / barrel re-export | edge `imports` yang mendarat di node `import` lain = re-export; chain di-walk (depth cap 8, cycle-safe, hop ambigu → skip) ke simbol terminal non-import → edge `references` + metadata `via` | 4/5 |
| `type_hint_edges` | type-parameter/annotation channels | ref `kind='type_hint'` (ditolak primer sebagai `unknown_ref_kind`) resolve exact ke simbol `class`/`interface`/`type` unik → edge `type_of`; ref ikut resolved dengan disiplin write resolver | 9/10 |
| `callback_edges` | `callback-synthesizer.ts` (`fieldEdges`, named-only) | ref `function_ref` resolved = callback bernama yang diregistrasi; caller registrar (kind function/method) → callback, channel capped (`max_callbacks_per_channel=40` / `max_dispatchers_per_channel=6`, pola `MAX_CALLBACKS_PER_CHANNEL`/`EVENT_FANOUT_CAP` — cap trip = channel diskip utuh, tak pernah dipotong) | 2/5 |

Catatan kejujuran: store kita tidak punya data field/string-channel milik
codegraph, jadi `callback_edges` memakai model hub (caller-of-registrar) —
lebih lemah dari field-pairing, dan karena itu confidence-nya paling rendah
(2/5) serta FP-nya diukur eksplisit (lihat §4).

## 3. Integrasi

`resolve_and_synthesize(store, …)` = `ReferenceResolver.resolve_pending` →
`NameMatchResolver.resolve_unmatched` → `Synthesizer.run`, mengembalikan
`PipelineReport(resolver, name_match, synthesis)`. Synthesis selalu pass
terpisah setelah resolusi primer, dengan stamp provenance sendiri — tidak
pernah menyamar sebagai edge hasil parse. `resolve/__init__.py` sengaja
TIDAK disentuh (aturan pool: satu `__init__.py` satu agen); modul baru
diimpor via path lengkap `know_your_soul.resolve.{name_matcher,synthesis}`.

## 4. Bukti gate (semua real run, exit code nyata)

- **RED**: 3 file test → collection error `ModuleNotFoundError`, pytest
  exit=2.
- **GREEN**: `pytest tests/test_name_matcher.py tests/test_synthesis.py
  tests/test_synthesis_properties.py` → **186 passed** (target 150+ ✓).
- **Regresi resolver**: + `test_resolve_resolver.py` +
  `test_resolve_properties.py` + `test_retrieval_v2_resolver_addendum.py`
  → **320 passed**.
- **Full suite** (2 run bersih, hasil identik): **6044 passed / 136 failed**,
  dengan 6 file test WIP sibling di-ignore (collection error milik sibling:
  `test_kernel_rust.py` — AttributeError LanguageSpec dari `languages.py`
  sibling; `test_serve_{readstore,daemon,proxy,spawn,arbiter}.py` —
  `from tests._serve_helpers import …` padahal `tests/` bukan package).
  Seluruh 136 kegagalan ada di `tests/test_webview.py` (milik SUB-101):
  root cause `runtime_guard.OutboundSocketDenied` — socket-guard leak dari
  kombinasi kumulatif test lain (webview pass 165/165 standalone DAN di
  setiap kombinasi pairwise yang kucoba; polusi N-arah, file webview baru
  wave-7 jadi interaksi ini mustahil disebabkan modulku yang murni aditif).
  Dua failure non-webview yang reprodusibel keduanya terbukti milik sibling:
  `test_no_ai_runtime::test_product_sources_never_shell_out_at_all` →
  offender `serve/superpower.py:1292 os.execvpe` (SUB-100);
  `test_codebase_bench::test_baseline_gate` → baseline vs perubahan
  `languages.py` sibling (SUB-102). Nol kegagalan menyentuh file scope-ku.
- Determinisme lintas PYTHONHASHSEED 0/1/42: 171 passed identik 3×
  (name_matcher + synthesis).
- `ruff check` + `ruff format --check` pada 5 file scope: 0 error (repo-wide
  ada 33 error lint, semuanya di file sibling `serve/` + `test_webview.py` +
  `test_serve_*` — bukan scope-ku, dilaporkan apa adanya).
- `pyright` pada 5 file scope: 0 errors / 0 warnings.
- `uv lock --check` OK; `license_guard` exit 0; `bench/run.py check` exit 0.

Property tests (`test_synthesis_properties.py`, hypothesis derandomized):

1. **Idempotensi** — run kedua `run_synthesis` / `resolve_unmatched` mengubah
   nothing (dump byte-level `graph_edges` + `unresolved_refs` identik).
2. **Determinisme** — dua store identik yang dibangun independen → report dan
   edge dump identik; `match_name` invariant terhadap urutan kandidat.
3. **Non-overwrite** — himpunan identitas (source,target,kind) pra-synthesis
   ⊆ pasca-synthesis tanpa duplikat; edge parse `ingest:*` byte-identik.
4. **Fail-closed** — nama duplikat lintas file tak pernah termatch; skor
   selalu Fraction dalam [0,1]; refleksif = 1.
5. **Caps keras** — edge callback tersintesis ≤ dispatchers × callbacks per
   channel.
6. **FP rate terukur di fixture tetap** (bukan klaim): containment — 3 edge
   disintesis, 2 TP + 1 FP terukur (method object-literal ter-link ke class
   terdekat; precision 2/3 pada fixture, dipin sebagai batas regresi);
   callback — 2 edge disintesis, 1 TP + 1 FP terukur (caller inspeksi ikut
   ter-link; model hub, didokumentasikan).

## 5. Keputusan & batasan

- **`type_hint` ref kind belum diemit extractor** (extractor saat ini hanya
  `calls`/`imports`; walker ada di scope sibling). Pass-nya siap dan
  teruji pada ref yang diparkir manual; pada repo nyata hari ini pass ini
  provably-empty (0 kandidat) — sama seperti gate `has()` codegraph.
- `function_ref` sudah ada di kontrak ref (`REF_KIND_TO_EDGE_KIND`) dan
  resolver sudah menanganinya; `callback_edges` mengkonsumsi edge hasil
  resolusinya.
- Tidak ada fuzzy Levenshtein di name-matcher (keputusan: typo "responce" ≠
  "response"; struktur saja). Ini disengaja — determinisme + presisi di atas
  recall.
- Tidak ada penulisan ke tabel lain, tidak ada kontrak baru: semua
  penambahan murni baris `graph_edges`/`unresolved_refs` lewat API store
  yang sudah ada.
