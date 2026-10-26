"""Static single-file HTML knowledge-graph viewer (ATM F-B5).

The viewer is one self-contained document:

* **No external anything.** No CDN, no web fonts, no images, no ``fetch``,
  no ``eval`` — enforced structurally by a strict Content Security Policy
  (``default-src 'none'`` plus inline script/style only) and by emitting every
  byte ourselves.
* **Embedded canonical data.** The graph index is serialized once as
  canonical JSON inside an inert ``application/json`` island. The serialized
  text escapes ``&``, ``<``, ``>`` and the JS line separators, so no payload
  can ever close the island early (anti stored-XSS).
* **Safe DOM access.** The small vanilla script renders exclusively through
  ``textContent`` and ``createElement`` behind a single delegated ``click``
  listener on the document; no string of graph data ever becomes markup.
* **Sanitization.** Every node text field passes through
  :func:`know_your_soul.security.sanitize_text` before serialization.
* **Determinism.** Byte-identical output for a given read model: canonical
  ordering everywhere, coordinates quantized to three decimals, no clock.
* **Explicit caps.** ``max_nodes`` bounds the embedded index; overflow is
  elided with a *marked* banner (never silent). ``max_bytes`` is a
  fail-closed typed breach.

Layout positions arrive as a parameter (from a layout engine or a caller's
own placement). Nodes without a supplied position fall back to a simple
internal grid — this module never imports the layout engine.

origin: feature idea from Graphify ``exporters/html.py`` (single-file viewer,
search, neighbour panel, legend); markup, CSS, JS and palette are original.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from html import escape as _html_escape
from typing import Any

from know_your_soul.domain import PRIMARY_DOMAINS, canonical_json
from know_your_soul.exporters.orchestrator import (
    ExportError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
)
from know_your_soul.security import sanitize_text

__all__ = [
    "HTML_VIEW_MAX_BYTES",
    "MAX_NODES_FOR_VIEW",
    "SCHEMA_VERSION",
    "HtmlViewError",
    "html_view_format",
    "register_html_view",
    "render_html_view",
]

SCHEMA_VERSION = 1
MAX_NODES_FOR_VIEW = 5000
HTML_VIEW_MAX_BYTES = 4 * 1024 * 1024

_CANVAS = 1000
_COORD_QUANTUM = Decimal("0.001")
_ISLAND_OPEN = '<script type="application/json" id="graph-data">'
_ISLAND_CLOSE = "</script>"

# Our own categorical palette (original values; one colour per primary
# domain, shared by the legend swatches, the SVG nodes and the node list).
_DOMAIN_COLORS: dict[str, str] = {
    "P1": "#2e6f9e",
    "P2": "#9e5a2e",
    "P3": "#3d8a5a",
    "P4": "#8a3d6e",
    "P5": "#6b5cae",
    "P6": "#a8842c",
    "P7": "#4f7f7a",
}
_FALLBACK_COLOR = "#6e6e78"


class HtmlViewError(ExportError):
    """A typed HTML-viewer contract violation (cap, bad input, bad position)."""


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return sanitize_text(str(value))


def _score_text(scores: Any, key: str) -> str:
    if isinstance(scores, Mapping):
        value = scores.get(key)
        if value is not None:
            return str(value)
    return "0.000000000000"


def _quantize_coord(value: Any, node_id: str) -> str:
    try:
        quantized = Decimal(str(value)).quantize(_COORD_QUANTUM, rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, ValueError) as error:
        raise HtmlViewError(f"position for node {node_id!r} is not a number: {value!r}") from error
    return format(quantized, "f")


def _grid_positions(node_ids: list[str]) -> dict[str, tuple[str, str]]:
    """Deterministic fallback placement: row-major grid in sorted-id order."""
    count = len(node_ids)
    columns = max(1, math.ceil(math.sqrt(count)))
    rows = max(1, math.ceil(count / columns))
    cell_w = Decimal(_CANVAS) / columns
    cell_h = Decimal(_CANVAS) / rows
    positions: dict[str, tuple[str, str]] = {}
    for index, node_id in enumerate(node_ids):
        column, row = index % columns, index // columns
        x = ((Decimal(column) + Decimal("0.5")) * cell_w).quantize(_COORD_QUANTUM, rounding=ROUND_HALF_EVEN)
        y = ((Decimal(row) + Decimal("0.5")) * cell_h).quantize(_COORD_QUANTUM, rounding=ROUND_HALF_EVEN)
        positions[node_id] = (format(x, "f"), format(y, "f"))
    return positions


def _json_island(payload: Mapping[str, Any]) -> str:
    """Canonical JSON safe to embed inside a ``script`` island."""
    raw = canonical_json(payload)
    return (
        raw.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _domain_class(domain: str) -> str:
    return f"nd-{domain}" if domain in _DOMAIN_COLORS else "nd-x"


def _build_payload(
    read_model: Mapping[str, Any],
    *,
    positions: Mapping[str, Any] | None,
    max_nodes: int,
    title: str,
) -> dict[str, Any]:
    raw_nodes = read_model.get("nodes")
    nodes = [node for node in raw_nodes if isinstance(node, Mapping)] if isinstance(raw_nodes, list) else []
    raw_links = read_model.get("links")
    links = [link for link in raw_links if isinstance(link, Mapping)] if isinstance(raw_links, list) else []

    seen: set[str] = set()
    ordered_ids: list[str] = []
    by_id: dict[str, Mapping[str, Any]] = {}
    for node in sorted(nodes, key=lambda entry: str(entry.get("id"))):
        node_id = str(node.get("id"))
        if node_id in seen:
            continue
        seen.add(node_id)
        ordered_ids.append(node_id)
        by_id[node_id] = node

    total = len(ordered_ids)
    kept_ids = ordered_ids[:max_nodes]
    kept = set(kept_ids)

    grid = _grid_positions(kept_ids)

    def _position_of(node_id: str) -> tuple[str, str]:
        if positions is not None and node_id in positions:
            pair = positions[node_id]
            try:
                raw_x, raw_y = pair  # type: ignore[misc]
            except (TypeError, ValueError) as error:
                raise HtmlViewError(f"position for node {node_id!r} must be an (x, y) pair") from error
            return _quantize_coord(raw_x, node_id), _quantize_coord(raw_y, node_id)
        return grid[node_id]

    neighbours: dict[str, list[dict[str, str]]] = {node_id: [] for node_id in kept_ids}
    clean_links: list[dict[str, str]] = []
    seen_links: set[tuple[str, str, str]] = set()
    for link in sorted(
        links,
        key=lambda entry: (str(entry.get("source")), str(entry.get("target")), str(entry.get("relation"))),
    ):
        source, target = str(link.get("source")), str(link.get("target"))
        if source not in kept or target not in kept or source == target:
            continue
        relation = _clean_text(link.get("relation")) or "related_to"
        key = (source, target, relation)
        if key in seen_links:
            continue
        seen_links.add(key)
        clean_links.append({"source": source, "target": target, "relation": relation})
        neighbours[source].append({"id": target, "relation": relation, "direction": "out"})
        neighbours[target].append({"id": source, "relation": relation, "direction": "in"})

    payload_nodes: list[dict[str, Any]] = []
    for node_id in kept_ids:
        node = by_id[node_id]
        scores = node.get("scores")
        x, y = _position_of(node_id)
        version_raw = node.get("version")
        provenance_raw = node.get("provenance")
        provenance = (
            [_clean_text(entry) for entry in provenance_raw if entry is not None]
            if isinstance(provenance_raw, list)
            else []
        )
        payload_nodes.append(
            {
                "id": node_id,
                "label": _clean_text(node.get("label")) or node_id,
                "domain": _clean_text(node.get("domain")) or "unknown",
                "kind": _clean_text(node.get("kind")) or "unknown",
                "version": version_raw if isinstance(version_raw, int) else 0,
                "scores": {
                    "p": _score_text(scores, "p"),
                    "m": _score_text(scores, "m"),
                    "e": _score_text(scores, "e"),
                    "uncertainty": _score_text(scores, "uncertainty"),
                    "confidence": _score_text(scores, "confidence"),
                },
                "confidence": _clean_text(node.get("confidence")) or "0.000000000000",
                "professional_level": node.get("professional_level")
                if isinstance(node.get("professional_level"), int)
                else 0,
                "provenance": provenance,
                "neighbors": sorted(
                    neighbours[node_id], key=lambda entry: (entry["id"], entry["relation"], entry["direction"])
                ),
                "x": x,
                "y": y,
            }
        )

    domains = sorted({node["domain"] for node in payload_nodes})
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact": "html-view",
        "title": _clean_text(title) or "Knowledge Graph",
        "elided": {"cap": max_nodes, "total": total, "omitted": total - len(kept_ids)},
        "domains": domains,
        "nodes": payload_nodes,
        "links": clean_links,
    }


# ------------------------------------------------------------------ document

_STYLES = """
:root{
  --bg:#f7f6f3;--fg:#1d2433;--panel:#ffffff;--line:#d8d4cc;--muted:#5b6472;
  --accent:#2e6f9e;--accent-fg:#ffffff;
  --c-p1:#2e6f9e;--c-p2:#9e5a2e;--c-p3:#3d8a5a;--c-p4:#8a3d6e;
  --c-p5:#6b5cae;--c-p6:#a8842c;--c-p7:#4f7f7a;--c-x:#6e6e78;
}
[data-theme="dark"]{
  --bg:#14161c;--fg:#e8e6e1;--panel:#1d2029;--line:#3a3f4d;--muted:#9aa2b1;
  --accent:#7fb2dd;--accent-fg:#10131a;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:ui-sans-serif,system-ui,sans-serif;font-size:14px;line-height:1.45}
header{display:flex;gap:12px;align-items:center;padding:10px 16px;
  border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:16px;margin:0;flex:0 0 auto}
header input{flex:1 1 auto;padding:6px 10px;border:1px solid var(--line);border-radius:6px;
  background:var(--bg);color:var(--fg)}
header button,#node-list button,#p-neighbors button{cursor:pointer;border:1px solid var(--line);
  border-radius:6px;background:var(--panel);color:var(--fg);padding:4px 8px}
main{display:grid;grid-template-columns:230px minmax(0,1fr) 320px;gap:12px;padding:12px 16px}
aside,section.stage{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:4px 0 8px}
#legend ul{list-style:none;margin:0 0 12px;padding:0}
#legend li{display:flex;align-items:center;gap:8px;margin:2px 0}
.sw{width:12px;height:12px;border-radius:3px;flex:0 0 auto}
#node-list{list-style:none;margin:0;padding:0;max-height:46vh;overflow:auto}
#node-list li{margin:1px 0}
#node-list button{display:block;width:100%;text-align:left;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#node-list button[aria-selected="true"]{background:var(--accent);color:var(--accent-fg);border-color:var(--accent)}
.stage svg{width:100%;height:auto;display:block}
.edge{stroke:var(--line);stroke-width:1.5}
.nd{stroke:var(--bg);stroke-width:1.5;cursor:pointer}
.nd:hover{stroke:var(--fg)}
.nd-P1{fill:var(--c-p1)}.nd-P2{fill:var(--c-p2)}.nd-P3{fill:var(--c-p3)}.nd-P4{fill:var(--c-p4)}
.nd-P5{fill:var(--c-p5)}.nd-P6{fill:var(--c-p6)}.nd-P7{fill:var(--c-p7)}.nd-x{fill:var(--c-x)}
#panel dl{display:grid;grid-template-columns:auto 1fr;gap:2px 8px;margin:0 0 8px}
#panel dt{color:var(--muted)}#panel dd{margin:0;overflow-wrap:anywhere}
#p-provenance,#p-neighbors{list-style:none;margin:0 0 8px;padding:0;max-height:22vh;overflow:auto}
#p-neighbors button{margin:1px 0}
#elision{border:1px dashed var(--accent);border-radius:6px;padding:6px 8px;margin:0 0 10px;color:var(--muted)}
footer{padding:8px 16px;color:var(--muted);border-top:1px solid var(--line);font-size:12px}
"""

# One delegated listener at the document root; every dynamic string is
# assigned through textContent. There is no fetch, no eval, no markup built
# from graph data — the CSP above forbids anything external regardless.
_SCRIPT = """
(function () {
  "use strict";
  var island = document.getElementById("graph-data");
  var data = JSON.parse(island.textContent);
  var byId = Object.create(null);
  var i;
  for (i = 0; i < data.nodes.length; i = i + 1) { byId[data.nodes[i].id] = data.nodes[i]; }

  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }

  function listButton(nodeId, text) {
    var li = document.createElement("li");
    var button = document.createElement("button");
    button.type = "button";
    button.setAttribute("data-node-id", nodeId);
    button.textContent = text;
    li.appendChild(button);
    return li;
  }

  var listEl = document.getElementById("node-list");
  function renderList(query) {
    clear(listEl);
    var needle = query.toLowerCase();
    for (var j = 0; j < data.nodes.length; j = j + 1) {
      var node = data.nodes[j];
      if (needle && node.id.toLowerCase().indexOf(needle) < 0 && node.label.toLowerCase().indexOf(needle) < 0) {
        continue;
      }
      listEl.appendChild(listButton(node.id, node.label + " — " + node.id));
    }
  }

  function setText(id, value) { document.getElementById(id).textContent = value; }

  function showNode(nodeId) {
    var node = byId[nodeId];
    if (!node) { return; }
    setText("p-label", node.label);
    setText("p-id", node.id);
    setText("p-domain", node.domain);
    setText("p-kind", node.kind);
    setText("p-version", String(node.version));
    setText("p-p", node.scores.p);
    setText("p-m", node.scores.m);
    setText("p-e", node.scores.e);
    setText("p-u", node.scores.uncertainty);
    setText("p-c", node.scores.confidence);
    setText("p-level", String(node.professional_level));
    var prov = document.getElementById("p-provenance");
    clear(prov);
    node.provenance.forEach(function (entry) {
      var li = document.createElement("li");
      li.textContent = entry;
      prov.appendChild(li);
    });
    var neigh = document.getElementById("p-neighbors");
    clear(neigh);
    node.neighbors.forEach(function (edge) {
      var other = byId[edge.id];
      var arrow = edge.direction === "out" ? "→ " : "← ";
      var text = arrow + edge.relation + "  " + (other ? other.label : edge.id);
      neigh.appendChild(listButton(edge.id, text));
    });
    var buttons = listEl.querySelectorAll("button");
    for (var k = 0; k < buttons.length; k = k + 1) {
      buttons[k].setAttribute("aria-selected", buttons[k].getAttribute("data-node-id") === nodeId ? "true" : "false");
    }
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!target || !target.closest) { return; }
    var el = target.closest("[data-node-id]");
    if (el) { showNode(el.getAttribute("data-node-id")); }
  });

  var themeButton = document.getElementById("theme-toggle");
  function setTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    try { window.localStorage.setItem("kys-viewer-theme", theme); } catch (error) { /* storage may be unavailable */ }
  }
  themeButton.addEventListener("click", function () {
    setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
  });
  var stored = null;
  try { stored = window.localStorage.getItem("kys-viewer-theme"); } catch (error) { stored = null; }
  if (stored === "dark" || stored === "light") { setTheme(stored); }
  else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) { setTheme("dark"); }

  var search = document.getElementById("q");
  search.addEventListener("input", function () { renderList(search.value); });
  renderList("");
})();
"""


def _legend_html(domains_present: list[str]) -> str:
    rows: list[str] = []
    titles = {domain.value: domain.title for domain in PRIMARY_DOMAINS}
    ordered = [code for code in titles] + [d for d in domains_present if d not in titles]
    for code in ordered:
        title = titles.get(code, code)
        color = _DOMAIN_COLORS.get(code, _FALLBACK_COLOR)
        rows.append(
            '<li><span class="sw" style="background-color:'
            + color
            + '"></span><span>'
            + _html_escape(f"{code} — {title}")
            + "</span></li>"
        )
    return "<ul>" + "".join(rows) + "</ul>"


def _svg_html(payload: Mapping[str, Any]) -> str:
    parts: list[str] = ['<svg id="stage-svg" viewBox="0 0 1000 1000" role="img" aria-label="Knowledge graph">']
    positions = {node["id"]: (node["x"], node["y"]) for node in payload["nodes"]}
    parts.append('<g class="edges">')
    for link in payload["links"]:
        x1, y1 = positions[link["source"]]
        x2, y2 = positions[link["target"]]
        parts.append(f'<line class="edge" x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}"></line>')
    parts.append("</g><g>")
    for node in payload["nodes"]:
        node_id = _html_escape(node["id"], quote=True)
        label = _html_escape(node["label"])
        parts.append(
            f'<circle class="nd {_domain_class(node["domain"])}" cx="{node["x"]}" cy="{node["y"]}" r="9" '
            f'data-node-id="{node_id}"><title>{label}</title></circle>'
        )
    parts.append("</g></svg>")
    return "".join(parts)


def render_html_view(
    read_model: Mapping[str, Any],
    *,
    positions: Mapping[str, Any] | None = None,
    max_nodes: int = MAX_NODES_FOR_VIEW,
    max_bytes: int | None = HTML_VIEW_MAX_BYTES,
    title: str = "Knowledge Graph",
) -> str:
    """Render the read model as one deterministic, self-contained HTML page.

    ``positions`` optionally maps node id → ``(x, y)`` on a ``0..1000``
    canvas (e.g. from a deterministic layout engine); nodes without a
    supplied position fall back to the internal grid. When the graph exceeds
    ``max_nodes`` the first ``max_nodes`` nodes by sorted id are kept and the
    elision is marked in the payload and on the page — never silent.
    """
    if max_nodes < 1:
        raise HtmlViewError(f"max_nodes must be >= 1, got {max_nodes}")
    if max_bytes is not None and max_bytes < 1:
        raise HtmlViewError(f"max_bytes must be positive, got {max_bytes}")

    payload = _build_payload(read_model, positions=positions, max_nodes=max_nodes, title=title)
    island = _json_island(payload)
    safe_title = _html_escape(payload["title"])

    if payload["elided"]["omitted"]:
        elision = (
            '<p id="elision">Index capped: showing '
            + str(len(payload["nodes"]))
            + " of "
            + str(payload["elided"]["total"])
            + " nodes — "
            + str(payload["elided"]["omitted"])
            + " omitted by the "
            + str(payload["elided"]["cap"])
            + "-node cap.</p>"
        )
    else:
        elision = '<p id="elision" hidden></p>'

    document = "".join(
        [
            "<!doctype html>\n",
            '<html lang="en" data-theme="light">\n<head>\n',
            '<meta charset="utf-8">\n',
            '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            "script-src 'unsafe-inline'; style-src 'unsafe-inline'\">\n",
            f"<title>{safe_title}</title>\n",
            "<style>",
            _STYLES,
            "</style>\n</head>\n<body>\n",
            "<header>\n",
            f"<h1>{safe_title}</h1>\n",
            '<input id="q" type="search" placeholder="Search nodes…" aria-label="Search nodes">\n',
            '<button id="theme-toggle" type="button">Toggle theme</button>\n',
            "</header>\n<main>\n",
            '<aside id="legend">\n<h2>Domains</h2>\n',
            _legend_html(payload["domains"]),
            "<h2>Nodes</h2>\n",
            elision,
            "\n",
            '<ul id="node-list"></ul>\n</aside>\n',
            '<section class="stage">',
            _svg_html(payload),
            "</section>\n",
            '<aside id="panel">\n<h2>Node</h2>\n',
            '<dl><dt>Label</dt><dd id="p-label">—</dd>',
            '<dt>ID</dt><dd id="p-id">—</dd>',
            '<dt>Domain</dt><dd id="p-domain">—</dd>',
            '<dt>Kind</dt><dd id="p-kind">—</dd>',
            '<dt>Version</dt><dd id="p-version">—</dd>',
            '<dt>P</dt><dd id="p-p">—</dd>',
            '<dt>M</dt><dd id="p-m">—</dd>',
            '<dt>E</dt><dd id="p-e">—</dd>',
            '<dt>Uncertainty</dt><dd id="p-u">—</dd>',
            '<dt>Confidence</dt><dd id="p-c">—</dd>',
            '<dt>Level</dt><dd id="p-level">—</dd></dl>\n',
            '<h2>Provenance</h2>\n<ul id="p-provenance"></ul>\n',
            '<h2>Neighbors</h2>\n<ul id="p-neighbors"></ul>\n',
            "</aside>\n</main>\n",
            "<footer>Know Your Soul — deterministic static export. No network, no scripts beyond this file.</footer>\n",
            _ISLAND_OPEN,
            island,
            _ISLAND_CLOSE,
            "\n<script>",
            _SCRIPT,
            "</script>\n</body>\n</html>\n",
        ]
    )

    if max_bytes is not None and len(document.encode("utf-8")) > max_bytes:
        raise HtmlViewError(f"html view is {len(document.encode('utf-8'))} bytes, over the {max_bytes}-byte cap")
    return document


# ------------------------------------------------------------ registry seam


def _roundtrip(data: bytes) -> None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"graph.html does not decode as UTF-8: {error}") from error
    if not text.startswith("<!doctype html>"):
        raise ExportRoundTripError("graph.html is missing its doctype")
    if _ISLAND_OPEN not in text or _ISLAND_CLOSE not in text.split(_ISLAND_OPEN, 1)[1]:
        raise ExportRoundTripError("graph.html is missing its embedded JSON island")
    raw = text.split(_ISLAND_OPEN, 1)[1].split(_ISLAND_CLOSE, 1)[0]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ExportRoundTripError(f"graph.html island does not parse: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ExportRoundTripError("graph.html island carries an unknown schema_version")
    if not isinstance(payload.get("nodes"), list) or not isinstance(payload.get("links"), list):
        raise ExportRoundTripError("graph.html island is missing nodes/links")


def html_view_format(
    *,
    max_nodes: int = MAX_NODES_FOR_VIEW,
    max_bytes: int = HTML_VIEW_MAX_BYTES,
    title: str = "Knowledge Graph",
    name: str = "exports/graph.html",
) -> ExportFormat:
    """The ``exports/graph.html`` registry entry (producer + round-trip).

    The byte cap is enforced by the orchestrator (fail-closed
    ``ExportCapExceededError``), so the producer renders without its own
    byte cap; ``max_nodes`` elision stays inside the producer.
    """

    def _producer(read_model: Mapping[str, Any]) -> bytes:
        rendered = render_html_view(read_model, positions=None, max_nodes=max_nodes, max_bytes=None, title=title)
        return rendered.encode("utf-8")

    return ExportFormat(
        name=name,
        version=SCHEMA_VERSION,
        kind="format",
        max_bytes=max_bytes,
        producer=_producer,
        roundtrip=_roundtrip,
    )


def register_html_view(
    registry: ExportRegistry,
    *,
    max_nodes: int = MAX_NODES_FOR_VIEW,
    max_bytes: int = HTML_VIEW_MAX_BYTES,
    title: str = "Knowledge Graph",
    name: str = "exports/graph.html",
) -> None:
    """Register the viewer on an existing registry (integration seam)."""
    registry.register(html_view_format(max_nodes=max_nodes, max_bytes=max_bytes, title=title, name=name))


# A stable content fingerprint of the viewer shell (styles + script), handy
# for audits: two shells with the same fingerprint are byte-identical.
def _shell_fingerprint() -> str:
    return hashlib.sha256((_STYLES + _SCRIPT).encode("utf-8")).hexdigest()[:16]
