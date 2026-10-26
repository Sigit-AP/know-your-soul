"""SUB-35 wave-3 re-review residuals: redaction false positives and the
Zs-class split-secret bypass.

* SUB-35-F1: the entropy-run rule counted ``-`` (and path ``/``) toward the
  symbol class, so the repo's own dated kebab-case paths/slugs (>= 40 chars)
  were redacted as ``[REDACTED:high-entropy token]`` — 35 of 55 docs tripped
  (verified: 121 runs under the old rule). The rule now fires only on runs
  with no identifier separators (``-``/``_``) that mix the lower, upper and
  digit character classes — hash-shaped pure-hex runs were already exempt.
* SUB-35-F2: the split-secret match view stripped only Cf/Cc separators, so
  Zs-class spaces (NBSP, hair/thin/narrow/ideographic space, ...) still
  evaded redaction AND projection quarantine. The match view now drops every
  non-ASCII Zs character *before* NFKC (NFKC would otherwise fold them into
  plain ASCII spaces, keeping the secret split). Quarantine inherits the fix
  because ``projection._secret_category`` scans the same view.
"""

from __future__ import annotations

import base64
import json
import random
from pathlib import Path

import pytest

from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from know_your_soul.projection import GraphProjector
from know_your_soul.security import _ENTROPY_RUN_RE, _PURE_HEX_RE, redact_text
from know_your_soul.store import EventStore

REPO_ROOT = Path(__file__).resolve().parents[1]

AWS_KEY_TAIL = "IOSFODNN7EXAMPLE"  # the canonical AWS docs example key tail


def _old_wave3_rule_would_redact(run: str) -> bool:
    """The pre-fix entropy rule (symbol class included ``-``): the RED oracle."""
    if _PURE_HEX_RE.fullmatch(run):
        return False
    classes = (
        any(char.islower() for char in run),
        any(char.isupper() for char in run),
        any(char.isdigit() for char in run),
        any(char in "+/=_-" for char in run),
    )
    return sum(classes) >= 3


# ---------------------------------------------------------------- SUB-35-F1


def test_redact_text_leaves_dated_kebab_paths_byte_identical() -> None:
    """The reviewer's F1 probe plus the residual shapes found in repo docs."""
    samples = [
        "see docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design for details",
        "docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md",
        "docs/recovery/wave1-R2-importer-bounded-contained-reads.md",
        "open tests/test_relearn_events_carry_zero_evidence_weight.py",
        "set GRAPHIFY_BACKEND=none/off/disable/disabled here",
        "sampling/prompts/logging/completions/PR/gh",
        "id 550e8400-e29b-41d4-a716-446655440000 is fine",
    ]
    for sample in samples:
        assert redact_text(sample) == sample, sample


def test_redact_text_leaves_every_repo_doc_entropy_run_alone() -> None:
    """Two-way repo-wide guard: every >=40-char run in docs/ survives.

    Non-vacuous: the pre-fix rule redacted 121 such runs across 35 docs; the
    guards below pin that scale so this test cannot silently pass on an
    empty scan.
    """
    docs = sorted((REPO_ROOT / "docs").rglob("*.md"))
    assert len(docs) >= 50
    total_runs = 0
    old_rule_hits = 0
    old_rule_docs: set[Path] = set()
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        runs = [match.group(0) for match in _ENTROPY_RUN_RE.finditer(text)]
        total_runs += len(runs)
        for run in runs:
            if _old_wave3_rule_would_redact(run):
                old_rule_hits += 1
                old_rule_docs.add(doc)
        if not runs:
            continue
        redacted = redact_text(text)
        for run in runs:
            assert run in redacted, (str(doc.relative_to(REPO_ROOT)), run[:60])
    assert total_runs >= 200, "the repo docs must contain many >=40-char runs"
    assert old_rule_hits >= 100, "the old rule provably tripped >=100 runs (SUB-35-F1)"
    assert len(old_rule_docs) >= 30, "the old rule provably tripped >=30 docs (SUB-35-F1)"


def test_redact_text_still_catches_real_secret_blobs() -> None:
    """The other direction: real secret shapes stay caught by the rule."""
    rng = random.Random(20261026)
    random_b64 = base64.b64encode(bytes(rng.randrange(256) for _ in range(36))).decode()
    blobs = [
        "dGhpcyBpcyBhIGJhc2U2NCBlbmNvZGVkIHNlY3JldCB0b2tlbiBmb3IgdGVzdHM=",  # base64 with padding
        "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",  # AWS-secret-key shape (40 chars)
        "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8s9T0",  # mixed-case alnum 40
        random_b64,  # seeded random standard-base64 blob
    ]
    assert _ENTROPY_RUN_RE.fullmatch(random_b64) is not None
    for blob in blobs:
        redacted = redact_text(f"the blob {blob} appeared")
        assert blob not in redacted
        assert "[REDACTED:high-entropy token]" in redacted


def test_redact_text_frozen_bench_corpus_stays_byte_identical() -> None:
    """Token-estimator parity contract: clean input is never rewritten."""
    corpus = sorted(path for path in (REPO_ROOT / "bench" / "corpus").iterdir() if path.is_file())
    assert len(corpus) >= 28
    for doc in corpus:
        text = doc.read_text(encoding="utf-8")
        assert redact_text(text) == text, doc.name


# ---------------------------------------------------------------- SUB-35-F2

# Every Unicode Zs (space separator) character except plain ASCII space.
ZS_SEPARATORS = [
    "\u00a0",  # no-break space
    "\u1680",  # ogham space mark
    "\u2000",  # en quad
    "\u2001",  # em quad
    "\u2002",  # en space
    "\u2003",  # em space
    "\u2004",  # three-per-em space
    "\u2005",  # four-per-em space
    "\u2006",  # six-per-em space
    "\u2007",  # figure space
    "\u2008",  # punctuation space
    "\u2009",  # thin space
    "\u200a",  # hair space
    "\u202f",  # narrow no-break space
    "\u205f",  # medium mathematical space
    "\u3000",  # ideographic space
]


@pytest.mark.parametrize("separator", ZS_SEPARATORS)
def test_redact_text_catches_zs_split_aws_key(separator: str) -> None:
    import unicodedata

    assert unicodedata.category(separator) == "Zs"
    redacted = redact_text(f"AKIA{separator}{AWS_KEY_TAIL}")
    assert AWS_KEY_TAIL not in redacted
    assert "AKIA" not in redacted
    assert "[REDACTED" in redacted


def test_redact_text_catches_multi_split_and_zs_bearer_token() -> None:
    redacted = redact_text("AKIA\u200aIOSF\u200aODNN7EXAMPLE")
    assert "ODNN7EXAMPLE" not in redacted
    assert "[REDACTED" in redacted
    bearer = redact_text("bearer abcdef\u2009ghijklmnpqrstuvwxyz")
    assert "abcdefghijklmnpqrstuvwxyz" not in bearer


def test_redact_text_leaves_legitimate_zs_spacing_byte_identical() -> None:
    """Two-way guard: exotic-but-clean spacing must not be rewritten."""
    samples = [
        "le montant est 1\u202f000\u00a0\u20ac, merci",  # French NNBSP thousands + NBSP
        "word\u2003word",  # em space between plain words
        "normal ascii spaces stay exactly the same",
    ]
    for sample in samples:
        assert redact_text(sample) == sample, repr(sample)


def _append_node(store: EventStore, node: Node, key: str) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
            actor="test",
        ),
        idempotency_key=key,
    )


def test_zs_split_secret_node_is_quarantined_from_published_graph(tmp_path: Path) -> None:
    """Quarantine must scan the same Zs-normalized view as redaction."""
    store = EventStore(tmp_path / "soul.sqlite3")
    _append_node(
        store,
        Node(
            node_id="n-zs-evil",
            kind=NodeKind.LEAF,
            domain=PrimaryDomain.P1,
            summary=f"operator notes AKIA\u200a{AWS_KEY_TAIL} inside",
            payload={"source_file": "soul://ops"},
            provenance=("soul://ops",),
            source_version_id="sv-test",
        ),
        "k-zs-evil",
    )
    _append_node(
        store,
        Node(
            node_id="n-clean",
            kind=NodeKind.LEAF,
            domain=PrimaryDomain.P1,
            summary="ordinary operator notes with no secret material",
            payload={"source_file": "soul://ops"},
            provenance=("soul://ops",),
            source_version_id="sv-test",
        ),
        "k-clean",
    )
    receipt = GraphProjector(store, output_root=tmp_path / "out").rebuild()
    graph_text = (Path(receipt.generation_dir) / "graph.json").read_text(encoding="utf-8")
    assert AWS_KEY_TAIL not in graph_text
    assert "n-zs-evil" not in graph_text
    assert "n-clean" in graph_text, "clean nodes keep serving"
    rows = store.execute_raw(
        "SELECT payload_json FROM event_log WHERE kind = 'audit' AND event_type = 'security.projection_quarantine'"
    ).fetchall()
    assert len(rows) == 1, "exactly one quarantine audit event for the rebuild"
    payload = json.loads(str(rows[0]["payload_json"]))
    assert any(entry["node_id"] == "n-zs-evil" for entry in payload["quarantined"])
    assert AWS_KEY_TAIL not in str(rows[0]["payload_json"])
    assert store.verify_chain().ok


# ------------------------------------------------------- full bypass matrix


def test_full_split_secret_bypass_matrix_stays_closed() -> None:
    """Re-run of the complete bypass surface: every obfuscation family."""
    split_cases = [
        f"AKIA{AWS_KEY_TAIL}",  # plain (pinned pattern)
        f"AKIA\u200b{AWS_KEY_TAIL}",  # ZWSP (Cf)
        f"AKIA\u200c{AWS_KEY_TAIL}",  # ZWNJ (Cf)
        f"AKIA\ufeff{AWS_KEY_TAIL}",  # BOM (Cf)
        f"AKIA\t{AWS_KEY_TAIL}",  # tab (Cc)
        f"AKIA\u00a0{AWS_KEY_TAIL}",  # NBSP (Zs)
        f"AKIA\u200a{AWS_KEY_TAIL}",  # hair space (Zs)
        f"AKIA\u3000{AWS_KEY_TAIL}",  # ideographic space (Zs)
    ]
    for text in split_cases:
        redacted = redact_text(text)
        assert AWS_KEY_TAIL not in redacted, repr(text)
        assert "[REDACTED" in redacted, repr(text)
    # Fullwidth separator and homoglyph credential keys (wave-3 families).
    assert "hunter2secret" not in redact_text("password：hunter2secret")
    assert "hunter2secret" not in redact_text("pаssword: hunter2secret")
    # High-entropy base64 blob caught; hash-shaped hex deliberately left alone.
    blob = "dGhpcyBpcyBhIGJhc2U2NCBlbmNvZGVkIHNlY3JldCB0b2tlbiBmb3IgdGVzdHM="
    assert blob not in redact_text(blob)
    sha256 = "2d38d30862f3629710781ea5384f19215f9b55d3dcf7ac22f01af92034fc0f7d"
    assert redact_text(f"commit {sha256} is pinned") == f"commit {sha256} is pinned"
