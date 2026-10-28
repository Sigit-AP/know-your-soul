"""Query parser for retrieval v2 (wave W4).

Field-qualified syntax in the style of codegraph ``parseQuery``
(RESEARCH-QUERY §1B): ``kind:``, ``lang:``/``language:``, ``path:`` and
``name:`` prefixes become structured filters; everything else — including
unknown prefixes like ``TODO:foo`` — passes through as free text. Values
may be double-quoted (``path:"src/some path"``). The parser never raises
on string input; non-string input is a :class:`TypeError`.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

__all__ = ["KNOWN_FIELDS", "ParsedQuery", "parse_query"]

#: Recognised field prefixes (lowercase only — matching is case-sensitive).
KNOWN_FIELDS: frozenset[str] = frozenset({"kind", "lang", "language", "path", "name"})

#: MCP-style kind aliases (codegraph tools.ts:3186 maps ``type`` → ``type_alias``).
_KIND_ALIASES: dict[str, str] = {"type": "type_alias"}

_FIELD_RE = re.compile(r"^([a-z_]+):(.*)$")


@dataclass(frozen=True)
class ParsedQuery:
    """The structured form of one raw query string."""

    text: str
    terms: tuple[str, ...]
    kinds: tuple[str, ...]
    languages: tuple[str, ...]
    path_filters: tuple[str, ...]
    name_filters: tuple[str, ...]


def parse_query(raw: str) -> ParsedQuery:
    """Split ``raw`` into field filters and free-text terms.

    Quoting follows POSIX ``shlex``; when quotes are unbalanced the raw
    string is split on whitespace verbatim (quotes kept, no field parsing)
    so a malformed query can never crash a search.
    """
    if not isinstance(raw, str):
        raise TypeError(f"query must be a string, got {type(raw).__name__}")
    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        tokens = raw.split()
        return ParsedQuery(
            text=" ".join(tokens),
            terms=tuple(tokens),
            kinds=(),
            languages=(),
            path_filters=(),
            name_filters=(),
        )
    kinds: list[str] = []
    languages: list[str] = []
    path_filters: list[str] = []
    name_filters: list[str] = []
    free: list[str] = []
    for token in tokens:
        match = _FIELD_RE.match(token)
        if match is not None and match.group(1) in KNOWN_FIELDS and match.group(2):
            field, value = match.group(1), match.group(2)
            if field == "kind":
                kinds.append(_KIND_ALIASES.get(value, value))
            elif field in ("lang", "language"):
                languages.append(value)
            elif field == "path":
                path_filters.append(value)
            else:
                name_filters.append(value)
        else:
            free.append(token)
    return ParsedQuery(
        text=" ".join(free),
        terms=tuple(free),
        kinds=tuple(kinds),
        languages=tuple(languages),
        path_filters=tuple(path_filters),
        name_filters=tuple(name_filters),
    )
