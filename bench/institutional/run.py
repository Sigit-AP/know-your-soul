"""Institutional-grade graph benchmark harness (LDBC Graphalytics / GAPBS aligned).

Compares the ``know_your_soul.analytics`` kernels against reference
implementations (networkx 3.7, a pinned main dependency) on deterministic
datasets:

* small graphs with hand-verified structure (triangle, diamond, star, …);
* seeded synthetic graphs (``random.Random(fixed_seed)``, no network, no RNG
  leakage — datasets are pure functions of their name).

Per kernel the harness reports:

* CORRECTNESS — ours vs reference (exact / equivalence / epsilon matcher,
  following the LDBC Graphalytics validation strategies), plus an exact-cover
  property check for partitioning kernels;
* DETERMINISM — ours run twice must produce byte-identical canonical output;
* PERFORMANCE — median wall time of ours vs reference and the ratio. Two
  timings are reported: the public entrypoint (honest API cost) and, where
  the kernel is buried inside a suite, the isolated kernel (clearly labeled
  as reaching into private helpers).

Kernels are auto-discovered: each adapter imports its module lazily, so a
kernel whose implementation has not landed yet (e.g. the mega-wave-2
``analytics.algorithms`` module) is reported as ``skipped`` with an honest
reason instead of failing the run.

Usage::

    python bench/institutional/run.py [--repeats N] [--quick] [--strict]
                                      [--json PATH] [--md PATH]

Exit codes: 0 = harness ran and produced a report; 1 = harness ran but at
least one available kernel FAILED correctness (only with ``--strict``);
2 = bad invocation.
"""

from __future__ import annotations

import argparse
import importlib
import json
import random
import statistics
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # networkx is a pinned main dependency; guard anyway for honest skips.
    import networkx as nx
except ImportError:  # pragma: no cover - exercised only in stripped envs
    nx = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
HARNESS_KIND = "bench.institutional"

# Validation tolerances, following LDBC Graphalytics matching strategies:
# exact (BFS/CDLP-style), equivalence (WCC-style partition equality), and
# epsilon for floating scores. Graphalytics uses 0.01% relative epsilon;
# our PageRank runs a fixed 64 iterations (no early convergence stop), so the
# residual against a fully converged reference is bounded by d^64 ~ 3e-5 —
# we therefore score PR with abs<=1e-4 and report the observed error exactly.
PR_REL_TOL = 1e-3
PR_ABS_TOL = 1e-4
SCORE_REL_TOL = 1e-6
SCORE_ABS_TOL = 1e-9

PAGERANK_ITERATIONS = 64  # matches analytics.centrality.DEFAULT_PAGERANK_ITERATIONS
PAGERANK_DAMPING = 0.85  # matches analytics.centrality._PAGERANK_DAMPING

RelationFactory = Callable[[], Any]


# ------------------------------------------------------------------ datasets


def _make_graph(node_ids: list[str], edges: list[tuple[str, str]], relation: str = "depends_on") -> dict[str, Any]:
    """Build a canonical graph.json-shaped mapping: sorted unique edges, no self-loops."""
    seen: set[tuple[str, str]] = set()
    links: list[dict[str, Any]] = []
    for source, target in sorted(set(edges)):
        if source == target or (source, target) in seen:
            continue
        seen.add((source, target))
        links.append({"source": source, "target": target, "relation": relation})
    return {
        "directed": True,
        "nodes": [{"id": node_id, "domain": "P1", "kind": "leaf"} for node_id in sorted(node_ids)],
        "links": links,
        "hyperedges": [],
    }


def _erdos(name_prefix: str, n: int, m: int, seed: int) -> dict[str, Any]:
    """Deterministic Erdos-Renyi-like digraph: seeded, unique pairs, no self-loops."""
    rng = random.Random(seed)
    node_ids = [f"{name_prefix}{index:04d}" for index in range(n)]
    edges: set[tuple[str, str]] = set()
    while len(edges) < m:
        source = rng.randrange(n)
        target = rng.randrange(n)
        if source != target:
            edges.add((node_ids[source], node_ids[target]))
    return _make_graph(node_ids, sorted(edges))


def _bidirectional(edges: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return edges + [(target, source) for source, target in edges]


@dataclass(frozen=True)
class Dataset:
    name: str
    graph: dict[str, Any]
    tags: frozenset[str]
    note: str

    @property
    def n(self) -> int:
        return len(self.graph["nodes"])

    @property
    def m(self) -> int:
        return len(self.graph["links"])


def _cliques(clique_count: int, clique_size: int) -> dict[str, Any]:
    node_ids: list[str] = []
    edges: list[tuple[str, str]] = []
    for clique in range(clique_count):
        members = [f"k{clique}n{index}" for index in range(clique_size)]
        node_ids.extend(members)
        for i, source in enumerate(members):
            for target in members[i + 1 :]:
                edges.append((source, target))
                edges.append((target, source))
    return _make_graph(node_ids, edges)


def build_datasets() -> list[Dataset]:
    """All datasets are pure functions of this module — no I/O, no wall clock."""
    datasets: list[Dataset] = []

    datasets.append(
        Dataset(
            name="path3",
            graph=_make_graph(["a", "b", "c"], [("a", "b"), ("b", "c")]),
            tags=frozenset({"known", "small"}),
            note="3-node path; WCC=1, LCC=0, no cycles.",
        )
    )
    datasets.append(
        Dataset(
            name="diamond",
            graph=_make_graph(["a", "b", "c", "d"], [("a", "b"), ("a", "c"), ("a", "d"), ("b", "c")]),
            tags=frozenset({"known", "small"}),
            note="Diamond; hand-verified LCC avg=7/12, transitivity=3/5, triangles=1.",
        )
    )
    datasets.append(
        Dataset(
            name="two_triangles_bridge",
            graph=_make_graph(
                ["a", "b", "c", "d", "e", "f"],
                [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d")],
            ),
            tags=frozenset({"known", "small", "unambiguous-partition"}),
            note="Two triangles + bridge; pinned Louvain Q=5/14, partition {a,b,c}|{d,e,f}.",
        )
    )
    star_edges = [("hub", f"leaf{index}") for index in range(8)]
    datasets.append(
        Dataset(
            name="star9_bidir",
            graph=_make_graph(["hub", *[f"leaf{index}" for index in range(8)]], _bidirectional(star_edges)),
            tags=frozenset({"known", "small"}),
            note="Bidirectional 8-leaf star; hand-verified normalized BC hub=1.0, leaves=0.",
        )
    )
    ring = [f"c{index}" for index in range(6)]
    datasets.append(
        Dataset(
            name="directed_cycle6",
            graph=_make_graph(ring, [(ring[i], ring[(i + 1) % 6]) for i in range(6)]),
            tags=frozenset({"known", "small"}),
            note="Directed 6-ring; exactly one simple cycle.",
        )
    )
    datasets.append(
        Dataset(
            name="two_cycles_shared_node",
            graph=_make_graph(
                ["a", "b", "c", "d", "e"],
                [("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("d", "e"), ("e", "c")],
            ),
            tags=frozenset({"known", "small"}),
            note="Two directed triangles sharing node c; exactly two simple cycles.",
        )
    )
    datasets.append(
        Dataset(
            name="disconnected3",
            graph=_make_graph(
                ["x1", "x2", "y1", "y2", "y3", "z1"],
                [("x1", "x2"), ("y1", "y2"), ("y2", "y3")],
            ),
            tags=frozenset({"known", "small"}),
            note="Three weak components incl. one isolated node; WCC=3.",
        )
    )
    datasets.append(
        Dataset(
            name="cliques_4x5",
            graph=_cliques(4, 5),
            tags=frozenset({"known", "small", "unambiguous-partition"}),
            note="Four disjoint bidirectional K5; any correct community detection must return the 4 cliques.",
        )
    )
    datasets.append(
        Dataset(
            name="erdos_n64_m192_s11",
            graph=_erdos("e64n", 64, 192, 11),
            tags=frozenset({"seeded"}),
            note="Seeded synthetic n=64 m=192 (random.Random(11)).",
        )
    )
    datasets.append(
        Dataset(
            name="erdos_n256_m1024_s12",
            graph=_erdos("e256n", 256, 1024, 12),
            tags=frozenset({"seeded"}),
            note="Seeded synthetic n=256 m=1024 (random.Random(12)).",
        )
    )
    datasets.append(
        Dataset(
            name="erdos_n512_m2048_s13",
            graph=_erdos("e512n", 512, 2048, 13),
            tags=frozenset({"seeded"}),
            note="Seeded synthetic n=512 m=2048 (random.Random(13)); scaling point.",
        )
    )
    return datasets


# ------------------------------------------------------------------ nx views


def _nx_digraph(dataset: Dataset) -> Any:
    graph = nx.DiGraph()
    graph.add_nodes_from(node["id"] for node in dataset.graph["nodes"])
    graph.add_edges_from((link["source"], link["target"]) for link in dataset.graph["links"])
    return graph


def _nx_undirected(dataset: Dataset) -> Any:
    graph = nx.Graph()
    graph.add_nodes_from(node["id"] for node in dataset.graph["nodes"])
    graph.add_edges_from((link["source"], link["target"]) for link in dataset.graph["links"])
    return graph


# ------------------------------------------------------------------ helpers


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _time_ms(fn: Callable[[], Any], repeats: int) -> tuple[float, Any]:
    """Median wall time over *repeats* calls; returns (median_ms, last_result)."""
    samples: list[float] = []
    result: Any = None
    for _ in range(max(1, repeats)):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples), result


def _compare_score_maps(
    ours: Mapping[str, float], reference: Mapping[str, float], *, rel: float, abs_tol: float
) -> tuple[bool, dict[str, Any]]:
    """Epsilon matcher (Graphalytics LCC/PR/SSSP style): per-node |diff| <= abs+rel*|ref|."""
    if set(ours) != set(reference):
        return False, {"error": "node-id sets differ", "only_ours": sorted(set(ours) - set(reference))[:5]}
    max_abs = 0.0
    max_rel = 0.0
    worst = ""
    for node_id in ours:
        diff = abs(ours[node_id] - reference[node_id])
        denom = abs(reference[node_id])
        rel_diff = diff / denom if denom else 0.0
        if diff > max_abs:
            max_abs, worst = diff, node_id
        max_rel = max(max_rel, rel_diff)
        if diff > abs_tol + rel * denom:
            return False, {
                "max_abs_err": max_abs,
                "max_rel_err": max_rel,
                "worst_node": node_id,
                "failed_node": node_id,
            }
    return True, {"max_abs_err": max_abs, "max_rel_err": max_rel, "worst_node": worst}


def _partition_of(memberships: Any) -> frozenset[frozenset[str]]:
    return frozenset(frozenset(members) for members in memberships)


def _partitions_equal(left: frozenset[frozenset[str]], right: frozenset[frozenset[str]]) -> bool:
    """Equivalence matcher (Graphalytics WCC style): same equivalence classes."""
    return left == right


def _is_exact_cover(memberships: list[list[str]], node_ids: list[str]) -> bool:
    flat = sorted(member for members in memberships for member in members)
    return flat == sorted(node_ids)


# ------------------------------------------------------------------ ours: discovery + runners


def _load(module_name: str, attr: str) -> Callable[..., Any] | None:
    """Auto-discover a kernel entrypoint; None means 'not landed yet'."""
    try:
        module = importlib.import_module(module_name)
    except ImportError:
        return None
    return getattr(module, attr, None)


def ours_pagerank_public(graph: dict[str, Any]) -> dict[str, float]:
    centrality = importlib.import_module("know_your_soul.analytics.centrality")
    result = centrality.compute_centrality(graph, pagerank_iterations=PAGERANK_ITERATIONS)
    return {entry["node_id"]: float(entry["score"]) for entry in result["global"]["pagerank"]}


def ours_pagerank_kernel(graph: dict[str, Any]) -> dict[str, float]:
    """Isolated PageRank via private helpers (labeled as such in the report)."""
    centrality = importlib.import_module("know_your_soul.analytics.centrality")
    node_ids, _domains, links, _dropped = centrality._prepare_graph(graph)
    adjacency = centrality._directed_adjacency(node_ids, links)
    scores = centrality._pagerank_scores(node_ids, links, adjacency, PAGERANK_ITERATIONS)
    return {node_id: float(score) for node_id, score in scores.items()}


def ours_wcc_public(graph: dict[str, Any]) -> list[list[str]]:
    components = importlib.import_module("know_your_soul.analytics.components")
    result = components.analyze_components(graph)
    return [entry["nodes"] for entry in result["weak_components"]]


def ours_wcc_kernel(graph: dict[str, Any]) -> list[list[str]]:
    """Isolated weak-components via private helpers (labeled as such)."""
    components = importlib.import_module("know_your_soul.analytics.components")
    node_ids, _domains, links, _dropped = components._prepare_graph(graph)
    undirected = components._undirected_adjacency(node_ids, links)
    return components._weak_components(node_ids, undirected)


def ours_cdlp_public(graph: dict[str, Any]) -> list[list[str]]:
    communities = importlib.import_module("know_your_soul.analytics.communities")
    result = communities.detect_communities(graph["nodes"], graph["links"])
    return [entry["members"] for entry in result["communities"]]


def ours_bc_public(graph: dict[str, Any]) -> dict[str, float]:
    centrality = importlib.import_module("know_your_soul.analytics.centrality")
    node_count = len(graph["nodes"])
    result = centrality.compute_centrality(graph, betweenness_samples=max(node_count, 1))
    return {entry["node_id"]: float(entry["score"]) for entry in result["global"]["betweenness"]}


def ours_bc_kernel(graph: dict[str, Any]) -> dict[str, float]:
    """Isolated exact Brandes (samples >= n forces exactness), private helpers."""
    centrality = importlib.import_module("know_your_soul.analytics.centrality")
    node_ids, _domains, links, _dropped = centrality._prepare_graph(graph)
    adjacency = centrality._directed_adjacency(node_ids, links)
    scores, _sampled = centrality._betweenness_scores(node_ids, adjacency, max(len(node_ids), 1))
    return {node_id: float(score) for node_id, score in scores.items()}


def ours_cycles_public(graph: dict[str, Any]) -> list[tuple[str, ...]]:
    cycles = importlib.import_module("know_your_soul.analytics.cycles")
    result = cycles.find_cycles(
        graph,
        relations=cycles.DEFAULT_CYCLE_RELATIONS,
        max_length=8,
        max_cycles=100_000,
        max_expansions=10_000_000,
    )
    if result["guards"]:
        raise RuntimeError(f"cycle enumeration truncated: {result['guards']}")
    return [tuple(entry["nodes"]) for entry in result["cycles"]]


# ------------------------------------------------------------------ references


def ref_pagerank(dataset: Dataset) -> dict[str, float]:
    """Converged PageRank reference in numpy (networkx.pagerank needs scipy, which is
    intentionally not a dependency). Self-validating: asserts the L1 residual of the
    returned fixed point is below 1e-10, so the reference is verified, not trusted."""
    import numpy as np

    graph = dataset.graph
    node_ids = sorted(node["id"] for node in graph["nodes"])
    index = {node_id: i for i, node_id in enumerate(node_ids)}
    n = len(node_ids)
    if n == 0:
        return {}
    edges = {(link["source"], link["target"]) for link in graph["links"]}
    out_degree = [0] * n
    columns: list[list[int]] = [[] for _ in range(n)]  # predecessors per node
    for source, target in edges:
        out_degree[index[source]] += 1
        columns[index[target]].append(index[source])
    transition = np.zeros((n, n))
    for target in range(n):
        for source in columns[target]:
            transition[target, source] = 1.0 / out_degree[source]
    dangling_mask = np.array([1.0 if out_degree[i] == 0 else 0.0 for i in range(n)])
    rank = np.full(n, 1.0 / n)
    base = (1.0 - PAGERANK_DAMPING) / n
    for _ in range(10_000):
        dangling_mass = float(rank @ dangling_mask)
        nxt = PAGERANK_DAMPING * (transition @ rank + dangling_mass / n) + base
        if float(np.abs(nxt - rank).sum()) < 1e-13:
            rank = nxt
            break
        rank = nxt
    residual = PAGERANK_DAMPING * (transition @ rank + float(rank @ dangling_mask) / n) + base - rank
    assert float(np.abs(residual).sum()) < 1e-10, "pagerank reference did not converge"
    assert abs(float(rank.sum()) - 1.0) < 1e-9, "pagerank reference lost mass"
    return {node_id: float(rank[index[node_id]]) for node_id in node_ids}


def ref_wcc(dataset: Dataset) -> list[list[str]]:
    graph = _nx_undirected(dataset)
    return [sorted(members) for members in nx.connected_components(graph)]


def ref_bc(dataset: Dataset) -> dict[str, float]:
    graph = _nx_digraph(dataset)
    scores = nx.betweenness_centrality(graph, normalized=True, endpoints=False)
    return {str(node): float(value) for node, value in scores.items()}


def ref_cycles(dataset: Dataset) -> list[tuple[str, ...]]:
    graph = _nx_digraph(dataset)
    found = []
    for cycle in nx.simple_cycles(graph):
        pivot = cycle.index(min(cycle))
        found.append(tuple(cycle[pivot:] + cycle[:pivot]))
    return found


def ref_cdlp(dataset: Dataset) -> list[list[str]]:
    graph = _nx_undirected(dataset)
    communities = nx.community.asyn_lpa_communities(graph, seed=0)
    return [sorted(members) for members in communities]


def ref_louvain(dataset: Dataset) -> list[list[str]]:
    graph = _nx_undirected(dataset)
    communities = nx.community.louvain_communities(graph, seed=0, resolution=1.0)
    return [sorted(members) for members in communities]


def ref_lcc(dataset: Dataset) -> dict[str, float]:
    graph = _nx_undirected(dataset)
    return {str(node): float(value) for node, value in nx.clustering(graph).items()}


def ref_triangle_count(dataset: Dataset) -> int:
    graph = _nx_undirected(dataset)
    return int(sum(nx.triangles(graph).values()) // 3)


def ref_modularity_of(dataset: Dataset, memberships: list[list[str]]) -> float:
    graph = _nx_undirected(dataset)
    return float(nx.community.modularity(graph, [set(members) for members in memberships]))


# ------------------------------------------------------------------ kernel specs


@dataclass
class DatasetReport:
    dataset: str
    n: int
    m: int
    correctness: str  # "pass" | "fail" | "info" (perf-only / property-only)
    detail: dict[str, Any] = field(default_factory=dict)
    deterministic: bool = True
    ours_public_ms: float | None = None
    ours_kernel_ms: float | None = None
    reference_ms: float | None = None

    def ratio_public(self) -> float | None:
        if self.ours_public_ms is None or not self.reference_ms:
            return None
        return self.ours_public_ms / self.reference_ms

    def ratio_kernel(self) -> float | None:
        if self.ours_kernel_ms is None or not self.reference_ms:
            return None
        return self.ours_kernel_ms / self.reference_ms


@dataclass
class KernelReport:
    kernel: str
    suites: list[str]
    matcher: str
    status: str  # "pass" | "fail" | "skipped"
    reason: str = ""
    notes: list[str] = field(default_factory=list)
    datasets: list[DatasetReport] = field(default_factory=list)


def _dataset_allowed(dataset: Dataset, *, quick: bool, max_n: int) -> bool:
    limit = 64 if quick else max_n
    return dataset.n <= limit


def _run_score_kernel(
    spec_name: str,
    suites: list[str],
    matcher: str,
    datasets: list[Dataset],
    *,
    repeats: int,
    quick: bool,
    max_n: int,
    ours_public: Callable[[dict[str, Any]], dict[str, float]],
    ref: Callable[[Dataset], dict[str, float]],
    rel: float,
    abs_tol: float,
    ours_kernel: Callable[[dict[str, Any]], dict[str, float]] | None = None,
    kernel_note: str | None = None,
    dataset_filter: Callable[[Dataset], bool] | None = None,
) -> KernelReport:
    report = KernelReport(kernel=spec_name, suites=suites, matcher=matcher, status="pass")
    if kernel_note:
        report.notes.append(kernel_note)
    for dataset in datasets:
        if not _dataset_allowed(dataset, quick=quick, max_n=max_n):
            continue
        if dataset_filter is not None and not dataset_filter(dataset):
            continue
        entry = DatasetReport(dataset=dataset.name, n=dataset.n, m=dataset.m, correctness="pass")
        ours_ms, ours_value = _time_ms(lambda ds=dataset: ours_public(ds.graph), repeats)
        entry.ours_public_ms = ours_ms
        again = ours_public(dataset.graph)
        entry.deterministic = _canonical(ours_value) == _canonical(again)
        if ours_kernel is not None:
            kernel_ms, _ = _time_ms(lambda ds=dataset: ours_kernel(ds.graph), repeats)
            entry.ours_kernel_ms = kernel_ms
        ref_ms, ref_value = _time_ms(lambda ds=dataset: ref(ds), repeats)
        entry.reference_ms = ref_ms
        ok, detail = _compare_score_maps(ours_value, ref_value, rel=rel, abs_tol=abs_tol)
        entry.detail = detail
        if not entry.deterministic:
            ok = False
            entry.detail["determinism"] = "ours returned different outputs on repeated runs"
        entry.correctness = "pass" if ok else "fail"
        report.datasets.append(entry)
        if not ok:
            report.status = "fail"
    return report


def _kernel_pagerank(datasets: list[Dataset], *, repeats: int, quick: bool, max_n: int) -> KernelReport:
    if _load("know_your_soul.analytics.centrality", "compute_centrality") is None:
        return KernelReport(
            "pagerank", [], "", "skipped", "kernel belum tersedia: analytics.centrality.compute_centrality"
        )
    report = _run_score_kernel(
        "pagerank",
        ["LDBC Graphalytics: PR", "GAPBS: PR"],
        f"epsilon rel<={PR_REL_TOL:g} abs<={PR_ABS_TOL:g} vs self-validated numpy reference (L1 residual < 1e-10)",
        datasets,
        repeats=repeats,
        quick=quick,
        max_n=max_n,
        ours_public=ours_pagerank_public,
        ours_kernel=ours_pagerank_kernel,
        ref=ref_pagerank,
        rel=PR_REL_TOL,
        abs_tol=PR_ABS_TOL,
        kernel_note=(
            "ours runs a fixed 64 iterations (no convergence stop); public entrypoint computes the full "
            "centrality suite, so ratio_public overstates per-kernel cost — ratio_kernel isolates PageRank."
        ),
    )
    # Cross-check against a pure-python float clone of the same fixed-iteration
    # scheme: catches spec drift that the loose converged-reference tolerance
    # could hide.
    clone_detail = _pagerank_clone_check(datasets, quick=quick, max_n=max_n)
    report.notes.append(clone_detail)
    if clone_detail.startswith("FAIL"):
        report.status = "fail"
    return report


def _pagerank_clone(graph: dict[str, Any]) -> dict[str, float]:
    """Pure-python float clone of the documented fixed-iteration scheme."""
    node_ids = sorted(node["id"] for node in graph["nodes"])
    edges = {(link["source"], link["target"]) for link in graph["links"]}
    out_adj = {node: sorted({t for s, t in edges if s == node}) for node in node_ids}
    preds = {node: sorted({s for s, t in edges if t == node}) for node in node_ids}
    n = len(node_ids)
    if n == 0:
        return {}
    base = (1.0 - PAGERANK_DAMPING) / n
    rank = {node: 1.0 / n for node in node_ids}
    for _ in range(PAGERANK_ITERATIONS):
        dangling = sum(rank[node] for node in node_ids if not out_adj[node])
        rank = {
            node: base + PAGERANK_DAMPING * (sum(rank[p] / len(out_adj[p]) for p in preds[node]) + dangling / n)
            for node in node_ids
        }
    return rank


def _pagerank_clone_check(datasets: list[Dataset], *, quick: bool, max_n: int) -> str:
    worst = 0.0
    for dataset in datasets:
        if not _dataset_allowed(dataset, quick=quick, max_n=max_n):
            continue
        ours = ours_pagerank_kernel(dataset.graph)
        clone = _pagerank_clone(dataset.graph)
        ok, detail = _compare_score_maps(ours, clone, rel=1e-9, abs_tol=1e-9)
        if not ok:
            return f"FAIL: fixed-iteration clone diverges on {dataset.name}: {detail}"
        worst = max(worst, float(detail["max_abs_err"]))
    return f"fixed-iteration float clone cross-check OK (max_abs_err={worst:.3g}, tol=1e-9)"


def _kernel_wcc(datasets: list[Dataset], *, repeats: int, quick: bool, max_n: int) -> KernelReport:
    if _load("know_your_soul.analytics.components", "analyze_components") is None:
        return KernelReport("wcc", [], "", "skipped", "kernel belum tersedia: analytics.components.analyze_components")
    report = KernelReport(
        kernel="wcc",
        suites=["LDBC Graphalytics: WCC", "GAPBS: CC"],
        matcher="equivalence (same weak components as networkx)",
        status="pass",
        notes=[
            "public entrypoint computes the full connectivity suite (weak+strong+articulation+k-core); "
            "ratio_kernel isolates the WCC pass."
        ],
    )
    for dataset in datasets:
        if not _dataset_allowed(dataset, quick=quick, max_n=max_n):
            continue
        entry = DatasetReport(dataset=dataset.name, n=dataset.n, m=dataset.m, correctness="pass")
        ours_ms, ours_value = _time_ms(lambda ds=dataset: ours_wcc_public(ds.graph), repeats)
        entry.ours_public_ms = ours_ms
        again = ours_wcc_public(dataset.graph)
        entry.deterministic = _canonical(ours_value) == _canonical(again)
        kernel_ms, _ = _time_ms(lambda ds=dataset: ours_wcc_kernel(ds.graph), repeats)
        entry.ours_kernel_ms = kernel_ms
        ref_ms, ref_value = _time_ms(lambda ds=dataset: ref_wcc(ds), repeats)
        entry.reference_ms = ref_ms
        ok = _partitions_equal(_partition_of(ours_value), _partition_of(ref_value))
        entry.detail = {"ours_components": len(ours_value), "reference_components": len(ref_value)}
        if not entry.deterministic:
            ok = False
            entry.detail["determinism"] = "ours returned different outputs on repeated runs"
        entry.correctness = "pass" if ok else "fail"
        report.datasets.append(entry)
        if not ok:
            report.status = "fail"
    return report


def _kernel_bc(datasets: list[Dataset], *, repeats: int, quick: bool, max_n: int) -> KernelReport:
    if _load("know_your_soul.analytics.centrality", "compute_centrality") is None:
        return KernelReport("bc", [], "", "skipped", "kernel belum tersedia: analytics.centrality.compute_centrality")
    report = KernelReport(
        kernel="bc",
        suites=["GAPBS: BC"],
        matcher=f"epsilon rel<={SCORE_REL_TOL:g} abs<={SCORE_ABS_TOL:g} vs networkx Brandes (normalized, directed)",
        status="pass",
        notes=[
            "correctness runs with betweenness_samples >= n, forcing exact Brandes (default 32 samples is an "
            "estimator); ratio_kernel compares exact Brandes vs exact Brandes."
        ],
    )
    for dataset in datasets:
        if not _dataset_allowed(dataset, quick=quick, max_n=max_n):
            continue
        if dataset.n > 256:  # exact Brandes O(n*m) on both sides; keep the run bounded
            continue
        entry = DatasetReport(dataset=dataset.name, n=dataset.n, m=dataset.m, correctness="pass")
        ours_ms, ours_value = _time_ms(lambda ds=dataset: ours_bc_public(ds.graph), repeats)
        entry.ours_public_ms = ours_ms
        again = ours_bc_public(dataset.graph)
        entry.deterministic = _canonical(ours_value) == _canonical(again)
        kernel_ms, kernel_value = _time_ms(lambda ds=dataset: ours_bc_kernel(ds.graph), repeats)
        entry.ours_kernel_ms = kernel_ms
        ref_ms, ref_value = _time_ms(lambda ds=dataset: ref_bc(ds), repeats)
        entry.reference_ms = ref_ms
        ok, detail = _compare_score_maps(kernel_value, ref_value, rel=SCORE_REL_TOL, abs_tol=SCORE_ABS_TOL)
        entry.detail = detail
        if not entry.deterministic:
            ok = False
            entry.detail["determinism"] = "ours returned different outputs on repeated runs"
        entry.correctness = "pass" if ok else "fail"
        report.datasets.append(entry)
        if not ok:
            report.status = "fail"
    return report


def _kernel_cycles(datasets: list[Dataset], *, repeats: int, quick: bool, max_n: int) -> KernelReport:
    if _load("know_your_soul.analytics.cycles", "find_cycles") is None:
        return KernelReport("cycles", [], "", "skipped", "kernel belum tersedia: analytics.cycles.find_cycles")
    report = KernelReport(
        kernel="cycles",
        suites=["project-local (no LDBC/GAPBS counterpart)"],
        matcher="exact set match vs networkx.simple_cycles (canonical rotation)",
        status="pass",
        notes=["bounded to small datasets (n<=12): simple-cycle counts explode combinatorially."],
    )
    for dataset in datasets:
        if dataset.n > 12:
            continue
        if not _dataset_allowed(dataset, quick=quick, max_n=max_n):
            continue
        entry = DatasetReport(dataset=dataset.name, n=dataset.n, m=dataset.m, correctness="pass")
        try:
            ours_ms, ours_value = _time_ms(lambda ds=dataset: ours_cycles_public(ds.graph), repeats)
        except RuntimeError as exc:
            entry.correctness = "fail"
            entry.detail = {"error": str(exc)}
            report.datasets.append(entry)
            report.status = "fail"
            continue
        entry.ours_public_ms = ours_ms
        again = ours_cycles_public(dataset.graph)
        entry.deterministic = _canonical(ours_value) == _canonical(again)
        ref_ms, ref_value = _time_ms(lambda ds=dataset: ref_cycles(ds), repeats)
        entry.reference_ms = ref_ms
        ok = set(ours_value) == set(ref_value) and len(ours_value) == len(ref_value)
        entry.detail = {"ours_cycles": len(ours_value), "reference_cycles": len(ref_value)}
        if not entry.deterministic:
            ok = False
            entry.detail["determinism"] = "ours returned different outputs on repeated runs"
        entry.correctness = "pass" if ok else "fail"
        report.datasets.append(entry)
        if not ok:
            report.status = "fail"
    return report


def _run_partition_kernel(
    name: str,
    suites: list[str],
    matcher: str,
    datasets: list[Dataset],
    *,
    repeats: int,
    quick: bool,
    max_n: int,
    ours: Callable[[dict[str, Any]], list[list[str]]],
    ref: Callable[[Dataset], list[list[str]]],
    notes: list[str],
) -> KernelReport:
    """Community kernels: exact-cover property everywhere, equivalence on tagged datasets."""
    report = KernelReport(kernel=name, suites=suites, matcher=matcher, status="pass", notes=list(notes))
    for dataset in datasets:
        if not _dataset_allowed(dataset, quick=quick, max_n=max_n):
            continue
        entry = DatasetReport(dataset=dataset.name, n=dataset.n, m=dataset.m, correctness="info")
        ours_ms, ours_value = _time_ms(lambda ds=dataset: ours(ds.graph), repeats)
        entry.ours_public_ms = ours_ms
        again = ours(dataset.graph)
        entry.deterministic = _canonical(ours_value) == _canonical(again)
        node_ids = [node["id"] for node in dataset.graph["nodes"]]
        detail: dict[str, Any] = {
            "communities": len(ours_value),
            "exact_cover": _is_exact_cover(ours_value, node_ids),
        }
        ok = bool(detail["exact_cover"])
        if "unambiguous-partition" in dataset.tags:
            ref_ms, ref_value = _time_ms(lambda ds=dataset: ref(ds), repeats)
            entry.reference_ms = ref_ms
            equivalent = _partitions_equal(_partition_of(ours_value), _partition_of(ref_value))
            detail["reference_communities"] = len(ref_value)
            detail["equivalent_to_reference"] = equivalent
            ok = ok and equivalent
            entry.correctness = "pass" if ok else "fail"
        else:
            entry.correctness = "pass" if ok else "fail"
            detail["mode"] = "property-only (partition is ambiguous on this dataset)"
        if not entry.deterministic:
            ok = False
            detail["determinism"] = "ours returned different outputs on repeated runs"
            entry.correctness = "fail"
        entry.detail = detail
        report.datasets.append(entry)
        if not ok:
            report.status = "fail"
    return report


def _kernel_cdlp(datasets: list[Dataset], *, repeats: int, quick: bool, max_n: int) -> KernelReport:
    if _load("know_your_soul.analytics.communities", "detect_communities") is None:
        return KernelReport(
            "cdlp", [], "", "skipped", "kernel belum tersedia: analytics.communities.detect_communities"
        )
    return _run_partition_kernel(
        "cdlp",
        ["LDBC Graphalytics: CDLP"],
        "exact-cover everywhere + equivalence vs networkx asyn_lpa(seed=0) on unambiguous datasets",
        datasets,
        repeats=repeats,
        quick=quick,
        max_n=max_n,
        ours=ours_cdlp_public,
        ref=ref_cdlp,
        notes=[
            "LPA partitions are only well-defined on unambiguous structure; ambiguous datasets are scored "
            "on the exact-cover property only (Graphalytics itself validates CDLP exactly on fixed inputs).",
            "CDLP variant: canonical-order asynchronous LPA with hop attenuation (delta=0.25, HANP-style) "
            "+ self-vote + leaf-follow rule, fully deterministic. The previous plain LPA merged "
            "two_triangles_bridge into one community; the attenuated variant splits at the bridge, matching "
            "networkx asyn_lpa(seed=0) and the Louvain-pinned partition (Q=5/14).",
        ],
    )


def _kernel_lcc(datasets: list[Dataset], *, repeats: int, quick: bool, max_n: int) -> KernelReport:
    func = _load("know_your_soul.analytics.algorithms", "compute_clustering")
    if func is None:
        return KernelReport(
            "lcc",
            ["LDBC Graphalytics: LCC"],
            "",
            "skipped",
            "kernel belum tersedia: analytics.algorithms.compute_clustering (mega-wave 2 belum landed)",
        )

    def ours(graph: dict[str, Any]) -> dict[str, float]:
        result = func(graph)
        return {entry["node_id"]: float(entry["coefficient"]) for entry in result["per_node"]}

    return _run_score_kernel(
        "lcc",
        ["LDBC Graphalytics: LCC"],
        f"epsilon rel<={SCORE_REL_TOL:g} abs<={SCORE_ABS_TOL:g} vs networkx.clustering (undirected)",
        datasets,
        repeats=repeats,
        quick=quick,
        max_n=max_n,
        ours_public=ours,
        ref=ref_lcc,
        rel=SCORE_REL_TOL,
        abs_tol=SCORE_ABS_TOL,
    )


def _kernel_tc(datasets: list[Dataset], *, repeats: int, quick: bool, max_n: int) -> KernelReport:
    func = _load("know_your_soul.analytics.algorithms", "compute_clustering")
    if func is None:
        return KernelReport(
            "tc",
            ["GAPBS: TC"],
            "",
            "skipped",
            "kernel belum tersedia: analytics.algorithms.compute_clustering (mega-wave 2 belum landed)",
        )

    def ours(graph: dict[str, Any]) -> int:
        return int(func(graph)["global"]["triangles"])

    report = KernelReport(
        kernel="tc",
        suites=["GAPBS: TC"],
        matcher="exact integer equality vs networkx.triangles sum/3",
        status="pass",
    )
    for dataset in datasets:
        if not _dataset_allowed(dataset, quick=quick, max_n=max_n):
            continue
        entry = DatasetReport(dataset=dataset.name, n=dataset.n, m=dataset.m, correctness="pass")
        ours_ms, ours_value = _time_ms(lambda ds=dataset: ours(ds.graph), repeats)
        entry.ours_public_ms = ours_ms
        again = ours(dataset.graph)
        entry.deterministic = ours_value == again
        ref_ms, ref_value = _time_ms(lambda ds=dataset: ref_triangle_count(ds), repeats)
        entry.reference_ms = ref_ms
        ok = ours_value == ref_value and entry.deterministic
        entry.detail = {"ours_triangles": ours_value, "reference_triangles": ref_value}
        entry.correctness = "pass" if ok else "fail"
        report.datasets.append(entry)
        if not ok:
            report.status = "fail"
    return report


def _kernel_louvain(datasets: list[Dataset], *, repeats: int, quick: bool, max_n: int) -> KernelReport:
    func = _load("know_your_soul.analytics.algorithms", "compute_louvain")
    if func is None:
        return KernelReport(
            "louvain",
            ["project-local (GAPBS-adjacent)"],
            "",
            "skipped",
            "kernel belum tersedia: analytics.algorithms.compute_louvain (mega-wave 2 belum landed)",
        )

    def ours(graph: dict[str, Any]) -> list[list[str]]:
        result = func(graph)
        return [list(entry["members"]) for entry in result["communities"]]

    report = _run_partition_kernel(
        "louvain",
        ["project-local (GAPBS-adjacent)"],
        "exact-cover + modularity cross-check vs networkx + equivalence on unambiguous datasets",
        datasets,
        repeats=repeats,
        quick=quick,
        max_n=max_n,
        ours=ours,
        ref=ref_louvain,
        notes=[
            "modularity of OUR partition is recomputed with networkx.community.modularity and compared "
            "to ours (12-decimal) at abs<=1e-9."
        ],
    )
    # Modularity cross-check on every dataset where the partition passed.
    for entry in report.datasets:
        if entry.correctness != "pass":
            continue
        dataset = next(ds for ds in datasets if ds.name == entry.dataset)
        memberships = ours(dataset.graph)
        ours_q = float(func(dataset.graph)["modularity"])
        ref_q = ref_modularity_of(dataset, memberships)
        diff = abs(ours_q - ref_q)
        entry.detail["modularity_abs_err"] = diff
        if diff > SCORE_ABS_TOL:
            entry.correctness = "fail"
            entry.detail["modularity"] = f"ours={ours_q} networkx={ref_q}"
            report.status = "fail"
    return report


def _skipped_kernel(key: str, suites: list[str], reason: str) -> KernelReport:
    return KernelReport(kernel=key, suites=suites, matcher="", status="skipped", reason=reason)


# ------------------------------------------------------------------ harness

KERNEL_ORDER = [
    "pagerank",
    "wcc",
    "bc",
    "cdlp",
    "lcc",
    "tc",
    "louvain",
    "cycles",
    "bfs",
    "sssp",
]


def run_benchmark(*, repeats: int = 3, quick: bool = False, max_n: int = 512) -> dict[str, Any]:
    """Run every kernel; returns the JSON-serializable report dict."""
    datasets = build_datasets()
    reference_available = nx is not None
    kernels: list[KernelReport] = []
    if reference_available:
        kernels.append(_kernel_pagerank(datasets, repeats=repeats, quick=quick, max_n=max_n))
        kernels.append(_kernel_wcc(datasets, repeats=repeats, quick=quick, max_n=max_n))
        kernels.append(_kernel_bc(datasets, repeats=repeats, quick=quick, max_n=max_n))
        kernels.append(_kernel_cdlp(datasets, repeats=repeats, quick=quick, max_n=max_n))
        kernels.append(_kernel_lcc(datasets, repeats=repeats, quick=quick, max_n=max_n))
        kernels.append(_kernel_tc(datasets, repeats=repeats, quick=quick, max_n=max_n))
        kernels.append(_kernel_louvain(datasets, repeats=repeats, quick=quick, max_n=max_n))
        kernels.append(_kernel_cycles(datasets, repeats=repeats, quick=quick, max_n=max_n))
    else:  # pragma: no cover - networkx is a pinned main dependency
        for key, suites in [
            ("pagerank", ["LDBC Graphalytics: PR", "GAPBS: PR"]),
            ("wcc", ["LDBC Graphalytics: WCC", "GAPBS: CC"]),
            ("bc", ["GAPBS: BC"]),
            ("cdlp", ["LDBC Graphalytics: CDLP"]),
            ("lcc", ["LDBC Graphalytics: LCC"]),
            ("tc", ["GAPBS: TC"]),
            ("louvain", ["project-local"]),
            ("cycles", ["project-local"]),
        ]:
            kernels.append(_skipped_kernel(key, suites, "referensi networkx tidak tersedia"))
    kernels.append(
        _skipped_kernel(
            "bfs",
            ["LDBC Graphalytics: BFS", "GAPBS: BFS"],
            "kernel belum tersedia: tidak ada entrypoint BFS publik (hanya analytics.centrality._bfs_distances privat)",
        )
    )
    kernels.append(
        _skipped_kernel(
            "sssp",
            ["LDBC Graphalytics: SSSP", "GAPBS: SSSP"],
            "kernel belum tersedia: graph read-model tidak membawa bobot edge; tidak ada SSSP publik",
        )
    )

    ordered = sorted(kernels, key=lambda kernel: KERNEL_ORDER.index(kernel.kernel))
    available = [kernel for kernel in ordered if kernel.status != "skipped"]
    passed = [kernel for kernel in available if kernel.status == "pass"]
    failed = [kernel for kernel in available if kernel.status == "fail"]
    label = (
        f"networkx {getattr(nx, '__version__', 'unknown')} (+ self-validated numpy PageRank reference; "
        "networkx.pagerank requires scipy which is intentionally not a dependency)"
        if reference_available
        else "unavailable"
    )
    report = {
        "schema_version": SCHEMA_VERSION,
        "kind": HARNESS_KIND,
        "config": {"repeats": repeats, "quick": quick, "max_n": max_n},
        "reference": label,
        "datasets": [
            {"name": ds.name, "n": ds.n, "m": ds.m, "tags": sorted(ds.tags), "note": ds.note} for ds in datasets
        ],
        "kernels": [
            {
                "kernel": kernel.kernel,
                "suites": kernel.suites,
                "status": kernel.status,
                "reason": kernel.reason,
                "matcher": kernel.matcher,
                "notes": kernel.notes,
                "datasets": [
                    {
                        "dataset": entry.dataset,
                        "n": entry.n,
                        "m": entry.m,
                        "correctness": entry.correctness,
                        "deterministic": entry.deterministic,
                        "detail": entry.detail,
                        "ours_public_ms": entry.ours_public_ms,
                        "ours_kernel_ms": entry.ours_kernel_ms,
                        "reference_ms": entry.reference_ms,
                        "ratio_public": entry.ratio_public(),
                        "ratio_kernel": entry.ratio_kernel(),
                    }
                    for entry in kernel.datasets
                ],
            }
            for kernel in ordered
        ],
        "summary": {
            "kernels_total": len(ordered),
            "kernels_available": len(available),
            "kernels_passed": len(passed),
            "kernels_failed": len(failed),
            "kernels_skipped": len(ordered) - len(available),
            "score": f"{len(passed)}/{len(available)} available kernels PASS correctness",
        },
    }
    return report


# ------------------------------------------------------------------ markdown


def _fmt_ms(value: float | None) -> str:
    return f"{value:.3f}" if value is not None else "—"


def _fmt_ratio(value: float | None) -> str:
    return f"{value:.1f}x" if value is not None else "—"


def render_markdown(report: Mapping[str, Any]) -> str:
    lines: list[str] = []
    summary = report["summary"]
    lines.append("# Institutional graph benchmark — skor")
    lines.append("")
    lines.append(
        f"Referensi: `{report['reference']}` · repeats={report['config']['repeats']} · "
        f"quick={report['config']['quick']}"
    )
    lines.append("")
    lines.append(f"**{summary['score']}** (skipped jujur: {summary['kernels_skipped']} — kernel belum tersedia)")
    lines.append("")
    lines.append("| Kernel | Suite | Status | Detail |")
    lines.append("|---|---|---|---|")
    for kernel in report["kernels"]:
        status = {"pass": "PASS", "fail": "**FAIL**", "skipped": "SKIP"}[kernel["status"]]
        detail = kernel["reason"] or kernel["matcher"]
        lines.append(f"| {kernel['kernel']} | {', '.join(kernel['suites'])} | {status} | {detail} |")
    lines.append("")
    for kernel in report["kernels"]:
        if kernel["status"] == "skipped" or not kernel["datasets"]:
            continue
        lines.append(f"## {kernel['kernel']}")
        lines.append("")
        for note in kernel["notes"]:
            lines.append(f"- {note}")
        if kernel["notes"]:
            lines.append("")
        lines.append(
            "| Dataset | n | m | Correctness | Deterministic | Max abs err | "
            "Ours (public) ms | Ours (kernel) ms | Reference ms | Ratio public | Ratio kernel |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for entry in kernel["datasets"]:
            err = entry["detail"].get("max_abs_err")
            err_text = f"{err:.3g}" if isinstance(err, float) else "—"
            lines.append(
                f"| {entry['dataset']} | {entry['n']} | {entry['m']} | {entry['correctness']} | "
                f"{'yes' if entry['deterministic'] else 'NO'} | {err_text} | "
                f"{_fmt_ms(entry['ours_public_ms'])} | {_fmt_ms(entry['ours_kernel_ms'])} | "
                f"{_fmt_ms(entry['reference_ms'])} | {_fmt_ratio(entry['ratio_public'])} | "
                f"{_fmt_ratio(entry['ratio_kernel'])} |"
            )
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Institutional graph benchmark (LDBC Graphalytics / GAPBS aligned)")
    parser.add_argument("--json", dest="json_path", default="bench/institutional/results/institutional-latest.json")
    parser.add_argument("--md", dest="md_path", default="bench/institutional/results/institutional-latest.md")
    parser.add_argument("--repeats", type=int, default=3, help="timing repetitions per measurement (median)")
    parser.add_argument("--quick", action="store_true", help="small datasets only (n<=64)")
    parser.add_argument("--max-n", type=int, default=512, help="largest dataset size to include")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any available kernel FAILS correctness")
    args = parser.parse_args(argv)

    report = run_benchmark(repeats=args.repeats, quick=args.quick, max_n=args.max_n)

    json_path = Path(args.json_path)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path = Path(args.md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(render_markdown(report), encoding="utf-8")

    summary = report["summary"]
    print(f"[institutional-bench] reference: {report['reference']}")
    for kernel in report["kernels"]:
        marker = {"pass": "PASS", "fail": "FAIL", "skipped": "SKIP"}[kernel["status"]]
        suffix = f" — {kernel['reason']}" if kernel["status"] == "skipped" else ""
        print(f"[institutional-bench] {marker:4s} {kernel['kernel']}{suffix}")
    print(f"[institutional-bench] SCORE: {summary['score']}")
    print(f"[institutional-bench] report: {json_path} + {md_path}")
    if args.strict and summary["kernels_failed"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
