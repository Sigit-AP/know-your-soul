"""Two-phase reference resolution (wave W2, MASTER-PLAN pillar 3).

Extractors park cross-file references in ``unresolved_refs``; this package
turns them into graph edges in a separate, deterministic resolve pass — never
guessing at extraction time (codegraph ``src/resolution``, #1240).

Raw-ref contract shared with the extractor (SUB-87)
----------------------------------------------------

The extractor emits plain mappings::

    {"source_node_id": str, "target_name": str, "kind": str}

* ``source_node_id`` — a symbol node id (``kind:hash[:col]``) or a file node
  id (``file:<path>``) that must already exist in the store.
* ``target_name`` — the referenced name as written: a plain name
  (``"helper"``), a dotted/qualified name (``"pkg.mod.func"``), or a module
  specifier (``"./util"``, ``"pkg.mod"``). Python relative imports keep their
  leading dots (``".mod.func"``, ``"..shared.util"``).
* ``kind`` — ``"calls"``, ``"imports"`` or ``"function_ref"`` (mapped to a
  ``calls`` edge, codegraph's EdgeKind|'function_ref').

``name_tail`` and ``first_seen`` are store concerns; the extractor may leave
them to ``insert_unresolved_ref`` defaults.
"""

from know_your_soul.resolve.resolver import (
    PROVENANCE,
    REF_KIND_TO_EDGE_KIND,
    RESOLVER_VERSION,
    BatchReport,
    ReferenceResolver,
    RefOutcome,
    ResolverConfig,
    ResolverError,
    resolve_pending,
)

__all__ = [
    "PROVENANCE",
    "REF_KIND_TO_EDGE_KIND",
    "RESOLVER_VERSION",
    "BatchReport",
    "ReferenceResolver",
    "RefOutcome",
    "ResolverConfig",
    "ResolverError",
    "resolve_pending",
]
