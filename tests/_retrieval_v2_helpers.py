"""Shared fixtures for retrieval v2 tests (wave W4).

All helpers are deterministic: node ids are minted with the same
``kind:sha256(path:kind:name:line)`` shape the extractor uses (truncated
digest — uniqueness inside a test is all we need), and file rows use a
fixed mtime/size so two runs over the same fixture are byte-identical.
"""

from __future__ import annotations

import hashlib

from know_your_soul.store import EventStore

FIXED_MTIME = 1_700_000_000
FIXED_SIZE = 64


def make_node_id(path: str, kind: str, name: str, line: int, col: int = 0) -> str:
    digest = hashlib.sha256(f"{path}:{kind}:{name}:{line}".encode()).hexdigest()[:16]
    return f"{kind}:{digest}:{col}"


def add_file(store: EventStore, path: str, language: str = "python") -> None:
    store.upsert_file(
        path=path,
        sha256=hashlib.sha256(path.encode("utf-8")).hexdigest(),
        size=FIXED_SIZE,
        mtime=FIXED_MTIME,
        language=language,
        generated_hint=False,
        extraction_version=1,
    )


def add_symbol(
    store: EventStore,
    name: str,
    kind: str,
    path: str,
    line: int,
    col: int = 0,
    node_id: str | None = None,
) -> str:
    nid = node_id or make_node_id(path, kind, name, line, col)
    store.upsert_symbol(node_id=nid, name=name, kind=kind, path=path, line=line, col=col)
    return nid


def add_edge(
    store: EventStore,
    source: str,
    target: str,
    kind: str,
    line: int | None = None,
    col: int | None = None,
) -> bool:
    return store.add_graph_edge(source, target, kind, line=line, col=col)


def add_ref(
    store: EventStore,
    source_node_id: str,
    target_name: str,
    kind: str,
    name_tail: str | None = None,
) -> int:
    return store.insert_unresolved_ref(
        source_node_id=source_node_id,
        target_name=target_name,
        kind=kind,
        name_tail=name_tail,
        first_seen="2026-10-27T00:00:00+00:00",
    )
