"""Contract and property tests for the markdown book exporter (ATM F-B6).

The book is a deterministic ``dict[path -> markdown]`` page set: one
``book/index.md`` plus one page per primary domain and, when community data
is supplied, one page per community. Every relative link between pages must
resolve inside the returned mapping, filenames are byte-capped with a hash
suffix on truncation, and all caps fail closed with typed errors.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.book import (
    BOOK_FILENAME_MAX_BYTES,
    MAX_BOOK_PAGE_BYTES,
    MAX_BOOK_PAGES,
    BookCapExceededError,
    BookError,
    book_to_bytes,
    build_book,
    cap_filename,
    validate_book,
)

_LINK_RE = re.compile(r"\]\(([^)]+)\)")


def _node(
    node_id: str,
    *,
    label: str | None = None,
    domain: str = "P1",
    kind: str = "CHILD",
    version: int = 1,
    provenance: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "label": label if label is not None else node_id,
        "domain": domain,
        "kind": kind,
        "version": version,
        "scores": {
            "p": "0.100000000000",
            "m": "0.200000000000",
            "e": "0.300000000000",
            "uncertainty": "0.400000000000",
            "confidence": "0.500000000000",
        },
        "confidence": "0.500000000000",
        "professional_level": 2,
        "provenance": provenance if provenance is not None else ["soul://store"],
    }


def _read_model() -> dict[str, Any]:
    return {
        "directed": True,
        "nodes": [
            _node("a-node", label="Alpha summary", domain="P1", version=2),
            _node("b-node", label="Beta summary", domain="P2"),
            _node("c-node", label="Gamma summary", domain="P2", kind="PRIMARY"),
        ],
        "links": [
            {"source": "a-node", "target": "b-node", "relation": "depends_on"},
            {"source": "c-node", "target": "a-node", "relation": "parent_of"},
        ],
    }


def _communities() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "communities": [
            {
                "id": 0,
                "size": 2,
                "label": "Alpha summary",
                "label_node_id": "a-node",
                "membership_sig": "0123456789abcdef",
                "cohesion": "1.000000000000",
                "members": ["a-node", "c-node"],
            }
        ],
    }


def _all_links_resolve(pages: dict[str, str]) -> bool:
    for path, content in pages.items():
        base = path.rsplit("/", 1)[0]
        for target in _LINK_RE.findall(content):
            if ":" in target.split("/", 1)[0]:  # scheme like http: — never allowed
                return False
            page = target.split("#", 1)[0]
            resolved = f"{base}/{page}" if page else path
            if resolved not in pages:
                return False
    return True


# ------------------------------------------------------------------- content


def test_build_minimal_book_structure() -> None:
    pages = build_book(_read_model())
    assert sorted(pages) == ["book/P1.md", "book/P2.md", "book/index.md"]
    index = pages["book/index.md"]
    assert "know-your-soul-book v1" in index
    assert "[P1 — Pembelajaran](P1.md)" in index
    assert "[P2 — Gagasan](P2.md)" in index
    # No community section without community data.
    assert "Communities" not in index


def test_domain_page_lists_nodes_sorted_with_fields() -> None:
    pages = build_book(_read_model(), evidence_counts={"a-node": 2, "b-node": 5})
    page = pages["book/P1.md"]
    assert "a-node" in page
    assert "Alpha summary" in page
    assert "P 0.100000000000" in page
    assert "M 0.200000000000" in page
    assert "E 0.300000000000" in page
    assert "Evidence: 2" in page
    assert "Version: 2" in page
    p2 = pages["book/P2.md"]
    assert p2.index("b-node") < p2.index("c-node")
    # Neighbour links point at the neighbour's domain page, relative.
    assert "](P1.md)" in p2


def test_default_caps_are_explicit() -> None:
    assert MAX_BOOK_PAGES > 0
    assert MAX_BOOK_PAGE_BYTES > 0
    assert 0 < BOOK_FILENAME_MAX_BYTES <= 255


# --------------------------------------------------------------- determinism


def test_determinism_input_order_independent() -> None:
    model = _read_model()
    shuffled = {
        "directed": True,
        "nodes": list(reversed(model["nodes"])),
        "links": list(reversed(model["links"])),
    }
    assert build_book(model) == build_book(shuffled)


def test_repeat_build_identical() -> None:
    assert build_book(_read_model(), communities=_communities()) == build_book(
        _read_model(), communities=_communities()
    )


# ------------------------------------------------------------------ community


def test_communities_add_c_pages() -> None:
    pages = build_book(_read_model(), communities=_communities())
    assert "book/C0.md" in pages
    page = pages["book/C0.md"]
    assert "Alpha summary" in page
    assert "1.000000000000" in page
    assert "0123456789abcdef" in page
    assert "a-node" in page and "c-node" in page
    assert "](P1.md)" in page and "](P2.md)" in page
    assert "[C0 — Alpha summary](C0.md)" in pages["book/index.md"]


def test_communities_as_bare_list_accepted() -> None:
    pages = build_book(_read_model(), communities=_communities()["communities"])
    assert "book/C0.md" in pages


# ---------------------------------------------------------------------- links


def test_all_relative_links_resolve() -> None:
    pages = build_book(_read_model(), communities=_communities())
    assert _all_links_resolve(pages)


def test_index_lists_every_page() -> None:
    pages = build_book(_read_model(), communities=_communities())
    index_targets = {f"book/{t.split('#', 1)[0]}" for t in _LINK_RE.findall(pages["book/index.md"])}
    for path in pages:
        if path != "book/index.md":
            assert path in index_targets, f"{path} not linked from index"


# --------------------------------------------------------------- filename cap


def test_cap_filename_truncates_with_hash_suffix() -> None:
    name = "domain-" + "x" * 200 + ".md"
    capped = cap_filename(name)
    assert len(capped.encode("utf-8")) <= BOOK_FILENAME_MAX_BYTES
    assert capped.endswith(".md")
    assert re.search(r"-[0-9a-f]{8}\.md$", capped)
    # Deterministic and collision-distinct for different inputs.
    assert capped == cap_filename(name)
    assert cap_filename("domain-" + "y" * 200 + ".md") != capped


def test_cap_filename_short_names_pass_through() -> None:
    assert cap_filename("P1.md") == "P1.md"


def test_long_domain_name_gets_capped_page_name() -> None:
    model = _read_model()
    model["nodes"].append(_node("z-node", domain="D" * 100))
    pages = build_book(model)
    long_pages = [path for path in pages if "xxx" not in path and len(path.encode("utf-8")) > len("book/P1.md")]
    assert any(path.startswith("book/domain-") for path in pages)
    for path in pages:
        filename = path.rsplit("/", 1)[1]
        assert len(filename.encode("utf-8")) <= BOOK_FILENAME_MAX_BYTES
    assert _all_links_resolve(pages)
    assert long_pages  # sanity: the long domain page exists


# ------------------------------------------------------------------------ caps


def test_page_cap_fails_closed() -> None:
    model = _read_model()
    model["nodes"].append(_node("z-node", domain="P3"))
    with pytest.raises(BookCapExceededError, match="page"):
        build_book(model, max_pages=3)


def test_page_byte_cap_fails_closed() -> None:
    with pytest.raises(BookCapExceededError, match="byte"):
        build_book(_read_model(), max_page_bytes=64)


def test_cap_validation() -> None:
    with pytest.raises(BookError, match="max_pages"):
        build_book(_read_model(), max_pages=0)


# ----------------------------------------------------------------- markdown


def test_markdown_injection_payload_is_inert() -> None:
    evil = "[click](javascript:alert(1))\n\n<script>alert(2)</script>"
    model = _read_model()
    model["nodes"][0]["label"] = evil
    pages = build_book(model)
    for content in pages.values():
        assert "<script>" not in content
        assert not re.search(r"\]\(javascript:", content)
        assert "\x00" not in content
    # The flattened, bracket-escaped text is still present as plain text.
    assert "click" in pages["book/P1.md"]
    assert _all_links_resolve(pages)


# ------------------------------------------------------------------ validator


def test_validate_book_roundtrip() -> None:
    pages = build_book(_read_model(), communities=_communities())
    validate_book(pages)  # must not raise


def test_validate_book_rejects_missing_page() -> None:
    pages = build_book(_read_model())
    del pages["book/P2.md"]
    with pytest.raises(BookError, match="P2"):
        validate_book(pages)


def test_validate_book_rejects_missing_index() -> None:
    pages = build_book(_read_model())
    del pages["book/index.md"]
    with pytest.raises(BookError, match="index"):
        validate_book(pages)


def test_book_to_bytes_is_utf8_deterministic() -> None:
    pages = build_book(_read_model())
    blob = book_to_bytes(pages)
    assert blob == book_to_bytes(build_book(_read_model()))
    for path, data in blob.items():
        assert data.decode("utf-8") == pages[path]


# ------------------------------------------------------------------ property


@given(label=st.text(max_size=120))
@settings(derandomize=True, max_examples=60)
def test_property_arbitrary_label_keeps_links_resolving(label: str) -> None:
    model = _read_model()
    model["nodes"][0]["label"] = label
    model["nodes"][0]["provenance"] = [label]
    pages = build_book(model)
    assert _all_links_resolve(pages)
    assert pages == build_book(model)
    validate_book(pages)
