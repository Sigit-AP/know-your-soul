"""Contract and property tests for the static single-file HTML viewer (ATM F-B5).

The viewer is a fully self-contained document: no CDN, no external fonts, no
``fetch``, no ``eval``; all graph data is embedded as canonical JSON inside an
inert ``application/json`` island with strict escaping, and the small vanilla
JS layer renders exclusively through ``textContent`` plus a single delegated
click listener (no ``innerHTML`` built from data).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.html_view import (
    HTML_VIEW_MAX_BYTES,
    MAX_NODES_FOR_VIEW,
    HtmlViewError,
    html_view_format,
    register_html_view,
    render_html_view,
)
from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportRegistry,
    manifest_entries,
    produce_exports,
)

_ISLAND_OPEN = '<script type="application/json" id="graph-data">'
_ISLAND_CLOSE = "</script>"


def _node(
    node_id: str,
    *,
    label: str | None = None,
    domain: str = "P1",
    kind: str = "CHILD",
    version: int = 1,
    scores: dict[str, str] | None = None,
    provenance: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "label": label if label is not None else node_id,
        "domain": domain,
        "kind": kind,
        "version": version,
        "scores": scores
        or {
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
            _node("c-node", label="Gamma summary", domain="P7", kind="PRIMARY"),
        ],
        "links": [
            {"source": "a-node", "target": "b-node", "relation": "depends_on"},
            {"source": "c-node", "target": "a-node", "relation": "parent_of"},
        ],
    }


def _payload(html: str) -> dict[str, Any]:
    assert _ISLAND_OPEN in html, "embedded JSON island missing"
    body = html.split(_ISLAND_OPEN, 1)[1]
    raw = body.split(_ISLAND_CLOSE, 1)[0]
    return json.loads(raw)


# ------------------------------------------------------------------ structure


def test_render_is_single_self_contained_document() -> None:
    html = render_html_view(_read_model())
    assert html.startswith("<!doctype html>")
    assert html.rstrip().endswith("</html>")
    assert "<style>" in html and "</style>" in html
    assert _ISLAND_OPEN in html
    # Content security policy: nothing external may load, ever.
    assert "Content-Security-Policy" in html
    assert "default-src 'none'" in html
    # Light/dark themes via CSS custom properties + a toggle.
    assert ":root" in html and "--bg" in html
    assert '[data-theme="dark"]' in html
    assert 'data-theme="light"' in html
    assert 'id="theme-toggle"' in html


def test_no_external_resources_or_active_builtins() -> None:
    html = render_html_view(_read_model())
    for forbidden in ("http://", "https://", "@import", "<link", "src=", "url("):
        assert forbidden not in html, f"external resource marker {forbidden!r} present"
    for forbidden in ("fetch(", "eval(", "innerHTML", "document.write", "XMLHttpRequest"):
        assert forbidden not in html, f"active builtin {forbidden!r} present"


def test_js_uses_delegated_listener_and_textcontent() -> None:
    html = render_html_view(_read_model())
    assert "addEventListener" in html
    assert "textContent" in html


def test_domain_legend_covers_p1_to_p7() -> None:
    html = render_html_view(_read_model())
    for code in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert code in html
    for title in ("Pembelajaran", "Gagasan", "Tindakan", "Keputusan", "Disiplin", "Level Profesional"):
        assert title in html


def test_payload_carries_node_panel_fields() -> None:
    payload = _payload(render_html_view(_read_model()))
    assert payload["schema_version"] == 1
    assert payload["artifact"] == "html-view"
    nodes = {node["id"]: node for node in payload["nodes"]}
    alpha = nodes["a-node"]
    assert alpha["label"] == "Alpha summary"
    assert alpha["scores"]["p"] == "0.100000000000"
    assert alpha["scores"]["m"] == "0.200000000000"
    assert alpha["scores"]["e"] == "0.300000000000"
    assert alpha["version"] == 2
    assert alpha["provenance"] == ["soul://store"]
    neighbours = {(n["id"], n["relation"], n["direction"]) for n in alpha["neighbors"]}
    assert neighbours == {("b-node", "depends_on", "out"), ("c-node", "parent_of", "in")}
    assert [node["id"] for node in payload["nodes"]] == sorted(nodes)


# --------------------------------------------------------------- determinism


def test_determinism_input_order_independent() -> None:
    model = _read_model()
    shuffled = {
        "directed": True,
        "nodes": list(reversed(model["nodes"])),
        "links": list(reversed(model["links"])),
    }
    assert render_html_view(model) == render_html_view(shuffled)


def test_repeat_render_byte_identical() -> None:
    assert render_html_view(_read_model()) == render_html_view(_read_model())


# ----------------------------------------------------------------- positions


def test_positions_parameter_is_honored() -> None:
    html = render_html_view(_read_model(), positions={"a-node": (1, 2), "b-node": ("3.5", 4)})
    payload = _payload(html)
    nodes = {node["id"]: node for node in payload["nodes"]}
    assert nodes["a-node"]["x"] == "1.000"
    assert nodes["a-node"]["y"] == "2.000"
    assert nodes["b-node"]["x"] == "3.500"
    assert nodes["b-node"]["y"] == "4.000"
    # Nodes without an explicit position fall back to the internal grid.
    assert "x" in nodes["c-node"] and "y" in nodes["c-node"]


def test_grid_fallback_is_deterministic() -> None:
    first = _payload(render_html_view(_read_model()))
    second = _payload(render_html_view(_read_model()))
    assert [(n["id"], n["x"], n["y"]) for n in first["nodes"]] == [(n["id"], n["x"], n["y"]) for n in second["nodes"]]
    assert len({(n["x"], n["y"]) for n in first["nodes"]}) == len(first["nodes"])


def test_invalid_position_raises_typed() -> None:
    with pytest.raises(HtmlViewError, match="position"):
        render_html_view(_read_model(), positions={"a-node": ("not-a-number", 0)})


# -------------------------------------------------------------------- elision


def test_max_nodes_elision_is_marked() -> None:
    model = _read_model()
    model["nodes"].append(_node("d-node", domain="P3"))
    model["nodes"].append(_node("e-node", domain="P3"))
    html = render_html_view(model, max_nodes=3)
    payload = _payload(html)
    assert payload["elided"]["cap"] == 3
    assert payload["elided"]["total"] == 5
    assert payload["elided"]["omitted"] == 2
    assert [node["id"] for node in payload["nodes"]] == ["a-node", "b-node", "c-node"]
    # The elision is visible in the rendered document, not silent.
    assert "omitted" in html
    # Links touching elided nodes never dangle.
    kept = {node["id"] for node in payload["nodes"]}
    for link in payload["links"]:
        assert link["source"] in kept and link["target"] in kept


def test_default_cap_is_explicit() -> None:
    assert MAX_NODES_FOR_VIEW == 5000
    assert HTML_VIEW_MAX_BYTES > 0


def test_max_nodes_guard_validated() -> None:
    with pytest.raises(HtmlViewError, match="max_nodes"):
        render_html_view(_read_model(), max_nodes=0)


# ------------------------------------------------------------------- byte cap


def test_max_bytes_breach_raises_typed() -> None:
    with pytest.raises(HtmlViewError, match="byte"):
        render_html_view(_read_model(), max_bytes=1024)


# ------------------------------------------------------------------ XSS / escaping


def test_script_breakout_payload_is_inert() -> None:
    evil = "</script><script>alert(1)</script><img src=x onerror=alert(2)>"
    model = _read_model()
    model["nodes"][0]["label"] = evil
    html = render_html_view(model)
    # Exactly two script-close tags exist: the data island and the app script.
    assert html.count("</script>") == 2
    # No active markup: the payload can only survive as inert text.
    assert 'onerror="' not in html
    assert "<img" not in html
    # The payload survives only as escaped JSON text.
    assert "\\u003c/script\\u003e" in html
    payload = _payload(html)
    assert payload["nodes"][0]["label"] == evil


def test_attribute_breakout_payload_is_escaped() -> None:
    model = _read_model()
    model["nodes"][0]["id"] = 'x" onmouseover="alert(1)'
    html = render_html_view(model)
    assert 'onmouseover="' not in html
    assert "&quot;" in html


def test_control_and_bidi_characters_are_sanitized() -> None:
    model = _read_model()
    model["nodes"][0]["label"] = "abc\x00def‮ghi"
    payload = _payload(render_html_view(model))
    assert payload["nodes"][0]["label"] == "abcdefghi"


@given(label=st.text(max_size=200))
@settings(derandomize=True, max_examples=100)
def test_property_arbitrary_label_never_breaks_the_document(label: str) -> None:
    model = _read_model()
    model["nodes"][0]["label"] = label
    model["nodes"][0]["provenance"] = [label]
    html = render_html_view(model)
    assert html.count("</script>") == 2
    payload = _payload(html)
    assert payload["nodes"][0]["id"] == "a-node"
    # Rendering is deterministic for the same payload.
    assert html == render_html_view(model)


@given(seed=st.integers(min_value=0, max_value=2**32 - 1))
@settings(derandomize=True, max_examples=25)
def test_property_shuffled_read_model_is_byte_identical(seed: int) -> None:
    import random

    model = _read_model()
    rng = random.Random(seed)
    nodes = list(model["nodes"])
    links = list(model["links"])
    rng.shuffle(nodes)
    rng.shuffle(links)
    shuffled = {"directed": True, "nodes": nodes, "links": links}
    assert render_html_view(model) == render_html_view(shuffled)


# ------------------------------------------------------------ registry seam


def test_register_html_view_produces_capped_roundtripped_artifact() -> None:
    registry = ExportRegistry()
    register_html_view(registry)
    artifacts = produce_exports(registry, _read_model(), select=["exports/graph.html"])
    data = artifacts["exports/graph.html"]
    assert data.startswith(b"<!doctype html>")
    entries = manifest_entries(registry, artifacts)
    assert entries["exports/graph.html"]["kind"] == "format"
    assert entries["exports/graph.html"]["size"] == len(data)


def test_register_html_view_cap_breach_is_typed() -> None:
    registry = ExportRegistry()
    registry.register(html_view_format(max_bytes=64))
    with pytest.raises(ExportCapExceededError):
        produce_exports(registry, _read_model(), select=["exports/graph.html"])


def test_html_view_format_roundtrip_rejects_tampering() -> None:
    spec = html_view_format()
    with pytest.raises(Exception, match="doctype|island|schema"):
        spec.roundtrip(b"not html at all")
