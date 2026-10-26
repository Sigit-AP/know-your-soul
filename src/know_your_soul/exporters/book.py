"""Deterministic markdown "book" export per domain and per community (ATM F-B6).

The book is a ``dict[path -> markdown]`` page set, ready for the export
orchestrator to write:

* ``book/index.md`` — overview; **every** other page is linked from here, so
  the page list is explicit and a validator can stay fail-closed.
* ``book/<DOMAIN>.md`` — one page per domain present in the read model
  (``P1``..``P7`` carry their spec titles), nodes sorted by id, each with
  label, scores P/M/E, version, evidence count, provenance and neighbours.
* ``book/C<id>.md`` — one page per community, only when community data (the
  ``analytics.communities`` output shape, or a bare list of community
  mappings) is supplied as a parameter.

Guarantees: deterministic byte-identical output for a given read model;
every relative link resolves inside the returned mapping; filenames are
byte-capped with an 8-hex hash suffix on truncation; page-count and
per-page-byte caps fail closed with typed errors; all free text passes
through :func:`know_your_soul.security.sanitize_text` and markdown
metacharacters in labels are escaped/flattened (no active markup, no link
injection from data).

origin: feature idea from Graphify ``wiki.py`` (index + per-community
articles with resolved cross-links); structure, chapter shape and rendering
are original.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from typing import Any

from know_your_soul.domain import PRIMARY_DOMAINS
from know_your_soul.exporters.orchestrator import ExportError
from know_your_soul.security import sanitize_text

__all__ = [
    "BOOK_FILENAME_MAX_BYTES",
    "MAX_BOOK_PAGES",
    "MAX_BOOK_PAGE_BYTES",
    "SCHEMA_VERSION",
    "BookCapExceededError",
    "BookError",
    "book_to_bytes",
    "build_book",
    "cap_filename",
    "validate_book",
]

SCHEMA_VERSION = 1
MAX_BOOK_PAGES = 1024
MAX_BOOK_PAGE_BYTES = 1024 * 1024
BOOK_FILENAME_MAX_BYTES = 64

_INDEX_PATH = "book/index.md"
_BOOK_MARKER = "<!-- know-your-soul-book v1 -->"
_LINK_RE = re.compile(r"\]\(([^)]+)\)")
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]+")
_WHITESPACE_RE = re.compile(r"\s+")


class BookError(ExportError):
    """A typed book-export contract violation."""


class BookCapExceededError(BookError):
    """The book exceeded its explicit page-count or per-page byte cap."""


def cap_filename(name: str, *, max_bytes: int = BOOK_FILENAME_MAX_BYTES) -> str:
    """Byte-cap a filename deterministically, hash-suffixed on truncation.

    Unsafe characters collapse to ``-`` first; when the UTF-8 byte length
    exceeds ``max_bytes`` the stem is cut on a UTF-8 boundary and an
    8-hex-char sha256 suffix of the *original* name keeps it unique.
    """
    if max_bytes < 16:
        raise BookError(f"max_bytes for filenames must be >= 16, got {max_bytes}")
    cleaned = _SAFE_STEM_RE.sub("-", name).strip("-.") or "page"
    if len(cleaned.encode("utf-8")) <= max_bytes:
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:8]
    suffix = "-" + digest
    if "." in cleaned:
        stem, _, extension = cleaned.rpartition(".")
        tail = suffix + "." + extension
    else:
        stem, tail = cleaned, suffix
    budget = max_bytes - len(tail.encode("utf-8"))
    raw = stem.encode("utf-8")[:budget]
    while True:
        try:
            truncated = raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    return truncated.rstrip("-.") + tail


def _flatten(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _md_text(value: Any) -> str:
    """Sanitized, single-line, link-safe markdown text (never active markup)."""
    if value is None:
        return ""
    cleaned = _flatten(sanitize_text(str(value)))
    return (
        cleaned.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("`", "'")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )


def _domain_title(domain: str) -> str:
    titles = {item.value: item.title for item in PRIMARY_DOMAINS}
    return titles.get(domain, domain)


def _domain_page_path(domain: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}", domain):
        return f"book/{domain}.md"
    return f"book/{cap_filename('domain-' + domain + '.md')}"


def _score_of(scores: Any, key: str) -> str:
    if isinstance(scores, Mapping) and scores.get(key) is not None:
        return str(scores[key])
    return "0.000000000000"


def _normalize_nodes(read_model: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw_nodes = read_model.get("nodes")
    nodes = [node for node in raw_nodes if isinstance(node, Mapping)] if isinstance(raw_nodes, list) else []
    by_id: dict[str, dict[str, Any]] = {}
    for node in sorted(nodes, key=lambda entry: str(entry.get("id"))):
        node_id = str(node.get("id"))
        if node_id in by_id:
            continue
        scores = node.get("scores")

        version_raw = node.get("version")
        provenance_raw = node.get("provenance")
        by_id[node_id] = {
            "id": node_id,
            "label": _md_text(node.get("label")) or node_id,
            "domain": str(node.get("domain") or "unknown"),
            "kind": str(node.get("kind") or "unknown"),
            "version": version_raw if isinstance(version_raw, int) else 0,
            "scores": {key: _score_of(scores, key) for key in ("p", "m", "e", "uncertainty", "confidence")},
            "provenance": (
                [_md_text(entry) for entry in provenance_raw if entry is not None]
                if isinstance(provenance_raw, list)
                else []
            ),
        }
    return by_id


def _neighbors(read_model: Mapping[str, Any], known: set[str]) -> dict[str, list[tuple[str, str, str]]]:
    """Undirected adjacency: node id → sorted [(other_id, relation, direction)]."""
    raw_links = read_model.get("links")
    links = [link for link in raw_links if isinstance(link, Mapping)] if isinstance(raw_links, list) else []
    adjacency: dict[str, set[tuple[str, str, str]]] = {node_id: set() for node_id in known}
    for link in links:
        source, target = str(link.get("source")), str(link.get("target"))
        if source not in known or target not in known or source == target:
            continue
        relation = str(link.get("relation") or "related_to")
        adjacency[source].add((target, relation, "out"))
        adjacency[target].add((source, relation, "in"))
    return {node_id: sorted(entries) for node_id, entries in adjacency.items()}


def _normalize_communities(communities: Any) -> list[dict[str, Any]]:
    if communities is None:
        return []
    if isinstance(communities, Mapping):
        communities = communities.get("communities", [])
    result: list[dict[str, Any]] = []
    for community in communities:
        if not isinstance(community, Mapping):
            continue
        members_raw = community.get("members", [])
        members = sorted(str(member) for member in members_raw) if isinstance(members_raw, list) else []
        community_id = community.get("id")
        result.append(
            {
                "id": community_id if isinstance(community_id, int) else len(result),
                "label": _md_text(community.get("label")) or "community",
                "cohesion": str(community.get("cohesion") or "0.000000000000"),
                "membership_sig": str(community.get("membership_sig") or ""),
                "members": members,
            }
        )
    result.sort(key=lambda entry: entry["id"])
    return result


def _node_section(
    node: Mapping[str, Any],
    evidence_count: int,
    neighbours: list[tuple[str, str, str]],
    domain_page: Mapping[str, str],
    nodes: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    scores = node["scores"]
    lines = [
        f"### {node['label']}",
        "",
        f"- ID: {_md_text(node['id'])}",
        f"- Domain: {_md_text(node['domain'])} · Kind: {_md_text(node['kind'])} · Version: {node['version']}",
        f"- Scores: P {scores['p']} · M {scores['m']} · E {scores['e']}"
        f" (uncertainty {scores['uncertainty']}, confidence {scores['confidence']})",
        f"- Evidence: {evidence_count}",
    ]
    if node["provenance"]:
        lines.append("- Provenance: " + "; ".join(node["provenance"]))
    if neighbours:
        rendered = []
        for other_id, relation, direction in neighbours:
            other = nodes[other_id]
            arrow = "→" if direction == "out" else "←"
            rendered.append(f"{arrow} `{_md_text(relation)}` [{other['label']}]({domain_page[other['domain']]})")
        lines.append("- Neighbors: " + ", ".join(rendered))
    lines.append("")
    return lines


def build_book(
    read_model: Mapping[str, Any],
    *,
    communities: Any = None,
    evidence_counts: Mapping[str, int] | None = None,
    max_pages: int = MAX_BOOK_PAGES,
    max_page_bytes: int = MAX_BOOK_PAGE_BYTES,
    include_domains: Iterable[str] | None = None,
) -> dict[str, str]:
    """Build the book page set (``path -> markdown``), fail-closed on caps.

    ``communities`` accepts the ``analytics.communities.detect_communities``
    output mapping or a bare list of community mappings; without it the book
    is per-domain only. ``evidence_counts`` maps node id → evidence count
    (nodes default to 0 — the read model carries no evidence cardinality).
    ``include_domains`` forces a page for each listed domain even when it
    holds no nodes, so a caller (the built-in registry) can publish a fixed,
    data-independent page set.
    """
    if max_pages < 1:
        raise BookError(f"max_pages must be >= 1, got {max_pages}")
    if max_page_bytes < 1:
        raise BookError(f"max_page_bytes must be >= 1, got {max_page_bytes}")

    nodes = _normalize_nodes(read_model)
    neighbours = _neighbors(read_model, set(nodes))
    community_list = _normalize_communities(communities)
    counts = {str(key): int(value) for key, value in (evidence_counts or {}).items()}

    domains = sorted({node["domain"] for node in nodes.values()} | {str(domain) for domain in (include_domains or ())})
    domain_page = {domain: _domain_page_path(domain) for domain in domains}
    # Two distinct domains must never collapse onto one filename.
    if len(set(domain_page.values())) != len(domain_page):
        raise BookError("domain page filenames collide after capping")
    community_page = {entry["id"]: f"book/C{entry['id']}.md" for entry in community_list}

    pages: dict[str, str] = {}
    total_pages = 1 + len(domain_page) + len(community_page)
    if total_pages > max_pages:
        raise BookCapExceededError(f"book needs {total_pages} pages, over the {max_pages}-page cap")

    for domain in domains:
        members = [node for node in nodes.values() if node["domain"] == domain]
        members.sort(key=lambda node: node["id"])
        title = _domain_title(domain)
        lines = [
            f"# Domain {_md_text(domain)} — {_md_text(title)}",
            "",
            f"Nodes: {len(members)} · [Index](index.md)",
            "",
        ]
        for node in members:
            lines.extend(
                _node_section(
                    node,
                    counts.get(node["id"], 0),
                    neighbours[node["id"]],
                    {key: value.rsplit("/", 1)[1] for key, value in domain_page.items()},
                    nodes,
                )
            )
        pages[domain_page[domain]] = "\n".join(lines)

    for entry in community_list:
        lines = [
            f"# Community C{entry['id']} — {entry['label']}",
            "",
            f"Members: {len(entry['members'])} · Cohesion: {entry['cohesion']} · Signature: {entry['membership_sig']}",
            "",
            "[Index](index.md)",
            "",
        ]
        for member_id in entry["members"]:
            if member_id not in nodes:
                continue
            node = nodes[member_id]
            target = domain_page[node["domain"]].rsplit("/", 1)[1]
            lines.append(f"- [{node['label']}]({target}) — `{_md_text(member_id)}` (domain {_md_text(node['domain'])})")
        lines.append("")
        pages[community_page[entry["id"]]] = "\n".join(lines)

    index_lines = [
        "# Knowledge Book",
        "",
        _BOOK_MARKER,
        "",
        f"Nodes: {len(nodes)} · Domains: {len(domains)} · Community pages: {len(community_list)}",
        "",
        "## Domains",
        "",
    ]
    for domain in domains:
        target = domain_page[domain].rsplit("/", 1)[1]
        count = sum(1 for node in nodes.values() if node["domain"] == domain)
        index_lines.append(f"- [{_md_text(domain)} — {_md_text(_domain_title(domain))}]({target}) — {count} nodes")
    if community_list:
        index_lines.extend(["", "## Communities", ""])
        for entry in community_list:
            target = community_page[entry["id"]].rsplit("/", 1)[1]
            index_lines.append(f"- [C{entry['id']} — {entry['label']}]({target}) — {len(entry['members'])} members")
    index_lines.append("")
    pages[_INDEX_PATH] = "\n".join(index_lines)

    for path, content in pages.items():
        size = len(content.encode("utf-8"))
        if size > max_page_bytes:
            raise BookCapExceededError(f"book page {path} is {size} bytes, over the {max_page_bytes}-byte cap")
    return pages


def book_to_bytes(pages: Mapping[str, str]) -> dict[str, bytes]:
    """UTF-8 encode the page set for the export orchestrator to write."""
    return {path: content.encode("utf-8") for path, content in pages.items()}


def validate_book(pages: Mapping[str, str]) -> None:
    """Fail-closed structural validation of a book page set.

    The index must exist with its marker, every relative link in every page
    must resolve inside the page set, and every non-index page must be
    listed in the index — the explicit page list the manifest relies on.
    """
    if _INDEX_PATH not in pages:
        raise BookError(f"book is missing its {_INDEX_PATH}")
    index = pages[_INDEX_PATH]
    if _BOOK_MARKER not in index:
        raise BookError("book index is missing its schema marker")
    for path, content in pages.items():
        base = path.rsplit("/", 1)[0]
        for target in _LINK_RE.findall(content):
            scheme = target.split("/", 1)[0]
            if ":" in scheme:
                raise BookError(f"book page {path} links to a non-relative target {target!r}")
            page = target.split("#", 1)[0]
            resolved = f"{base}/{page}" if page else path
            if resolved not in pages:
                raise BookError(f"book page {path} links to missing page {resolved!r}")
    index_targets = {f"book/{target.split('#', 1)[0]}" for target in _LINK_RE.findall(index)}
    for path in pages:
        if path != _INDEX_PATH and path not in index_targets:
            raise BookError(f"book page {path} is not listed in the index")
