"""Wave-3 ops-boundary and bench-rubric regression probes (SUB-34).

Report: ``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave3.md``.

* W3-7: ops-path containment was path-only — a hardlink inside the ops root
  to an outside inode passed containment, and the resolve-then-use window let
  a parent-directory swap redirect a pre-resolved path. Fixed by refusing
  multi-linked regular files and by re-resolving every ops path after the
  operation (post-op TOCTOU detection).
* W3-9: ``body_timeout_seconds=0`` wedged every request carrying a body.
  Fixed by failing closed at construction: non-positive timeouts are
  rejected before any socket is bound.
* W3-8(a): the bench rubric scored provenance URIs, so a rubric term could
  be satisfied by a corpus *filename* alone. Fixed by excluding the
  ``provenance`` field from rubric scoring text on both the system side and
  the compliant-ceiling side (it still counts toward token estimation).

Every test pins one attack/misconfiguration shape: the old hole must now
fail closed, and the legitimate flow must keep working (non-vacuous).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest

from soul_knowledge import http_api
from soul_knowledge.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
)
from soul_knowledge.http_api import BoundaryMiddleware, build_stack, create_app, serve
from soul_knowledge.store import EventStore

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_bench() -> Any:
    """Load bench/run.py (a script) as a module without a package install."""
    spec = importlib.util.spec_from_file_location("soul_bench_run_wave3", REPO_ROOT / "bench" / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load_bench()

# --------------------------------------------------------------------- setup


def _seed(store: EventStore) -> None:
    def upsert(node: Node) -> None:
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id=node.node_id,
                domain=node.domain,
                node=node,
                source_version_id=node.source_version_id,
            ),
            idempotency_key=f"test:wave3:upsert:{node.node_id}",
        )

    upsert(
        Node(
            node_id="KROOT",
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            provenance=("soul://roots",),
            source_version_id="sv-test",
        )
    )
    for domain in PrimaryDomain:
        upsert(
            Node(
                node_id=domain.value,
                parent_id="KROOT",
                kind=NodeKind.PRIMARY,
                domain=domain,
                summary=domain.title,
                provenance=("soul://roots",),
                source_version_id="sv-test",
            )
        )
    upsert(
        Node(
            node_id="n-gates",
            parent_id="P1",
            kind=NodeKind.CHILD,
            domain=PrimaryDomain.P1,
            summary="Run the gates before commit",
            context_tags=("gates", "discipline"),
            provenance=("soul://corpus/lesson.md",),
            source_version_id="sv-test",
        )
    )
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id="P1",
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.PARENT_OF, source_node_id="P1", target_node_id="n-gates"),
        ),
        idempotency_key="test:wave3:rel:P1:n-gates",
    )


@pytest.fixture()
def ops_stack(tmp_path: Path):
    """A stack whose ops root is a *subdirectory* of tmp_path, so 'outside the
    ops root but inside the filesystem' is available for hardlink probes."""
    ops_root = tmp_path / "ops"
    ops_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    built = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out", ops_root=ops_root)
    _seed(built.store)
    built.projector.rebuild()
    yield built
    built.close()


def _post(app: Any, path: str, payload: Any) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload).encode("utf-8")
    headers = [
        (b"host", b"localhost"),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 5000),
        "server": ("127.0.0.1", 80),
    }
    messages: list[dict[str, Any]] = []
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    import asyncio

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    raw = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return int(start["status"]), json.loads(raw.decode("utf-8"))


# ------------------------------------------- W3-9: body_timeout validation


class TestBodyTimeoutValidation:
    """W3-9: a non-positive body deadline wedges every request with a body;
    the configuration must be refused at startup (fail-closed), not served."""

    def test_create_app_rejects_zero_body_timeout(self, ops_stack: Any) -> None:
        with pytest.raises(ValueError, match="body_timeout_seconds"):
            create_app(ops_stack, body_timeout_seconds=0.0)

    def test_create_app_rejects_negative_body_timeout(self, ops_stack: Any) -> None:
        with pytest.raises(ValueError, match="body_timeout_seconds"):
            create_app(ops_stack, body_timeout_seconds=-1.5)

    def test_middleware_rejects_nonpositive_body_timeout_directly(self) -> None:
        async def _dummy(scope: Any, receive: Any, send: Any) -> None:  # pragma: no cover
            raise AssertionError("never called")

        with pytest.raises(ValueError, match="body_timeout_seconds"):
            BoundaryMiddleware(
                _dummy,
                api_key=None,
                host_allowlist=("localhost",),
                max_body_bytes=1024,
                body_timeout_seconds=0.0,
            )

    def test_serve_rejects_zero_body_timeout_before_binding(self, ops_stack: Any, tmp_path: Path) -> None:
        """serve() must fail before any socket exists (fail-closed startup)."""
        socket_path = tmp_path / "should-never-exist.sock"
        with pytest.raises(ValueError, match="body_timeout_seconds"):
            serve(ops_stack, socket_path=socket_path, body_timeout_seconds=0.0)
        assert not socket_path.exists(), "a socket was bound despite the invalid configuration"

    def test_positive_body_timeout_still_serves(self, ops_stack: Any) -> None:
        """Non-vacuous positive control: a valid timeout serves requests."""
        app = create_app(ops_stack, body_timeout_seconds=5.0)
        status, out = _post(app, "/v1/knowledge/query", {"purpose": "gates", "domain": "P1", "token_budget": 512})
        assert status == 200
        assert out["audit_event_id"]


# ------------------------------------- W3-7: hardlink + TOCTOU containment


class TestOpsPathHardlinkRefused:
    """W3-7: a hardlink inside the ops root to an outside inode shares the
    inode — path-only containment is blind to it. Multi-linked regular files
    are now refused before any read or write."""

    def test_restore_from_hardlink_refused(self, ops_stack: Any, tmp_path: Path) -> None:
        outside_file = tmp_path / "outside" / "secret.sqlite3"
        outside_file.write_bytes(b"not-a-real-backup")
        link_inside = Path(ops_stack.ops_root) / "linked.sqlite3"
        os.link(outside_file, link_inside)
        assert link_inside.stat().st_nlink == 2, "probe setup: the hardlink must exist"

        app = create_app(ops_stack)
        status, out = _post(
            app,
            "/v1/ops/restore",
            {"backup_path": "linked.sqlite3", "dest_path": "restored.sqlite3"},
        )
        assert status == 400
        assert out["error"]["code"] == "PATH_ESCAPE"
        assert "linked.sqlite3" in out["error"]["message"]
        assert not (Path(ops_stack.ops_root) / "restored.sqlite3").exists(), "the restore must not run"

    def test_backup_onto_hardlink_refused(self, ops_stack: Any, tmp_path: Path) -> None:
        outside_file = tmp_path / "outside" / "victim.sqlite3"
        outside_file.write_bytes(b"victim content")
        link_inside = Path(ops_stack.ops_root) / "dest.sqlite3"
        os.link(outside_file, link_inside)

        app = create_app(ops_stack)
        status, out = _post(app, "/v1/ops/backup", {"dest_path": "dest.sqlite3"})
        assert status == 400
        assert out["error"]["code"] == "PATH_ESCAPE"
        assert outside_file.read_bytes() == b"victim content", "the outside inode must be untouched"

    def test_normal_backup_and_restore_still_work(self, ops_stack: Any, tmp_path: Path) -> None:
        """Non-vacuous positive control: ordinary single-link ops paths pass."""
        app = create_app(ops_stack)
        status, out = _post(app, "/v1/ops/backup", {"dest_path": "good.sqlite3"})
        assert status == 200
        backup = Path(ops_stack.ops_root) / "good.sqlite3"
        assert backup.exists() and backup.stat().st_nlink == 1

        status, out = _post(
            app,
            "/v1/ops/restore",
            {"backup_path": "good.sqlite3", "dest_path": "restored.sqlite3"},
        )
        assert status == 200
        assert (Path(ops_stack.ops_root) / "restored.sqlite3").exists()


class TestOpsPathToctouRecheck:
    """W3-7: the resolve-then-use window is closed by a post-operation
    re-resolution — a parent-directory swap mid-operation is detected and
    surfaced as PATH_ESCAPE instead of being silently trusted."""

    def test_backup_parent_swap_detected(self, ops_stack: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ops_root = Path(ops_stack.ops_root)
        (ops_root / "sub").mkdir()
        outside = tmp_path / "outside"
        real_backup = http_api.backup_store

        def swapping_backup(store: Any, dest: Path) -> Any:
            receipt = real_backup(store, dest)
            # Attacker with ops-root write access swaps the parent directory
            # for a symlink to an outside tree *after* the path was resolved.
            victim = ops_root / "sub"
            for child in victim.iterdir():
                child.unlink()
            victim.rmdir()
            os.symlink(outside, victim)
            return receipt

        monkeypatch.setattr(http_api, "backup_store", swapping_backup)
        app = create_app(ops_stack)
        status, out = _post(app, "/v1/ops/backup", {"dest_path": "sub/backup.sqlite3"})
        assert status == 400
        assert out["error"]["code"] == "PATH_ESCAPE"

    def test_restore_parent_swap_detected(
        self, ops_stack: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ops_root = Path(ops_stack.ops_root)
        app = create_app(ops_stack)
        status, out = _post(app, "/v1/ops/backup", {"dest_path": "seed.sqlite3"})
        assert status == 200
        (ops_root / "sub").mkdir()
        outside = tmp_path / "outside"
        real_restore = http_api.restore_store

        def swapping_restore(backup: Path, dest: Path, *, overwrite: bool = False) -> Any:
            receipt = real_restore(backup, dest, overwrite=overwrite)
            victim = ops_root / "sub"
            for child in victim.iterdir():
                child.unlink()
            victim.rmdir()
            os.symlink(outside, victim)
            return receipt

        monkeypatch.setattr(http_api, "restore_store", swapping_restore)
        status, out = _post(
            app,
            "/v1/ops/restore",
            {"backup_path": "seed.sqlite3", "dest_path": "sub/restored.sqlite3"},
        )
        assert status == 400
        assert out["error"]["code"] == "PATH_ESCAPE"

    def test_undisturbed_backup_passes_the_recheck(self, ops_stack: Any) -> None:
        """Non-vacuous positive control: with no swap the re-check is silent."""
        ops_root = Path(ops_stack.ops_root)
        (ops_root / "sub").mkdir()
        app = create_app(ops_stack)
        status, out = _post(app, "/v1/ops/backup", {"dest_path": "sub/backup.sqlite3"})
        assert status == 200
        assert (ops_root / "sub" / "backup.sqlite3").exists()


# ----------------------------- W3-8(a): provenance excluded from the rubric


class TestRubricExcludesProvenance:
    """W3-8(a): provenance is routing metadata and contributes zero relevance
    (spec); a rubric term present only in a provenance URI (a corpus
    filename) must not count as delivered knowledge."""

    def test_provenance_only_term_scores_zero(self) -> None:
        payload = {
            "results": [{"summary": "unrelated content"}],
            "provenance": ["soul://corpus/09-premortem-steelman.md"],
        }
        scoring_text = bench.rubric_scoring_text(payload)
        assert "provenance" not in scoring_text
        assert bench.rubric_score(["steelman"], scoring_text) == 0.0
        # Non-vacuous control: the term IS visible when provenance is scored.
        full_text = json.dumps(payload, ensure_ascii=False)
        assert bench.rubric_score(["steelman"], full_text) == 1.0

    def test_provenance_still_counts_toward_token_estimation(self) -> None:
        """The exclusion is scoring-only: provenance stays on the wire and in
        the token budget on both sides of the comparison."""
        with_prov = {"results": [], "provenance": ["soul://corpus/09-premortem-steelman.md"]}
        without_prov = {"results": []}
        assert bench.estimate_tokens_of_payload(with_prov) > bench.estimate_tokens_of_payload(without_prov)

    def test_q16_steel_man_hit_is_content_delivered(self, tmp_path: Path) -> None:
        """q16's 'steelman' rubric hit must survive provenance exclusion: it
        is carried by the delivered result summaries, not the URI filename."""
        cases = {case["id"]: case for case in bench.load_cases()}
        case = cases["q16-premortem-steelman"]
        service = bench.build_service(tmp_path)
        response, _ = bench.query_once(service, case)
        payload = response.canonical_payload()
        scoring_text = bench.rubric_scoring_text(payload).casefold()
        assert "steelman" in scoring_text, "q16 must earn 'steelman' from delivered knowledge"
        assert "soul://corpus/09-premortem-steelman.md" not in scoring_text
