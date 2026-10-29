"""Loopback-only, token-authenticated, read-only web viewer (W7 SUB-101).

A stdlib-only (``http.server``) web UI over the knowledge store, following
the codegraph ``ui-server`` security model (RESEARCH-COVERAGE-AUDIT §4.5):

* **Threat model: DNS rebinding.** The ``Host`` header must name a loopback
  address (``localhost`` / ``127.0.0.1`` / ``[::1]``) **with the server's own
  port** — anything else is 403, checked before auth. A non-loopback
  ``Origin`` header is likewise 403. No CORS header is ever emitted.
* **Token auth in the URL.** A per-start random token (``?token=``) gates
  every byte — assets and API alike — compared with
  :func:`hmac.compare_digest`. The token is never echoed in responses or
  embedded in served assets.
* **Read-only store.** Only ``GET``/``HEAD`` exist (everything else is 405);
  the app runs ``SELECT`` queries and the existing read APIs
  (:mod:`know_your_soul.retrieval_v2`, :mod:`know_your_soul.ops`,
  :mod:`know_your_soul.analytics`) — never a write.
* **No path traversal.** Assets are served from a fixed name allowlist under
  ``webview_assets/``; no request path is ever joined into a filesystem path.
* **JSON is markup-safe.** Every API payload is serialized with
  :func:`escape_json_bytes`, which escapes ``&``, ``<``, ``>`` and the JS
  line separators, so a hostile node label cannot break out of the page
  (anti stored-XSS; the client renders via ``textContent`` only).

Endpoints (all GET, JSON unless noted):

* ``/`` — the static SPA shell (``text/html``, strict CSP).
* ``/static/app.js`` / ``/static/style.css`` — the bundled vanilla assets.
* ``/api/graph`` — nodes + edges + server-computed :mod:`viz.layout` frame.
* ``/api/node/<id>`` — node detail: P/M/E scores, provenance, relations.
* ``/api/search?q=&kind=&lang=&limit=`` — tiered symbol search (retrieval_v2).
* ``/api/hierarchy/<id>?depth=`` — callers + callees (retrieval_v2).
* ``/api/metrics`` — :func:`ops.metrics.collect_ops_metrics` (redacted).
* ``/api/health`` — :func:`ops.diagnose.run_diagnostics` report.
* ``/api/analytics`` — centrality + communities + bridges.
* ``/api/audit?limit=`` — newest audit events, payloads redacted.
* ``/api/proposals`` — review-signal candidates (proposal-only; never writes).
* ``/api/refs`` — unresolved-reference statistics.

Determinism: every payload is a pure function of store state (sorted keys,
canonical orderings inherited from the underlying modules), so two requests
against an unchanged store answer byte-identically.
"""

from __future__ import annotations

import argparse
import hmac
import json
import re
import secrets
import sys
import threading
import webbrowser
from collections.abc import Mapping, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from know_your_soul.analytics.bridges import find_bridges
from know_your_soul.analytics.centrality import compute_centrality
from know_your_soul.analytics.communities import detect_communities
from know_your_soul.analytics.review_signals import collect_review_signals
from know_your_soul.domain import Node
from know_your_soul.ops.diagnose import exit_code as diagnose_exit_code
from know_your_soul.ops.diagnose import run_diagnostics
from know_your_soul.ops.metrics import collect_ops_metrics
from know_your_soul.retrieval_v2 import CallHierarchy, HierarchyError, LookupError, SymbolLookup
from know_your_soul.security import redact_value, sanitize_text
from know_your_soul.store import EventStore
from know_your_soul.viz.layout import LayoutError
from know_your_soul.viz.layout import layout as compute_layout

__all__ = [
    "ANALYTICS_MAX_NODES",
    "AUDIT_LIMIT_MAX",
    "DEFAULT_HOST_ALLOWLIST",
    "HIERARCHY_DEPTH_MAX",
    "HIERARCHY_DEPTH_MIN",
    "MAX_GRAPH_NODES",
    "SCHEMA_VERSION",
    "SEARCH_LIMIT_MAX",
    "StoreUnavailableError",
    "WebviewApp",
    "WebviewError",
    "WebviewServer",
    "escape_json_bytes",
    "generate_token",
    "host_allowed",
    "main",
    "origin_allowed",
]

SCHEMA_VERSION = 1

#: Fail-closed caps: the layout engine itself caps at 4096 nodes; analytics
#: is capped lower and reports ``partial`` honestly rather than lying.
MAX_GRAPH_NODES = 4096
ANALYTICS_MAX_NODES = 2048
SEARCH_LIMIT_MAX = 100
AUDIT_LIMIT_MAX = 500
HIERARCHY_DEPTH_MIN, HIERARCHY_DEPTH_MAX = 1, 8

#: Loopback hostnames accepted in the ``Host`` header (codegraph rule 1).
DEFAULT_HOST_ALLOWLIST: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")

_FRAME_WIDTH = 1600.0
_FRAME_HEIGHT = 1000.0
_TOP_RANKING = 50
_TOKEN_MIN_LENGTH = 16
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
_KIND_LANG_RE = re.compile(r"[a-z0-9_+#-]{1,32}")

_ASSETS_DIR = Path(__file__).resolve().parent / "webview_assets"
_STATIC_ASSETS: dict[str, tuple[str, str]] = {
    "index.html": ("text/html; charset=utf-8", "index.html"),
    "app.js": ("text/javascript; charset=utf-8", "app.js"),
    "style.css": ("text/css; charset=utf-8", "style.css"),
}

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
    "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)

_LATEST_REVISIONS_SQL = (
    "SELECT nr.node_id AS node_id, nr.payload_json AS payload_json FROM node_revision nr "
    "JOIN (SELECT node_id, MAX(version) AS v FROM node_revision GROUP BY node_id) latest "
    "ON latest.node_id = nr.node_id AND latest.v = nr.version "
    "WHERE json_extract(nr.payload_json, '$.lifecycle_status') != 'superseded' "
    "ORDER BY nr.node_id"
)


# ------------------------------------------------------------------ errors


class WebviewError(ValueError):
    """Invalid webview configuration or argument (fail-closed)."""


class StoreUnavailableError(WebviewError):
    """The store path does not exist or is not a readable store file."""


class _NodeNotFound(Exception):
    """Internal: the requested node id is unknown (mapped to 404)."""


class _BadRequest(Exception):
    """Internal: the request parameters failed validation (mapped to 400)."""


# ------------------------------------------------------------------ security


def generate_token() -> str:
    """A fresh URL-safe bearer token for one viewer session."""
    return secrets.token_urlsafe(32)


def escape_json_bytes(payload: Any) -> bytes:
    """Serialize *payload* as deterministic, markup-safe JSON bytes.

    Sorted keys and fixed separators make the output byte-stable; ``&``,
    ``<`` and ``>`` are escaped to ``\\uXXXX`` (``ensure_ascii`` already
    covers U+2028/U+2029), so the bytes can never close an HTML context or
    smuggle markup into the page.
    """
    text = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return text.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").encode("utf-8")


def _split_host_port(header: str) -> tuple[str, int | None]:
    """Parse ``Host`` into (lowercased host, port); port ``None`` if absent/invalid."""
    value = header.strip().lower()
    if not value:
        return "", None
    if value.startswith("["):  # [v6]:port
        end = value.find("]")
        if end == -1:
            return "", None
        host = value[1:end]
        rest = value[end + 1 :]
        if not rest:
            return host, None
        if not rest.startswith(":"):
            return "", None
        port_text = rest[1:]
    else:
        if value.count(":") != 1:
            return value, None  # bare hostname or junk
        host, port_text = value.rsplit(":", 1)
    if not port_text.isdigit():
        return host, None
    return host, int(port_text)


def host_allowed(header: str, port: int, allowlist: Sequence[str] = DEFAULT_HOST_ALLOWLIST) -> bool:
    """The DNS-rebinding guard: loopback host *and* the server's own port.

    A bare host without a port is refused: the browser always sends one for
    a non-default port, and accepting a port-less Host would let a rebound
    origin's fetch (which copies its own port) mismatch silently.
    """
    host, claimed_port = _split_host_port(header)
    if claimed_port is None or claimed_port != port:
        return False
    return host in {entry.lower() for entry in allowlist}


def origin_allowed(origin: str | None, port: int) -> bool:
    """A present ``Origin`` must be loopback with the server's port (rule 2)."""
    if origin is None:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    hostname = (parts.hostname or "").lower()
    if hostname not in DEFAULT_HOST_ALLOWLIST:
        return False
    try:
        return parts.port == port
    except ValueError:
        return False


def _valid_token(token: str) -> bool:
    return len(token) >= _TOKEN_MIN_LENGTH and _TOKEN_RE.fullmatch(token) is not None


# --------------------------------------------------------------- the app


class WebviewApp:
    """Read-only application logic over one store; shared by HTTP and tests."""

    def __init__(self, store_path: str | Path) -> None:
        path = Path(store_path)
        if not path.is_file():
            raise StoreUnavailableError(f"store not found or not a file: {path}")
        self._store_path = path
        self._store = EventStore(path)
        self._closed = False
        self._lookup = SymbolLookup()
        self._hierarchy = CallHierarchy()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._store.close()

    # ------------------------------------------------------- read model

    def _live_nodes(self) -> list[dict[str, Any]]:
        """Latest non-superseded revision per node, canonical id order."""
        with self._store.read() as conn:
            rows = conn.execute(_LATEST_REVISIONS_SQL).fetchall()
        nodes: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            scores = payload.get("scores") or {}
            nodes.append(
                {
                    "id": str(payload["node_id"]),
                    "label": sanitize_text(str(payload.get("summary") or payload["node_id"])),
                    "domain": str(payload.get("domain", "")),
                    "kind": str(payload.get("kind", "leaf")),
                    "lifecycle_status": str(payload.get("lifecycle_status", "active")),
                    "version": int(payload.get("version", 1)),
                    "parent_id": payload.get("parent_id"),
                    "scores": {
                        key: str(scores.get(key, "0.000000000000"))
                        for key in ("p", "m", "e", "uncertainty", "confidence")
                    },
                    "provenance": [sanitize_text(str(item)) for item in payload.get("provenance") or []],
                }
            )
        return nodes

    def _live_links(self, nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """``node_relation`` rows plus ``parent_of`` edges, canonical order."""
        node_ids = {node["id"] for node in nodes}
        links: list[dict[str, Any]] = []
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT source_node_id, target_node_id, relation FROM node_relation ORDER BY relation_seq"
            ).fetchall()
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            source, target, relation = str(row["source_node_id"]), str(row["target_node_id"]), str(row["relation"])
            if source in node_ids and target in node_ids and (source, target, relation) not in seen:
                seen.add((source, target, relation))
                links.append({"source": source, "target": target, "relation": relation, "confidence": "EXTRACTED"})
        for node in nodes:
            parent = node.get("parent_id")
            if isinstance(parent, str) and parent in node_ids:
                key = (parent, node["id"], "parent_of")
                if key not in seen:
                    seen.add(key)
                    links.append(
                        {"source": parent, "target": node["id"], "relation": "parent_of", "confidence": "EXTRACTED"}
                    )
        links.sort(key=lambda link: (link["source"], link["target"], link["relation"]))
        return links

    def _read_model(self, *, cap: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, bool]:
        nodes = self._live_nodes()
        total = len(nodes)
        truncated = total > cap
        kept = nodes[:cap]
        links = self._live_links(kept)
        return kept, links, total, truncated

    # ------------------------------------------------------- endpoints

    def graph_payload(self) -> dict[str, Any]:
        nodes, links, total, truncated = self._read_model(cap=MAX_GRAPH_NODES)
        layout_input = {
            "nodes": [{"id": node["id"]} for node in nodes],
            "links": [{"source": link["source"], "target": link["target"]} for link in links],
        }
        try:
            result = compute_layout(layout_input, width=_FRAME_WIDTH, height=_FRAME_HEIGHT)
            positions = result.positions
            layout_meta = dict(result.metadata)
        except LayoutError:
            positions = {}
            layout_meta = {"mode": "none", "node_count": len(nodes), "quantum": 0.001}
        out_nodes = []
        for node in nodes:
            x, y = positions.get(node["id"], (_FRAME_WIDTH / 2.0, _FRAME_HEIGHT / 2.0))
            entry = {key: value for key, value in node.items() if key != "parent_id"}
            entry["x"], entry["y"] = float(x), float(y)
            out_nodes.append(entry)
        return {
            "schema_version": SCHEMA_VERSION,
            "nodes": out_nodes,
            "edges": [{"source": link["source"], "target": link["target"], "kind": link["relation"]} for link in links],
            "layout": {**layout_meta, "width": _FRAME_WIDTH, "height": _FRAME_HEIGHT},
            "total_nodes": total,
            "total_edges": len(links),
            "truncated": truncated,
        }

    def node_payload(self, node_id: str) -> dict[str, Any]:
        node = self._store.current_node(node_id)
        if node is None:
            raise _NodeNotFound(node_id)
        return self._node_detail(node)

    def _node_detail(self, node: Node) -> dict[str, Any]:
        # Relations and evidence live in their own tables: a node's stored
        # revision predates later relation.add / evidence.append events, so
        # the truth is queried, never read from the stale revision payload.
        relations = [
            {"kind": rel.kind.value, "target": rel.target_node_id}
            for rel in sorted(
                self._store.relations_from(node.node_id),
                key=lambda rel: (rel.kind.value, rel.target_node_id),
            )
        ]
        incident: list[dict[str, str]] = []
        if node.parent_id is not None:
            incident.append({"source": node.parent_id, "target": node.node_id, "kind": "parent_of"})
        for rel in relations:
            incident.append({"source": node.node_id, "target": rel["target"], "kind": rel["kind"]})
        with self._store.read() as conn:
            incoming = conn.execute(
                "SELECT source_node_id, relation FROM node_relation WHERE target_node_id = ? ORDER BY relation_seq",
                (node.node_id,),
            ).fetchall()
            evidence_rows = conn.execute(
                "SELECT evidence_id FROM evidence "
                "WHERE json_extract(payload_json, '$.node_id') = ? ORDER BY evidence_id",
                (node.node_id,),
            ).fetchall()
        evidence_ids = [str(row["evidence_id"]) for row in evidence_rows]
        for row in incoming:
            incident.append(
                {"source": str(row["source_node_id"]), "target": node.node_id, "kind": str(row["relation"])}
            )
        symbol = self._store.symbol_by_node(node.node_id)
        return {
            "schema_version": SCHEMA_VERSION,
            "node_id": node.node_id,
            "parent_id": node.parent_id,
            "domain": node.domain.value,
            "kind": node.kind.value,
            "summary": sanitize_text(node.summary),
            "lifecycle_status": node.lifecycle_status.value,
            "version": node.version,
            "scores": node.scores.quantized(),
            "professional_level": node.scores.professional_level,
            "provenance": [sanitize_text(item) for item in node.provenance],
            "context_tags": [sanitize_text(item) for item in node.context_tags],
            "evidence_ids": evidence_ids,
            "relations": relations,
            "incident_edges": incident,
            "symbol": (
                {"name": symbol.name, "kind": symbol.kind, "path": symbol.path, "line": symbol.line, "col": symbol.col}
                if symbol is not None
                else None
            ),
        }

    def search_payload(self, query: str, *, kind: str | None, lang: str | None, limit: int) -> dict[str, Any]:
        if not query.strip():
            raise _BadRequest("q must be a non-empty search string")
        for label, value in (("kind", kind), ("lang", lang)):
            if value is not None and _KIND_LANG_RE.fullmatch(value) is None:
                raise _BadRequest(f"{label} filter must match {_KIND_LANG_RE.pattern}, got {value!r}")
        if not 1 <= limit <= SEARCH_LIMIT_MAX:
            raise _BadRequest(f"limit must be in [1, {SEARCH_LIMIT_MAX}], got {limit}")
        composed = query.strip()
        if kind is not None:
            composed += f" kind:{kind}"
        if lang is not None:
            composed += f" lang:{lang}"
        try:
            hits = self._lookup.search(self._store, composed, limit=limit)
        except LookupError as error:
            raise _BadRequest(str(error)) from error
        results = [
            {
                "node_id": hit.node_id,
                "name": hit.name,
                "kind": hit.kind,
                "path": hit.path,
                "line": hit.line,
                "col": hit.col,
                "score": hit.score,
                "source": hit.source,
            }
            for hit in hits
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "query": query,
            "filters": {"kind": kind, "lang": lang},
            "limit": limit,
            "count": len(results),
            "results": results,
        }

    def hierarchy_payload(self, node_id: str, *, depth: int) -> dict[str, Any]:
        if not HIERARCHY_DEPTH_MIN <= depth <= HIERARCHY_DEPTH_MAX:
            raise _BadRequest(f"depth must be in [{HIERARCHY_DEPTH_MIN}, {HIERARCHY_DEPTH_MAX}], got {depth}")
        try:
            callers_report = self._hierarchy.callers(self._store, node_id, depth=depth)
            callees_report = self._hierarchy.callees(self._store, node_id, depth=depth)
        except HierarchyError as error:
            raise _BadRequest(str(error)) from error
        if not callers_report.definitions and not callees_report.definitions:
            raise _NodeNotFound(node_id)

        def _entries(report: Any) -> dict[str, Any]:
            entries = [
                {
                    "node_id": entry.node_id,
                    "name": entry.name,
                    "kind": entry.kind,
                    "path": entry.path,
                    "line": entry.line,
                    "depth": entry.depth,
                    "via_edge_kind": entry.via_edge_kind,
                    "via_line": entry.via_line,
                }
                for definition in report.definitions
                for entry in definition.entries
            ]
            return {
                "entries": entries,
                "definitions": len(report.definitions),
                "truncated": any(definition.truncated for definition in report.definitions),
            }

        return {
            "schema_version": SCHEMA_VERSION,
            "node_id": node_id,
            "depth": depth,
            "callers": _entries(callers_report),
            "callees": _entries(callees_report),
        }

    def metrics_payload(self) -> dict[str, Any]:
        return redact_value(collect_ops_metrics(self._store))

    def health_payload(self) -> dict[str, Any]:
        report = run_diagnostics(self._store)
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": report.ok,
            "error_count": report.error_count,
            "warning_count": report.warning_count,
            "worst_severity": report.worst_severity,
            "exit_code": diagnose_exit_code(report),
            "checked": list(report.checked),
            "findings": [
                {
                    "code": finding.code,
                    "severity": finding.severity,
                    "message": sanitize_text(finding.message),
                    "details": redact_value(finding.details),
                }
                for finding in report.findings
            ],
        }

    def analytics_payload(self) -> dict[str, Any]:
        nodes, links, total, truncated = self._read_model(cap=ANALYTICS_MAX_NODES)
        graph = {
            "nodes": [
                {"id": node["id"], "domain": node["domain"], "label": node["label"], "version": node["version"]}
                for node in nodes
            ],
            "links": links,
        }
        centrality = compute_centrality(graph, top=_TOP_RANKING) if nodes else {"global": {}}
        communities = detect_communities(graph["nodes"], links) if nodes else {"communities": []}
        membership = {
            member: community["id"]
            for community in communities.get("communities", [])
            for member in community["members"]
        }
        bridges = (
            find_bridges(graph["nodes"], links, membership=membership)
            if nodes
            else {"bridges": [], "counts": {"candidates": 0, "emitted": 0, "by_kind": {}}}
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "centrality": centrality,
            "communities": communities,
            "bridges": bridges,
            "node_count": len(nodes),
            "total_nodes": total,
            "partial": truncated,
        }

    def audit_payload(self, *, limit: int) -> dict[str, Any]:
        if not 1 <= limit <= AUDIT_LIMIT_MAX:
            raise _BadRequest(f"limit must be in [1, {AUDIT_LIMIT_MAX}], got {limit}")
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT event_seq, event_type, actor, occurred_at, payload_json FROM event_log "
                "WHERE kind = 'audit' ORDER BY event_seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except json.JSONDecodeError:
                payload = None
            events.append(
                {
                    "event_seq": int(row["event_seq"]),
                    "event_type": str(row["event_type"]),
                    "actor": str(row["actor"]),
                    "occurred_at": str(row["occurred_at"]),
                    "payload": redact_value(payload),
                }
            )
        return {"schema_version": SCHEMA_VERSION, "count": len(events), "events": events}

    def proposals_payload(self) -> dict[str, Any]:
        nodes, links, _, _ = self._read_model(cap=ANALYTICS_MAX_NODES)
        communities = detect_communities([{"id": node["id"], "label": node["label"]} for node in nodes], links)
        report = collect_review_signals(
            [{"id": node["id"], "version": node["version"], "provenance": node["provenance"]} for node in nodes],
            links,
            communities=communities.get("communities", []),
        )
        items = report.get("items", [])
        return {
            "schema_version": SCHEMA_VERSION,
            "count": len(items),
            "signals": items,
            "signal_counts": report.get("signal_counts", {}),
        }

    def refs_payload(self) -> dict[str, Any]:
        with self._store.read() as conn:
            counts = {
                str(row["status"]): int(row["n"])
                for row in conn.execute("SELECT status, COUNT(*) AS n FROM unresolved_refs GROUP BY status").fetchall()
            }
            top_failed = conn.execute(
                "SELECT name_tail, COUNT(*) AS n FROM unresolved_refs WHERE status = 'failed' "
                "GROUP BY name_tail ORDER BY n DESC, name_tail LIMIT 20"
            ).fetchall()
            samples = conn.execute(
                "SELECT source_node_id, target_name, name_tail, kind, failure_reason, attempts "
                "FROM unresolved_refs WHERE status = 'failed' ORDER BY ref_id LIMIT 20"
            ).fetchall()
        return {
            "schema_version": SCHEMA_VERSION,
            "pending": counts.get("pending", 0),
            "resolved": counts.get("resolved", 0),
            "failed": counts.get("failed", 0),
            "total": sum(counts.values()),
            "top_failed": [{"name_tail": str(row["name_tail"]), "count": int(row["n"])} for row in top_failed],
            "failed_samples": [
                {
                    "source_node_id": str(row["source_node_id"]),
                    "target_name": str(row["target_name"]),
                    "name_tail": str(row["name_tail"]),
                    "kind": str(row["kind"]),
                    "failure_reason": str(row["failure_reason"]) if row["failure_reason"] is not None else None,
                }
                for row in samples
            ],
        }


# --------------------------------------------------------------- http layer


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "retryable": False}}


class WebviewServer:
    """The loopback-only viewer server; one token per instance."""

    def __init__(
        self,
        store_path: str | Path,
        *,
        port: int = 0,
        token: str | None = None,
        host_allowlist: Sequence[str] = DEFAULT_HOST_ALLOWLIST,
    ) -> None:
        if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
            raise WebviewError(f"port must be an int in [0, 65535], got {port!r}")
        resolved_token = token if token is not None else generate_token()
        if not _valid_token(resolved_token):
            raise WebviewError(
                f"token must be URL-safe ([A-Za-z0-9_-]) and >= {_TOKEN_MIN_LENGTH} chars, got {resolved_token!r}"
            )
        self._app = WebviewApp(store_path)
        self._token = resolved_token
        self._host_allowlist = tuple(host_allowlist)
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._httpd.daemon_threads = True
        self._thread: threading.Thread | None = None
        self._serving = False
        self._closed = False

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def token(self) -> str:
        return self._token

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?token={self._token}"

    def start(self) -> None:
        """Serve in a daemon thread (returns immediately)."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="kys-webview", daemon=True)
        self._thread.start()

    def serve_forever(self) -> None:
        self._serving = True
        try:
            self._httpd.serve_forever()
        finally:
            self._serving = False

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        # BaseServer.shutdown() blocks forever unless serve_forever() is
        # actually looping, so only signal it when serving (started in a
        # thread or foreground); an unstarted server just closes.
        if self._thread is not None or self._serving:
            self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        self._app.close()

    # ---------------------------------------------------------- handler

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            server_version = "KysWebview/1"
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
                return  # quiet by design: the terminal belongs to the operator

            # -- plumbing ------------------------------------------------

            def _respond(self, status: int, body: bytes, content_type: str, *, head_only: bool = False) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Cache-Control", "no-store")
                if content_type.startswith("text/html"):
                    self.send_header("Content-Security-Policy", _CSP)
                self.end_headers()
                if not head_only:
                    self.wfile.write(body)

            def _respond_json(self, status: int, payload: Any, *, head_only: bool = False) -> None:
                self._respond(
                    status, escape_json_bytes(payload), "application/json; charset=utf-8", head_only=head_only
                )

            def _reject(self, status: int, code: str, message: str) -> None:
                self._respond_json(status, _error_payload(code, message))

            # -- guards (order: host -> origin -> method -> token) -------

            def _guards_pass(self) -> bool:
                host = self.headers.get("Host")
                if host is None or not host_allowed(host, server_ref.port, server_ref._host_allowlist):
                    self._respond_json(
                        403, _error_payload("HOST_NOT_ALLOWED", "Host header is not an allowlisted loopback origin")
                    )
                    return False
                origin = self.headers.get("Origin")
                if not origin_allowed(origin, server_ref.port):
                    self._respond_json(
                        403, _error_payload("ORIGIN_NOT_ALLOWED", "Origin header is not an allowlisted loopback origin")
                    )
                    return False
                query = parse_qs(urlsplit(self.path).query)
                provided = query.get("token", [None])[-1]
                if provided is None or not hmac.compare_digest(str(provided), server_ref._token):
                    self._respond_json(401, _error_payload("UNAUTHORIZED", "a valid token query parameter is required"))
                    return False
                return True

            # -- verbs ---------------------------------------------------

            def do_GET(self) -> None:
                self._dispatch(head_only=False)

            def do_HEAD(self) -> None:
                self._dispatch(head_only=True)

            def do_POST(self) -> None:
                self._method_not_allowed()

            def do_PUT(self) -> None:
                self._method_not_allowed()

            def do_DELETE(self) -> None:
                self._method_not_allowed()

            def do_PATCH(self) -> None:
                self._method_not_allowed()

            def do_OPTIONS(self) -> None:
                self._method_not_allowed()

            def _method_not_allowed(self) -> None:
                # Guards still run first: an unauthenticated probe must not
                # learn which methods exist.
                if self._guards_pass():
                    self._reject(405, "METHOD_NOT_ALLOWED", "the viewer is read-only; only GET and HEAD exist")

            def _dispatch(self, *, head_only: bool) -> None:
                if not self._guards_pass():
                    return
                try:
                    self._route(head_only=head_only)
                except BrokenPipeError, ConnectionResetError:
                    pass  # client left; nothing to report

            # -- routing -------------------------------------------------

            def _route(self, *, head_only: bool) -> None:
                split = urlsplit(self.path)
                raw_path = split.path
                if "%" in raw_path:
                    raw_path = unquote(raw_path, errors="strict")
                params = parse_qs(split.query)
                if raw_path == "/":
                    self._serve_asset("index.html", head_only=head_only)
                    return
                if raw_path.startswith("/static/"):
                    name = raw_path[len("/static/") :]
                    if "/" in name or name not in _STATIC_ASSETS or name == "index.html":
                        self._reject(404, "NOT_FOUND", "unknown static asset")
                        return
                    self._serve_asset(name, head_only=head_only)
                    return
                if raw_path.startswith("/api/"):
                    self._route_api(raw_path[len("/api/") :], params, head_only=head_only)
                    return
                self._reject(404, "NOT_FOUND", "unknown path")

            def _serve_asset(self, name: str, *, head_only: bool) -> None:
                content_type, filename = _STATIC_ASSETS[name]
                try:
                    body = (_ASSETS_DIR / filename).read_bytes()
                except OSError:
                    self._reject(404, "NOT_FOUND", "asset missing from the installed package")
                    return
                self._respond(200, body, content_type, head_only=head_only)

            def _route_api(self, tail: str, params: Mapping[str, list[str]], *, head_only: bool) -> None:
                app = server_ref._app

                def _int_param(name: str, default: int) -> int:
                    raw = params.get(name, [None])[-1]
                    if raw is None:
                        return default
                    if not re.fullmatch(r"[0-9]+", raw):
                        raise _BadRequest(f"{name} must be a non-negative integer, got {raw!r}")
                    return int(raw)

                def _str_param(name: str) -> str | None:
                    raw = params.get(name, [None])[-1]
                    return raw if raw else None

                try:
                    if tail == "graph":
                        payload = app.graph_payload()
                    elif tail.startswith("node/"):
                        node_id = tail[len("node/") :]
                        if not node_id or "/" in node_id:
                            raise _BadRequest("node id must be a single non-empty path segment")
                        payload = app.node_payload(node_id)
                    elif tail == "search":
                        payload = app.search_payload(
                            params.get("q", [""])[-1],
                            kind=_str_param("kind"),
                            lang=_str_param("lang"),
                            limit=_int_param("limit", 10),
                        )
                    elif tail.startswith("hierarchy/"):
                        node_id = tail[len("hierarchy/") :]
                        if not node_id or "/" in node_id:
                            raise _BadRequest("node id must be a single non-empty path segment")
                        payload = app.hierarchy_payload(node_id, depth=_int_param("depth", 1))
                    elif tail == "metrics":
                        payload = app.metrics_payload()
                    elif tail == "health":
                        payload = app.health_payload()
                    elif tail == "analytics":
                        payload = app.analytics_payload()
                    elif tail == "audit":
                        payload = app.audit_payload(limit=_int_param("limit", 50))
                    elif tail == "proposals":
                        payload = app.proposals_payload()
                    elif tail == "refs":
                        payload = app.refs_payload()
                    else:
                        self._reject(404, "NOT_FOUND", "unknown api endpoint")
                        return
                except _NodeNotFound:
                    self._reject(404, "NOT_FOUND", "unknown node id")
                    return
                except _BadRequest as error:
                    self._reject(400, "BAD_REQUEST", str(error))
                    return
                self._respond_json(200, payload, head_only=head_only)

        return _Handler


# ------------------------------------------------------------------ cli


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m know_your_soul.serve.webview`` entry point."""
    parser = argparse.ArgumentParser(
        prog="webview",
        description="loopback-only, token-authenticated, read-only web viewer for the knowledge store",
    )
    parser.add_argument("--store", required=True, help="path to the store SQLite file (must exist)")
    parser.add_argument("--port", type=int, default=0, help="loopback port; 0 picks a free one (default)")
    parser.add_argument("--token", default=None, help="explicit session token (default: generated per start)")
    parser.add_argument("--open", dest="open_browser", action="store_true", help="open the URL in a browser")
    parser.add_argument(
        "--serve-once",
        action="store_true",
        help="print the URL and exit immediately (smoke/CI mode; the server is shut down again)",
    )
    args = parser.parse_args(argv)
    try:
        server = WebviewServer(args.store, port=int(args.port), token=args.token)
    except WebviewError as error:
        print(f"webview: {error}", file=sys.stderr)
        return 2
    print(f"webview: {server.url}", flush=True)
    if args.open_browser:
        webbrowser.open(server.url)
    if args.serve_once:
        server.shutdown()
        return 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
