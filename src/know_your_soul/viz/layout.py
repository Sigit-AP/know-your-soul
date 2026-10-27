"""Deterministic pure-Python graph layout engine (ATM wave B, plan F-B7).

Origin: the *idea* of a force-directed placement pass and of a radial tree
for hub-dominated graphs is public-domain graph-drawing practice
(Fruchterman-Reingold style springs plus a tidy radial tree); the
implementation here is original and shares no code with any other project.
It is a general engine serving the exporter family.

Determinism contract (plan section 5, item 2):

* node and edge iteration is always over canonically sorted tuples, never
  over hash order;
* the only randomness is a locally seeded :class:`random.Random` drawn in
  sorted node order (initial-position jitter in force mode);
* the iteration count is fixed by the caller — no convergence-based early
  exit, so wall-clock timing can never change the output;
* final coordinates are quantized to a fixed grid (``QUANTUM``), so the
  serialized form is byte-identical across runs and hash seeds for the same
  read model, seed, iteration count and frame.

Mode selection is itself deterministic and derived from the graph *shape*:
a graph whose undirected skeleton has at most ``n - 1`` edges per connected
part is a forest, and forests (the star-of-stars corpus topology) get the
radial tree; anything denser gets the force pass. Both modes accept graphs
of any shape — ``auto`` only picks the default.
"""

from __future__ import annotations

import math
import random
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DEFAULT_ITERATIONS",
    "DEFAULT_SEED",
    "MAX_LAYOUT_ITERATIONS",
    "MAX_LAYOUT_NODES",
    "QUANTUM",
    "LayoutError",
    "LayoutResult",
    "layout",
]

# Fixed default seed ("KYS"); callers may pin any explicit seed. The seed is
# never derived from input ordering, wall-clock, or object identity.
DEFAULT_SEED = 0x4B5953
DEFAULT_ITERATIONS = 128
MAX_LAYOUT_ITERATIONS = 10_000

# Explicit fail-closed caps: layout cost is O(n^2 * iterations) in force
# mode, so the node count is bounded rather than discovered by an outage.
MAX_LAYOUT_NODES = 4096

# Coordinate quantum: positions are rounded to this grid, which removes
# sub-visible float noise and makes serialized output byte-stable.
QUANTUM = 0.001

# Golden angle (radians) for the deterministic spiral initialization.
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))

_LAYOUT_MODES = ("auto", "radial", "force")


class LayoutError(ValueError):
    """Invalid layout request: bad frame, bad mode, or an explicit cap breach."""


@dataclass(frozen=True)
class LayoutResult:
    """The layout of one read model: ``node_id -> (x, y)`` plus metadata.

    ``positions`` maps every accepted node id to a quantized coordinate
    inside ``[0, width] x [0, height]``. ``metadata`` records every input
    that shaped the result (mode actually used, seed, iterations, frame,
    counts, quantum) so a renderer can reproduce or audit the placement.
    """

    positions: dict[str, tuple[float, float]]
    metadata: dict[str, Any]


# ------------------------------------------------------------- read model


def _normalize_graph(read_model: Mapping[str, Any]) -> tuple[tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Reduce the read model to sorted node ids and sorted unique undirected edges.

    Malformed rows, unknown endpoints, self-loops and duplicates are dropped;
    the result is a pure function of graph content, never of row order.
    """
    if not isinstance(read_model, Mapping):
        raise LayoutError(f"read model must be a mapping, got {type(read_model).__name__}")
    node_ids: set[str] = set()
    raw_nodes = read_model.get("nodes")
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if not isinstance(entry, Mapping):
                continue
            node_id = entry.get("id")
            if isinstance(node_id, str) and node_id:
                node_ids.add(node_id)
    pairs: set[tuple[str, str]] = set()
    raw_links = read_model.get("links")
    if isinstance(raw_links, list):
        for entry in raw_links:
            if not isinstance(entry, Mapping):
                continue
            source, target = entry.get("source"), entry.get("target")
            if not (isinstance(source, str) and isinstance(target, str)):
                continue
            if source not in node_ids or target not in node_ids or source == target:
                continue
            pairs.add((source, target) if source < target else (target, source))
    return tuple(sorted(node_ids)), tuple(sorted(pairs))


def _undirected_adjacency(node_ids: tuple[str, ...], edges: tuple[tuple[str, str], ...]) -> dict[str, tuple[str, ...]]:
    neighbours: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for source, target in edges:
        neighbours[source].add(target)
        neighbours[target].add(source)
    return {node_id: tuple(sorted(peers)) for node_id, peers in neighbours.items()}


def _components(node_ids: tuple[str, ...], adjacency: Mapping[str, tuple[str, ...]]) -> list[tuple[str, ...]]:
    """Connected components in canonical order: largest first, ties by smallest id."""
    seen: set[str] = set()
    components: list[tuple[str, ...]] = []
    for start in node_ids:
        if start in seen:
            continue
        members: list[str] = []
        queue: deque[str] = deque([start])
        seen.add(start)
        while queue:
            current = queue.popleft()
            members.append(current)
            for peer in adjacency[current]:
                if peer not in seen:
                    seen.add(peer)
                    queue.append(peer)
        components.append(tuple(sorted(members)))
    components.sort(key=lambda members: (-len(members), members[0]))
    return components


def _quantize(value: float) -> float:
    """Snap a coordinate to the fixed grid; the result round-trips cleanly."""
    snapped = round(round(value / QUANTUM) * QUANTUM, 6)
    return snapped + 0.0  # normalize a possible negative zero


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


# --------------------------------------------------------------- radial mode


def _radial_forest(
    node_ids: tuple[str, ...],
    edges: tuple[tuple[str, str], ...],
    width: float,
    height: float,
) -> tuple[dict[str, tuple[float, float]], int]:
    """Tidy radial tree for every component; returns positions and max depth.

    Each component is rooted at its highest-degree node (ties broken by the
    smallest id), spanned by a BFS tree whose angular wedges are allocated
    proportionally to subtree leaf counts in sorted child order, and packed
    into a deterministic grid of frame cells. Depth rings are evenly spaced
    inside the cell, so a star-of-stars renders as hub rings around the root.
    """
    adjacency = _undirected_adjacency(node_ids, edges)
    components = _components(node_ids, adjacency)
    degree = {node_id: len(adjacency[node_id]) for node_id in node_ids}
    cols = max(1, math.ceil(math.sqrt(len(components))))
    rows = max(1, math.ceil(len(components) / cols))
    cell_w, cell_h = width / cols, height / rows
    margin = min(24.0, min(cell_w, cell_h) / 4.0)
    positions: dict[str, tuple[float, float]] = {}
    max_depth = 0
    for index, members in enumerate(components):
        col, row = index % cols, index // cols
        cx = (col + 0.5) * cell_w
        cy = (row + 0.5) * cell_h
        if len(members) == 1:
            positions[members[0]] = (cx, cy)
            continue
        root = min(members, key=lambda node_id: (-degree[node_id], node_id))
        # BFS tree from the root, neighbours in sorted order.
        depth = {root: 0}
        children: dict[str, list[str]] = {root: []}
        queue: deque[str] = deque([root])
        while queue:
            current = queue.popleft()
            for peer in adjacency[current]:
                if peer in depth:
                    continue
                depth[peer] = depth[current] + 1
                children.setdefault(current, []).append(peer)
                children.setdefault(peer, [])
                queue.append(peer)
        for node_id in members:
            if node_id not in depth:  # unreachable inside its own component: impossible, defensive
                depth[node_id] = 0
                children.setdefault(node_id, [])
        # Leaf counts by post-order over the BFS tree.
        leaf_count: dict[str, int] = {}
        order: list[str] = [root]
        for node_id in order:
            order.extend(children.get(node_id, []))
        for node_id in reversed(order):
            kids = children.get(node_id, [])
            leaf_count[node_id] = sum(leaf_count[kid] for kid in kids) if kids else 1
        component_depth = max(depth.values())
        max_depth = max(max_depth, component_depth)
        ring_gap = (min(cell_w, cell_h) / 2.0 - margin) / max(1, component_depth)
        # Angular wedge allocation: the root owns the full circle; each child
        # takes a share of its parent's wedge proportional to its leaf count.
        angle: dict[str, float] = {}
        wedges: dict[str, tuple[float, float]] = {root: (0.0, 2.0 * math.pi)}
        for node_id in order:
            start, span = wedges[node_id]
            angle[node_id] = start + span / 2.0
            kids = children.get(node_id, [])
            if not kids:
                continue
            total = leaf_count[node_id]
            cursor = start
            for kid in kids:
                share = span * (leaf_count[kid] / total)
                wedges[kid] = (cursor, share)
                cursor += share
        for node_id in members:
            ring = depth[node_id]
            theta = angle.get(node_id, 0.0)
            x = cx + ring_gap * ring * math.cos(theta)
            y = cy + ring_gap * ring * math.sin(theta)
            positions[node_id] = (
                _clamp(x, col * cell_w + margin, (col + 1) * cell_w - margin),
                _clamp(y, row * cell_h + margin, (row + 1) * cell_h - margin),
            )
    return positions, max_depth


# ---------------------------------------------------------------- force mode


def _force_directed(
    node_ids: tuple[str, ...],
    edges: tuple[tuple[str, str], ...],
    *,
    seed: int,
    iterations: int,
    width: float,
    height: float,
) -> dict[str, tuple[float, float]]:
    """Fixed-iteration spring layout with seeded initialization.

    A Fruchterman-Reingold style pass: all-pairs repulsion plus spring
    attraction along edges, displacements clamped by a linearly cooling
    temperature, every accumulation in canonical sorted order. The only
    randomness is the initial jitter drawn from ``random.Random(seed)`` in
    sorted node order on top of a golden-angle spiral.
    """
    count = len(node_ids)
    if count == 1:
        return {node_ids[0]: (width / 2.0, height / 2.0)}
    margin = min(24.0, min(width, height) / 4.0)
    rng = random.Random(seed)
    jitter = 0.01 * min(width, height)
    positions: dict[str, list[float]] = {}
    for index, node_id in enumerate(node_ids):
        radius = math.sqrt(index + 0.5) / math.sqrt(count)
        theta = index * _GOLDEN_ANGLE
        positions[node_id] = [
            width / 2.0 + radius * (width / 2.0 - margin) * math.cos(theta) + (rng.random() - 0.5) * jitter,
            height / 2.0 + radius * (height / 2.0 - margin) * math.sin(theta) + (rng.random() - 0.5) * jitter,
        ]
    ideal = math.sqrt(((width - 2.0 * margin) * (height - 2.0 * margin)) / count)
    start_temperature = min(width, height) / 8.0
    for step in range(iterations):
        temperature = start_temperature * (1.0 - step / iterations)
        displacement = {node_id: [0.0, 0.0] for node_id in node_ids}
        # Repulsion between every ordered pair, computed once per pair.
        for left_index in range(count):
            left = node_ids[left_index]
            lx, ly = positions[left]
            for right_index in range(left_index + 1, count):
                right = node_ids[right_index]
                rx, ry = positions[right]
                dx, dy = lx - rx, ly - ry
                distance = math.hypot(dx, dy)
                if distance < QUANTUM:
                    # Perfect overlap: separate along the deterministic spiral angle.
                    angle = (left_index - right_index) * _GOLDEN_ANGLE
                    dx, dy, distance = math.cos(angle) * QUANTUM, math.sin(angle) * QUANTUM, QUANTUM
                force = ideal * ideal / distance
                ux, uy = dx / distance * force, dy / distance * force
                displacement[left][0] += ux
                displacement[left][1] += uy
                displacement[right][0] -= ux
                displacement[right][1] -= uy
        # Attraction along every edge in sorted order.
        for source, target in edges:
            sx, sy = positions[source]
            tx, ty = positions[target]
            dx, dy = sx - tx, sy - ty
            distance = max(math.hypot(dx, dy), QUANTUM)
            force = distance * distance / ideal
            ux, uy = dx / distance * force, dy / distance * force
            displacement[source][0] -= ux
            displacement[source][1] -= uy
            displacement[target][0] += ux
            displacement[target][1] += uy
        # Apply clamped displacements in sorted node order.
        for node_id in node_ids:
            dx, dy = displacement[node_id]
            length = math.hypot(dx, dy)
            if length < QUANTUM:
                continue
            scale = min(length, temperature) / length
            positions[node_id][0] += dx * scale
            positions[node_id][1] += dy * scale
    # Fit the settled bounding box into the frame with a uniform scale so the
    # result is guaranteed inside the frame regardless of force overshoot.
    xs = [positions[node_id][0] for node_id in node_ids]
    ys = [positions[node_id][1] for node_id in node_ids]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    usable_x = width - 2.0 * margin
    usable_y = height - 2.0 * margin
    scale = 1.0
    if span_x > QUANTUM:
        scale = min(scale, usable_x / span_x)
    if span_y > QUANTUM:
        scale = min(scale, usable_y / span_y)
    fitted_cx = (max(xs) + min(xs)) / 2.0
    fitted_cy = (max(ys) + min(ys)) / 2.0
    result: dict[str, tuple[float, float]] = {}
    for node_id in node_ids:
        x = width / 2.0 + (positions[node_id][0] - fitted_cx) * scale
        y = height / 2.0 + (positions[node_id][1] - fitted_cy) * scale
        result[node_id] = (_clamp(x, margin, width - margin), _clamp(y, margin, height - margin))
    return result


# ------------------------------------------------------------------ public


def layout(
    read_model: Mapping[str, Any],
    *,
    seed: int = DEFAULT_SEED,
    iterations: int = DEFAULT_ITERATIONS,
    width: float = 1200.0,
    height: float = 800.0,
    mode: str = "auto",
) -> LayoutResult:
    """Compute a deterministic layout for the read model.

    ``mode`` is ``"auto"`` (radial tree for forests, force pass otherwise),
    ``"radial"`` or ``"force"``. Every accepted node id maps to a quantized
    coordinate inside the frame; invalid parameters and cap breaches raise
    :class:`LayoutError` (fail closed, typed).
    """
    if mode not in _LAYOUT_MODES:
        raise LayoutError(f"unknown layout mode {mode!r}; expected one of {_LAYOUT_MODES}")
    if not isinstance(width, (int, float)) or isinstance(width, bool) or not math.isfinite(width) or width <= 0.0:
        raise LayoutError(f"width must be a positive finite number, got {width!r}")
    if not isinstance(height, (int, float)) or isinstance(height, bool) or not math.isfinite(height) or height <= 0.0:
        raise LayoutError(f"height must be a positive finite number, got {height!r}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise LayoutError(f"seed must be an integer, got {seed!r}")
    if isinstance(iterations, bool) or not isinstance(iterations, int):
        raise LayoutError(f"iterations must be an integer, got {iterations!r}")
    if not 0 <= iterations <= MAX_LAYOUT_ITERATIONS:
        raise LayoutError(f"iterations must be within [0, {MAX_LAYOUT_ITERATIONS}], got {iterations}")
    frame_w, frame_h = float(width), float(height)
    node_ids, edges = _normalize_graph(read_model)
    if len(node_ids) > MAX_LAYOUT_NODES:
        raise LayoutError(f"graph has {len(node_ids)} nodes, over the {MAX_LAYOUT_NODES}-node layout cap")
    chosen = mode
    if mode == "auto":
        # A forest (m <= n - components) gets the radial tree; denser graphs
        # get the force pass. The decision is a pure function of graph shape.
        adjacency = _undirected_adjacency(node_ids, edges)
        component_count = len(_components(node_ids, adjacency))
        chosen = "radial" if len(edges) <= len(node_ids) - component_count else "force"
    if not node_ids:
        positions: dict[str, tuple[float, float]] = {}
        component_count = 0
        max_depth = 0
    elif chosen == "radial":
        raw, max_depth = _radial_forest(node_ids, edges, frame_w, frame_h)
        component_count = len(_components(node_ids, _undirected_adjacency(node_ids, edges)))
        positions = {node_id: (_quantize(x), _quantize(y)) for node_id, (x, y) in raw.items()}
    else:
        component_count = len(_components(node_ids, _undirected_adjacency(node_ids, edges)))
        max_depth = 0
        raw = _force_directed(node_ids, edges, seed=seed, iterations=iterations, width=frame_w, height=frame_h)
        positions = {node_id: (_quantize(x), _quantize(y)) for node_id, (x, y) in raw.items()}
    metadata: dict[str, Any] = {
        "mode": chosen,
        "seed": seed,
        "iterations": iterations,
        "width": frame_w,
        "height": frame_h,
        "node_count": len(node_ids),
        "edge_count": len(edges),
        "components": component_count,
        "max_depth": max_depth,
        "quantum": QUANTUM,
    }
    return LayoutResult(positions=positions, metadata=metadata)
