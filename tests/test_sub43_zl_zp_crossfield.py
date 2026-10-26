"""SUB-43 — final-audit security findings S-1 and S-2 (split-secret residuals).

* S-1 (audit-final-security.md): ``_redaction_match_view`` dropped only
  Zs-class separators, so a secret split with U+2028 LINE SEPARATOR (Zl) or
  U+2029 PARAGRAPH SEPARATOR (Zp) evaded redaction AND projection quarantine
  — publish sanitization (``\\s+`` collapse covers Zl/Zp) then reshaped the
  key into ``AKIA IOSFODNN7EXAMPLE`` in ``graph.json``, recoverable by a
  space-strip. The match view now drops every Z* separator (Zs/Zl/Zp)
  except plain ASCII space, before NFKC.
* S-2: the quarantine scan was per-field only, so a secret split ACROSS
  publish-bound fields (half in ``summary``, half in ``context_tags``; or
  half in the heading and half in the first body sentence fused into the
  enriched label by ``" — "``) published both halves side by side in one
  node. Quarantine now additionally scans a deterministic joined view per
  node (raw fields AND publish-bound fields), and ``_secret_category`` gains
  a compact token view (separator/punctuation-free) that catches shapes
  re-joined across arbitrary join punctuation such as ``" — "``.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pytest

from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from soul_knowledge.projection import GraphProjector, enriched_summary
from soul_knowledge.security import redact_text
from soul_knowledge.store import EventStore

AWS_KEY_TAIL = "IOSFODNN7EXAMPLE"  # the canonical AWS docs example key tail

# The joiner used to build the per-node cross-field scan view: a zero-width
# space (Cf) — dropped by the redaction match view (so split halves re-join),
# not matched by ``\s`` or any pinned pattern alphabet (so the join itself can
# never bridge a raw-pattern match). Kept as a literal here so the test pins
# the *behavior*, not the constant name.
CROSS_FIELD_JOINER = "\u200b"

ZL_ZP_SEPARATORS = [
    "\u2028",  # LINE SEPARATOR (Zl)
    "\u2029",  # PARAGRAPH SEPARATOR (Zp)
]

# ------------------------------------------------------------------ S-1


@pytest.mark.parametrize("separator", ZL_ZP_SEPARATORS)
def test_redact_text_catches_zl_zp_split_aws_key(separator: str) -> None:
    category = unicodedata.category(separator)
    assert category in ("Zl", "Zp"), "probe must pin the Zl/Zp category"
    redacted = redact_text(f"AKIA{separator}{AWS_KEY_TAIL}")
    assert AWS_KEY_TAIL not in redacted
    assert "AKIA" not in redacted
    assert "[REDACTED" in redacted


def test_redact_text_catches_mixed_zl_zp_with_other_separator_families() -> None:
    """One Zl/Zp anywhere in the split chain must not defeat the join."""
    cases = [
        f"AKIA{AWS_KEY_TAIL[:8]}\u2028{AWS_KEY_TAIL[8:]}",  # Zl mid-key
        "AKIA\u2028IOSF\u200aODNN7EXAMPLE",  # Zl + Zs hair space
        "AKIA\u2029IOSF\u200bODNN7EXAMPLE",  # Zp + ZWSP (Cf)
        "AKIA\u2028IOSFODNN7EXAMPL\u2029E",  # Zl and Zp
    ]
    for text in cases:
        redacted = redact_text(text)
        assert "ODNN7EXAMPLE" not in redacted, repr(text)
        assert "[REDACTED" in redacted, repr(text)


def test_redact_text_leaves_legitimate_zl_zp_text_byte_identical() -> None:
    """Two-way guard: rare-but-clean Zl/Zp text must not be rewritten."""
    samples = [
        "first visual line\u2028second visual line",  # U+2028 as line break
        "paragraph one\u2029paragraph two",  # U+2029 as paragraph break
        "mixed\u2028spacing\u2029with plain ascii words only",
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


def _clean_node(node_id: str = "n-clean") -> Node:
    return Node(
        node_id=node_id,
        kind=NodeKind.LEAF,
        domain=PrimaryDomain.P1,
        summary="ordinary operator notes with no secret material",
        payload={"source_file": "soul://ops"},
        provenance=("soul://ops",),
        context_tags=("ops", "rotation"),
        source_version_id="sv-test",
    )


def _rebuild_and_read_graph(store: EventStore, out: Path) -> str:
    receipt = GraphProjector(store, output_root=out).rebuild()
    return (Path(receipt.generation_dir) / "graph.json").read_text(encoding="utf-8")


def _quarantine_events(store: EventStore) -> list[dict[str, object]]:
    rows = store.execute_raw(
        "SELECT payload_json FROM event_log WHERE kind = 'audit' AND event_type = 'security.projection_quarantine'"
    ).fetchall()
    return [json.loads(str(row["payload_json"])) for row in rows]


@pytest.mark.parametrize("separator", ZL_ZP_SEPARATORS)
def test_zl_zp_split_secret_node_is_quarantined_from_published_graph(tmp_path: Path, separator: str) -> None:
    """Quarantine must see through Zl/Zp exactly like Zs (SUB-35-F2 class)."""
    store = EventStore(tmp_path / "soul.sqlite3")
    _append_node(
        store,
        Node(
            node_id="n-zlzp-evil",
            kind=NodeKind.LEAF,
            domain=PrimaryDomain.P1,
            summary=f"operator notes AKIA{separator}{AWS_KEY_TAIL} inside",
            payload={"source_file": "soul://ops"},
            provenance=("soul://ops",),
            source_version_id="sv-test",
        ),
        "k-zlzp-evil",
    )
    _append_node(store, _clean_node(), "k-clean")
    graph_text = _rebuild_and_read_graph(store, tmp_path / "out")
    assert AWS_KEY_TAIL not in graph_text
    assert "n-zlzp-evil" not in graph_text
    assert "n-clean" in graph_text, "clean nodes keep serving"
    events = _quarantine_events(store)
    assert len(events) == 1, "exactly one quarantine audit event for the rebuild"
    assert any(entry["node_id"] == "n-zlzp-evil" for entry in events[0]["quarantined"])  # type: ignore[index]
    assert AWS_KEY_TAIL not in json.dumps(events[0]), "audit never carries secret material"
    assert store.verify_chain().ok


# ------------------------------------------------------------------ S-2


def test_cross_field_split_secret_node_is_quarantined(tmp_path: Path) -> None:
    """Half in ``summary``, half in ``context_tags``: no single field matches,
    but the joined publish-bound view does — the node must be quarantined."""
    store = EventStore(tmp_path / "soul.sqlite3")
    _append_node(
        store,
        Node(
            node_id="n-xfield-evil",
            kind=NodeKind.LEAF,
            domain=PrimaryDomain.P1,
            summary="note about AKIAIOSFOD",
            payload={"source_file": "soul://ops"},
            provenance=("soul://ops",),
            context_tags=("NN7EXAMPLE",),
            source_version_id="sv-test",
        ),
        "k-xfield-evil",
    )
    _append_node(store, _clean_node(), "k-clean")
    graph_text = _rebuild_and_read_graph(store, tmp_path / "out")
    assert "n-xfield-evil" not in graph_text, "both halves must never reach the artifact"
    assert "AKIAIOSFOD" not in graph_text
    assert "NN7EXAMPLE" not in graph_text
    assert "n-clean" in graph_text, "clean nodes keep serving"
    events = _quarantine_events(store)
    assert len(events) == 1
    assert any(entry["node_id"] == "n-xfield-evil" for entry in events[0]["quarantined"])  # type: ignore[index]
    assert "AKIAIOSFOD" not in json.dumps(events[0]) and "NN7EXAMPLE" not in json.dumps(events[0])
    assert store.verify_chain().ok


def test_enriched_label_heading_sentence_split_is_caught() -> None:
    """The enriched label fuses heading + first sentence with ``" — "``; the
    secret shape must be caught even though neither fragment matches alone."""
    label = enriched_summary("AKIAIOSFOD", ([], ["NN7EXAMPLE is the rest of the sentence."]))
    assert label == "AKIAIOSFOD — NN7EXAMPLE is the rest of the sentence."
    assert GraphProjector._secret_category(label) is not None
    # Neither half alone is a match — the probe is non-vacuous.
    assert GraphProjector._secret_category("AKIAIOSFOD") is None
    assert GraphProjector._secret_category("NN7EXAMPLE is the rest of the sentence.") is None


def test_joined_view_catches_split_across_every_publish_field_pair(tmp_path: Path) -> None:
    """Unit-level matrix: halves re-join even with unrelated fields between
    them and in either field order (pairwise both-orders join)."""
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    fields = ["note about AKIAIOSFOD", "soul://ops", "NN7EXAMPLE"]
    assert projector._joined_secret_category(fields) is not None
    assert projector._joined_secret_category(list(reversed(fields))) is not None
    # Adjacent two-field join via the plain category scan (no helper needed).
    assert GraphProjector._secret_category(CROSS_FIELD_JOINER.join(["note about AKIAIOSFOD", "NN7EXAMPLE"]))
    # And each individual field stays clean (non-vacuous probe).
    for field_text in fields:
        assert GraphProjector._secret_category(field_text) is None


def test_legit_multi_field_node_still_publishes(tmp_path: Path) -> None:
    """Two-way guard: clean multi-field nodes must survive the joined scan."""
    store = EventStore(tmp_path / "soul.sqlite3")
    _append_node(
        store,
        Node(
            node_id="n-legit",
            kind=NodeKind.LEAF,
            domain=PrimaryDomain.P1,
            summary="deploy key rotation policy, version 2.0 draft",
            payload={"source_file": "soul://ops"},
            provenance=("soul://ops", "git:abc123"),
            context_tags=("ops", "security", "rotation-2026"),
            source_version_id="sv-test",
        ),
        "k-legit",
    )
    graph_text = _rebuild_and_read_graph(store, tmp_path / "out")
    assert "n-legit" in graph_text, "clean multi-field node must publish"
    assert _quarantine_events(store) == []
    assert store.verify_chain().ok


def test_legit_zl_zp_node_still_publishes_with_collapsed_separator(tmp_path: Path) -> None:
    """Rare-but-clean Zl/Zp text stays publishable; publish sanitization
    collapses the separator to one ASCII space (W3-6), unchanged behavior."""
    store = EventStore(tmp_path / "soul.sqlite3")
    _append_node(
        store,
        Node(
            node_id="n-zl-clean",
            kind=NodeKind.LEAF,
            domain=PrimaryDomain.P1,
            summary="first visual line\u2028second visual line",
            payload={"source_file": "soul://ops"},
            provenance=("soul://ops",),
            source_version_id="sv-test",
        ),
        "k-zl-clean",
    )
    graph_text = _rebuild_and_read_graph(store, tmp_path / "out")
    assert "n-zl-clean" in graph_text
    assert "\u2028" not in graph_text, "publish sanitization still collapses Zl"
    assert _quarantine_events(store) == []
    assert store.verify_chain().ok


# ------------------------------------------------------- full bypass matrix


def test_full_bypass_matrix_including_zl_zp_and_cross_field() -> None:
    """Re-run of the complete bypass surface after S-1/S-2: every obfuscation
    family closed at the redaction layer, plus the cross-field family closed
    at the quarantine layer."""
    split_cases = [
        f"AKIA{AWS_KEY_TAIL}",  # plain (pinned pattern)
        f"AKIA\u200b{AWS_KEY_TAIL}",  # ZWSP (Cf)
        f"AKIA\u200c{AWS_KEY_TAIL}",  # ZWNJ (Cf)
        f"AKIA\ufeff{AWS_KEY_TAIL}",  # BOM (Cf)
        f"AKIA\t{AWS_KEY_TAIL}",  # tab (Cc)
        f"AKIA\u00a0{AWS_KEY_TAIL}",  # NBSP (Zs)
        f"AKIA\u200a{AWS_KEY_TAIL}",  # hair space (Zs)
        f"AKIA\u3000{AWS_KEY_TAIL}",  # ideographic space (Zs)
        f"AKIA\u2028{AWS_KEY_TAIL}",  # LINE SEPARATOR (Zl) — S-1
        f"AKIA\u2029{AWS_KEY_TAIL}",  # PARAGRAPH SEPARATOR (Zp) — S-1
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
    # Cross-field family (S-2): halves in different publish-bound fields.
    half_a, half_b = "AKIAIOSFOD", "NN7EXAMPLE"
    assert GraphProjector._secret_category(half_a) is None
    assert GraphProjector._secret_category(half_b) is None
    joined = CROSS_FIELD_JOINER.join([f"note about {half_a}", half_b])
    assert GraphProjector._secret_category(joined) is not None
    # Same split fused by the enrichment join punctuation " — ".
    assert GraphProjector._secret_category(f"{half_a} — {half_b} is the rest.") is not None
