# W7 — Selective language breadth (SUB-102)

Tanggal: 2026-10-28. Repo: `/root/know-your-soul` @ `main`. TIDAK di-commit
(aturan operator). Bahasa: 12 → **19** (17 dual-engine Rust+Python,
2 Python-fallback: swift, dart).

## 1. Audit tree-sitter-language-pack 1.22.1 (get_parser, bukan docs)

`tree_sitter_language_pack.SupportedLanguage` mendaftar **372** grammar.
Semua 19 kandidat dari brief **lolos `get_parser(lang)` + parse smoke-test**
(ABI 14/15, dalam rentang runtime tree-sitter 0.25):

```
kotlin swift scala lua dart elixir haskell ocaml julia r perl sql
html css yaml toml json dockerfile markdown   → semuanya LOAD OK
```

Provenance: aset rilis `grammar-provenance-1.22.1.json` (sha256
`412b404ac3355ededf2da71f93c2ab493f0ca165084c2b4baeedfd6866329f3f`,
diverifikasi thd `.sha256` resmi) memberi repo+rev bundled per bahasa.

## 2. Seleksi — kenapa hanya 7 dari 19 kandidat

`LanguageSpec` adalah data deklaratif murni; walker (extractor.py/walker.rs)
di luar scope. Kandidat dievaluasi dengan **parsing sampel nyata** dan inspeksi
field grammar programatik. Kandidat yang tak bisa dipetakan secara jujur
ditolak, bukan dipaksakan:

| kandidat | keputusan | alasan terukur |
|---|---|---|
| kotlin, swift, scala, lua, haskell, perl | **DITERIMA** | spec deklaratif lengkap (deklarasi+import; call bila grammar punya field callee) |
| dart | **DITERIMA (Python-fallback)** | ekstraksi struktur+import bersih; engine Rust mustahil (lihat §4) |
| elixir | DITOLAK | `defmodule`/`def`/`defp` adalah `call` polos (`call target: (identifier)`); walker tak bisa mencocokkan teks target untuk deklarasi — semua call akan jadi node semu |
| julia | DITOLAK | nama `function_definition` terkubur dua level tanpa field (`signature → call_expression → identifier`); walker hanya menelusuri field |
| r | DITOLAK | `f <- function(x)` adalah `binary_operator` ber-field `lhs`/`rhs`; deteksi fungsi-via-variabel walker membaca field **`value`** (hardcoded) — tak bisa diekspresikan; semua op biner top-level (`x + y`) ikut terdeklarasi |
| ocaml | DITOLAK | pemisah fungsi/nilai `let` butuh cek field `body`; walker membaca `value` (hardcoded). `let_binding`→function tanpa syarat akan memalsukan `let x = 5` |
| sql | DITOLAK | tak ada konstruk import (gate `import_rules` non-empty); grammar (DerekStride) menghasilkan `ERROR` pada `CREATE FUNCTION greet(name TEXT)` — fixture tak bisa parse-bersih |
| html, css, yaml, toml, json, dockerfile, markdown | DITOLAK | tak ada semantik function/class/import di grammar; memetakan heading/key/FROM ke kind kontrak akan memalsukan struktur |

## 3. Spec per bahasa baru (dari grammar nyata, bukan README)

| bahasa | deklarasi (node→kind) | import | call | catatan jujur |
|---|---|---|---|---|
| kotlin | class_declaration/object_declaration→class, function_declaration→function, package_header→module | `import_header` regex | — | grammar melebur interface ke class_declaration; **call_expression tak punya field callee → tanpa call** |
| swift | class_declaration→class (mencakup struct/enum), protocol_declaration→interface, function_declaration→function, protocol_function_declaration→method | `import_declaration` regex | — | **call_expression tak punya field callee → tanpa call** |
| scala | class_definition→class, trait_definition→interface, object_definition→class, enum/type_definition→type, function_definition/declaration→function, package_clause→module | `import_declaration` regex (field `path` per-segmen — akan terpotong) | call_expression.function | val/var_definition→variable (scope file/class saja) |
| lua | function_declaration→function | `function_call` match_text=require + regex | function_call (`name`; dot-index via `field`) | `M.greet` dinamai `greet` (name_fields `name`,`field`); tak ada konstanta (assignment tak ber-field nama langsung) |
| dart | class_definition/mixin_declaration→class, enum_declaration→type, function_signature→function (method_signature membungkusnya → 1 node/metode) | `import_specification` regex uri | — | grammar memisah signature/body jadi sibling; **invocation = rantai identifier+selector, tak ada node call** |
| haskell | header→module, data_type/newtype→type, class→interface, **signature**→function | `import` field `module` | apply.function | value-equation `function` BERBAGI node type dgn type-level `A -> B`; mendeklarasikannya memalsukan node (fallback identifier). Hanya signature yang mendeklarasi — fungsi tanpa signature tak menjadi node; call di dalam equation teratribusi ke scope terdekat |
| perl | package_statement→module, subroutine_declaration_statement→function | `use_statement` field `module`; `require_expression` regex | function_call_expression / method_call_expression | `our $VAR` tak diekstrak (field `variable` → scalar, bukan identifier) |

Gate dua arah baru: `call_types` kosong **wajib** untuk dart/kotlin/swift
(grammar terbukti tanpa field callee) dan **wajib non-empty** untuk sisanya
(`tests/test_ingest_languages.py::NO_CALL_FIELD_GRAMMARS`);
`test_call_refs_emitted` menegaskan dua arah pada fixture.

## 4. Engine (Rust kernel)

Verifikasi identitas grammar (metode SUB-93): hash blob git berkas `src/**`
pada rev bundled (dari provenance) vs setiap versi crate di crates.io
(sparse index + static.crates.io; `parser.c`/`grammar.json`/`node-types.json`
dll dibandingkan lengkap).

| bahasa | crate crates.io | hasil | pin |
|---|---|---|---|
| haskell | tree-sitter-haskell | **0.24.1 src/ IDENTIK** | `=0.24.1` |
| kotlin | tree-sitter-kotlin (0.2.8–0.3.8) | tak ada yg cocok (0/7 berkas src identik) | git rev `1852ea17` |
| swift | tree-sitter-swift (0.3.6–0.7.4) | tak ada yg cocok | git rev `d36d4527` |
| scala | tree-sitter-scala (0.22–0.26.3) | tak ada yg cocok (terdekat 5/7) | git rev `db390f31` |
| lua | tree-sitter-lua (0.0.12–0.5.0) | tak ada yg cocok | git rev `4fbec840` |
| perl | ts-parser-perl (1.1.0–2.0.3) | tak ada yg cocok (terdekat 10/13) | git rev `04477eeb`, `package="ts-parser-perl"` |
| swift | tree-sitter-swift (0.3.6–0.7.4) | **Python-fallback**: rev bundled `d36d4527` LEBIH BARU dari 0.7.4 (grammar.json/scanner.c berbeda) DAN tak punya `src/parser.c` ter-commit (pack menjalankan `tree-sitter generate` sendiri) → git pin tak bisa kompilasi; tak ada crate parse-identik yang buildable | — |
| dart | tree-sitter-dart | **Python-fallback**: rev bundled memakai binding lama (tree-sitter 0.22, tanpa `LANGUAGE` shim — tipe inkompatibel dgn runtime 0.25); crates.io 0.3.0 adalah fork (nielsenko) dgn `parser.c` BERBEDA dari grammar bundled | — |

Semua grammar baru berlisensi **MIT** (kolom `spdx` provenance) — kelas
atribusi yang sama dengan grammar tree-sitter yang sudah tercatat di NOTICE.

`LanguageSpec.engine` (`"rust"`/`"python"`, default fail-closed `"python"`)
kini bagian dari registry bersama: `tools/dump_language_registry.py`
mengemisikannya; `registry.rs` memakai `#[serde(default)]` dan
`supported_languages()` kernel hanya melaporkan bahasa `engine == "rust"`
(jujur: yang benar-benar punya grammar terkompilasi). Bahasa python-engine
tetap ter-ekstrak lewat fallback bridge (kernel menolak "no compiled grammar"
→ walker referensi), dipin oleh
`test_python_fallback_language_routes_to_reference`.

## 5. Fixture & test

`tests/fixtures/ingest/{kotlin,swift,scala,lua,dart,haskell,perl}/sample.*` —
semuanya parse-bersih (`parse_error == False`) dengan node function+class+
import nyata (dipin di `LANGUAGE_CASES`, `tests/test_ingest_extractor.py`).

## 6. Gates (bukti nyata)

- RED nyata: 354 gagal sebelum implementasi (registry/engine/fixture absen).
- `pytest tests/test_ingest_languages.py tests/test_ingest_extractor.py
  tests/test_kernel_rust_registry.py`: **1048 passed**.
- `cargo test --release` (parity.rs): 12 test hijau, 17 fixture dual-engine
  deterministik.
- `tests/test_kernel_rust.py`: paritas byte-identik bridge+CLI untuk 17
  bahasa dual-engine (34 kasus); swift+dart tertolak-dari-kernel + fallback
  byte-identik ke walker referensi.
- `tools/dump_language_registry.py --check` OK; dump deterministik lintas
  PYTHONHASHSEED 0/1/42 (subprocess, dipin test); subset ingest 486p ×3 seed
  identik.
- bench: `bench/codebase/run.py check` OK; `run` gate **PASS** setelah
  `--update-baseline` — refresh sah (preseden SUB-95): registrasi `.swift`
  membuat 2 file swift di repo tree-sitter-python yang sebelumnya
  `UNSUPPORTED_MEDIA_TYPE` kini ter-ekstrak (proposed 43→45, failed 32→30,
  nodes 1439→1445). `tests/test_codebase_bench.py` 14p hijau.
- Full suite + ruff + pyright: lihat entri board (angka final).
- Catatan scope: 2 kegagalan suite yang tersisa BUKAN milik SUB-102 —
  `test_no_ai_runtime` tersandung `src/know_your_soul/serve/superpower.py`
  (file WIP SUB-100, untracked, `os.execvpe` non-literal), dan test
  `test_serve_*`/`test_webview` (WIP SUB-100/101) menggantung di suite penuh
  sehingga di-exclude (set ignore yang sama dipakai sibling sendiri).

## 7. Residual terdokumentasi

- kotlin/swift/dart tanpa call refs (keterbatasan grammar, dua-arah ter-gate).
- haskell: hanya signature yang mendeklarasikan fungsi; call dalam equation
  teratribusi ke scope terdekat.
- dart, swift: engine python — setiap file memicu satu warning fallback
  (`rust kernel failed ... falling back`), by design; routing tanpa warning
  butuh ubahan extractor.py (di luar scope wave ini).
- lua: tanpa node konstanta; scala: val/var hanya di scope file/class.
