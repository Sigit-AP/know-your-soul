"""Deterministic visual-asset generator for Know Your Soul.

Renders the four operator-facing SVGs under ``docs/assets/`` from REAL
project data — no mocks, no network, no AI calls:

* ``graph-formation.svg`` — the live projection graph built from
  ``bench/corpus`` (28 frozen files -> 36 nodes -> 35 typed edges), laid out
  with a hand-rolled deterministic radial-tree layout (sorted ids, structural
  placement, no wall-clock or hash-order dependence => byte-identical output).
* ``learning-timeline.svg`` — P/M/E score evolution of a real graph node
  over a deterministic 12-outcome demo scenario scored by the real frozen
  ``ScoreEngine`` at fixed timestamps, plus the cumulative event-store
  growth over the real seed-import event sequence.
* ``bench-impact.svg`` — per-case token saving from a real bench ledger
  (``bench/results/ledger-*.json``; newest by default, pin with
  ``--ledger``).
* ``architecture.svg`` — layered architecture diagram of the real module
  structure.

Regenerate everything (from the repository root):

    uv run python tools/generate_visual_assets.py

Determinism check (rebuilds into a temp dir and byte-compares):

    uv run python tools/generate_visual_assets.py --check

Stdlib + the project's own packages only. Python 3.14.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from xml.sax.saxutils import escape

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from know_your_soul.domain import Evidence, PrimaryDomain  # noqa: E402
from know_your_soul.importer import DEFAULT_PARSER_VERSIONS, SeedImporter  # noqa: E402
from know_your_soul.projection import GraphProjector  # noqa: E402
from know_your_soul.scoring import ScoreEngine  # noqa: E402
from know_your_soul.service import KnowledgeService  # noqa: E402
from know_your_soul.source_registry import SourceKind, SourceRegistry  # noqa: E402
from know_your_soul.store import EventStore  # noqa: E402

CORPUS_DIR = REPO_ROOT / "bench" / "corpus"
RESULTS_DIR = REPO_ROOT / "bench" / "results"
DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "assets"

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

BG = "#0d1117"
PANEL = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
FONT = "ui-sans-serif, system-ui, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"

DOMAIN_COLORS = {
    "P1": "#58a6ff",  # blue
    "P2": "#d2a8ff",  # violet
    "P3": "#ff7b72",  # red
    "P4": "#ffa657",  # orange
    "P5": "#f778ba",  # pink
    "P6": "#3fb950",  # green
    "P7": "#e3b341",  # yellow
}
DOMAIN_TITLES = {d.value: PrimaryDomain(d).title for d in PrimaryDomain}

SCORE_COLORS = {"P": "#58a6ff", "M": "#3fb950", "E": "#ffa657"}

# Fixed timestamps keep the scoring demo byte-deterministic (the engine's
# recency decay is computed against an explicit ``at``, never wall-clock).
DEMO_T0 = datetime(2026, 1, 5, 9, 0, 0, tzinfo=UTC)

# Deterministic demo scenario: twelve outcomes recorded against one real
# node — a plausible learning curve with one mid-course setback (step 8).
DEMO_OUTCOMES = [0.40, 0.55, 0.50, 0.70, 0.65, 0.80, 0.85, 0.30, 0.85, 0.90, 0.95, 1.00]


# ---------------------------------------------------------------------------
# Real data acquisition
# ---------------------------------------------------------------------------


def build_projection(workdir: Path) -> tuple[dict, list[tuple[str, str]], str]:
    """Seed a store from the frozen bench corpus and project it.

    Mirrors ``bench/run.py::build_service`` but derives the file->domain map
    from the pinned corpus manifest. Returns (graph_dict, event_timeline,
    store_path) where event_timeline is [(kind, node_id)] in replay order.
    """
    manifest = json.loads((CORPUS_DIR / "manifest.json").read_text(encoding="utf-8"))
    file_domain = {entry["file"]: entry["expected_primary_domain"] for entry in manifest["files"]}

    store = EventStore(workdir / "assets.sqlite3")
    registry = SourceRegistry(store)
    version = registry.observe(CORPUS_DIR, SourceKind.SOUL_CORPUS, DEFAULT_PARSER_VERSIONS)
    importer = SeedImporter(store, registry=registry)
    importer.ensure_roots(version.source_version_id)
    for entry in sorted(CORPUS_DIR.glob("*.md")):
        importer.import_file(
            entry,
            domain=PrimaryDomain(file_domain[entry.name]),
            source_uri=f"soul://corpus/{entry.name}",
        )
    projector = GraphProjector(store, output_root=workdir / "graph")
    projector.rebuild()
    graph_path = sorted((workdir / "graph").glob("generations/*/graph.json"))[-1]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    timeline = [(str(ev.kind), ev.node_id or "") for ev in store.replay_domain()]
    store.close()
    return graph, timeline, str(workdir / "assets.sqlite3")


def run_scoring_demo(store_path: str, node_id: str) -> list[dict[str, float]]:
    """Record the deterministic outcome scenario and score after each step.

    Uses the real ``KnowledgeService.record_outcome`` write path and the real
    frozen ``ScoreEngine``; every timestamp is fixed, so the curve is
    byte-deterministic across runs.
    """
    store = EventStore(store_path)
    projector = GraphProjector(store, output_root=Path(store_path).parent / "graph")
    service = KnowledgeService(store, projector)
    engine = ScoreEngine(store)
    node = store.current_node(node_id)
    if node is None:
        raise SystemExit(f"demo node {node_id!r} not found in projection store")
    source_version_id = node.source_version_id

    curve: list[dict[str, float]] = []
    for i, outcome in enumerate(DEMO_OUTCOMES):
        observed = DEMO_T0 + timedelta(hours=12 * i)
        quality = Decimal("0.55") + Decimal("0.035") * i
        evidence = Evidence(
            evidence_id=f"viz-demo-{i:02d}",
            node_id=node_id,
            source_version_id=source_version_id,
            source_uri="soul://demo/visual-assets",
            observed_at=observed,
            context_tags=("demo", "visual-assets"),
            independence_key=f"viz-demo-src-{i % 4}",
            outcome=Decimal(str(outcome)),
            correctness=min(Decimal("1"), quality),
            reliability=Decimal("0.85"),
            reproducibility=Decimal("0.80"),
            scope_fit=Decimal("0.90"),
            successful_use=Decimal("1") if outcome >= 0.5 else Decimal("0.4"),
            task_coverage=min(Decimal("1"), quality),
            quality_at_use=min(Decimal("1"), quality),
            contradiction=Decimal("0") if outcome >= 0.5 else Decimal("0.2"),
            utilization_u=Decimal("0.5"),
            latency_ms=42,
            compute_ms=7,
            output_chars=512,
        )
        service.record_outcome(evidence, actor="visual-assets-demo")
        assessment = engine.score_node(node_id, at=observed + timedelta(hours=1))
        curve.append(
            {
                "step": i + 1,
                "p": float(assessment.p),
                "m": float(assessment.m),
                "e": float(assessment.e),
                "level": assessment.professional_level,
            }
        )
    store.close()
    return curve


def load_bench_ledger(ledger: str | None) -> tuple[Path, dict]:
    """Load a real bench ledger (newest with measured cases by default)."""
    candidates = sorted(RESULTS_DIR.glob("ledger-*.json"))
    if ledger:
        path = Path(ledger)
        if not path.is_absolute():
            path = RESULTS_DIR / ledger
        return path, json.loads(path.read_text(encoding="utf-8"))
    for path in reversed(candidates):
        data = json.loads(path.read_text(encoding="utf-8"))
        measured = [c for c in data.get("cases", []) if c.get("status") == "measured"]
        if len(measured) >= 20:
            return path, data
    raise SystemExit("no measured bench ledger found under bench/results")


# ---------------------------------------------------------------------------
# Deterministic layout (hand-rolled radial tree + optional seeded relaxation)
# ---------------------------------------------------------------------------

_LAYOUT_SEED = 58


def radial_layout(
    node_ids: list[str],
    edges: list[tuple[str, str]],
    width: float,
    height: float,
    *,
    seed: int = _LAYOUT_SEED,
) -> dict[str, tuple[float, float]]:
    """Deterministic radial-tree layout, hand-rolled (no layout library).

    The projected graph is a star-of-stars (KROOT -> P1..P7 -> file nodes);
    a plain Fruchterman-Reingold pass over all-pairs repulsion pushes the
    leaf ring into the frame bounds, so the primary placement is a regular
    radial tree: KROOT pinned at the center, the seven domain roots on a
    ring, and each domain's children on an outward-facing arc with an
    alternating ring offset. Every quantity derives from sorted ids and the
    fixed ``seed`` (kept for API stability and future jitter passes), so the
    output is byte-identical on every run.
    """
    del seed  # placement is fully structural; the seed is reserved API
    ids = sorted(node_ids)
    cx, cy = width / 2.0, height / 2.0
    domains = sorted(d for d in ids if d in DOMAIN_COLORS)
    pos: dict[str, list[float]] = {}
    if "KROOT" in ids:
        pos["KROOT"] = [cx, cy]
    radius = min(width, height) * 0.30
    children: dict[str, list[str]] = {}
    edge_list = sorted((a, b) for a, b in edges if a in ids and b in ids)
    for a, b in edge_list:
        if a in domains and b not in domains and b != "KROOT":
            children.setdefault(a, []).append(b)
    arc_fill = 0.78
    ring_gap = 105.0
    for i, dom in enumerate(domains):
        theta = -math.pi / 2 + 2 * math.pi * i / len(domains)
        pos[dom] = [cx + radius * math.cos(theta), cy + radius * math.sin(theta)]
        ch = sorted(children.get(dom, []))
        span = (2 * math.pi / len(domains)) * arc_fill
        for j, c in enumerate(ch):
            ang = theta - span / 2 + span * (j / (len(ch) - 1) if len(ch) > 1 else 0.5)
            rr = radius + ring_gap + (30.0 if j % 2 else 0.0)
            pos[c] = [cx + rr * math.cos(ang), cy + rr * math.sin(ang)]
    for n in pos:  # safety clamp inside the frame
        pos[n][0] = min(width - 12.0, max(12.0, pos[n][0]))
        pos[n][1] = min(height - 12.0, max(12.0, pos[n][1]))
    return {n: (pos[n][0], pos[n][1]) for n in ids}


# ---------------------------------------------------------------------------
# SVG helpers
# ---------------------------------------------------------------------------


def svg_header(width: int, height: int, comment: str) -> list[str]:
    return [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f"<!-- {escape(comment)} -->",
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="{FONT}">',
        f'<rect width="{width}" height="{height}" fill="{BG}"/>',
    ]


def text(
    x: float,
    y: float,
    s: str,
    size: int = 12,
    fill: str = TEXT,
    anchor: str = "start",
    weight: str = "normal",
    family: str | None = None,
    opacity: float = 1.0,
) -> str:
    fam = f' font-family="{family}"' if family else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" text-anchor="{anchor}"'
        f' font-weight="{weight}" opacity="{opacity}"{fam}>{escape(s)}</text>'
    )


def panel(x: float, y: float, w: float, h: float, rx: int = 10) -> str:
    return (
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{rx}"'
        f' fill="{PANEL}" stroke="{BORDER}" stroke-width="1"/>'
    )


def write_svg(path: Path, parts: list[str]) -> None:
    parts.append("</svg>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. graph-formation.svg
# ---------------------------------------------------------------------------


def short_label(node: dict) -> str:
    if node["id"] == "KROOT":
        return "KROOT"
    if node["id"] in DOMAIN_COLORS:
        return node["id"]
    source = node.get("source_file") or node["id"]
    return source.removesuffix(".md")


def render_graph_formation(graph: dict, timeline: list[tuple[str, str]], out: Path) -> None:
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = [(e["source"], e["target"]) for e in graph["links"]]
    width, height = 1240, 880
    gx, gy, gw, gh = 320.0, 110.0, 880.0, 700.0
    pos = radial_layout(list(nodes), edges, gw, gh)
    pos = {n: (x + gx, y + gy) for n, (x, y) in pos.items()}

    file_nodes = [n for n in nodes.values() if n.get("kind") == "child"]
    n_upsert = sum(1 for k, _ in timeline if k.endswith("node.upsert"))
    n_rel = sum(1 for k, _ in timeline if k.endswith("relation.add"))

    parts = svg_header(
        width,
        height,
        "know-your-soul graph formation — real projection of bench/corpus; regenerate: "
        "uv run python tools/generate_visual_assets.py",
    )
    parts.append(text(40, 52, "How the graph is formed", 26, TEXT, weight="bold"))
    parts.append(
        text(
            40,
            78,
            f"Real projection of the frozen bench corpus: {len(file_nodes)} files → "
            f"{len(nodes)} nodes → {len(edges)} typed edges (hand-rolled deterministic radial layout)",
            13,
            MUTED,
        )
    )

    # Pipeline panel (left column) — real counts.
    px, py, pw = 40.0, 110.0, 240.0
    parts.append(panel(px, py, pw, 700))
    parts.append(text(px + 20, py + 34, "FORMATION PIPELINE", 12, MUTED, weight="bold"))
    steps = [
        ("1 · Corpus files", f"{len(file_nodes)} frozen .md files", "bench/corpus (sha256-pinned)"),
        ("2 · Seed import", f"{n_upsert} node.upsert events", "immutable domain event store"),
        ("3 · Typed relations", f"{n_rel} relation.add events", "parent_of, confidence 1.0"),
        ("4 · Projection", f"{len(nodes)} nodes · {len(edges)} edges", "content-addressed graph.json"),
    ]
    sy = py + 60.0
    for title, line1, line2 in steps:
        parts.append(
            f'<rect x="{px + 16}" y="{sy}" width="{pw - 32}" height="86" rx="8" fill="{BG}" stroke="{BORDER}"/>'
        )
        parts.append(text(px + 30, sy + 26, title, 13, ACCENT, weight="bold"))
        parts.append(text(px + 30, sy + 48, line1, 12, TEXT))
        parts.append(text(px + 30, sy + 68, line2, 10, MUTED))
        if title[0] != "4":
            ax = px + pw / 2
            parts.append(
                f'<line x1="{ax}" y1="{sy + 86}" x2="{ax}" y2="{sy + 104}"'
                f' stroke="{MUTED}" stroke-width="1.5" marker-end="url(#arr)"/>'
            )
        sy += 104.0
    # Domain legend.
    ly = sy + 16
    parts.append(text(px + 20, ly, "PRIMARY DOMAINS", 12, MUTED, weight="bold"))
    for i, dom in enumerate(sorted(DOMAIN_COLORS)):
        yy = ly + 24 + i * 26
        parts.append(f'<circle cx="{px + 28}" cy="{yy - 4}" r="6" fill="{DOMAIN_COLORS[dom]}"/>')
        parts.append(text(px + 44, yy, f"{dom} · {DOMAIN_TITLES[dom]}", 11, TEXT))

    parts.append(
        '<defs><marker id="arr" markerWidth="8" markerHeight="8" refX="6" refY="3"'
        f' orient="auto"><path d="M0,0 L6,3 L0,6" fill="none" stroke="{MUTED}"'
        ' stroke-width="1.2"/></marker></defs>'
    )

    # Graph panel.
    parts.append(panel(gx - 16, gy - 16, gw + 32, gh + 32))
    for a, b in edges:
        x1, y1 = pos[a]
        x2, y2 = pos[b]
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"'
            f' stroke="{BORDER}" stroke-width="1.2" opacity="0.85"/>'
        )
    for nid in sorted(nodes, key=lambda n: (nodes[n].get("kind") != "child", n)):
        node = nodes[nid]
        x, y = pos[nid]
        kind = node.get("kind")
        dom = node.get("domain", "P1")
        color = DOMAIN_COLORS.get(dom, ACCENT)
        if nid == "KROOT":
            r, label, lfill, lsize = 11.0, "KROOT", TEXT, 12
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{BG}" stroke="{TEXT}" stroke-width="2"/>')
        elif kind == "child":
            r, label, lfill, lsize = 5.5, short_label(node), MUTED, 9
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" stroke="{BG}" stroke-width="1"/>')
        else:  # domain root
            r, label, lfill, lsize = 9.0, nid, color, 13
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" stroke="{BG}" stroke-width="1.5"/>')
        parts.append(
            text(
                x,
                y - r - 5,
                label,
                lsize,
                lfill,
                anchor="middle",
                weight="bold" if kind != "child" else "normal",
                family=MONO,
            )
        )
    parts.append(
        text(
            gx,
            gy + gh + 40,
            "Nodes coloured by primary domain · edges are real parent_of relations from the projected graph.json",
            11,
            MUTED,
        )
    )
    write_svg(out, parts)


# ---------------------------------------------------------------------------
# 2. learning-timeline.svg
# ---------------------------------------------------------------------------


def polyline(points: list[tuple[float, float]], color: str, width_: float = 2.0) -> str:
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="{width_}" stroke-linejoin="round"/>'


def render_learning_timeline(
    curve: list[dict[str, float]], timeline: list[tuple[str, str]], node_label: str, out: Path
) -> None:
    width, height = 1240, 860
    parts = svg_header(
        width,
        height,
        "know-your-soul learning timeline — real ScoreEngine P/M/E over a deterministic "
        "outcome scenario + real event-store growth; regenerate: uv run python "
        "tools/generate_visual_assets.py",
    )
    parts.append(text(40, 52, "Learning over time", 26, TEXT, weight="bold"))
    parts.append(
        text(
            40,
            78,
            "P/M/E scores from the frozen scoring engine (contract 1.0.0) over recorded outcomes "
            "· event-store growth from the real seed import",
            13,
            MUTED,
        )
    )

    # --- Panel A: P/M/E evolution -------------------------------------------
    ax, ay, aw, ah = 60.0, 130.0, 1120.0, 340.0
    parts.append(panel(ax - 20, ay - 24, aw + 40, ah + 78))
    parts.append(
        text(
            ax,
            ay - 2,
            f"P / M / E score evolution — node “{node_label}” (deterministic 12-outcome demo)",
            14,
            TEXT,
            weight="bold",
        )
    )
    vmax = max(1.0, max(max(c["p"], c["m"], c["e"]) for c in curve) * 1.15)
    n = len(curve)

    def xmap(i: int) -> float:
        return ax + 40 + (aw - 80) * i / (n - 1)

    def ymap(v: float) -> float:
        return ay + ah - (ah - 20) * v / vmax

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = vmax * frac
        yy = ymap(v)
        parts.append(
            f'<line x1="{ax + 40}" y1="{yy:.1f}" x2="{ax + aw - 40}" y2="{yy:.1f}"'
            f' stroke="{BORDER}" stroke-width="1" stroke-dasharray="3 4"/>'
        )
        parts.append(text(ax + 30, yy + 4, f"{v:.2f}", 9, MUTED, anchor="end", family=MONO))
    for key, color in (("p", SCORE_COLORS["P"]), ("m", SCORE_COLORS["M"]), ("e", SCORE_COLORS["E"])):
        pts = [(xmap(i), ymap(c[key])) for i, c in enumerate(curve)]
        parts.append(polyline(pts, color))
        for xx, yy in pts:
            parts.append(f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="3.4" fill="{color}" stroke="{BG}"/>')
    for i, c in enumerate(curve):
        parts.append(text(xmap(i), ay + ah + 18, str(c["step"]), 10, MUTED, anchor="middle", family=MONO))
    parts.append(text(ax + aw - 40, ay + ah + 40, "recorded outcome →", 11, MUTED, anchor="end"))
    lx = ax + 60
    for name, color in (
        ("P · proficiency", SCORE_COLORS["P"]),
        ("M · maturity", SCORE_COLORS["M"]),
        ("E · effectiveness", SCORE_COLORS["E"]),
    ):
        parts.append(
            f'<line x1="{lx}" y1="{ay + 16}" x2="{lx + 26}" y2="{ay + 16}" stroke="{color}" stroke-width="3"/>'
        )
        parts.append(text(lx + 34, ay + 20, name, 11, TEXT))
        lx += 170
    finals = curve[-1]
    parts.append(
        text(
            ax + aw - 40,
            ay + 20,
            f"final: P={finals['p']:.3f} M={finals['m']:.3f} E={finals['e']:.3f} · level {finals['level']}",
            11,
            MUTED,
            anchor="end",
            family=MONO,
        )
    )

    # --- Panel B: event-store growth ----------------------------------------
    bx, by, bw, bh = 60.0, 560.0, 1120.0, 220.0
    parts.append(panel(bx - 20, by - 24, bw + 40, bh + 78))
    parts.append(text(bx, by - 2, "Event-store growth during seed import (real replay order)", 14, TEXT, weight="bold"))
    nodes_c, edges_c = 0, 0
    series_n: list[tuple[float, float]] = []
    series_e: list[tuple[float, float]] = []
    total = len(timeline)
    for i, (kind, _) in enumerate(timeline):
        if kind.endswith("node.upsert"):
            nodes_c += 1
        elif kind.endswith("relation.add"):
            edges_c += 1
        series_n.append((i, nodes_c))
        series_e.append((i, edges_c))
    maxv = float(max(nodes_c, edges_c))

    def bxmap(i: float) -> float:
        return bx + 40 + (bw - 80) * i / max(total - 1, 1)

    def bymap(v: float) -> float:
        return by + bh - (bh - 20) * v / (maxv * 1.1)

    for frac in (0.0, 0.5, 1.0):
        v = maxv * frac
        yy = bymap(v)
        parts.append(
            f'<line x1="{bx + 40}" y1="{yy:.1f}" x2="{bx + bw - 40}" y2="{yy:.1f}"'
            f' stroke="{BORDER}" stroke-width="1" stroke-dasharray="3 4"/>'
        )
        parts.append(text(bx + 30, yy + 4, f"{v:.0f}", 9, MUTED, anchor="end", family=MONO))
    parts.append(polyline([(bxmap(i), bymap(v)) for i, v in series_n], ACCENT))
    parts.append(polyline([(bxmap(i), bymap(v)) for i, v in series_e], "#3fb950"))
    parts.append(text(bx + 60, by + 18, f"— nodes (node.upsert, {nodes_c})", 11, ACCENT))
    parts.append(text(bx + 300, by + 18, f"— relations (relation.add, {edges_c})", 11, "#3fb950"))
    parts.append(text(bx + bw - 40, by + bh + 36, "domain event sequence →", 11, MUTED, anchor="end"))
    parts.append(
        text(bx + 40, by + bh + 36, f"{total} knowledge-affecting events, immutable, replay-ordered", 11, MUTED)
    )
    write_svg(out, parts)


# ---------------------------------------------------------------------------
# 3. bench-impact.svg
# ---------------------------------------------------------------------------


def render_bench_impact(ledger_path: Path, ledger: dict, out: Path) -> None:
    cases = [c for c in ledger["cases"] if c.get("status") == "measured"]
    cases.sort(key=lambda c: c["id"])
    baseline = ledger["baseline"]["estimated_tokens"]
    savings = [c["token_saving_ratio"] for c in cases]
    mean_saving = sum(savings) / len(savings)
    accepted = sum(1 for c in cases if c.get("verdict") == "accepted")

    row_h = 26
    width = 1240
    top = 120.0
    height = int(top + row_h * len(cases) + 110)
    bx, bw = 300.0, 760.0  # bar origin / full width == 100 %

    parts = svg_header(
        width,
        height,
        f"know-your-soul bench impact — real ledger {ledger_path.name}; regenerate: "
        "uv run python tools/generate_visual_assets.py",
    )
    parts.append(text(40, 52, "Bench impact — token saving per case", 26, TEXT, weight="bold"))
    gate_version = ledger.get("gate", {}).get("gate_version", "?")
    parts.append(
        text(
            40,
            78,
            f"{len(cases)} measured cases · mean saving {mean_saving * 100:.1f}% vs raw-corpus baseline "
            f"({baseline:,} est. tokens) · {accepted}/{len(cases)} accepted (gate {gate_version})"
            f" · ledger {ledger_path.name}",
            13,
            MUTED,
        )
    )

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        xx = bx + bw * frac
        parts.append(
            f'<line x1="{xx:.1f}" y1="{top - 10}" x2="{xx:.1f}" y2="{top + row_h * len(cases)}"'
            f' stroke="{BORDER}" stroke-width="1"/>'
        )
        parts.append(
            text(xx, top + row_h * len(cases) + 18, f"{frac * 100:.0f}%", 10, MUTED, anchor="middle", family=MONO)
        )
    mean_x = bx + bw * mean_saving
    parts.append(
        f'<line x1="{mean_x:.1f}" y1="{top - 14}" x2="{mean_x:.1f}" y2="{top + row_h * len(cases)}"'
        f' stroke="{ACCENT}" stroke-width="1.6" stroke-dasharray="6 4"/>'
    )
    parts.append(
        text(
            mean_x, top - 20, f"mean {mean_saving * 100:.1f}%", 11, ACCENT, anchor="middle", weight="bold", family=MONO
        )
    )

    for i, c in enumerate(cases):
        yy = top + i * row_h
        dom = c.get("expected_primary_domain", "P1")
        color = DOMAIN_COLORS.get(dom, ACCENT)
        ratio = c["token_saving_ratio"]
        bar_w = bw * ratio
        parts.append(text(bx - 12, yy + row_h / 2 + 4, c["id"], 11, TEXT, anchor="end", family=MONO))
        parts.append(
            f'<rect x="{bx}" y="{yy + 4}" width="{bar_w:.1f}" height="{row_h - 9}" rx="3"'
            f' fill="{color}" opacity="0.9"/>'
        )
        parts.append(text(bx + bar_w + 8, yy + row_h / 2 + 4, f"{ratio * 100:.1f}%", 11, TEXT, family=MONO))
        p50 = c.get("latency_ms", {}).get("p50")
        sys_tokens = c.get("system_estimated_tokens", 0)
        right = f"{sys_tokens:,} tok"
        if p50 is not None:
            right += f" · p50 {p50:.1f} ms"
        verdict = c.get("verdict", "?")
        mark = "✓" if verdict == "accepted" else "✗"
        mfill = "#3fb950" if verdict == "accepted" else "#ff7b72"
        parts.append(text(bx + bw + 16, yy + row_h / 2 + 4, f"{mark} {right}", 10, mfill, family=MONO))
    parts.append(
        text(
            40,
            height - 40,
            "Bars: 1 − (system response tokens / raw-corpus baseline) · quality gate: quality_system ≥ "
            "per-case compliant ceiling · colours: expected primary domain",
            11,
            MUTED,
        )
    )
    # Domain legend.
    lx = bx
    for dom in sorted(DOMAIN_COLORS):
        parts.append(f'<rect x="{lx}" y="{height - 32}" width="10" height="10" rx="2" fill="{DOMAIN_COLORS[dom]}"/>')
        parts.append(text(lx + 16, height - 23, dom, 10, TEXT, family=MONO))
        lx += 52
    write_svg(out, parts)


# ---------------------------------------------------------------------------
# 4. architecture.svg
# ---------------------------------------------------------------------------


def render_architecture(out: Path) -> None:
    width, height = 1240, 920
    parts = svg_header(
        width,
        height,
        "know-your-soul layered architecture — real module map of src/know_your_soul; "
        "regenerate: uv run python tools/generate_visual_assets.py",
    )
    parts.append(text(40, 52, "Architecture — layered, deterministic, local-first", 26, TEXT, weight="bold"))
    parts.append(
        text(
            40,
            78,
            "Real module structure of src/know_your_soul · no model, provider, or vector calls anywhere",
            13,
            MUTED,
        )
    )

    layers = [
        (
            "ADAPTERS",
            "#1f6feb33",
            [
                ("cli.py", "know-your-soul CLI"),
                ("mcp_server.py", "MCP tools"),
                ("http_api.py", "REST / unix socket"),
                ("source_watcher.py", "gated relearn offers"),
            ],
        ),
        (
            "SERVICE",
            "#1f6feb22",
            [
                ("service.py", "KnowledgeService · 14 ops"),
                ("contracts.py", "frozen request/response"),
                ("retrieval.py", "query · path · explain"),
                ("improvement.py", "propose / apply / rollback"),
            ],
        ),
        (
            "DOMAIN + SCORING",
            "#3fb95022",
            [
                ("domain.py", "Node · Event · Evidence"),
                ("scoring.py", "P/M/E engine (contract 1.0.0)"),
                ("review.py", "quality review"),
                ("relearn.py", "operator-gated relearn"),
            ],
        ),
        (
            "PROJECTION",
            "#ffa65722",
            [
                ("projection.py", "GraphProjector · generations/"),
                ("graphify_adapter.py", "pinned partitioner"),
                ("read_model_validator.py", "artifact gate"),
                ("fragment_generator.py", "fragment cache"),
            ],
        ),
        (
            "EVENTS + STORE",
            "#f778ba22",
            [
                ("store.py", "SQLite event store"),
                ("events.py", "immutable domain events"),
                ("audit.py", "invocation audit log"),
                ("backup.py", "atomic backup / restore"),
            ],
        ),
        (
            "CROSS-CUTTING",
            "#8b949e22",
            [
                ("security.py", "sanitise · redact"),
                ("runtime_guard.py", "fail-closed guards"),
                ("observability.py", "metrics · health"),
                ("source_registry.py", "source versions"),
            ],
        ),
    ]

    lx, ly = 60.0, 116.0
    lw, lh = 1120.0, 118.0
    for name, tint, modules in layers:
        parts.append(
            f'<rect x="{lx - 20}" y="{ly - 14}" width="{lw + 40}" height="{lh}" rx="10"'
            f' fill="{PANEL}" stroke="{BORDER}"/>'
        )
        parts.append(f'<rect x="{lx - 20}" y="{ly - 14}" width="8" height="{lh}" rx="4" fill="{tint}"/>')
        parts.append(text(lx, ly + 6, name, 11, MUTED, weight="bold", family=MONO))
        bw_ = (lw - 40) / 4
        for i, (mod, desc) in enumerate(modules):
            mx = lx + i * bw_ + 6
            my = ly + 18
            parts.append(
                f'<rect x="{mx:.1f}" y="{my}" width="{bw_ - 12:.1f}" height="64" rx="8" fill="{BG}" stroke="{BORDER}"/>'
            )
            parts.append(text(mx + 12, my + 26, mod, 12, ACCENT, family=MONO, weight="bold"))
            parts.append(text(mx + 12, my + 46, desc, 10, MUTED))
        ly += lh + 26.0
        if name != "CROSS-CUTTING":
            ax = lx + lw / 2
            parts.append(
                f'<line x1="{ax}" y1="{ly - 40}" x2="{ax}" y2="{ly - 14}" stroke="{MUTED}"'
                ' stroke-width="1.5" marker-end="url(#arr2)"/>'
            )
    parts.append(
        '<defs><marker id="arr2" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto">'
        f'<path d="M0,0 L6,3 L0,6" fill="none" stroke="{MUTED}" stroke-width="1.2"/></marker></defs>'
    )
    parts.append(
        text(
            60,
            height - 34,
            "Dependency direction: adapters → service → domain/scoring → projection → event store; "
            "the store is the single source of truth, projections are disposable and content-addressed.",
            11,
            MUTED,
        )
    )
    write_svg(out, parts)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def generate_all(out_dir: Path, ledger: str | None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kys-visual-") as tmp:
        graph, timeline, store_path = build_projection(Path(tmp))
        # Scoring demo re-opens the same store against a real child node.
        child_ids = sorted(n["id"] for n in graph["nodes"] if n.get("kind") == "child")
        demo_node = (
            next(n for n in child_ids if "heuristics" in n)
            if any("heuristics" in n for n in child_ids)
            else child_ids[0]
        )
        curve = run_scoring_demo(store_path, demo_node)
        demo_label = next(
            n.get("source_file", n["id"]).removesuffix(".md") for n in graph["nodes"] if n["id"] == demo_node
        )
    ledger_path, ledger_data = load_bench_ledger(ledger)

    outputs = {
        "graph-formation.svg": out_dir / "graph-formation.svg",
        "learning-timeline.svg": out_dir / "learning-timeline.svg",
        "bench-impact.svg": out_dir / "bench-impact.svg",
        "architecture.svg": out_dir / "architecture.svg",
    }
    render_graph_formation(graph, timeline, outputs["graph-formation.svg"])
    render_learning_timeline(curve, timeline, demo_label, outputs["learning-timeline.svg"])
    render_bench_impact(ledger_path, ledger_data, outputs["bench-impact.svg"])
    render_architecture(outputs["architecture.svg"])
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="output directory")
    parser.add_argument("--ledger", default=None, help="pin a bench ledger file (default: newest measured)")
    parser.add_argument(
        "--check", action="store_true", help="regenerate into a temp dir and byte-compare against --out"
    )
    args = parser.parse_args()
    out_dir = Path(args.out)

    if args.check:
        import filecmp

        with tempfile.TemporaryDirectory(prefix="kys-visual-check-") as tmp:
            regenerated = generate_all(Path(tmp), args.ledger)
            mismatches = [
                name
                for name, fresh in regenerated.items()
                if not (out_dir / name).exists() or not filecmp.cmp(fresh, out_dir / name, shallow=False)
            ]
        if mismatches:
            print(f"check FAILED: stale or missing assets: {', '.join(mismatches)}", file=sys.stderr)
            return 1
        print(f"check OK: {len(regenerated)} assets byte-identical to regeneration")
        return 0

    outputs = generate_all(out_dir, args.ledger)
    for path in sorted(outputs.values()):
        size = path.stat().st_size
        print(f"wrote {path.relative_to(REPO_ROOT)} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
