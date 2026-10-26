"""Property tests for the injective id scheme (wave-3 W3-2, importer boundary).

The importer re-scopes lossy fragment ids onto an injective scheme at the
import boundary. These tests pin the injectivity claim itself, not just the
four known colliding pairs:

* ``_escape_id_text`` is a bijection onto its image: a decoder written from
  the encoding's own rules (literal ``\\w`` chars, everything else as
  ``-hex-``) reconstructs every input exactly;
* distinct inputs never share an encoding (pairwise fuzz over a hostile
  alphabet: hyphens, underscores, dots, spaces, unicode);
* ``injective_file_id`` is invariant under the declared path normalization
  (backslash/slash, leading slashes, ``..`` removal, suffix stripping) and
  collision-free outside it;
* section ids never collide across headings or occurrence numbers.

All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from know_your_soul.importer import _escape_id_text, _section_node_id, injective_file_id, injective_section_key

_hostile_char = st.sampled_from(
    [
        *"abAB019_",
        "-",
        ".",
        " ",
        "/",
        ":",
        "é",
        "\u200b",  # zero-width space
        "—",  # em dash
        "~",
    ]
)
_text = st.text(alphabet=_hostile_char, max_size=20)


def _decode_id_text(encoded: str) -> str:
    """Decode using only the encoding's documented rules (test-side oracle)."""
    out: list[str] = []
    index = 0
    while index < len(encoded):
        char = encoded[index]
        if char == "-":
            end = encoded.index("-", index + 1)
            out.append(chr(int(encoded[index + 1 : end], 16)))
            index = end + 1
        else:
            out.append(char)
            index += 1
    return "".join(out)


def _normalized_segments(rel_path: str) -> tuple[str, ...]:
    """The declared path normalization, re-derived from the module docstring."""
    normalized = rel_path.replace("\\", "/").lstrip("/")
    while ".." in normalized:
        normalized = normalized.replace("..", "")
    stem = PurePosixPath(normalized).with_suffix("").as_posix()
    return tuple(segment for segment in stem.split("/") if segment)


# ------------------------------------------------------------- escape codec


@given(text=st.text(max_size=40))
@settings(derandomize=True, max_examples=60, deadline=None)
def test_escape_round_trips_through_an_independent_decoder(text: str) -> None:
    assert _decode_id_text(_escape_id_text(text)) == text


@given(first=_text, second=_text)
@settings(derandomize=True, max_examples=60, deadline=None)
def test_escape_never_collides_on_distinct_inputs(first: str, second: str) -> None:
    assume(first != second)
    assert _escape_id_text(first) != _escape_id_text(second)


@given(text=st.text(max_size=40))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_escaped_output_has_no_bare_hyphen(text: str) -> None:
    # Every "-" in the output delimits a ``-hex-`` escape block; a bare hyphen
    # would make the encoding ambiguous. The decoder oracle consumes it fully.
    encoded = _escape_id_text(text)
    assert _decode_id_text(encoded) == text
    for char in encoded:
        assert char == "-" or char.isalnum() or char == "_" or ord(char) > 127


# ------------------------------------------------------------- file ids


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("a/b/c.md", "a\\b\\c.md"),  # separator style
        ("a/b.md", "/a/b.md"),  # leading slash
        ("a/b.md", "a/../b.md"),  # textual dot-dot removal (a//b == a/b)
        ("a/b.md", "a//b.md"),  # empty segment
        ("a/b.md", "a/b"),  # suffix stripping
    ],
)
def test_file_id_is_invariant_under_declared_normalization(left: str, right: str) -> None:
    assert injective_file_id(left) == injective_file_id(right)


@given(
    first=st.text(alphabet=_hostile_char, min_size=1, max_size=16),
    second=st.text(alphabet=_hostile_char, min_size=1, max_size=16),
)
@settings(derandomize=True, max_examples=80, deadline=None)
def test_file_id_never_collides_outside_declared_normalization(first: str, second: str) -> None:
    try:
        left_segments = _normalized_segments(first)
        right_segments = _normalized_segments(second)
        left_id = injective_file_id(first)
        right_id = injective_file_id(second)
    except ValueError:
        # Empty-name paths raise fail-closed in both the oracle and the module.
        return
    if left_segments != right_segments:
        assert left_id != right_id
    else:
        assert left_id == right_id


@given(path=st.text(alphabet=_hostile_char, min_size=1, max_size=16))
@settings(derandomize=True, max_examples=40, deadline=None)
def test_file_id_is_deterministic_and_path_safe(path: str) -> None:
    try:
        first = injective_file_id(path)
        second = injective_file_id(path)
    except ValueError:
        return
    assert first == second
    assert "/" not in first
    assert ".." not in first


@given(path=st.text(alphabet=st.characters(whitelist_categories=("Cc", "Cf")), max_size=6))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_file_id_never_raises_untyped_errors_on_control_soup(path: str) -> None:
    try:
        injective_file_id(path)
    except ValueError:
        return  # fail-closed on empty names is the typed outcome


# ------------------------------------------------------------- section ids


@given(first=_text, second=_text)
@settings(derandomize=True, max_examples=50, deadline=None)
def test_section_key_never_collides_on_distinct_headings(first: str, second: str) -> None:
    assume(first != second)
    assert injective_section_key(first) != injective_section_key(second)


@given(heading=_text)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_section_key_round_trips_through_the_decoder(heading: str) -> None:
    assert _decode_id_text(injective_section_key(heading)) == heading


@given(file_id=st.text(alphabet=st.sampled_from("ab1:_"), min_size=1, max_size=8), heading=_text)
@settings(derandomize=True, max_examples=40, deadline=None)
def test_section_node_ids_differ_across_occurrences_and_headings(file_id: str, heading: str) -> None:
    first = _section_node_id(file_id, heading, 1)
    second = _section_node_id(file_id, heading, 2)
    other_heading = _section_node_id(file_id, heading + "x", 1)
    assert len({first, second, other_heading}) == 3
