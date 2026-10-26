"""Deterministic visualization support (ATM wave B).

Pure-Python, dependency-free building blocks for static graph rendering:
:mod:`know_your_soul.viz.layout` computes byte-stable node positions for the
export family (F-B4 GraphML/SVG today, the HTML viewer in a later wave).
No AI, no network, no wall-clock — every quantity derives from the read
model content, an explicit seed, and canonical (sorted) iteration order.
"""

from know_your_soul.viz.layout import (
    DEFAULT_ITERATIONS,
    DEFAULT_SEED,
    MAX_LAYOUT_NODES,
    QUANTUM,
    LayoutError,
    LayoutResult,
    layout,
)

__all__ = [
    "DEFAULT_ITERATIONS",
    "DEFAULT_SEED",
    "MAX_LAYOUT_NODES",
    "QUANTUM",
    "LayoutError",
    "LayoutResult",
    "layout",
]
