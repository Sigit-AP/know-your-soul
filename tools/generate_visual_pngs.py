"""High-quality PNG visual assets for Know Your Soul (display/showcase grade).

Renders the same four visuals as ``generate_visual_assets.py`` but as
high-DPI PNG images via matplotlib, from the SAME real project data (live
corpus projection, real ScoreEngine run, real bench ledger). Dark theme,
≥150 DPI, ≥1200 px wide.

Regenerate everything (from the repository root):

    uv run --with matplotlib --with pillow python tools/generate_visual_pngs.py

IMPORTANT: matplotlib and pillow are on the repo's FORBIDDEN_DISTRIBUTIONS
list (``runtime_guard``) — they must NEVER enter ``pyproject.toml`` /
``uv.lock`` / the project venv. The ``uv run --with`` overlay above keeps
them ephemeral (uv cache only), so the dependency-policy guard stays green.
The PNGs themselves are committed; regeneration is only needed when the
underlying data changes.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.patheffects as pe
import matplotlib.pyplot as plt
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tools"))
sys.path.insert(0, str(REPO_ROOT / "src"))

import generate_visual_assets as svg_gen  # noqa: E402

DEFAULT_OUT_DIR = REPO_ROOT / "docs" / "assets"

# Theme (mirrors the SVG theme).
BG = "#0d1117"
PANEL = "#161b22"
BORDER = "#30363d"
TEXT = "#e6edf3"
MUTED = "#8b949e"
ACCENT = "#58a6ff"
GREEN = "#3fb950"
RED = "#ff7b72"
DOMAIN_COLORS = svg_gen.DOMAIN_COLORS
DOMAIN_TITLES = svg_gen.DOMAIN_TITLES
SCORE_COLORS = svg_gen.SCORE_COLORS

DPI = 200  # every canvas unit == 1/100 in, so pixel size = units * DPI/100
HALO = [pe.withStroke(linewidth=3.0, foreground=BG)]


def new_canvas(w: float, h: float):
    """Full-bleed canvas in SVG-like coordinates (origin top-left, y down)."""
    fig = plt.figure(figsize=(w / 100.0, h / 100.0), dpi=DPI)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.axis("off")
    fig.patch.set_facecolor(BG)
    ax.set_facecolor(BG)
    return fig, ax


def panel(
    ax, x: float, y: float, w: float, h: float, r: float = 12, face: str = PANEL, edge: str = BORDER, lw: float = 1.2
) -> None:
    ax.add_patch(
        FancyBboxPatch(
            (x, y),
            w,
            h,
            boxstyle=f"round,pad=0,rounding_size={r}",
            facecolor=face,
            edgecolor=edge,
            linewidth=lw,
            mutation_aspect=1.0,
        )
    )


def T(
    ax,
    x: float,
    y: float,
    s: str,
    size: float,
    color: str = TEXT,
    *,
    weight: str = "normal",
    ha: str = "left",
    va: str = "baseline",
    family: str = "sans",
    halo: bool = False,
    alpha: float = 1.0,
):
    t = ax.text(
        x,
        y,
        s,
        fontsize=size,
        color=color,
        fontweight=weight,
        ha=ha,
        va=va,
        alpha=alpha,
        zorder=10,
        family="monospace" if family == "mono" else "sans-serif",
    )
    if halo:
        t.set_path_effects(HALO)
    return t


def title_block(ax, w: float, title: str, subtitle: str) -> None:
    T(ax, 50, 58, title, 26, TEXT, weight="bold")
    T(ax, 50, 88, subtitle, 12.5, MUTED)
    ax.plot([50, w - 50], [108, 108], color=BORDER, lw=1.2)


def save(fig, path: Path) -> None:
    fig.savefig(path, dpi=DPI, facecolor=BG)
    plt.close(fig)


def _text_corners(fig, t) -> list[tuple[float, float]]:
    """True (rotated) corner polygon of a text artist in display pixels."""
    import math

    r = fig.canvas.get_renderer()
    rot = t.get_rotation()
    if rot:
        t.set_rotation(0)
        bbox = t.get_window_extent(renderer=r)
        t.set_rotation(rot)
        ax_, ay_ = t.get_transform().transform(t.get_position())
        theta = math.radians(rot)
        c, s = math.cos(theta), math.sin(theta)
        corners = []
        for x, y in ((bbox.x0, bbox.y0), (bbox.x1, bbox.y0), (bbox.x1, bbox.y1), (bbox.x0, bbox.y1)):
            dx, dy = x - ax_, y - ay_
            corners.append((ax_ + dx * c - dy * s, ay_ + dx * s + dy * c))
        return corners
    bbox = t.get_window_extent(renderer=r)
    return [(bbox.x0, bbox.y0), (bbox.x1, bbox.y0), (bbox.x1, bbox.y1), (bbox.x0, bbox.y1)]


def _polys_intersect(p: list[tuple[float, float]], q: list[tuple[float, float]]) -> bool:
    """Separating-axis test for two convex polygons."""
    for poly in (p, q):
        for i in range(len(poly)):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % len(poly)]
            nx, ny = -(y2 - y1), x2 - x1  # edge normal
            proj_p = [nx * x + ny * y for x, y in p]
            proj_q = [nx * x + ny * y for x, y in q]
            if max(proj_p) < min(proj_q) or max(proj_q) < min(proj_p):
                return False
    return True


def label_overlaps(fig, texts, verbose: bool = False) -> int:
    """Geometric QA: count true pairwise overlaps of (possibly rotated) texts."""
    fig.canvas.draw()
    polys = [_text_corners(fig, t) for t in texts]
    hits = 0
    for i in range(len(polys)):
        for j in range(i + 1, len(polys)):
            if _polys_intersect(polys[i], polys[j]):
                hits += 1
                if verbose:
                    print(f"    overlap: {texts[i].get_text()!r} × {texts[j].get_text()!r}")
    return hits


def radial_layout_png(
    node_ids: list[str], edges: list[tuple[str, str]], width: float, height: float
) -> dict[str, tuple[float, float]]:
    """Radial-tree layout tuned for labelled PNG output.

    Same star-of-stars structure as the SVG layout, but (a) each domain's arc
    span is proportional to its child count so dense fans (P7: 8 leaves) get
    room and sparse ones (P2: 1 leaf) stay compact, (b) domain slots around
    the ring interleave large and small fans so neighbours never collide, and
    (c) leaves alternate between two clearly separated outer rings.
    Deterministic: everything derives from sorted ids and edge order.
    """
    import math

    ids = sorted(node_ids)
    cx, cy = width / 2.0, height / 2.0
    domains = sorted(d for d in ids if d in DOMAIN_COLORS)
    children: dict[str, list[str]] = {}
    for a, b in sorted((a, b) for a, b in edges if a in ids and b in ids):
        if a in domains and b not in domains and b != "KROOT":
            children.setdefault(a, []).append(b)

    # Slot interleave: sort domains by fan size desc, deal into slots
    # 0,2,4,6,1,3,5 so the biggest fans are never adjacent.
    by_size = sorted(domains, key=lambda d: (-len(children.get(d, [])), d))
    slots = sorted(range(len(domains)), key=lambda i: (i % 2, i))
    slot_of = {dom: slots[i] for i, dom in enumerate(by_size)}

    pos: dict[str, tuple[float, float]] = {}
    if "KROOT" in ids:
        pos["KROOT"] = (cx, cy)
    radius = min(width, height) * 0.185
    pad = 1.4  # arc padding per domain, in "virtual child" units
    total_units = sum(len(children.get(d, [])) + pad for d in domains)
    for dom in domains:
        theta = -math.pi / 2 + 2 * math.pi * slot_of[dom] / len(domains)
        pos[dom] = (cx + radius * math.cos(theta), cy + radius * math.sin(theta))
        ch = sorted(children.get(dom, []))
        span = min(2 * math.pi * (len(ch) + pad) / total_units * 0.92, 2 * math.pi / len(domains) * 1.12)
        for j, c in enumerate(ch):
            ang = theta - span / 2 + span * (j / (len(ch) - 1) if len(ch) > 1 else 0.5)
            rr = radius + 95.0 + (40.0 if j % 2 else 0.0)
            pos[c] = (cx + rr * math.cos(ang), cy + rr * math.sin(ang))
    return {n: pos.get(n, (cx, cy)) for n in ids}


def short(name: str, n: int = 30) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


# ---------------------------------------------------------------------------
# 1. graph-formation.png
# ---------------------------------------------------------------------------


def render_graph_formation(graph: dict, timeline: list[tuple[str, str]], out: Path) -> None:
    nodes = {n["id"]: n for n in graph["nodes"]}
    edges = [(e["source"], e["target"]) for e in graph["links"]]
    file_nodes = [n for n in nodes.values() if n.get("kind") == "child"]
    n_upsert = sum(1 for k, _ in timeline if k.endswith("node.upsert"))
    n_rel = sum(1 for k, _ in timeline if k.endswith("relation.add"))

    W, H = 1560, 1420
    fig, ax = new_canvas(W, H)
    title_block(
        ax,
        W,
        "How the graph is formed",
        f"Real projection of the frozen bench corpus: {len(file_nodes)} files → "
        f"{len(nodes)} nodes → {len(edges)} typed edges",
    )

    # Left column: formation pipeline + legend.
    px, py, pw, ph = 50, 130, 300, 1230
    panel(ax, px, py, pw, ph)
    T(ax, px + 22, py + 34, "FORMATION PIPELINE", 11, MUTED, weight="bold")
    steps = [
        ("1 · Corpus files", f"{len(file_nodes)} frozen .md files", "bench/corpus (sha256-pinned)"),
        ("2 · Seed import", f"{n_upsert} node.upsert events", "immutable domain event store"),
        ("3 · Typed relations", f"{n_rel} relation.add events", "parent_of, confidence 1.0"),
        ("4 · Projection", f"{len(nodes)} nodes · {len(edges)} edges", "content-addressed graph.json"),
    ]
    sy = py + 52.0
    for idx, (title, line1, line2) in enumerate(steps):
        panel(ax, px + 16, sy, pw - 32, 92, r=9, face=BG)
        T(ax, px + 32, sy + 28, title, 12.5, ACCENT, weight="bold")
        T(ax, px + 32, sy + 52, line1, 11.5, TEXT)
        T(ax, px + 32, sy + 74, line2, 10, MUTED)
        if idx < 3:
            ax.annotate(
                "",
                xy=(px + pw / 2, sy + 116),
                xytext=(px + pw / 2, sy + 94),
                arrowprops={"arrowstyle": "-|>", "color": MUTED, "lw": 1.6},
            )
        sy += 116.0
    ly = sy + 18
    T(ax, px + 22, ly, "PRIMARY DOMAINS", 11, MUTED, weight="bold")
    for i, dom in enumerate(sorted(DOMAIN_COLORS)):
        yy = ly + 34 + i * 30
        ax.add_patch(Circle((px + 34, yy - 4), 7, facecolor=DOMAIN_COLORS[dom], edgecolor=BG, linewidth=1.2))
        T(ax, px + 52, yy, f"{dom} · {DOMAIN_TITLES[dom]}", 10.5, TEXT)

    # Graph panel: radial layout tuned for labelled output (see radial_layout_png).
    # The tree core stays compact so the rotated spoke labels get a wide outer band.
    gx, gy, gw, gh = 400.0, 150.0, 1110.0, 1180.0
    panel(ax, gx - 24, gy - 24, gw + 48, gh + 48)
    pos = radial_layout_png(list(nodes), edges, gw, gh)
    pos = {n: (x + gx, y + gy) for n, (x, y) in pos.items()}
    cx, cy = gx + gw / 2, gy + gh / 2

    for a, b in edges:
        x1, y1 = pos[a]
        x2, y2 = pos[b]
        dom = nodes[a].get("domain") if a in DOMAIN_COLORS else nodes[b].get("domain", "")
        ax.plot([x1, x2], [y1, y2], color=DOMAIN_COLORS.get(dom, BORDER), lw=1.0, alpha=0.35, zorder=1)

    label_texts = []
    for nid in sorted(nodes, key=lambda n: (nodes[n].get("kind") != "child", n)):
        node = nodes[nid]
        x, y = pos[nid]
        kind = node.get("kind")
        dom = node.get("domain", "P1")
        color = DOMAIN_COLORS.get(dom, ACCENT)
        if nid == "KROOT":
            ax.add_patch(Circle((x, y), 13, facecolor=BG, edgecolor=TEXT, linewidth=2.2, zorder=4))
            label_texts.append(
                T(ax, x, y + 30, "KROOT", 12, TEXT, weight="bold", ha="center", family="mono", halo=True)
            )
        elif kind == "child":
            ax.add_patch(Circle((x, y), 6.5, facecolor=color, edgecolor=BG, linewidth=1.2, zorder=3))
            # Radial label, rotated to point outward from the centre (classic
            # radial-tree labelling: dense fans stay collision-free because
            # text runs along the spokes, not across them). Left-half labels
            # are flipped so text is never upside down.
            import math as _m

            dx, dy = x - cx, y - cy
            mag = max((dx**2 + dy**2) ** 0.5, 1.0)
            ux, uy = dx / mag, dy / mag
            name = short(svg_gen.short_label(node), 30)
            lx, lyv = x + ux * 15, y + uy * 15
            ang = _m.degrees(_m.atan2(-uy, ux))  # display-space (y-up) angle
            ha = "left"
            if ang > 90 or ang < -90:
                ang += 180
                ha = "right"
            label_texts.append(
                ax.text(
                    lx,
                    lyv,
                    name,
                    fontsize=9,
                    color="#c9d1d9",
                    ha=ha,
                    va="center",
                    family="monospace",
                    rotation=ang,
                    rotation_mode="anchor",
                    zorder=10,
                )
            )
            label_texts[-1].set_path_effects(HALO)
        else:  # domain root
            ax.add_patch(Circle((x, y), 11, facecolor=color, edgecolor=BG, linewidth=1.8, zorder=4))
            dx, dy = x - cx, y - cy
            mag = max((dx**2 + dy**2) ** 0.5, 1.0)
            label_texts.append(
                T(
                    ax,
                    x + dx / mag * 20,
                    y + dy / mag * 20,
                    nid,
                    13,
                    color,
                    weight="bold",
                    ha="center",
                    va="center",
                    family="mono",
                    halo=True,
                )
            )

    T(
        ax,
        gx - 8,
        gy + gh + 62,
        "Nodes coloured by primary domain · edges are real parent_of relations from the projected graph.json",
        10.5,
        MUTED,
    )
    n_overlap = label_overlaps(fig, label_texts, verbose=True)
    print(f"  graph-formation: {len(label_texts)} labels, overlapping pairs: {n_overlap}")
    save(fig, out)


# ---------------------------------------------------------------------------
# 2. learning-timeline.png
# ---------------------------------------------------------------------------


def render_learning_timeline(
    curve: list[dict[str, float]], timeline: list[tuple[str, str]], node_label: str, out: Path
) -> None:
    W, H = 1560, 1040
    fig, ax = new_canvas(W, H)
    title_block(
        ax,
        W,
        "Learning over time",
        "P/M/E scores from the frozen scoring engine (contract 1.0.0) over recorded outcomes "
        "· event-store growth from the real seed import",
    )

    # Panel A: P/M/E evolution.
    ax_, ay, aw, ah = 110.0, 190.0, 1390.0, 360.0
    panel(ax, ax_ - 60, ay - 60, aw + 120, ah + 150)
    T(
        ax,
        ax_ - 30,
        ay - 24,
        f"P / M / E score evolution — node “{node_label}” (deterministic 12-outcome demo)",
        14,
        TEXT,
        weight="bold",
    )
    vmax = 1.0  # P/M/E scores are capped at 1.0 by the frozen engine
    n = len(curve)

    def xmap(i: int) -> float:
        return ax_ + (aw) * i / (n - 1)

    def ymap(v: float) -> float:
        return ay + ah - ah * v / vmax

    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = vmax * frac
        yy = ymap(v)
        ax.plot([ax_, ax_ + aw], [yy, yy], color=BORDER, lw=0.9, ls=(0, (3, 4)))
        T(ax, ax_ - 12, yy, f"{v:.2f}", 9, MUTED, ha="right", va="center", family="mono")
    for i in range(n):
        T(ax, xmap(i), ay + ah + 22, str(i + 1), 9.5, MUTED, ha="center", family="mono")

    for key, color in (("p", SCORE_COLORS["P"]), ("m", SCORE_COLORS["M"]), ("e", SCORE_COLORS["E"])):
        xs = [xmap(i) for i in range(n)]
        ys = [ymap(c[key]) for c in curve]
        ax.plot(xs, ys, color=color, lw=2.4, zorder=3, solid_capstyle="round", solid_joinstyle="round")
        ax.scatter(xs, ys, s=26, facecolor=color, edgecolor=BG, linewidth=1.2, zorder=4)

    # Highlight the mid-course setback (step 8, outcome 0.30): arrow tip lands
    # exactly on the P marker of that step.
    sx, sy_ = xmap(7), ymap(curve[7]["p"])
    ax.annotate(
        "step 8 setback: outcome 0.30",
        xy=(sx - 3, sy_ - 6),
        xytext=(sx - 210, sy_ + 165),
        fontsize=10,
        color=RED,
        ha="center",
        arrowprops={"arrowstyle": "-|>", "color": RED, "lw": 1.4},
        path_effects=HALO,
        zorder=11,
    )

    # Legend inside the plot area (top-left headroom), never on the title.
    lx = ax_ + 26
    for name, color in (
        ("P · proficiency", SCORE_COLORS["P"]),
        ("M · maturity", SCORE_COLORS["M"]),
        ("E · effectiveness", SCORE_COLORS["E"]),
    ):
        ax.plot([lx, lx + 30], [ay + 24, ay + 24], color=color, lw=3.2, solid_capstyle="round", zorder=5)
        T(ax, lx + 40, ay + 24, name, 11, TEXT, va="center", halo=True)
        lx += 220
    finals = curve[-1]
    T(
        ax,
        ax_ + aw + 40,
        ay - 30,
        f"final: P={finals['p']:.3f}  M={finals['m']:.3f}  E={finals['e']:.3f}  ·  level {finals['level']}",
        11,
        MUTED,
        ha="right",
        va="center",
        family="mono",
    )
    T(ax, ax_ + aw, ay + ah + 48, "recorded outcome →", 10.5, MUTED, ha="right")

    # Panel B: event-store growth (real replay order).
    bx, by, bw, bh = 110.0, 700.0, 1390.0, 220.0
    panel(ax, bx - 60, by - 60, bw + 120, bh + 150)
    T(ax, bx - 30, by - 24, "Event-store growth during seed import (real replay order)", 14, TEXT, weight="bold")
    nodes_c, edges_c = 0, 0
    series_n, series_e = [], []
    total = len(timeline)
    for i, (kind, _) in enumerate(timeline):
        if kind.endswith("node.upsert"):
            nodes_c += 1
        elif kind.endswith("relation.add"):
            edges_c += 1
        series_n.append((i, nodes_c))
        series_e.append((i, edges_c))
    maxv = float(max(nodes_c, edges_c)) * 1.1

    def bxmap(i: float) -> float:
        return bx + bw * i / max(total - 1, 1)

    def bymap(v: float) -> float:
        return by + bh - bh * v / maxv

    for frac in (0.0, 0.5, 1.0):
        v = maxv * frac
        yy = bymap(v)
        ax.plot([bx, bx + bw], [yy, yy], color=BORDER, lw=0.9, ls=(0, (3, 4)))
        T(ax, bx - 12, yy, f"{v:.0f}", 9, MUTED, ha="right", va="center", family="mono")
    for ev_i in range(0, total, 10):
        xx = bxmap(ev_i)
        ax.plot([xx, xx], [by + bh, by + bh + 6], color=MUTED, lw=1.0)
        T(ax, xx, by + bh + 20, str(ev_i + 1), 9, MUTED, ha="center", family="mono")

    xs_n = [bxmap(i) for i, _ in series_n]
    ax.fill_between(xs_n, [bymap(v) for _, v in series_n], by + bh, color=ACCENT, alpha=0.12)
    ax.plot(xs_n, [bymap(v) for _, v in series_n], color=ACCENT, lw=2.4)
    xs_e = [bxmap(i) for i, _ in series_e]
    ax.fill_between(xs_e, [bymap(v) for _, v in series_e], by + bh, color=GREEN, alpha=0.12)
    ax.plot(xs_e, [bymap(v) for _, v in series_e], color=GREEN, lw=2.4)

    # Legend inside the plot area (top-left headroom), never on the title.
    T(ax, bx + 26, by + 24, f"— nodes (node.upsert, {nodes_c})", 11, ACCENT, va="center", halo=True)
    T(ax, bx + 336, by + 24, f"— relations (relation.add, {edges_c})", 11, GREEN, va="center", halo=True)
    T(ax, bx + bw, by + bh + 42, "domain event sequence →", 10.5, MUTED, ha="right")
    T(ax, bx, by + bh + 42, f"{total} knowledge-affecting events · immutable · replay-ordered", 10.5, MUTED)
    save(fig, out)


# ---------------------------------------------------------------------------
# 3. bench-impact.png
# ---------------------------------------------------------------------------


def render_bench_impact(ledger_path: Path, ledger: dict, out: Path) -> None:
    cases = [c for c in ledger["cases"] if c.get("status") == "measured"]
    cases.sort(key=lambda c: c["id"])
    baseline = ledger["baseline"]["estimated_tokens"]
    savings = [c["token_saving_ratio"] for c in cases]
    mean_saving = sum(savings) / len(savings)
    accepted = sum(1 for c in cases if c.get("verdict") == "accepted")
    p50s = [c["latency_ms"]["p50"] for c in cases if c.get("latency_ms", {}).get("p50") is not None]
    med_p50 = sorted(p50s)[len(p50s) // 2] if p50s else None
    gate_version = ledger.get("gate", {}).get("gate_version", "?")

    row_h = 34
    W = 1560
    top = 250.0
    H = top + row_h * len(cases) + 170
    bx, bw = 360.0, 830.0

    fig, ax = new_canvas(W, H)
    T(ax, 50, 58, "Bench impact — token saving per case", 26, TEXT, weight="bold")
    T(
        ax,
        50,
        88,
        f"{len(cases)} measured cases vs raw-corpus baseline ({baseline:,} est. tokens) "
        f"· quality gate {gate_version} · ledger {ledger_path.name}",
        12.5,
        MUTED,
    )
    ax.plot([50, W - 50], [108, 108], color=BORDER, lw=1.2)

    # Headline stat chips.
    chips = [
        (f"{mean_saving * 100:.1f}%", "mean token saving", ACCENT),
        (f"{accepted}/{len(cases)}", "cases accepted", GREEN if accepted == len(cases) else RED),
        (f"{med_p50:.1f} ms" if med_p50 is not None else "n/a", "median p50 latency", "#ffa657"),
    ]
    chx = 50.0
    for big, small, color in chips:
        panel(ax, chx, 126, 300, 84, r=10)
        T(ax, chx + 24, 172, big, 24, color, weight="bold", family="mono")
        T(ax, chx + 24, 196, small, 11, MUTED)
        chx += 324

    # Grid + mean line.
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        xx = bx + bw * frac
        ax.plot([xx, xx], [top - 8, top + row_h * len(cases)], color=BORDER, lw=0.9)
        T(ax, xx, top + row_h * len(cases) + 20, f"{frac * 100:.0f}%", 9.5, MUTED, ha="center", family="mono")
    mean_x = bx + bw * mean_saving
    ax.plot(
        [mean_x, mean_x],
        [top - 14, top + row_h * len(cases)],
        color=ACCENT,
        lw=1.8,
        ls=(0, (6, 4)),
        zorder=1,
        alpha=0.8,
    )
    T(ax, mean_x, top - 24, f"mean {mean_saving * 100:.1f}%", 11.5, ACCENT, ha="center", weight="bold", family="mono")

    for i, c in enumerate(cases):
        yy = top + i * row_h
        dom = c.get("expected_primary_domain", "P1")
        color = DOMAIN_COLORS.get(dom, ACCENT)
        ratio = c["token_saving_ratio"]
        bar_w = bw * ratio
        if i % 2 == 0:
            ax.add_patch(Rectangle((60, yy), W - 120, row_h, facecolor="#ffffff", alpha=0.018, zorder=0))
        T(ax, bx - 14, yy + row_h / 2, c["id"], 10.5, TEXT, ha="right", va="center", family="mono")
        ax.add_patch(
            FancyBboxPatch(
                (bx, yy + 6),
                bar_w,
                row_h - 12,
                boxstyle="round,pad=0,rounding_size=4",
                facecolor=color,
                edgecolor="none",
                alpha=0.92,
                mutation_aspect=1.0,
                zorder=2,
            )
        )
        T(ax, bx + bar_w + 10, yy + row_h / 2, f"{ratio * 100:.1f}%", 10.5, TEXT, va="center", family="mono", halo=True)
        p50 = c.get("latency_ms", {}).get("p50")
        right = f"{c.get('system_estimated_tokens', 0):,} tok"
        if p50 is not None:
            right += f" · p50 {p50:.1f} ms"
        verdict = c.get("verdict", "?")
        ok = verdict == "accepted"
        # Fixed right column, far clear of the bar-end percentage labels.
        T(
            ax,
            W - 60,
            yy + row_h / 2,
            ("✓ " if ok else "✗ ") + right,
            9.5,
            GREEN if ok else RED,
            ha="right",
            va="center",
            family="mono",
        )

    fy = top + row_h * len(cases) + 56
    T(
        ax,
        50,
        fy,
        "Bars: 1 − (system response tokens / raw-corpus baseline) · quality gate: quality_system ≥ "
        "per-case compliant ceiling · colours: expected primary domain",
        10.5,
        MUTED,
    )
    lx = bx
    for dom in sorted(DOMAIN_COLORS):
        ax.add_patch(Rectangle((lx, fy + 19), 12, 12, facecolor=DOMAIN_COLORS[dom]))
        T(ax, lx + 18, fy + 30, dom, 10, TEXT, family="mono", va="center")
        lx += 60
    save(fig, out)


# ---------------------------------------------------------------------------
# 4. architecture.png
# ---------------------------------------------------------------------------


def render_architecture(out: Path) -> None:
    W, H = 1560, 1230
    fig, ax = new_canvas(W, H)
    title_block(
        ax,
        W,
        "Architecture — layered, deterministic, local-first",
        "Real module structure of src/know_your_soul · no model, provider, or vector calls anywhere",
    )

    layers = [
        (
            "ADAPTERS",
            ACCENT,
            [
                ("cli.py", "know-your-soul CLI"),
                ("mcp_server.py", "MCP tools"),
                ("http_api.py", "REST / unix socket"),
                ("source_watcher.py", "gated relearn offers"),
            ],
        ),
        (
            "SERVICE",
            "#39c5cf",
            [
                ("service.py", "KnowledgeService · 14 ops"),
                ("contracts.py", "frozen request/response"),
                ("retrieval.py", "query · path · explain"),
                ("improvement.py", "propose / apply / rollback"),
            ],
        ),
        (
            "DOMAIN + SCORING",
            GREEN,
            [
                ("domain.py", "Node · Event · Evidence"),
                ("scoring.py", "P/M/E engine (contract 1.0.0)"),
                ("review.py", "quality review"),
                ("relearn.py", "operator-gated relearn"),
            ],
        ),
        (
            "PROJECTION",
            "#ffa657",
            [
                ("projection.py", "GraphProjector · generations/"),
                ("graphify_adapter.py", "pinned partitioner"),
                ("read_model_validator.py", "artifact gate"),
                ("fragment_generator.py", "fragment cache"),
            ],
        ),
        (
            "EVENTS + STORE",
            "#f778ba",
            [
                ("store.py", "SQLite event store"),
                ("events.py", "immutable domain events"),
                ("audit.py", "invocation audit log"),
                ("backup.py", "atomic backup / restore"),
            ],
        ),
        (
            "CROSS-CUTTING",
            MUTED,
            [
                ("security.py", "sanitise · redact"),
                ("runtime_guard.py", "fail-closed guards"),
                ("observability.py", "metrics · health"),
                ("source_registry.py", "source versions"),
            ],
        ),
    ]

    lx, ly = 90.0, 150.0
    lw, lh = 1380.0, 138.0
    gap = 34.0
    for li, (name, color, modules) in enumerate(layers):
        cross_cutting = name == "CROSS-CUTTING"
        if cross_cutting:
            # Detached, dashed band: cross-cutting concerns are consumed by
            # every layer, they are not downstream of the event store.
            ax.add_patch(
                FancyBboxPatch(
                    (lx - 30, ly - 24),
                    lw + 60,
                    lh,
                    boxstyle="round,pad=0,rounding_size=12",
                    facecolor=PANEL,
                    edgecolor=BORDER,
                    linewidth=1.2,
                    linestyle=(0, (5, 4)),
                )
            )
        else:
            panel(ax, lx - 30, ly - 24, lw + 60, lh)
        ax.add_patch(
            FancyBboxPatch(
                (lx - 30, ly - 24),
                10,
                lh,
                boxstyle="round,pad=0,rounding_size=5",
                facecolor=color,
                edgecolor="none",
                alpha=0.85,
            )
        )
        T(
            ax,
            lx,
            ly + 2,
            name + (" — applies to all layers" if cross_cutting else ""),
            11.5,
            color,
            weight="bold",
            family="mono",
        )
        bw_ = (lw - 40) / 4
        for i, (mod, desc) in enumerate(modules):
            mx = lx + i * bw_ + 6
            my = ly + 22
            panel(ax, mx, my, bw_ - 14, 76, r=9, face=BG)
            T(ax, mx + 14, my + 30, mod, 12, TEXT if cross_cutting else color, family="mono", weight="bold")
            T(ax, mx + 14, my + 56, desc, 10, MUTED)
        if (
            not cross_cutting
            and (li + 1 >= len(layers) or layers[li + 1][0] != "CROSS-CUTTING")
            and li < len(layers) - 1
        ):
            # Arrows aligned to the four card centres, tips clear of the band
            # below.
            for i in range(4):
                axm = lx + i * bw_ + 6 + (bw_ - 14) / 2
                ax.annotate(
                    "",
                    xy=(axm, ly + lh + gap - 30),
                    xytext=(axm, ly + lh - 22),
                    arrowprops={"arrowstyle": "-|>", "color": MUTED, "lw": 1.5},
                    zorder=5,
                )
        ly += lh + gap

    T(
        ax,
        60,
        H - 40,
        "Dependency direction: adapters → service → domain/scoring → projection → event store · "
        "the store is the single source of truth; projections are disposable and content-addressed.",
        10.5,
        MUTED,
    )
    save(fig, out)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def generate_all(out_dir: Path, ledger: str | None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kys-visual-png-") as tmp:
        graph, timeline, store_path = svg_gen.build_projection(Path(tmp))
        child_ids = sorted(n["id"] for n in graph["nodes"] if n.get("kind") == "child")
        demo_node = (
            next(n for n in child_ids if "heuristics" in n)
            if any("heuristics" in n for n in child_ids)
            else child_ids[0]
        )
        curve = svg_gen.run_scoring_demo(store_path, demo_node)
        demo_label = next(
            n.get("source_file", n["id"]).removesuffix(".md") for n in graph["nodes"] if n["id"] == demo_node
        )
    ledger_path, ledger_data = svg_gen.load_bench_ledger(ledger)

    outputs = {
        "graph-formation.png": out_dir / "graph-formation.png",
        "learning-timeline.png": out_dir / "learning-timeline.png",
        "bench-impact.png": out_dir / "bench-impact.png",
        "architecture.png": out_dir / "architecture.png",
    }
    render_graph_formation(graph, timeline, outputs["graph-formation.png"])
    render_learning_timeline(curve, timeline, demo_label, outputs["learning-timeline.png"])
    render_bench_impact(ledger_path, ledger_data, outputs["bench-impact.png"])
    render_architecture(outputs["architecture.png"])
    return outputs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="output directory")
    parser.add_argument("--ledger", default=None, help="pin a bench ledger file (default: newest measured)")
    args = parser.parse_args()
    outputs = generate_all(Path(args.out), args.ledger)
    for path in sorted(outputs.values()):
        size = path.stat().st_size
        print(f"wrote {path.relative_to(REPO_ROOT)} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
