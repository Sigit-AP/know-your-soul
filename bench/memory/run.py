#!/usr/bin/env python3
"""Memory benchmark: Know Your Soul vs Graphify's published LOCOMO numbers.

SUB-83. Deterministic, no-AI measurement of the product's retrieval recall on
the LOCOMO conversational-memory dataset, compared against the numbers
Graphify publishes in BENCHMARKS.md (LOCOMO n=300 recall@10 = 0.497, mem0
0.048, supermemory 0.149, BM25 0.362).

Methodology (Graphify's harness `memory/runner.py` is referenced by their
BENCHMARKS.md but is NOT published in Graphify-Labs/graphify@v8, verified
2026-10-27, so the criteria below are an explicit reconstruction):

* dataset   : locomo10.json from snap-research/locomo (pinned sha256 below).
* unit      : one retrieval candidate = one dialog turn (LOCOMO ``dia_id``),
  matching the granularity of LOCOMO's per-question ``evidence`` lists.
* query     : the QA ``question`` text, verbatim.
* hit@k     : at least one of the top-k retrieved turn ids is listed in the
  question's ``evidence`` (any-hit). all-hit is reported as a secondary metric.
* recall@k  : mean hit@k over the evaluated questions. k=10.
* sample    : Graphify ran ``--n 300`` without publishing the selection, so we
  report (a) a deterministic seeded sample of 300 (seed 83, drawn from all QA
  pairs with non-empty evidence) and (b) the full evaluable set (n=1982).

Systems measured, all deterministic, no LLM anywhere:

* ``kys_federated`` : the product as-is. Turns are written through
  ``KnowledgeService.log_event`` and published by ``GraphProjector.rebuild``.
  The product's per-artifact cap (ARTIFACT_MAX_BYTES = 64 KiB, enforced by the
  write-side publish-capacity guard) bounds one store to ~40 turn nodes, so
  the corpus is sharded 32 turns per store (a real, supported topology: N
  independent product stores). Per shard the published ``graph.json`` nodes
  are ranked with the product's own relevance engine
  (``LexicalIndex.from_graph_nodes(...).rank(query_terms(question))`` — the
  exact index ``KnowledgeService.query`` uses). Shard rankings are merged by
  the product's own ordering (-score, -field_rank, node_id). Cross-shard IDF
  is per-shard (federation artifact, documented in the report).
* ``kys_global_diagnostic`` : NOT a product-supported topology. The same
  published node dicts are merged into one ``LexicalIndex`` to isolate index
  quality from the 64 KiB artifact cap. Diagnostic only.
* ``bm25_anchor`` : a stdlib Okapi BM25 (k1=1.5, b=0.75) over the same turn
  texts with the same hit criteria, as a comparability anchor against
  Graphify's published BM25 row (0.362).

``keyfact`` subcommand: ERPNext cross-tool equivalent-construct measurement on
our own frozen benchmark corpus (Graphify's crosstool harness, its n=6 graded
questions, and its Claude-agent loop are unpublished and LLM-bound, so the
original is not replicable). Measures rubric key-fact coverage of the
product's query responses over bench/cases.json. Equivalent, NOT identical.

QA accuracy (LOCOMO and LongMemEval) requires an LLM reader/judge and is out
of scope under the product's no-AI runtime boundary: reported as N/A, never
fabricated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent.parent
RESULTS_DIR = BENCH_DIR / "results"

DATASET_PATH = Path("/tmp/locomo/locomo10.json")
DATASET_URL = "https://raw.githubusercontent.com/snap-research/locomo/main/data/locomo10.json"
DATASET_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
DATASET_FETCHED = "2026-10-27"

WORK_ROOT = Path("/tmp/kys-locomo")
SHARD_SIZE = 32  # turns per product store; the write-side publish guard trips ~42
TOP_K = 10
SAMPLE_N = 300
SAMPLE_SEED = 83
SANITY_QUESTIONS = 12  # service.query cross-check of index ordering

GRAPHIFY_LOCOMO = {  # Graphify BENCHMARKS.md (2026-07-05), LOCOMO n=300
    "graphify_graph_expand": 0.497,
    "hybrid_rrf": 0.493,
    "graphify_surreal": 0.485,
    "dense_rag": 0.439,
    "bm25": 0.362,
    "supermemory": 0.149,
    "mem0": 0.048,
}


def utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------- dataset


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def download_dataset() -> int:
    import urllib.request

    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DATASET_PATH.exists():
        digest = sha256_file(DATASET_PATH)
        if digest == DATASET_SHA256:
            print(f"dataset present and pinned: {DATASET_PATH} sha256={digest}")
            return 0
        print(f"dataset present but sha256 mismatch: {digest} != {DATASET_SHA256}", file=sys.stderr)
        return 1
    print(f"fetching {DATASET_URL} -> {DATASET_PATH}")
    with urllib.request.urlopen(DATASET_URL, timeout=120) as resp:  # noqa: S310 - pinned public dataset
        DATASET_PATH.write_bytes(resp.read())
    digest = sha256_file(DATASET_PATH)
    if digest != DATASET_SHA256:
        print(f"FATAL: downloaded sha256 {digest} != pinned {DATASET_SHA256}", file=sys.stderr)
        return 1
    print(f"pinned ok: sha256={digest}")
    return 0


def load_dataset() -> list[dict[str, Any]]:
    if not DATASET_PATH.exists():
        raise SystemExit(f"dataset missing: {DATASET_PATH} (run: download)")
    digest = sha256_file(DATASET_PATH)
    if digest != DATASET_SHA256:
        raise SystemExit(f"dataset sha256 mismatch: {digest} != pinned {DATASET_SHA256}")
    return json.loads(DATASET_PATH.read_text(encoding="utf-8"))


def canonical_node_id(sample_id: str, dia_id: str) -> str:
    """Pre-canonicalized id that the projection's make_id passes through unchanged.

    The projection (graphify make_id recipe) casefolds and rewrites non-word
    characters, and rewrites non-canonical ids as ``make_id(source, id)`` —
    which desynchronizes edge endpoints and fails the build. Ids that are
    already canonical ([a-z0-9_]) survive byte-identical, so the bench keeps
    an exact id -> (sample_id, dia_id) mapping.
    """
    return "t_" + sample_id.replace("-", "") + "_" + dia_id.replace(":", "_").lower()


def parse_node_id(node_id: str) -> tuple[str, str]:
    """Inverse of canonical_node_id: ``t_conv26_d10_1`` -> (``conv-26``, ``D10:1``)."""
    body = node_id.removeprefix("t_")
    sample_slug, session, turn = body.split("_")
    sample_id = f"{sample_slug[:4]}-{sample_slug[4:]}"
    return sample_id, f"{session.upper()}:{turn}"


def iter_turns(data: list[dict[str, Any]]):
    """Yield (node_id, sample_id, dia_id, label) for every dialog turn."""
    for conv in data:
        sample_id = conv["sample_id"]
        conv = conv["conversation"]
        for key in sorted(k for k in conv if k.startswith("session_") and not k.endswith("_date_time")):
            for turn in conv[key]:
                dia_id = turn["dia_id"]
                yield (
                    canonical_node_id(sample_id, dia_id),
                    sample_id,
                    dia_id,
                    f"{turn['speaker']}: {turn['text']}",
                )


def iter_qa(data: list[dict[str, Any]]):
    """Yield (qid, sample_id, question, evidence, category)."""
    for conv in data:
        sample_id = conv["sample_id"]
        for idx, qa in enumerate(conv["qa"]):
            yield (f"{sample_id}#q{idx}", sample_id, qa["question"], list(qa.get("evidence") or []), qa.get("category"))


# ---------------------------------------------------------------------- build


def build_shards(data: list[dict[str, Any]], *, force: bool = False) -> dict[str, Any]:
    """Write all turns through the product path into sharded stores."""
    from know_your_soul.domain import Node, NodeKind, PrimaryDomain
    from know_your_soul.projection import GraphProjector
    from know_your_soul.service import KnowledgeService
    from know_your_soul.store import EventStore

    manifest_path = WORK_ROOT / "build_manifest.json"
    if manifest_path.exists() and not force:
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("dataset_sha256") == DATASET_SHA256 and manifest.get("shard_size") == SHARD_SIZE:
            print(f"build cache hit: {manifest['shard_count']} shards at {WORK_ROOT}")
            return manifest

    turns = list(iter_turns(data))
    shards = [turns[i : i + SHARD_SIZE] for i in range(0, len(turns), SHARD_SIZE)]
    if WORK_ROOT.exists() and force:
        import shutil

        shutil.rmtree(WORK_ROOT)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    shard_records = []
    for shard_idx, shard_turns in enumerate(shards):
        shard_dir = WORK_ROOT / f"shard-{shard_idx:04d}"
        shard_dir.mkdir(parents=True, exist_ok=True)
        store = EventStore(shard_dir / "bench.sqlite3")
        projector = GraphProjector(store, output_root=shard_dir / "graph")
        service = KnowledgeService(store, projector)
        root = Node(
            node_id="P1",
            kind=NodeKind.PRIMARY,
            domain=PrimaryDomain.P1,
            summary="Pembelajaran",
            provenance=("locomo://roots",),
            source_version_id="locomo10-v1",
        )
        service.log_event(root, actor="bench-memory", idempotency_key=f"locomo:root:{shard_idx}")
        for node_id, sample_id, _dia_id, label in shard_turns:
            node = Node(
                node_id=node_id,
                parent_id="P1",
                kind=NodeKind.LEAF,
                domain=PrimaryDomain.P1,
                summary=label,
                provenance=(f"locomo://{sample_id}",),
                context_tags=("locomo", sample_id),
                source_version_id="locomo10-v1",
            )
            service.log_event(node, actor="bench-memory", idempotency_key=f"locomo:{shard_idx}:{node_id}")
        projector.rebuild()
        graph_json = sorted((shard_dir / "graph" / "generations").glob("*/graph.json"))[-1]
        graph = json.loads(graph_json.read_text())
        published_ids = {str(n["id"]) for n in graph["nodes"]}
        expected_ids = {t[0] for t in shard_turns} | {"P1"}
        extra = published_ids - expected_ids
        if extra:
            raise SystemExit(
                f"shard {shard_idx}: projection re-identified nodes (extra={sorted(extra)[:3]}); "
                "the bench requires byte-identical id pass-through"
            )
        # Missing-but-expected ids are the product's fail-closed publish
        # quarantine (secret-shaped content is withheld from the read model).
        # They are genuinely unretrievable, so they score as misses.
        quarantined = sorted(expected_ids - published_ids)
        if quarantined:
            print(f"shard {shard_idx}: {len(quarantined)} node(s) quarantined at publish: {quarantined}")
        shard_records.append(
            {
                "shard": shard_idx,
                "dir": str(shard_dir),
                "turns": len(shard_turns),
                "graph_nodes": len(graph["nodes"]),
                "graph_bytes": graph_json.stat().st_size,
                "quarantined": quarantined,
            }
        )
        if (shard_idx + 1) % 20 == 0 or shard_idx + 1 == len(shards):
            print(f"  built {shard_idx + 1}/{len(shards)} shards ({time.monotonic() - started:.0f}s)")

    manifest = {
        "dataset_sha256": DATASET_SHA256,
        "shard_size": SHARD_SIZE,
        "shard_count": len(shards),
        "turn_count": len(turns),
        "build_seconds": round(time.monotonic() - started, 2),
        "shards": shard_records,
        "max_graph_bytes": max(r["graph_bytes"] for r in shard_records),
        "quarantined_total": sum(len(r["quarantined"]) for r in shard_records),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"built {len(shards)} shards / {len(turns)} turns in {manifest['build_seconds']}s")
    return manifest


# ------------------------------------------------------------------ retrieval


def load_shard_indexes() -> list[tuple[Any, dict[str, dict[str, Any]]]]:
    """One product LexicalIndex per shard, built from the published graph.json."""
    from know_your_soul.retrieval import LexicalIndex

    indexes = []
    for shard_dir in sorted(WORK_ROOT.glob("shard-*")):
        graph_json = sorted((shard_dir / "graph" / "generations").glob("*/graph.json"))[-1]
        graph = json.loads(graph_json.read_text())
        nodes = {str(n["id"]): n for n in graph["nodes"]}
        indexes.append((LexicalIndex.from_graph_nodes(nodes.values()), nodes))
    return indexes


def kys_federated_topk(indexes, question: str, k: int) -> list[str]:
    """Merge per-shard product rankings by the product's own ordering."""
    from know_your_soul.retrieval import query_terms

    terms = query_terms(question)
    merged = []
    for index, _nodes in indexes:
        # cheap skip: no shard hit possible when every term is absent
        if not any(index.document_frequency(t) for t in terms):
            continue
        merged.extend(index.rank(terms))
    merged.sort(key=lambda h: (-h.score, -h.field_rank, h.node_id))
    return [h.node_id for h in merged[:k]]


class Bm25Anchor:
    """Stdlib Okapi BM25 over turn labels; comparability anchor, not product code."""

    def __init__(self, docs: dict[str, str], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.ids = sorted(docs)
        self.tf: list[Counter[str]] = []
        df: Counter[str] = Counter()
        lengths = []
        self.postings: dict[str, list[int]] = defaultdict(list)
        for pos, doc_id in enumerate(self.ids):
            terms = Counter(re.findall(r"[a-z0-9]+", docs[doc_id].lower()))
            self.tf.append(terms)
            lengths.append(sum(terms.values()))
            for term in terms:
                df[term] += 1
                self.postings[term].append(pos)
        self.df = df
        self.dl = lengths
        self.avgdl = sum(lengths) / max(1, len(lengths))
        self.n = len(self.ids)

    def topk(self, query: str, k: int) -> list[str]:
        terms = set(re.findall(r"[a-z0-9]+", query.lower()))
        positions = {p for term in terms for p in self.postings.get(term, ())}
        scored = []
        for pos in positions:
            tf = self.tf[pos]
            dl = self.dl[pos]
            score = 0.0
            for term in terms:
                f = tf.get(term, 0)
                if not f:
                    continue
                idf = math.log((self.n - self.df[term] + 0.5) / (self.df[term] + 0.5) + 1.0)
                score += idf * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
            if score > 0:
                scored.append((score, self.ids[pos]))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [doc_id for _score, doc_id in scored[:k]]


class GlobalIndexView:
    """Postings-accelerated wrapper over the product LexicalIndex (diagnostic).

    Scores only nodes that contain at least one query term; nodes with no
    query term score exactly 0 in the product's ``score`` and never enter the
    positive-hit list, so the result is identical to ``index.rank(terms)[:k]``.
    Uses the product's own ``score``/``field_rank``/ordering unchanged.
    """

    def __init__(self, index) -> None:
        self._index = index
        self._postings: dict[str, list[tuple[str, Any]]] = defaultdict(list)
        for node_id, fields in index._entries:  # noqa: SLF001 - read-only bench introspection
            present: set[str] = set()
            for terms in fields.values():
                present |= set(terms)
            for term in present:
                self._postings[term].append((node_id, fields))

    def topk(self, terms: list[str], k: int) -> list[str]:
        from know_your_soul.retrieval import normalize_term

        norm = list(dict.fromkeys(normalize_term(t) for t in terms))
        candidates: dict[str, Any] = {}
        for term in norm:
            for node_id, fields in self._postings.get(term, ()):
                candidates[node_id] = fields
        scored = []
        for node_id, fields in candidates.items():
            score = self._index.score(fields, norm)
            if score > 0:
                scored.append((score, self._index.field_rank(fields, norm), node_id))
        scored.sort(key=lambda item: (-item[0], -item[1], item[2]))
        return [node_id for _s, _f, node_id in scored[:k]]


def hit_any(topk: list[str], sample_id: str, evidence: list[str]) -> bool:
    want = set(evidence)
    for node_id in topk:
        if not node_id.startswith("t_"):
            continue
        sid, dia = parse_node_id(node_id)
        if sid == sample_id and dia in want:
            return True
    return False


def hit_all(topk: list[str], sample_id: str, evidence: list[str]) -> bool:
    got = {dia for node_id in topk if node_id.startswith("t_") for sid, dia in [parse_node_id(node_id)] if sid == sample_id}
    return bool(evidence) and set(evidence) <= got


# -------------------------------------------------------------------- sanity


def sanity_query_path(sample_qas, n: int) -> dict[str, Any]:
    """Cross-check: KnowledgeService.query top-k ordering == LexicalIndex.rank.

    Runs the full product read path (routing -> traversal -> token budget) on
    the shard holding each sampled question's evidence and compares the first
    TOP_K result node ids with the bench-side index ranking.
    """
    from know_your_soul.contracts import QueryRequest
    from know_your_soul.projection import GraphProjector
    from know_your_soul.retrieval import LexicalIndex, query_terms
    from know_your_soul.service import KnowledgeService
    from know_your_soul.store import EventStore

    # map sample_id -> shard dir (first shard whose graph contains an evidence node)
    shard_dirs = sorted(WORK_ROOT.glob("shard-*"))
    checks = []
    rng = random.Random(SAMPLE_SEED + 1)
    chosen = rng.sample(sample_qas, min(n, len(sample_qas)))
    for qid, sample_id, question, evidence, _cat in chosen:
        target_node = canonical_node_id(sample_id, evidence[0])
        owner = None
        for shard_dir in shard_dirs:
            graph_json = sorted((shard_dir / "graph" / "generations").glob("*/graph.json"))[-1]
            graph = json.loads(graph_json.read_text())
            if any(str(nd["id"]) == target_node for nd in graph["nodes"]):
                owner = (shard_dir, graph)
                break
        if owner is None:
            checks.append({"qid": qid, "status": "evidence_node_missing"})
            continue
        shard_dir, graph = owner
        store = EventStore(shard_dir / "bench.sqlite3")
        service = KnowledgeService(store, GraphProjector(store, output_root=shard_dir / "graph"))
        response = service.query(
            QueryRequest(purpose=question[:512], domain="P1", token_budget=8192, depth=1, max_nodes=1024),
            actor="bench-memory",
        )
        via_query = [r.node_id for r in response.results[:TOP_K]]
        index = LexicalIndex.from_graph_nodes(graph["nodes"])
        via_index = [h.node_id for h in index.rank(query_terms(question[:512]))[:TOP_K]]
        checks.append(
            {
                "qid": qid,
                "routing": response.routing,
                "query_top10": via_query,
                "index_top10": via_index,
                "match": via_query == via_index,
            }
        )
    matched = sum(1 for c in checks if c.get("match"))
    return {"checks": checks, "matched": matched, "total": len(checks)}


# ----------------------------------------------------------------------- run


def run_locomo(*, skip_sanity: bool = False) -> int:
    data = load_dataset()
    manifest = build_shards(data)
    indexes = load_shard_indexes()
    print(f"loaded {len(indexes)} shard indexes")

    # global diagnostic view: same published node dicts, one merged index
    from know_your_soul.retrieval import LexicalIndex, query_terms

    all_nodes: dict[str, dict[str, Any]] = {}
    for _index, nodes in indexes:
        all_nodes.update(nodes)
    global_index = GlobalIndexView(LexicalIndex.from_graph_nodes(all_nodes.values()))
    bm25 = Bm25Anchor({node_id: label for node_id, _sid, _dia, label in iter_turns(data)})

    qas = [(qid, sid, q, ev, cat) for qid, sid, q, ev, cat in iter_qa(data) if ev]
    # Equivalence self-check: the postings accelerator must reproduce the
    # product's rank() output exactly before it is trusted for 1982 questions.
    raw_global = LexicalIndex.from_graph_nodes(all_nodes.values())
    for qid, _sid, question, _ev, _cat in qas[:25]:
        terms = query_terms(question[:512])
        expected = [h.node_id for h in raw_global.rank(terms)[:TOP_K]]
        got = global_index.topk(terms, TOP_K)
        if expected != got:
            raise SystemExit(f"accelerator divergence on {qid}: {expected} != {got}")
    print("accelerator equivalence check: 25/25 exact match vs LexicalIndex.rank")
    rng = random.Random(SAMPLE_SEED)
    sample = rng.sample(qas, SAMPLE_N)
    sample_set = {q[0] for q in sample}

    systems = ("kys_federated", "kys_global_diagnostic", "bm25_anchor")
    stats: dict[str, dict[str, Any]] = {
        name: {"hits": 0, "all_hits": 0, "n": 0, "sample_hits": 0, "per_category": defaultdict(lambda: [0, 0])}
        for name in systems
    }
    started = time.monotonic()
    for i, (qid, sample_id, question, evidence, category) in enumerate(qas):
        terms = query_terms(question[:512])
        topks = {
            "kys_federated": kys_federated_topk(indexes, question[:512], TOP_K),
            "kys_global_diagnostic": global_index.topk(terms, TOP_K),
            "bm25_anchor": bm25.topk(question, TOP_K),
        }
        for name, topk in topks.items():
            entry = stats[name]
            entry["n"] += 1
            any_hit = hit_any(topk, sample_id, evidence)
            entry["hits"] += int(any_hit)
            entry["all_hits"] += int(hit_all(topk, sample_id, evidence))
            entry["per_category"][str(category)][0] += int(any_hit)
            entry["per_category"][str(category)][1] += 1
            if qid in sample_set:
                entry["sample_hits"] += int(any_hit)
        if (i + 1) % 250 == 0 or i + 1 == len(qas):
            print(f"  evaluated {i + 1}/{len(qas)} questions ({time.monotonic() - started:.0f}s)")

    sanity = None if skip_sanity else sanity_query_path(sample, SANITY_QUESTIONS)

    results: dict[str, Any] = {}
    for name in systems:
        entry = stats[name]
        results[name] = {
            "recall_at_10_full": round(entry["hits"] / entry["n"], 4),
            "n_full": entry["n"],
            "recall_at_10_sample300": round(entry["sample_hits"] / SAMPLE_N, 4),
            "all_evidence_recall_at_10_full": round(entry["all_hits"] / entry["n"], 4),
            "per_category_full": {
                cat: {"recall_at_10": round(h / n, 4), "n": n} for cat, (h, n) in sorted(entry["per_category"].items())
            },
        }

    ledger = {
        "ledger": "kys-memory-benchmark/1.0.0",
        "generated_at": utc_now_iso(),
        "dataset": {
            "name": "LOCOMO locomo10.json",
            "url": DATASET_URL,
            "sha256": DATASET_SHA256,
            "fetched": DATASET_FETCHED,
            "conversations": len(data),
            "turns": manifest["turn_count"],
            "qa_total": sum(len(c["qa"]) for c in data),
            "qa_evaluable_non_empty_evidence": len(qas),
        },
        "methodology": {
            "unit": "one candidate = one dialog turn (dia_id), the granularity of LOCOMO evidence",
            "hit_at_k": "any top-k node id maps to a dia_id in the question's evidence list",
            "k": TOP_K,
            "sample": f"deterministic random.Random({SAMPLE_SEED}).sample of {SAMPLE_N} from the {len(qas)} evaluable QA pairs",
            "graphify_harness": "memory/runner.py referenced by Graphify BENCHMARKS.md but not published in Graphify-Labs/graphify@v8 (verified 2026-10-27); criteria reconstructed as documented here",
        },
        "product_topology": {
            "shard_size_turns": SHARD_SIZE,
            "shard_count": manifest["shard_count"],
            "max_graph_json_bytes": manifest["max_graph_bytes"],
            "artifact_cap_bytes": 65536,
            "note": "the product's 64 KiB per-artifact cap bounds one store to ~40 turns; sharding into independent product stores is the supported scale-out topology; cross-shard IDF is per-shard (federation artifact)",
        },
        "results": results,
        "graphify_published_locomo_recall_at_10": GRAPHIFY_LOCOMO,
        "sanity_query_vs_index": sanity,
        "build_seconds": manifest["build_seconds"],
        "eval_seconds": round(time.monotonic() - started, 2),
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"locomo-{utc_now_iso().replace(':', '')}.json"
    out.write_text(json.dumps(ledger, indent=2))
    (RESULTS_DIR / "locomo-latest.json").write_text(json.dumps(ledger, indent=2))
    print(f"ledger: {out}")
    for name in systems:
        r = results[name]
        print(f"{name:24s} recall@10 full={r['recall_at_10_full']:.4f} (n={r['n_full']})  sample300={r['recall_at_10_sample300']:.4f}")
    if sanity:
        print(f"sanity service.query vs index.rank: {sanity['matched']}/{sanity['total']} exact top-10 match")
    return 0


# -------------------------------------------------------------------- keyfact


def run_keyfact() -> int:
    """ERPNext cross-tool equivalent-construct: key-fact coverage on our corpus."""
    sys.path.insert(0, str(REPO_ROOT / "bench"))
    import importlib.util

    spec = importlib.util.spec_from_file_location("soul_bench", REPO_ROOT / "bench" / "run.py")
    if spec is None or spec.loader is None:
        print("cannot load bench/run.py", file=sys.stderr)
        return 1
    soul_bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(soul_bench)

    cases = json.loads((REPO_ROOT / "bench" / "cases.json").read_text())["cases"]
    with tempfile.TemporaryDirectory() as tmp:
        service = soul_bench.build_service(Path(tmp))
        rows = []
        for case in cases:
            response, latency_ms = soul_bench.query_once(service, case)
            payload = json.dumps(
                [r.model_dump(mode="json") for r in response.results], ensure_ascii=False, sort_keys=True
            ).lower()
            terms = [t for t in case["rubric_terms"]]
            covered = sum(1 for t in terms if t.lower() in payload)
            rows.append(
                {
                    "case": case["id"],
                    "covered": covered,
                    "total": len(terms),
                    "coverage": round(covered / len(terms), 4) if terms else None,
                    "results": len(response.results),
                    "routing": response.routing,
                    "latency_ms": round(latency_ms, 2),
                }
            )
    mean_cov = sum(r["coverage"] for r in rows if r["coverage"] is not None) / len(rows)
    ledger = {
        "ledger": "kys-keyfact-coverage/1.0.0",
        "generated_at": utc_now_iso(),
        "construct": "key-fact (rubric term) coverage of the product's query responses over the frozen SOUL benchmark corpus (bench/cases.json, 24 cases)",
        "equivalence_note": (
            "Graphify's ERPNext cross-tool row (82.0% vs grep/read 70.8%) is NOT replicable: the crosstool "
            "harness, the n=6 graded questions and the key-fact gold sets are unpublished, and the harness "
            "drives a Claude Opus coding agent (LLM-bound, outside our no-AI boundary). This ledger measures "
            "the same construct (does retrieval surface the known key facts) on our own frozen corpus. "
            "Equivalent construct, NOT an identical benchmark."
        ),
        "mean_coverage": round(mean_cov, 4),
        "cases": rows,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"keyfact-{utc_now_iso().replace(':', '')}.json"
    out.write_text(json.dumps(ledger, indent=2))
    (RESULTS_DIR / "keyfact-latest.json").write_text(json.dumps(ledger, indent=2))
    print(f"ledger: {out}")
    print(f"mean key-fact coverage over {len(rows)} frozen cases: {mean_cov:.4f}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("download")
    p_build = sub.add_parser("build")
    p_build.add_argument("--force", action="store_true")
    p_run = sub.add_parser("run")
    p_run.add_argument("--skip-sanity", action="store_true")
    sub.add_parser("keyfact")
    args = parser.parse_args()

    if args.cmd == "download":
        return download_dataset()
    if args.cmd == "build":
        build_shards(load_dataset(), force=args.force)
        return 0
    if args.cmd == "run":
        return run_locomo(skip_sanity=args.skip_sanity)
    if args.cmd == "keyfact":
        return run_keyfact()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
