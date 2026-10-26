"""Single serialized projector and content-addressed, atomically activated generations.

Task 3 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 7.4 and matrix adapter A3.

Flow (all inside one non-blocking rebuild lock, so there is exactly one
writer per corpus root):

1. Replay knowledge-affecting domain events in ``knowledge_seq`` order up to
   the target watermark (the store's current knowledge high-water mark, or an
   explicit fixed mark for deterministic rebuild probes).
2. Assemble the Graphify-schema extraction, validate it strictly (build
   warnings are gate failures), build the directed graph with ``dedup=False``
   and partition it with the pinned partitioner and hub policy.
3. Write a content-addressed generation directory (``graph.json``,
   ``GRAPH_REPORT.md``, ``analysis.json``, ``labels.json``,
   ``signatures.json``, ``graph.html``, ``manifest.json``). Every artifact is
   written through an idempotent atomic writer and fsynced; unchanged bytes
   keep their inode. The manifest binds the schema version, the pinned
   partitioner, the hub policy, the generator version, the corpus
   high-water mark and per-artifact content hashes. Dates and commit stamps
   never enter a generation.
4. Validate the whole generation with the product read-model validator; a
   failure raises and nothing is activated.
5. Activate: one store transaction compares-and-swaps
   ``active_projection_version``/``active_projection_watermark`` — an older
   knowledge watermark can never replace a newer one — then the ``CURRENT``
   pointer is swapped atomically (same-directory temp file, ``os.replace``,
   directory fsync) and only when it actually changes.

Crash discipline: the pointer swap is last, so a crash mid-activation leaves
the previous generation serving; the store CAS then disagrees with the
pointer, and :meth:`GraphProjector.repair_if_stale` — also the
``AuditLog.recover_incomplete(projection_repair=...)`` hook — re-derives the
content-addressed generation and re-points ``CURRENT``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from soul_knowledge.domain import Event, EventKind, Node, RelationKind, canonical_json, new_id, quantize_score
from soul_knowledge.graphify_adapter import (
    ADAPTER_VERSION,
    CONFIDENCE_BY_RELATION,
    CONFIDENCE_SCORE,
    GRAPHIFY_PARTITIONER,
    PINNED_HUB_POLICY,
    GraphifyAdapter,
)
from soul_knowledge.importer import injective_file_id, injective_section_key
from soul_knowledge.read_model_validator import ReadModelValidator
from soul_knowledge.security import (
    DEFAULT_MAX_TEXT_LENGTH,
    SECRET_PATTERNS,
    _redaction_match_view,
    redact_text,
    sanitize_text,
)
from soul_knowledge.store import EventStore

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX-only fallback
    fcntl = None  # type: ignore[assignment]

__all__ = [
    "ARTIFACT_MAX_BYTES",
    "ARTIFACT_NAMES",
    "ENRICHED_SUMMARY_MAX_LENGTH",
    "FragmentCache",
    "GraphProjector",
    "OversizedArtifactError",
    "ProjectionError",
    "ProjectionReceipt",
    "ProjectionValidationError",
    "ProjectorBusy",
    "ShrinkGuardError",
    "StalePublicationError",
    "enriched_summary",
]

# manifest.json is activated last and verified by canonical equality, so it is
# not part of its own artifact hash set.
ARTIFACT_NAMES = (
    "graph.json",
    "GRAPH_REPORT.md",
    "analysis.json",
    "labels.json",
    "signatures.json",
    "graph.html",
    "manifest.json",
)
HASHED_ARTIFACTS = ARTIFACT_NAMES[:-1]

logger = logging.getLogger(__name__)

_MANIFEST_VERSION = 1

# Publish caps (R2). Both are fail-closed and checked *before/while* the
# generation is assembled, never after the full buffer exists:
# * ``PUBLISH_TEXT_MAX_CHARS`` is the per-field text cap already encoded in
#   ``security.sanitize_text``; the projection enforces it itself so an
#   oversized store field raises a typed ``OversizedArtifactError`` naming
#   the node and field instead of a bare security error mid-build.
# * ``ARTIFACT_MAX_BYTES`` bounds every published artifact; a generation
#   whose artifacts exceed it is rejected before any file is written.
PUBLISH_TEXT_MAX_CHARS = DEFAULT_MAX_TEXT_LENGTH
ARTIFACT_MAX_BYTES = 64 * 1024

# Summary enrichment (ruling "Opsi A", 2026-10-25): a projected node label is
# the heading plus the leading sentence(s) of its source section, sanitized
# and length-bounded. The cap matches the read-side ``ResultNode.summary``
# limit and is enforced on the WRITE side at a word boundary (wave-3 W3-5):
# the heading itself is capped too, so no downstream truncation can cut a
# term mid-word and the bench ceiling never counts an undeliverable label.
ENRICHED_SUMMARY_MAX_LENGTH = 256

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_LIST_ITEM_RE = re.compile(r"^(?:[-*+]|\d+[.)])\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_EMPHASIS_RE = re.compile(r"\*\*|__|`")
_WHITESPACE_RE = re.compile(r"\s+")
# SUB-43 S-2: the quarantine scan joins a node's publish-bound fields with a
# zero-width space — dropped by the redaction match view (so halves of a
# secret split ACROSS fields re-join), not matched by ``\s`` or any pinned
# pattern alphabet (so the join itself can never bridge a raw-pattern match).
_CROSS_FIELD_JOINER = "\u200b"
# The alphabet of the compact token view (``_secret_category`` third pass):
# every character outside it is dropped, so a secret fused with arbitrary
# join punctuation (``" — "``, ``"; "``, …) re-forms. ``_-.`` are kept
# because the github/slack/jwt pinned shapes carry them.
_COMPACT_TOKEN_ALPHABET = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")
# Mirrors the read-model validator's portable-header rule: absolute POSIX /
# drive paths may never appear in a published artifact, so they are masked
# out of enriched summaries instead of failing the whole generation.
_ABS_PATH_RE = re.compile(r"(?<![\w@:+/.-])/(?:[\w@.+-]+/){2,}[\w@.+-]+")
_DRIVE_PATH_RE = re.compile(r"(?<![\w@:+/.-])[A-Za-z]:[\\/]")


class ProjectionError(RuntimeError):
    """Base class for projection contract violations."""


class ProjectorBusy(ProjectionError):
    """Another projector holds this corpus root's rebuild lock."""


class StalePublicationError(ProjectionError):
    """An older knowledge watermark tried to replace a newer one."""


class ShrinkGuardError(ProjectionError):
    """The new generation would silently drop nodes relative to the active one."""


class ProjectionValidationError(ProjectionError):
    """The extraction or the built generation failed a validation gate."""


class OversizedArtifactError(ProjectionError):
    """A published field or artifact exceeds the fail-closed publish cap (R2).

    The store accepts free text up to its own limits, so an oversized node
    summary would otherwise surface as a bare ``OversizedPayloadError`` deep
    inside the build — after the whole generation was assembled in memory —
    and wedge every subsequent rebuild behind an untyped error. The publish
    path instead checks sizes *before/while* assembling, and raises this
    typed error naming the offending node field or artifact.
    """


class ProjectionReceipt(BaseModel):
    """The published projection identity (spec section 7.4)."""

    model_config = ConfigDict(frozen=True)

    projection_version: str
    source_knowledge_high_watermark: int
    content_hash: str
    generation_dir: str
    activated: bool
    node_count: int
    edge_count: int
    repaired: bool = False


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ------------------------------------------------------- summary enrichment
#
# Ruling "Opsi A" (operator, 2026-10-25): at projection time a node label is
# enriched from "heading" to "heading — leading sentence(s) of its source
# section", so the knowledge that lives in source bodies reaches the query
# payload without changing any frozen contract. Everything here is
# deterministic: the same store and the same source bytes always produce the
# same summary, sources are re-verified against their recorded content hash
# before they are read, and no model/statistic/embedding is involved.


def _section_blocks(body_lines: list[str]) -> list[str]:
    """Deterministic block stream of one section body.

    A block is a list item or a paragraph (consecutive non-list lines joined
    with a space); code fences are skipped.
    """
    blocks: list[str] = []
    paragraph: list[str] = []
    in_fence = False
    for line in body_lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            if paragraph:
                blocks.append(" ".join(paragraph))
                paragraph = []
            continue
        if _LIST_ITEM_RE.match(stripped):
            if paragraph:
                blocks.append(" ".join(paragraph))
                paragraph = []
            blocks.append(_LIST_ITEM_RE.sub("", stripped))
        else:
            paragraph.append(stripped)
    if paragraph:
        blocks.append(" ".join(paragraph))
    return blocks


_BOLD_LEAD_RE = re.compile(r"^\*\*(.+?)\*\*\s*[—–-]")


def _section_fragments(body_lines: list[str]) -> tuple[list[str], list[str]]:
    """A section's summary material: (defined-term leads, sentence stream).

    Knowledge notes define terms as ``**Term** — gloss`` items; the bold lead
    is the item's own mini-heading, so a digest of leads is the densest
    deterministic summary of a definition list. Prose sections have no leads
    and fall back to their sentence stream.
    """
    leads: list[str] = []
    sentences: list[str] = []
    for block in _section_blocks(body_lines):
        lead = _BOLD_LEAD_RE.match(block)
        if lead:
            leads.append(lead.group(1).strip())
        sentences.extend(part for part in (p.strip() for p in _SENTENCE_SPLIT_RE.split(block)) if part)
    return leads, sentences


def _source_section_fragments(text: str, source_file: str) -> dict[str, tuple[list[str], list[str]]]:
    """Map every node id a source file projects to its section's fragments.

    The id walk replicates the importer's injective re-scoping exactly
    (title handling, encoded heading keys, occurrence suffixes; wave-3
    W3-2), so the key set is precisely the node ids the importer derived
    from the same bytes.
    """
    lines = text.split("\n")
    headings: list[tuple[int, str, int]] = []
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2), index))
    file_id = injective_file_id(source_file)
    sections = headings
    result: dict[str, tuple[list[str], list[str]]] = {}
    if headings and headings[0][0] == 1:
        end = headings[1][2] if len(headings) > 1 else len(lines)
        result[file_id] = _section_fragments(lines[headings[0][2] + 1 : end])
        sections = headings[1:]
    elif not headings:
        result[file_id] = _section_fragments(lines)
    used: dict[str, int] = {}
    for position, (_level, heading, line_index) in enumerate(sections):
        key = injective_section_key(heading)
        occurrence = used.get(key, 0) + 1
        used[key] = occurrence
        section_id = f"{file_id}.{key}" if occurrence == 1 else f"{file_id}.{key}-n{occurrence}"
        end = sections[position + 1][2] if position + 1 < len(sections) else len(lines)
        result[section_id] = _section_fragments(lines[line_index + 1 : end])
    return result


def _clean_sentence(sentence: str) -> str:
    """Sanitize one sentence for publication: control/bidi/zero-width
    characters stripped, emphasis removed, whitespace collapsed, paths
    masked — and secrets redacted LAST (wave-3 W3-3: redact-then-strip
    reconstituted zero-width-split secrets into validator-matching shapes;
    the service read path has always stripped first, redacted last)."""
    cleaned = "".join(char for char in sentence if unicodedata.category(char) not in {"Cc", "Cf"})
    cleaned = _EMPHASIS_RE.sub("", cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    cleaned = _ABS_PATH_RE.sub("[path]", cleaned)
    cleaned = _DRIVE_PATH_RE.sub("[path]", cleaned)
    return redact_text(cleaned).strip()


def _truncate_at_word_boundary(text: str, max_length: int) -> str:
    """Cap *text* at *max_length*, cutting at the last whole word (W3-5).

    Write-side truncation on a word boundary: the published label is then
    byte-identical to what the read-side 256-char limit can deliver, so the
    bench ceiling never counts an undeliverable label. A single word longer
    than the cap is hard-cut — nothing better exists.
    """
    if len(text) <= max_length:
        return text
    window = text[: max_length + 1]
    boundary = window.rfind(" ")
    if boundary > 0:
        return window[:boundary]
    return text[:max_length]


def enriched_summary(
    heading: str,
    fragments: tuple[list[str], list[str]],
    *,
    max_length: int = ENRICHED_SUMMARY_MAX_LENGTH,
) -> str:
    """``heading — defined-term digest (+ first prose sentence)``, capped.

    Phase 1 greedily appends the section's defined-term leads (``; ``-joined):
    ``**Term** — gloss`` leads are the section's own mini-headings, i.e.
    label-like knowledge, not raw prose. Phase 2 appends at most the FIRST
    sentence that does not merely restate an included lead — the ruling's
    "kalimat pertama". Only whole fragments are appended, in source order; a
    fragment that would overflow the cap ends the fill, and raw body text
    beyond the first sentence is never published. The heading itself is
    capped on the write side at a word boundary (W3-5), so the returned
    label never exceeds *max_length*.
    """
    heading = _truncate_at_word_boundary(heading, max_length)
    body = ""
    included: list[str] = []
    for lead in fragments[0]:
        cleaned = _clean_sentence(lead)
        if not cleaned:
            continue
        candidate = f"{body}; {cleaned}" if body else cleaned
        if len(heading) + len(" — ") + len(candidate) > max_length:
            break
        body = candidate
        included.append(cleaned)
    for sentence in fragments[1]:
        cleaned = _clean_sentence(sentence)
        if not cleaned or any(cleaned.startswith(lead) for lead in included):
            continue
        candidate = f"{body} {cleaned}" if body else cleaned
        if len(heading) + len(" — ") + len(candidate) > max_length:
            break
        body = candidate
        break  # first prose sentence only (Opsi A: "kalimat pertama")
    return f"{heading} — {body}" if body else heading


class FragmentCache:
    """Content-hash fragment cache; one process per root, portable across roots.

    Keys are ``sha256(relative path + content hash)`` inside a namespace pinned
    to the fragment-generator version: mtime never participates (row 4), a
    generator bump is a full re-derivation (row 5), the cache directory can be
    moved between absolute roots and still hit (row 8), and ``prune`` bounds
    the entry count by the live document set (row 9).
    """

    def __init__(self, cache_root: str | Path, *, namespace: str | None = None) -> None:
        from soul_knowledge.fragment_generator import FRAGMENT_GENERATOR_VERSION

        self._namespace = namespace or f"fragment-generator-{FRAGMENT_GENERATOR_VERSION}"
        self._dir = Path(cache_root) / self._namespace

    @property
    def namespace(self) -> str:
        return self._namespace

    def key_for(self, rel_path: str, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()
        return _sha256(f"soul-fragment\0{rel_path}\0{digest}".encode())

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._dir / f"{key}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None

    def put(self, key: str, fragment: Mapping[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / f"{key}.json.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(fragment))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self._dir / f"{key}.json")

    def prune(self, live_keys: list[str]) -> int:
        """Drop every entry outside *live_keys*; returns the number removed."""
        live = set(live_keys)
        removed = 0
        if not self._dir.is_dir():
            return 0
        for path in sorted(self._dir.glob("*.json")):
            if path.stem not in live:
                path.unlink()
                removed += 1
        return removed

    def entries(self) -> list[str]:
        if not self._dir.is_dir():
            return []
        return sorted(path.stem for path in self._dir.glob("*.json"))


class GraphProjector:
    """The single serialized projector for one corpus root."""

    def __init__(
        self,
        store: EventStore,
        *,
        output_root: str | Path,
        adapter: GraphifyAdapter | None = None,
        validator: ReadModelValidator | None = None,
        cache: FragmentCache | None = None,
        corpus_label: str = "soul-corpus",
        allow_shrink: bool = False,
        max_artifact_bytes: int = ARTIFACT_MAX_BYTES,
    ) -> None:
        if max_artifact_bytes < 1:
            raise ValueError("max_artifact_bytes must be positive")
        self._store = store
        self._output_root = Path(output_root)
        self._adapter = adapter if adapter is not None else GraphifyAdapter()
        self._validator = (
            validator
            if validator is not None
            else ReadModelValidator(extra_forbidden_roots=(str(self._output_root.resolve()),))
        )
        self._cache = cache
        self._corpus_label = corpus_label
        self._allow_shrink = allow_shrink
        self._max_artifact_bytes = max_artifact_bytes

    # ------------------------------------------------------------------ lock

    @contextmanager
    def hold_lock(self) -> Iterator[None]:
        """The single-writer rebuild lock (flock; released on process death).

        A competing projector raises :class:`ProjectorBusy` instead of
        interleaving a publication; work committed while the lock is held is
        drained by the next rebuild (matrix row 13).
        """
        self._output_root.mkdir(parents=True, exist_ok=True)
        (self._output_root / "generations").mkdir(exist_ok=True)
        fd = os.open(self._output_root / "projector.lock", os.O_CREAT | os.O_RDWR, 0o644)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    raise ProjectorBusy("another projector holds the rebuild lock for this corpus root") from error
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    # ------------------------------------------------------------- replay

    def _check_artifact_size(self, name: str, data: bytes) -> None:
        """R2 write-side cap: reject an oversized artifact the moment it is
        assembled, before any byte of the generation reaches disk."""
        if len(data) > self._max_artifact_bytes:
            raise OversizedArtifactError(
                f"artifact {name} is {len(data)} bytes, over the {self._max_artifact_bytes}-byte publish cap"
            )

    @staticmethod
    def _check_publish_text(value: str, *, field: str, node_id: str) -> None:
        """R2 write-side cap on free text: typed and eager, naming the node."""
        if len(value) > PUBLISH_TEXT_MAX_CHARS:
            raise OversizedArtifactError(
                f"node {node_id!r} field {field} holds {len(value)} chars, "
                f"over the {PUBLISH_TEXT_MAX_CHARS}-char publish cap"
            )

    @classmethod
    def _sanitize_entry(cls, value: str, node_id: str, field: str) -> str:
        """Typed-cap check plus publish sanitization for one free-text entry.

        Wave-3 W3-6: after ``sanitize_text`` (which keeps ``\\t\\n\\r``),
        every remaining whitespace run is collapsed to a single space, so no
        published field can carry raw newlines/tabs that read surfaces would
        fuse words over or that would pollute markdown consumers.
        """
        cls._check_publish_text(value, field=field, node_id=node_id)
        return _WHITESPACE_RE.sub(" ", sanitize_text(value)).strip()

    def _source_sentences(self, nodes: dict[str, Node]) -> dict[str, tuple[list[str], list[str]]]:
        """node_id -> section fragments, hash-verified (Opsi A).

        A node is enriched only when its recorded source version resolves to
        readable bytes whose sha256 still matches the store's content hash; a
        missing, moved or drifted source degrades to the heading-only label
        (fail-safe, and deterministic for a given store + filesystem state).
        """
        wanted: dict[str, list[tuple[str, str]]] = {}
        for node_id in sorted(nodes):
            node = nodes[node_id]
            source_file = node.payload.get("source_file")
            if not isinstance(source_file, str) or not source_file or "://" in source_file:
                continue
            wanted.setdefault(node.source_version_id, []).append((node_id, source_file))
        if not wanted:
            return {}
        resolved: dict[str, tuple[list[str], list[str]]] = {}
        with self._store.read() as conn:
            for version_id in sorted(wanted):
                row = conn.execute(
                    "SELECT source_uri, content_sha256 FROM source_version WHERE source_version_id = ?",
                    (version_id,),
                ).fetchone()
                if row is None:
                    continue
                for node_id, source_file in wanted[version_id]:
                    uri = Path(str(row["source_uri"]))
                    path = uri if uri.is_file() else uri / source_file
                    try:
                        data = path.read_bytes()
                    except OSError:
                        continue
                    if _sha256(data) != str(row["content_sha256"]):
                        continue
                    text = unicodedata.normalize("NFC", data.decode("utf-8", errors="replace"))
                    text = text.replace("\r\n", "\n").replace("\r", "\n")
                    resolved[node_id] = _source_section_fragments(text, source_file).get(node_id, ([], []))
        return resolved

    # ------------------------------------------------------------- replay

    @staticmethod
    def _secret_category(text: str) -> str | None:
        """The shared SECRET_PATTERNS category matching *text*, or ``None``.

        Wave-3 W3-4: matching runs over the raw text AND the normalized
        redaction match view (NFKC + control/format strip + homoglyph
        fold), so a zero-width-split, fullwidth or homoglyph-obfuscated
        secret shape in a node field is quarantined exactly like its
        plaintext form.

        SUB-43 S-2: a third pass runs over the *compact token view* — the
        match view reduced to the pinned-pattern alphabet (letters, digits,
        ``_-.``) with every separator and punctuation character dropped.
        A secret split across field boundaries or fused into the enriched
        label by the ``" — "`` join re-forms there, so both halves of one
        key can never publish side by side in a single node. Patterns that
        require whitespace or ``[:=]`` (bearer, credential assignment,
        private-key blocks) simply cannot match this view; the split-N-way
        family beyond one join point is a documented design boundary.
        """
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text) is not None:
                return name
        view = _redaction_match_view(text)
        if view != text:
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(view) is not None:
                    return name
        compact = "".join(char for char in view if char in _COMPACT_TOKEN_ALPHABET)
        if compact != view:
            for name, pattern in SECRET_PATTERNS:
                if pattern.search(compact) is not None:
                    return name
        return None

    def _joined_secret_category(self, values: Sequence[str]) -> str | None:
        """Secret category of the cross-field joined views, or ``None``.

        SUB-43 S-2: a secret split ACROSS fields matches no single field.
        The full join (field order) catches halves that land adjacently; the
        pairwise both-orders joins catch a two-way split with unrelated
        fields in between, whichever field holds the head. Splits wider
        than two fields are the documented split-N-way design boundary.
        """
        if len(values) < 2:
            return None
        hit = self._secret_category(_CROSS_FIELD_JOINER.join(values))
        if hit is not None:
            return hit
        for head in range(len(values)):
            for tail in range(len(values)):
                if head == tail:
                    continue
                hit = self._secret_category(values[head] + _CROSS_FIELD_JOINER + values[tail])
                if hit is not None:
                    return hit
        return None

    def _quarantine_secret_nodes(
        self,
        node_dicts: dict[str, dict[str, Any]],
        raw_fields: dict[str, tuple[tuple[str, str], ...]] | None = None,
    ) -> list[tuple[str, str, str]]:
        """Wave-2 F7: fail closed *per node*, not per generation.

        A node whose publish-bound fields carry a secret-shaped string is
        removed from the extraction (its edges drop with it via the ``_edge``
        membership check), so one poisoned write can never take down every
        read behind the whole-generation validator gate. The removal is the
        strongest form of the invariant: the secret never reaches any
        artifact. Returns ``(node_id, field, category)`` triples for the
        typed audit/warning — never any secret material.

        SUB-35-F2: the raw (pre-sanitization) free-text fields are scanned
        too, because publish sanitization collapses Zs-class separators to
        plain ASCII spaces (W3-6) — a split secret would otherwise reach the
        scan already re-shaped into a form the match view keeps split.

        SUB-43 S-2: alongside the per-field scans, each node is scanned over
        two deterministic joined views (raw fields; publish-bound fields) so
        a secret split ACROSS fields — half in ``summary``, half in
        ``context_tags``, or fused into the enriched label by the ``" — "``
        join — quarantines the node even though every field alone is clean.
        The joiner is a zero-width space: the match view drops it (halves
        re-join) while no raw pattern can bridge it (no false cross-field
        matches from the join itself).
        """
        quarantined: list[tuple[str, str, str]] = []
        for node_id in sorted(node_dicts):
            offending_field: str | None = None
            offending_category: str | None = None
            if raw_fields is not None:
                fields = raw_fields.get(node_id, ())
                for field, raw_value in fields:
                    hit = self._secret_category(raw_value)
                    if hit is not None:
                        offending_field = field
                        offending_category = hit
                        break
                # SUB-43 S-2: scan the deterministic joined views too — a
                # secret split ACROSS fields matches no single field.
                if offending_field is None and len(fields) > 1:
                    hit = self._joined_secret_category([value for _, value in fields])
                    if hit is not None:
                        offending_field = "+".join(sorted({field for field, _ in fields}))
                        offending_category = hit

            def _scan(value: Any) -> str | None:
                if isinstance(value, str):
                    return self._secret_category(value)
                if isinstance(value, (list, tuple)):
                    for item in value:
                        hit = _scan(item)
                        if hit is not None:
                            return hit
                if isinstance(value, Mapping):
                    for item in value.values():
                        hit = _scan(item)
                        if hit is not None:
                            return hit
                return None

            if offending_field is None:
                for field, value in node_dicts[node_id].items():
                    hit = _scan(value)
                    if hit is not None:
                        offending_field = field
                        offending_category = hit
                        break
            if offending_field is None:
                # SUB-43 S-2: same joined views over the publish-bound
                # (sanitized) fields — covers splits introduced or kept by
                # enrichment/sanitization itself (e.g. the ``" — "`` label
                # join), in deterministic field order.
                publish_strings: list[str] = []
                for field in sorted(node_dicts[node_id]):
                    value = node_dicts[node_id][field]
                    if isinstance(value, str):
                        publish_strings.append(value)
                    elif isinstance(value, (list, tuple)):
                        publish_strings.extend(item for item in value if isinstance(item, str))
                if len(publish_strings) > 1:
                    hit = self._joined_secret_category(publish_strings)
                    if hit is not None:
                        offending_field = "<joined-publish-fields>"
                        offending_category = hit
            if offending_field is not None and offending_category is not None:
                del node_dicts[node_id]
                quarantined.append((node_id, offending_field, offending_category))
        return quarantined

    def _record_quarantine(
        self, quarantined: list[tuple[str, str, str]], *, projection_version: str, watermark: int
    ) -> None:
        """One typed, hash-chained audit event per quarantining rebuild.

        Rides the store's append-only event log (``kind='audit'``,
        ``knowledge_affecting=False``) through the existing public
        ``insert_event`` API — tamper-evident, and never an invocation
        journal (spec 7.7). The payload names node ids, fields and secret
        *categories* only; secret material is never persisted.
        """
        entries = [
            {"node_id": node_id, "field": field, "category": category} for node_id, field, category in quarantined
        ]
        logger.warning(
            "projection quarantined %d node(s) carrying secret-shaped content: %s",
            len(entries),
            ", ".join(entry["node_id"] for entry in entries),
        )
        with self._store.transaction() as conn:
            self._store.insert_event(
                conn,
                event_id=new_id(),
                kind="audit",
                event_type="security.projection_quarantine",
                knowledge_affecting=False,
                actor="projector",
                occurred_at=_now_iso(),
                payload={
                    "projection_version": projection_version,
                    "source_knowledge_high_watermark": watermark,
                    "quarantined": entries,
                },
            )

    def _knowledge_events(self, upto_knowledge_seq: int) -> list[Event]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM event_log WHERE kind = 'domain' AND knowledge_affecting = 1 "
                "AND knowledge_seq IS NOT NULL AND knowledge_seq <= ? ORDER BY event_seq",
                (upto_knowledge_seq,),
            ).fetchall()
        return [Event.model_validate(json.loads(str(row["payload_json"]))) for row in rows]

    def _extraction(self, events: list[Event]) -> tuple[dict[str, Any], list[tuple[str, str, str]]]:
        nodes: dict[str, Node] = {}
        versions: dict[str, int] = {}
        relations = []
        for event in events:
            if event.kind is EventKind.NODE_UPSERT and event.node is not None:
                versions[event.node_id] = versions.get(event.node_id, 0) + 1
                nodes[event.node_id] = event.node
            elif event.kind is EventKind.NODE_RETIRE:
                nodes.pop(event.node_id, None)
                versions.pop(event.node_id, None)
            elif event.kind is EventKind.RELATION_ADD and event.relation is not None:
                relations.append(event.relation)

        node_dicts: dict[str, dict[str, Any]] = {}
        raw_fields: dict[str, tuple[tuple[str, str], ...]] = {}
        sentences_by_node = self._source_sentences(nodes)
        for node_id in sorted(nodes):
            node = nodes[node_id]
            # Publish-time sanitization (audit wave-1 finding F6): free text is
            # stripped of control/bidi/zero-width characters through
            # ``security.sanitize_text`` so no published artifact (graph.json,
            # graph.html, GRAPH_REPORT.md) can carry trojan-source glyphs raw.
            # Identifiers are untouched; the read-model validator already fails
            # closed on ids outside the semantic charset.
            #
            # Wave-3: raw whitespace controls that survive sanitize_text are
            # collapsed to single spaces (W3-6 — read surfaces fused words over
            # raw newlines), and redaction runs LAST (W3-3/W3-4 — a heading
            # whose secret shape only appears after normalization is still
            # redacted before it can reach an artifact).
            #
            # R2: the 64 KiB text cap is enforced here, eagerly and typed, so
            # an oversized store field fails the build as an
            # ``OversizedArtifactError`` naming the node before the whole
            # generation is assembled in memory.
            self._check_publish_text(node.summary, field="summary", node_id=node_id)
            summary = self._sanitize_entry(node.summary, node_id, "summary") if node.summary else ""
            if summary:
                # Opsi A: heading + defined-term digest / leading sentences.
                # The heading is NOT pre-redacted here: a secret-shaped
                # heading is *quarantined* (per-node fail-closed + typed
                # audit, wave-2 F7) — redacting it would silently keep the
                # node and lose the security event. Enriched body sentences
                # are redacted inside ``_clean_sentence``.
                summary = enriched_summary(summary, sentences_by_node.get(node_id, ([], [])))
            raw_source = str(
                node.payload.get("source_file") or (node.provenance[-1] if node.provenance else "soul://store")
            )
            # SUB-35-F2: keep the pre-sanitization free text for the
            # quarantine scan — W3-6 whitespace collapsing would otherwise
            # reshape a Zs-split secret before the scan sees it.
            raw_fields[node_id] = (
                ("summary", node.summary or ""),
                ("source_file", raw_source),
                *(("provenance", entry) for entry in node.provenance),
                *(("context_tags", tag) for tag in node.context_tags),
            )
            source_file = self._sanitize_entry(raw_source, node_id, "source_file")
            node_dicts[node_id] = {
                "id": node.node_id,
                "label": summary or node.node_id,
                "file_type": "document",
                "source_file": source_file,
                "_origin": "semantic",
                "domain": node.domain.value,
                "kind": node.kind.value,
                "lifecycle_status": node.lifecycle_status.value,
                "version": versions.get(node_id, node.version),
                "source_version_id": node.source_version_id,
                "provenance": [self._sanitize_entry(entry, node_id, "provenance") for entry in node.provenance],
                "context_tags": [self._sanitize_entry(tag, node_id, "context_tags") for tag in node.context_tags],
                "scores": node.scores.quantized(),
                "confidence": str(quantize_score(node.scores.confidence)),
                "professional_level": node.professional_level,
            }

        # Wave-2 F7(a): per-node fail-closed. A node whose publish-bound
        # fields carry a secret-shaped string is quarantined from the
        # extraction *before* any artifact exists — the rest of the graph
        # keeps publishing and serving reads. The whole-generation validator
        # stays as the backstop for anything that slips past this scan.
        quarantined = self._quarantine_secret_nodes(node_dicts, raw_fields)

        edges: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()

        def _edge(source: str, target: str, relation_kind: str) -> None:
            key = (source, target, relation_kind)
            if key in seen or source not in node_dicts or target not in node_dicts:
                return
            seen.add(key)
            confidence = CONFIDENCE_BY_RELATION.get(RelationKind(relation_kind), "EXTRACTED")
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "relation": relation_kind,
                    "confidence": confidence,
                    "confidence_score": CONFIDENCE_SCORE[confidence],
                    "weight": 1.0,
                    "source_file": node_dicts[source]["source_file"],
                }
            )

        for node_id in sorted(nodes):
            parent_id = nodes[node_id].parent_id
            if parent_id is not None:
                _edge(parent_id, node_id, "parent_of")
        for relation in relations:
            _edge(relation.source_node_id, relation.target_node_id, relation.kind.value)

        return {"nodes": list(node_dicts.values()), "edges": edges, "hyperedges": []}, quarantined

    # ----------------------------------------------------------------- build

    def _build_generation(
        self,
        extraction: dict[str, Any],
        watermark: int,
        *,
        allow_shrink: bool,
        quarantined_count: int = 0,
    ) -> tuple[Path, dict[str, Any], int, int]:
        errors = self._adapter.validate_extraction_strict(extraction)
        if errors:
            raise ProjectionValidationError("extraction failed the build gate: " + "; ".join(errors))

        node_count = len(extraction["nodes"])
        if not allow_shrink:
            active = self.active_manifest()
            # Wave-2 F7(a): security-quarantined nodes are deliberate, loudly
            # audited removals — they are accounted into the shrink budget so
            # one poisoned node cannot wedge publication behind ShrinkGuard
            # either. Shrinkage *beyond* the quarantine still fails closed.
            if active is not None and node_count + quarantined_count < int(active["node_count"]):
                raise ShrinkGuardError(
                    f"refusing to publish {node_count} nodes over the active generation's "
                    f"{active['node_count']} without an explicit allow_shrink"
                )

        graph = self._adapter.build_graph(extraction)
        communities = self._adapter.partition(graph)
        signatures = self._adapter.community_signatures(communities)
        member_sig = {member: sig for sig, members in signatures.items() for member in members}
        cohesion = self._adapter.cohesion_by_signature(graph, communities)
        god_nodes = self._adapter.god_nodes(graph)

        node_list: list[dict[str, Any]] = []
        for node_id, attrs in graph.nodes(data=True):
            item = {"id": node_id, **dict(attrs)}
            sig = member_sig.get(str(node_id))
            if sig is not None:
                item["community_sig"] = sig
            node_list.append(item)
        link_list = [dict(edge) for edge in extraction["edges"]]

        graph_data = {
            "directed": True,
            "multigraph": False,
            "graph": {
                "schema_version": 1,
                "generator": "soul-knowledge",
                "partitioner": GRAPHIFY_PARTITIONER["name"],
            },
            "nodes": sorted(node_list, key=canonical_json),
            "links": sorted(link_list, key=canonical_json),
            "hyperedges": [],
        }
        artifacts: dict[str, bytes] = {"graph.json": (canonical_json(graph_data) + "\n").encode("utf-8")}
        self._check_artifact_size("graph.json", artifacts["graph.json"])

        confidences = [str(link["confidence"]) for link in link_list]
        report_lines = [
            "# SOUL Knowledge Graph Report",
            "",
            f"corpus: {self._corpus_label}",
            f"nodes: {len(node_list)}",
            f"edges: {len(link_list)}",
            f"communities: {len(signatures)}",
            "confidence: "
            + " ".join(f"{name}={confidences.count(name)}" for name in ("EXTRACTED", "INFERRED", "AMBIGUOUS")),
            "",
            "## Communities",
            *(f"- {sig}: {len(members)} nodes" for sig, members in sorted(signatures.items())),
            "",
            "## God Nodes",
            *(f"- {node['id']} (degree {node['degree']})" for node in god_nodes),
            "",
        ]
        artifacts["GRAPH_REPORT.md"] = "\n".join(report_lines).encode("utf-8")
        self._check_artifact_size("GRAPH_REPORT.md", artifacts["GRAPH_REPORT.md"])
        artifacts["analysis.json"] = (
            canonical_json(
                {
                    "counts": {
                        "nodes": len(node_list),
                        "edges": len(link_list),
                        "communities": len(signatures),
                    },
                    "god_nodes": god_nodes,
                    "structural_diagnostics": {
                        "cohesion_by_community_sig": cohesion,
                        "note": "Structural diagnostic only; never a knowledge-quality score (spec section 9).",
                    },
                }
            )
            + "\n"
        ).encode("utf-8")
        self._check_artifact_size("analysis.json", artifacts["analysis.json"])
        artifacts["labels.json"] = (canonical_json({sig: "UNREVIEWED" for sig in sorted(signatures)}) + "\n").encode(
            "utf-8"
        )
        self._check_artifact_size("labels.json", artifacts["labels.json"])
        artifacts["signatures.json"] = (canonical_json(dict(sorted(signatures.items()))) + "\n").encode("utf-8")
        self._check_artifact_size("signatures.json", artifacts["signatures.json"])

        artifact_hashes = {name: _sha256(data) for name, data in artifacts.items()}
        graph_content_hash = artifact_hashes["graph.json"]
        content_hash = _sha256(
            canonical_json(
                {
                    "adapter_version": ADAPTER_VERSION,
                    "artifacts": artifact_hashes,
                    "fragment_fingerprint": self._adapter.fragment_fingerprint,
                    "generator_version": self._adapter.generator_version,
                    "hub_policy": PINNED_HUB_POLICY,
                    "partitioner": GRAPHIFY_PARTITIONER,
                    "schema_version": 1,
                    "source_knowledge_high_watermark": watermark,
                }
            ).encode("utf-8")
        )
        projection_version = f"gen-{content_hash[:24]}"

        # graph.html is a derived, generation-keyed view; it embeds the
        # projection version, so it is hashed into the manifest but is not part
        # of the content identity (matrix row 35: never a source of truth).
        html = (
            '<!doctype html>\n<html><head><meta charset="utf-8"><title>SOUL Knowledge Graph</title></head>'
            "<body>\n"
            f"<h1>SOUL Knowledge Graph</h1>\n<p>projection: {projection_version}</p>\n"
            f"<p>nodes: {len(node_list)} edges: {len(link_list)} communities: {len(signatures)}</p>\n"
            f'<script type="application/json" id="graph">\n{json.dumps(graph_data, sort_keys=True)}\n</script>\n'
            "</body></html>\n"
        )
        artifacts["graph.html"] = html.encode("utf-8")
        self._check_artifact_size("graph.html", artifacts["graph.html"])
        artifact_hashes["graph.html"] = _sha256(artifacts["graph.html"])

        manifest: dict[str, Any] = {
            "manifest_version": _MANIFEST_VERSION,
            "projection_version": projection_version,
            "content_hash": content_hash,
            "source_knowledge_high_watermark": watermark,
            "schema_version": 1,
            "accepted_schema_versions": sorted(self._validator.accepted_schema_versions),
            "partitioner": GRAPHIFY_PARTITIONER,
            "hub_policy": PINNED_HUB_POLICY,
            "generator_version": self._adapter.generator_version,
            "adapter_version": ADAPTER_VERSION,
            "fragment_fingerprint": self._adapter.fragment_fingerprint,
            "cache_namespace": self._cache.namespace if self._cache is not None else None,
            "graph_content_hash": graph_content_hash,
            "artifacts": artifact_hashes,
            "node_count": len(node_list),
            "edge_count": len(link_list),
            "multigraph": False,
        }
        artifacts["manifest.json"] = (canonical_json(manifest) + "\n").encode("utf-8")
        self._check_artifact_size("manifest.json", artifacts["manifest.json"])

        generation_dir = self._output_root / "generations" / content_hash
        generation_dir.mkdir(parents=True, exist_ok=True)
        for name in ARTIFACT_NAMES:
            self._write_if_changed(generation_dir / name, artifacts[name])
        dir_fd = os.open(generation_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        # Durability (SUB-29 side finding, closed in wave 3): the generation
        # directory's own fsync persists its *contents*; the directory
        # *entry* for a brand-new generation persists only when the parent
        # chain is fsynced — generations/ (its mkdir entry) and the output
        # root (generations/' entry). Without this a power loss can strand a
        # fully written generation behind a missing directory entry.
        for ancestor in (generation_dir.parent, self._output_root):
            ancestor_fd = os.open(ancestor, os.O_RDONLY)
            try:
                os.fsync(ancestor_fd)
            finally:
                os.close(ancestor_fd)
        return generation_dir, manifest, len(node_list), len(link_list)

    def _write_if_changed(self, path: Path, data: bytes) -> bool:
        """Idempotent atomic write: unchanged bytes keep inode and mtime (row 24).

        The R2 write-side cap is re-checked here, at the actual write point,
        so no artifact over the publish cap can reach disk even if a future
        caller bypasses the build-time checks.
        """
        self._check_artifact_size(path.name, data)
        try:
            if path.read_bytes() == data:
                return False
        except OSError:
            pass
        tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True

    # ------------------------------------------------------------- activation

    def rebuild(
        self,
        *,
        upto_knowledge_seq: int | None = None,
        allow_shrink: bool | None = None,
        activate: bool = True,
    ) -> ProjectionReceipt:
        """Replay, build, validate and (by default) activate a generation."""
        with self.hold_lock():
            watermark = self._store.knowledge_high_watermark() if upto_knowledge_seq is None else upto_knowledge_seq
            extraction, quarantined = self._extraction(self._knowledge_events(watermark))
            generation_dir, manifest, node_count, edge_count = self._build_generation(
                extraction,
                watermark,
                allow_shrink=self._allow_shrink if allow_shrink is None else allow_shrink,
                quarantined_count=len(quarantined),
            )
            report = self._validator.validate(generation_dir, manifest)
            if not report.ok:
                raise ProjectionValidationError(
                    "generation failed the read-model validator: " + "; ".join(report.errors)
                )
            if quarantined:
                # The generation passed every gate with the poisoned nodes
                # excluded — the security event is real, so it is journaled.
                self._record_quarantine(
                    quarantined,
                    projection_version=str(manifest["projection_version"]),
                    watermark=watermark,
                )
            if activate:
                self._activate(manifest, generation_dir)
            return ProjectionReceipt(
                projection_version=str(manifest["projection_version"]),
                source_knowledge_high_watermark=watermark,
                content_hash=str(manifest["content_hash"]),
                generation_dir=str(generation_dir),
                activated=activate,
                node_count=node_count,
                edge_count=edge_count,
            )

    def _activate(self, manifest: Mapping[str, Any], generation_dir: Path) -> None:
        """CAS the store pointers, then atomically swap ``CURRENT`` (fsynced).

        The store transaction rejects an older knowledge watermark, so
        out-of-order publication is impossible; the pointer swap is idempotent
        and happens only after the CAS committed.
        """
        watermark = int(manifest["source_knowledge_high_watermark"])
        with self._store.transaction() as conn:
            row = conn.execute("SELECT value FROM system_state WHERE key = 'active_projection_watermark'").fetchone()
            current = int(str(row["value"])) if row is not None else -1
            if watermark < current:
                raise StalePublicationError(f"knowledge watermark {watermark} cannot replace the active {current}")
            for key, value in (
                ("active_projection_version", str(manifest["projection_version"])),
                ("active_projection_watermark", str(watermark)),
            ):
                conn.execute(
                    "INSERT INTO system_state (key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                    (key, value, _now_iso()),
                )
        pointer = self._output_root / "CURRENT"
        desired = generation_dir.name + "\n"
        try:
            if pointer.read_text(encoding="utf-8") == desired:
                return
        except OSError:
            pass
        tmp = self._output_root / f"CURRENT.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(desired)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, pointer)
        dir_fd = os.open(self._output_root, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    # ----------------------------------------------------------------- status

    def active_generation_dir(self) -> Path | None:
        pointer = self._output_root / "CURRENT"
        try:
            name = pointer.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if not name or len(name) != 64 or any(char not in "0123456789abcdef" for char in name):
            return None
        generation = self._output_root / "generations" / name
        return generation if generation.is_dir() else None

    def active_manifest(self) -> dict[str, Any] | None:
        generation = self.active_generation_dir()
        if generation is None:
            return None
        try:
            manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None
        return manifest if isinstance(manifest, dict) else None

    def repair_if_stale(self) -> ProjectionReceipt:
        """Repair a stale or torn publication; a no-op when already current.

        Stale means: no readable active manifest, the active manifest's
        knowledge watermark differs from the store's, the store's projection
        pointer disagrees with the manifest, or the generation no longer
        passes the read-model validator. This is the
        ``AuditLog.recover_incomplete(projection_repair=...)`` hook.
        """
        with self.hold_lock():
            store_watermark = self._store.knowledge_high_watermark()
            manifest = self.active_manifest()
            stale = manifest is None
            if manifest is not None:
                if int(manifest.get("source_knowledge_high_watermark", -1)) != store_watermark:
                    stale = True
                elif manifest.get("projection_version") != self._store.projection_version():
                    stale = True
                else:
                    generation = self.active_generation_dir()
                    assert generation is not None  # manifest implies a directory
                    if not self._validator.validate(generation, manifest).ok:
                        stale = True
            if not stale and manifest is not None:
                generation = self.active_generation_dir()
                assert generation is not None
                return ProjectionReceipt(
                    projection_version=str(manifest["projection_version"]),
                    source_knowledge_high_watermark=store_watermark,
                    content_hash=str(manifest["content_hash"]),
                    generation_dir=str(generation),
                    activated=False,
                    node_count=int(manifest["node_count"]),
                    edge_count=int(manifest["edge_count"]),
                    repaired=False,
                )
        receipt = self.rebuild()
        return receipt.model_copy(update={"repaired": True})
