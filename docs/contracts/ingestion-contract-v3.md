# Ingestion Contract (v3)

Contract version: 3.0.0 (addendum)
Status: ACTIVE (2026-10-27)
Authority: operator concept recorded in
`.superpowers/sdd/2026-10-27-universal-knowledge-graph/MASTER-PLAN.md`
("Kontrak beku tetap 14 ops; kapabilitas baru lewat addendum kontrak v3") and
the W1 dispatch of 2026-10-27 (SUB-86).
Scope: this addendum **extends** the frozen fourteen-operation knowledge
surface (the ten v1 operations plus the four v2 G1 retrieval verbs) with two
**ingestion proposal** operations (`ingest_file`, `ingest_directory`), one new
edge kind (`bridges`), and the primary-node rules. The v1 contracts and the
v2 addendum in this directory are **unchanged** and remain byte-identical; the
fourteen frozen operations keep their exact semantics.

Any change to the semantics below requires a new contract version and operator
approval. This document is frozen upon publication.

## Shared guarantees

Both verbs produce **proposals only**:

- an invocation never mutates the event store, the projection, or the graph;
  the output is a proposal payload (proposed nodes, proposed typed edges,
  pending cross-file references) that is applied exclusively through the
  existing operator-gated propose/apply path, whose semantics are unchanged;
- exactly one final audit event per invocation (`knowledge.ingest_file`,
  `knowledge.ingest_directory`), successes and typed failures alike; the event
  records the root-relative input path, the sha256 of the input bytes, the
  extractor's `extraction_version`, and the proposal counts — never the input
  content;
- extraction is deterministic and model-free: a pure function of the input
  bytes and `extraction_version`, so identical bytes under an identical
  extractor version yield a byte-identical proposal (modulo the per-invocation
  audit id). The only third-party distributions an extractor may use are the
  parser-category allowlist recorded by the runtime guard (`tree-sitter` /
  `tree-sitter-language-pack` for AST parsing, `pypdf` for the PDF text
  layer); provider, model, embedding and vector packages stay fail-closed
  forbidden;
- the frozen token-budget semantics: `token_budget` below 128 is
  `BUDGET_TOO_SMALL`, above the configured maximum is `BUDGET_EXCEEDS_MAX`;
- path discipline: every input path is resolved against the ingestion roots
  fixed at startup; a path that escapes the roots (`..` segments, an absolute
  path outside every root, a symlink whose target leaves the root) fails typed
  with `PATH_OUTSIDE_ROOT`, a missing path with `PATH_NOT_FOUND`, and an
  unreadable one with `PATH_NOT_READABLE`. The v2 rule that no MCP schema
  carries a path-like property is amended for exactly these two verbs (see the
  adapter mapping); every other operation keeps it;
- unsupported input is never silently skipped: a file with no registered
  extractor fails typed with `UNSUPPORTED_MEDIA_TYPE`, and a file above the
  configured size cap fails typed with `FILE_TOO_LARGE` (the cap value is
  reported with the error).

## `knowledge.ingest_file(path, token_budget=1024)`

Ingests a single file under an ingestion root. `status: "proposal"` carries:

- `input`: the root-relative path, the sha256 and byte size of the file, and
  the detected media type / language;
- `nodes`: the proposed node records. Every node id is the stable
  `kind:sha256(path:kind:name:line)[:col]` form, so renames do not change
  identity; each record carries `kind`, `name`, the source span (`line`,
  `col` where applicable), the sha256 `content_hash` of its source span, and
  `primary` (see the primary-node rules);
- `edges`: the proposed typed links. Hierarchy rides `contains` rooted at the
  file node; cross-primary linkage rides `bridges`;
- `unresolved_refs`: cross-file references the extractor could not resolve
  locally, each with `status: "pending"` for a separate resolution pass —
  references are never guessed;
- `extraction_version`: the extractor version, versioned independently of the
  storage schema version.

The proposal payload is **atomic**: it never truncates. If the complete
response exceeds `token_budget`, the call fails typed with
`METADATA_EXCEEDS_BUDGET`.

## `knowledge.ingest_directory(path, recursive=true, max_files=1024, token_budget=1024)`

Walks a directory under an ingestion root and proposes for every ingestible
file in it. Traversal order is deterministic: root-relative paths sorted
bytewise. `recursive` is a boolean; `max_files` is bounded to `[1,65536]`.

`status: "proposal"` carries `files`, a list of per-file entries in traversal
order. Each entry is either a file proposal (the `ingest_file` payload without
the envelope) or a typed per-file failure (`UNSUPPORTED_MEDIA_TYPE`,
`FILE_TOO_LARGE`, `PATH_NOT_READABLE`): a per-file failure never aborts the
walk and never converts the batch into an exception. `stats` carries the
deterministic counts (`proposed`, `failed`, `skipped`), and `input` the
root-relative directory path. Reaching `max_files` stops the walk at the
sorted-order boundary and reports `guards_hit: ["max_files"]`.

The `files` list truncates tail-first under budget pressure with
`truncated: true`; the envelope (`input`, `stats`, `guards_hit`) never
truncates away.

## Edge kind `bridges`

`bridges` joins the relation vocabulary as the cross-primary link:

- both endpoints of a `bridges` edge must be **primary** nodes; a bridge with
  a non-primary endpoint fails typed with `INVALID_BRIDGE_ENDPOINT` at apply
  time;
- a `bridges` edge never participates in the `contains` hierarchy: subtree
  and depth walks ignore it, and it contributes no parent/child structure;
- the edge is stored directed (source → target) and denotes a symmetric
  association; degree-based ranking (v2 `god_nodes`) counts every link row
  exactly as before;
- bridges are created only through proposals. An extractor may propose a
  bridge only from evidence present in the input (for example a resolved
  cross-primary reference), and the evidence is recorded in the edge metadata
  — never inferred implicitly.

## Primary node rules

- A **primary** node (`primary: true`) is a first-class decomposition root,
  created when a large context (a repository, a document, a corpus) is
  decomposed. Many primaries coexist; each is single-responsibility.
- A primary may root an arbitrarily deep `contains` hierarchy; depth is a
  property of the edge chain, never a column on the node.
- A primary is never contained by another primary: cross-primary structure is
  expressed exclusively with `bridges`.
- Primary creation is proposal-only and operator-gated like every other
  write: extractors propose primaries, the existing propose/apply path
  applies them.

## Adapter mapping

| Verb | CLI subcommand | HTTP route | MCP tool |
|------|----------------|------------|----------|
| `ingest_file` | `know-your-soul ingest-file` | `POST /v1/knowledge/ingest/file` | `knowledge_ingest_file` |
| `ingest_directory` | `know-your-soul ingest-directory` | `POST /v1/knowledge/ingest/directory` | `knowledge_ingest_directory` |

MCP schemas are closed (`additionalProperties: false`) with the same ranges
as the service: `max_files` `[1,65536]`, `token_budget` `[128, max]`. The two
ingestion schemas carry exactly one path-like property (`path`) each — the
explicit, confined amendment of the v2 no-path rule; the graph root itself
stays fixed at startup.
