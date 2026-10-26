"""Regression probes for the serve-time source watcher (architecture review F-1).

Spec 13.5: the doc-change watcher drives the deterministic update pipeline.
Architecture review F-1 found the flow dead: ``source_watcher.py`` and
``update_pipeline.py`` had no production caller; the only live detection path
was the manual ``ops/relearn_scan``. The fix wires a ``SourceWatchLoop`` into
the API server's lifespan:

* ``create_app``/``serve`` accept ``watch_root`` and start a daemon watch loop
  with the server; the loop stops when the server stops.
* Detection is gated exactly like the manual path (final-audit F-4): a change
  batch routes through the service's audited ``offer_relearn`` — an offer plus
  ``RELEARN_PENDING``, never an auto-apply, never a baseline flip.
* The loop is observable (structured log lines), rate-limited (poll interval
  plus a cooldown after an emitted offer), and fail-isolated: watcher errors
  are typed receipts, logged and backed off — they never take the server down.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
import uvicorn

from soul_knowledge.domain import PrimaryDomain
from soul_knowledge.http_api import build_stack, create_app
from soul_knowledge.importer import SeedImporter
from soul_knowledge.source_registry import RELEARN_PENDING
from soul_knowledge.source_watcher import SourceWatcher
from soul_knowledge.update_pipeline import SourceWatchLoop, UpdatePipelineError, WatchPollReceipt

ALPHA = (
    b"# Alpha Lessons\n"
    b"\n"
    b"## First Lesson\n"
    b"\n"
    b"Always roll the database back before retrying.\n"
    b"\n"
    b"## Second Lesson\n"
    b"\n"
    b"See [[beta]] for the counterexample.\n"
)
BETA = b"# Beta\n\n## Counterexample\n\nA rollback that was itself rolled back.\n"


def _corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    return corpus


def _wait_until(predicate: Callable[[], bool], *, timeout: float = 15.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ------------------------------------------------------------ loop unit probes


def _loop(
    tmp_path: Path,
    on_changes: Callable[..., object],
    *,
    interval: float = 0.05,
    cooldown: float = 0.5,
) -> tuple[SourceWatchLoop, Path]:
    corpus = _corpus(tmp_path)
    watcher = SourceWatcher(corpus, state_path=tmp_path / "watch-state.json")
    watcher.poll()  # baseline snapshot
    loop = SourceWatchLoop(
        watcher,
        on_changes,
        interval_seconds=interval,
        cooldown_seconds=cooldown,
        max_error_backoff_seconds=1.0,
    )
    return loop, corpus


def test_poll_once_without_changes_skips_the_callback(tmp_path: Path) -> None:
    calls: list[object] = []
    loop, _ = _loop(tmp_path, lambda changes: calls.append(changes))
    receipt = loop.poll_once()
    assert isinstance(receipt, WatchPollReceipt)
    assert receipt.changes == ()
    assert receipt.error is None
    assert receipt.offer_emitted is False
    assert calls == []


def test_poll_once_routes_a_change_batch_through_the_gate_callback(tmp_path: Path) -> None:
    calls: list[list[object]] = []
    loop, corpus = _loop(tmp_path, lambda changes: calls.append(list(changes)) or object())
    (corpus / "alpha.md").write_bytes(ALPHA + b"\n## Third Lesson\n\nNew.\n")
    receipt = loop.poll_once()
    assert receipt.error is None
    assert receipt.offer_emitted is True  # truthy callback result == offer emitted
    assert receipt.changes == tuple(calls[0])
    assert [change.path for change in receipt.changes] == ["alpha.md"]


def test_poll_once_offer_sets_the_cooldown(tmp_path: Path) -> None:
    loop, corpus = _loop(tmp_path, lambda changes: object())
    assert loop.cooldown_remaining() == 0.0
    (corpus / "alpha.md").write_bytes(ALPHA + b"\nchanged\n")
    receipt = loop.poll_once()
    assert receipt.offer_emitted is True
    assert 0.0 < loop.cooldown_remaining() <= 0.5


def test_poll_once_callback_failure_is_a_typed_receipt_not_a_crash(tmp_path: Path) -> None:
    def boom(changes: list[object]) -> object:
        raise RuntimeError("callback exploded")

    loop, corpus = _loop(tmp_path, boom)
    (corpus / "alpha.md").write_bytes(ALPHA + b"\nchanged\n")
    receipt = loop.poll_once()
    assert receipt.error is not None and "RuntimeError" in receipt.error
    assert receipt.offer_emitted is False
    # The loop survives: after the callback stops raising, detection resumes.
    loop._on_changes = lambda changes: object()  # noqa: SLF001 - test swaps the callback
    receipt = loop.poll_once()
    # The watcher persisted its snapshot before the callback ran, so the batch
    # is consumed; a fresh change proves the loop is alive.
    assert receipt.error is None
    (corpus / "beta.md").write_bytes(BETA + b"\nchanged\n")
    receipt = loop.poll_once()
    assert receipt.offer_emitted is True


def test_poll_once_watcher_state_failure_is_typed_and_recovers(tmp_path: Path) -> None:
    loop, corpus = _loop(tmp_path, lambda changes: object())
    state_path = tmp_path / "watch-state.json"
    state_path.write_text("not json", encoding="utf-8")
    receipt = loop.poll_once()
    assert receipt.error is not None and "WatcherStateError" in receipt.error
    state_path.unlink()  # operator repairs: fresh snapshot on next poll
    receipt = loop.poll_once()
    assert receipt.error is None


def test_watch_loop_rejects_nonpositive_timing(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    watcher = SourceWatcher(corpus, state_path=tmp_path / "watch-state.json")
    with pytest.raises(UpdatePipelineError):
        SourceWatchLoop(watcher, lambda changes: None, interval_seconds=0)
    with pytest.raises(UpdatePipelineError):
        SourceWatchLoop(watcher, lambda changes: None, cooldown_seconds=-1)


def test_watch_loop_thread_lifecycle(tmp_path: Path) -> None:
    fired = threading.Event()
    loop, corpus = _loop(tmp_path, lambda changes: fired.set() or object())
    loop.start()
    try:
        assert loop.running
        with pytest.raises(UpdatePipelineError):  # a double start is a typed error
            loop.start()
        (corpus / "alpha.md").write_bytes(ALPHA + b"\nlive detection\n")
        assert fired.wait(timeout=15), "running loop never invoked the gate callback"
        assert any(thread.name == "soul-source-watch" for thread in threading.enumerate())
    finally:
        loop.stop()
    assert not loop.running
    assert not any(thread.name == "soul-source-watch" for thread in threading.enumerate())
    loop.stop()  # idempotent


def test_running_loop_cools_down_after_an_offer(tmp_path: Path) -> None:
    offers: list[float] = []
    loop, corpus = _loop(
        tmp_path, lambda changes: offers.append(time.monotonic()) or object(), interval=0.05, cooldown=0.6
    )
    loop.start()
    try:
        (corpus / "alpha.md").write_bytes(ALPHA + b"\nedit one\n")
        _wait_until(lambda: len(offers) >= 1, what="first offer")
        (corpus / "beta.md").write_bytes(BETA + b"\nedit during cooldown\n")
        time.sleep(0.3)  # inside the 0.6s cooldown: no second offer yet
        assert len(offers) == 1, "a second offer fired inside the cooldown window"
        _wait_until(lambda: len(offers) >= 2, what="batched offer after cooldown")
        assert offers[1] - offers[0] >= 0.55, f"cooldown not honored: {offers[1] - offers[0]:.3f}s"
    finally:
        loop.stop()


def test_watch_loop_logs_detection_and_errors(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    loop, corpus = _loop(tmp_path, lambda changes: object())
    logger = logging.getLogger("soul_knowledge.update_pipeline")
    with caplog.at_level(logging.INFO, logger=logger.name):
        (corpus / "alpha.md").write_bytes(ALPHA + b"\nlogged change\n")
        receipt = loop.poll_once()
    assert receipt.offer_emitted is True
    assert any("offer" in record.getMessage() and "RELEARN_PENDING" in record.getMessage() for record in caplog.records)


# ------------------------------------------------------- serve wiring (F-1 fix)


def _serve_app(
    tmp_path: Path,
    app_kwargs: dict[str, object],
) -> tuple[object, uvicorn.Server, threading.Thread, Path]:
    stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    corpus = _corpus(tmp_path)
    importer = SeedImporter(stack.store, state_dir=tmp_path / "import-state")
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    stack.projector.rebuild()
    app = create_app(stack, **app_kwargs)  # type: ignore[arg-type]
    socket_path = tmp_path / "api.sock"
    config = uvicorn.Config(app, uds=str(socket_path), log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not socket_path.exists():
        assert time.monotonic() < deadline, "server did not start"
        time.sleep(0.05)
    return stack, server, thread, corpus


def _health(socket_path: Path) -> int:
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(10)
        conn.connect(str(socket_path))
        conn.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        chunks: list[bytes] = []
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    return int(b"".join(chunks).split(b"\r\n", 1)[0].split()[1])


def _offer_count(stack: object) -> int:
    with stack.store.read() as conn:  # type: ignore[attr-defined]
        row = conn.execute("SELECT COUNT(*) AS n FROM relearn_offer").fetchone()
    return int(row["n"])


def _soul_corpus_baseline_recorded(stack: object) -> bool:
    """The watcher's first gated detection inserted the SOUL_CORPUS baseline.

    This is the deterministic startup signal: ``relearn_status`` cannot
    express it (its active version covers every kind, so the import's
    OPERATOR_IMPORT versions satisfy it before the watcher ever polls), and
    the watcher state file alone is too early (it is persisted just *before*
    the gated callback records this row).
    """
    with stack.store.read() as conn:  # type: ignore[attr-defined]
        row = conn.execute("SELECT COUNT(*) AS n FROM source_version WHERE source_id = 'SOUL_CORPUS'").fetchone()
    return int(row["n"]) > 0


def _watcher_invocations(stack: object) -> list[tuple[str, str]]:
    with stack.store.read() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(
            "SELECT operation, actor FROM invocation_journal WHERE operation = 'knowledge.relearn_offer' ORDER BY rowid"
        ).fetchall()
    return [(str(row["operation"]), str(row["actor"])) for row in rows]


def test_serve_watcher_emits_offer_without_intervention_and_holds_the_baseline(tmp_path: Path) -> None:
    """The F-1 probe: a corpus edit surfaces a gated offer on its own."""
    stack, server, thread, corpus = _serve_app(
        tmp_path,
        {
            "watch_root": tmp_path / "corpus",
            "watch_interval_seconds": 0.1,
            "watch_cooldown_seconds": 1.0,
            "watch_state_path": tmp_path / "watch-state.json",
        },
    )
    try:
        service = stack.service  # type: ignore[attr-defined]
        # Startup: the first poll records the SOUL_CORPUS detection baseline;
        # no offer, nothing pending.
        _wait_until(lambda: _soul_corpus_baseline_recorded(stack), what="the watcher's startup baseline")
        assert service.relearn_status().state != RELEARN_PENDING
        baseline_id = service.relearn_status().active_source_version_id
        watermark = stack.store.knowledge_high_watermark()  # type: ignore[attr-defined]
        current_pointer = (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8")

        # The probe: edit the corpus; the offer must appear without intervention.
        (corpus / "alpha.md").write_bytes(ALPHA + b"\n## Third Lesson\n\nDetected by the watcher.\n")
        _wait_until(lambda: service.relearn_status().state == RELEARN_PENDING, what="RELEARN_PENDING after the edit")
        status = service.relearn_status()
        assert status.pending_offer is not None

        # No baseline flip: the operator-approved baseline is unchanged, the
        # store's knowledge and the projection are untouched.
        assert status.active_source_version_id == baseline_id
        assert stack.store.knowledge_high_watermark() == watermark  # type: ignore[attr-defined]
        assert (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8") == current_pointer

        # The detection rode the audited gated path as the watcher actor.
        assert _watcher_invocations(stack) and all(
            actor == "source-watcher" for _, actor in _watcher_invocations(stack)
        )
        assert _health(tmp_path / "api.sock") == 200
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        stack.close()  # type: ignore[attr-defined]
    assert not thread.is_alive()
    # The watch loop stopped with the server.
    assert not any(t.name == "soul-source-watch" for t in threading.enumerate())


def test_serve_watcher_survives_restart_without_duplicate_offers(tmp_path: Path) -> None:
    """Restart safety: an unchanged corpus re-emits nothing; a fresh edit is detected."""
    state_path = tmp_path / "watch-state.json"
    kwargs = {
        "watch_root": tmp_path / "corpus",
        "watch_interval_seconds": 0.1,
        "watch_cooldown_seconds": 0.3,
        "watch_state_path": state_path,
    }
    stack, server, thread, corpus = _serve_app(tmp_path, kwargs)
    service = stack.service  # type: ignore[attr-defined]
    try:
        _wait_until(lambda: _soul_corpus_baseline_recorded(stack), what="startup baseline")
        (corpus / "alpha.md").write_bytes(ALPHA + b"\nfirst edit\n")
        _wait_until(lambda: service.relearn_status().state == RELEARN_PENDING, what="first offer")
        assert _offer_count(stack) == 1
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        stack.close()  # type: ignore[attr-defined]
    assert not thread.is_alive()

    # Restart against the same store and watcher state.
    stack2 = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    service2 = stack2.service
    app = create_app(stack2, **kwargs)  # type: ignore[arg-type]
    config = uvicorn.Config(app, uds=str(tmp_path / "api.sock"), log_level="error")
    server2 = uvicorn.Server(config)
    thread2 = threading.Thread(target=server2.run, daemon=True)
    thread2.start()
    try:
        _wait_until(lambda: (tmp_path / "api.sock").exists(), what="restarted socket")
        time.sleep(0.5)  # several polls against an unchanged corpus
        assert _offer_count(stack2) == 1, "an unchanged corpus re-emitted an offer after restart"
        assert service2.relearn_status().state == RELEARN_PENDING  # gate state survived the restart
        (corpus / "beta.md").write_bytes(BETA + b"\npost-restart edit\n")
        _wait_until(lambda: _offer_count(stack2) == 2, what="the post-restart offer")
    finally:
        server2.should_exit = True
        thread2.join(timeout=15)
        stack2.close()
    assert not thread2.is_alive()


def test_serve_watcher_error_isolation_keeps_the_server_up(tmp_path: Path) -> None:
    """A broken watcher state file never takes the API down; repair resumes detection."""
    state_path = tmp_path / "watch-state.json"
    stack, server, thread, corpus = _serve_app(
        tmp_path,
        {
            "watch_root": tmp_path / "corpus",
            "watch_interval_seconds": 0.1,
            "watch_cooldown_seconds": 0.3,
            "watch_state_path": state_path,
        },
    )
    service = stack.service  # type: ignore[attr-defined]
    try:
        _wait_until(lambda: state_path.exists(), what="watcher state file")
        state_path.write_text("{corrupted", encoding="utf-8")
        time.sleep(0.5)  # several failing polls
        assert _health(tmp_path / "api.sock") == 200, "watcher failure took the server down"
        assert _offer_count(stack) == 0

        # Operator repairs by removing the corrupt state; detection resumes.
        state_path.unlink()
        (corpus / "alpha.md").write_bytes(ALPHA + b"\npost-repair edit\n")
        _wait_until(lambda: service.relearn_status().state == RELEARN_PENDING, what="offer after repair")
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        stack.close()  # type: ignore[attr-defined]
    assert not thread.is_alive()


def test_serve_watcher_refuses_a_missing_watch_root(tmp_path: Path) -> None:
    stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    try:
        with pytest.raises(UpdatePipelineError):
            create_app(stack, watch_root=tmp_path / "no-such-corpus")
    finally:
        stack.close()


def test_serve_watcher_refuses_state_inside_the_watched_root(tmp_path: Path) -> None:
    """A state file under the root would perturb the corpus fingerprint itself."""
    stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    corpus = _corpus(tmp_path)
    try:
        with pytest.raises(UpdatePipelineError):
            create_app(stack, watch_root=corpus, watch_state_path=corpus / "watch-state.json")
    finally:
        stack.close()


def test_serve_watcher_requires_state_path_for_in_memory_stores(tmp_path: Path) -> None:
    stack = build_stack(":memory:", tmp_path / "out")
    corpus = _corpus(tmp_path)
    try:
        with pytest.raises(UpdatePipelineError):
            create_app(stack, watch_root=corpus)
        # ...but an explicit state path wires it.
        app = create_app(stack, watch_root=corpus, watch_state_path=tmp_path / "watch-state.json")
        assert app is not None
    finally:
        stack.close()


def test_cli_serve_forwards_the_watch_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from soul_knowledge.cli import main

    corpus = _corpus(tmp_path)
    captured: dict[str, object] = {}

    def fake_serve(stack: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("soul_knowledge.cli.serve", fake_serve)
    code = main(
        [
            "--store",
            str(tmp_path / "soul.sqlite3"),
            "--output-root",
            str(tmp_path / "out"),
            "serve",
            "--socket",
            str(tmp_path / "s.sock"),
            "--watch",
            str(corpus),
            "--watch-interval",
            "2.5",
            "--watch-cooldown",
            "60",
            "--watch-state",
            str(tmp_path / "watch-state.json"),
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert captured["watch_root"] == str(corpus)
    assert captured["watch_interval_seconds"] == 2.5
    assert captured["watch_cooldown_seconds"] == 60.0
    assert captured["watch_state_path"] == str(tmp_path / "watch-state.json")


def test_cli_serve_without_watch_flag_serves_without_a_watcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from soul_knowledge.cli import main

    captured: dict[str, object] = {}

    def fake_serve(stack: object, **kwargs: object) -> None:
        captured.update(kwargs)

    monkeypatch.setattr("soul_knowledge.cli.serve", fake_serve)
    code = main(
        [
            "--store",
            str(tmp_path / "soul.sqlite3"),
            "--output-root",
            str(tmp_path / "out"),
            "serve",
            "--socket",
            str(tmp_path / "s.sock"),
        ]
    )
    assert code == 0, capsys.readouterr().err
    assert "watch_root" not in captured
