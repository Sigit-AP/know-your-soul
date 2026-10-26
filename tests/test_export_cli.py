"""Contract tests for the operational ``export``/``analytics`` CLI (ATM F-D1).

These are OPS commands: they write artifacts from the active generation's
read model into a contained output directory and print a canonical JSON
summary. They are *not* knowledge operations — they never join the frozen
fourteen-operation surface and are never exposed over MCP or HTTP.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from know_your_soul.cli import main
from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
)
from know_your_soul.exporters.orchestrator import default_registry
from know_your_soul.http_api import KNOWLEDGE_OPERATIONS, OPS_OPERATIONS
from know_your_soul.projection import GraphProjector
from know_your_soul.store import EventStore


def _seeded_paths(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "soul.sqlite3"
    out_path = tmp_path / "out"
    store = EventStore(db_path)
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="KROOT",
            domain=PrimaryDomain.P1,
            node=Node(
                node_id="KROOT",
                kind=NodeKind.ROOT,
                domain=PrimaryDomain.P1,
                summary="Knowledge Root",
                provenance=("soul://roots",),
                source_version_id="sv-test",
            ),
            source_version_id="sv-test",
        ),
        idempotency_key="test:upsert:KROOT",
    )
    GraphProjector(store, output_root=out_path).rebuild()
    store.close()
    return db_path, out_path


def _invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict | None, dict | None]:
    code = main(argv)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    err = json.loads(captured.err) if captured.err.strip().startswith("{") else None
    return code, out, err


def _base(db_path: Path, out_path: Path) -> list[str]:
    return ["--store", str(db_path), "--output-root", str(out_path)]


# --------------------------------------------------------------- happy paths


def test_export_writes_default_formats_and_prints_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["export"], capsys)
    assert code == 0, err
    assert out is not None
    assert out["ok"] is True
    assert out["command"] == "export"
    registry = default_registry()
    expected = set(registry.names(kind="format"))
    written = {entry["name"]: entry for entry in out["artifacts"]}  # type: ignore[index]
    assert set(written) == expected
    out_dir = Path(str(out["output_dir"]))
    for name, entry in written.items():
        data = (out_dir / name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
        assert entry["size"] == len(data)
    assert out["generation"] == (out_path / "CURRENT").read_text(encoding="utf-8").strip()


def test_analytics_writes_analytics_modules(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["analytics"], capsys)
    assert code == 0, err
    assert out is not None
    assert out["command"] == "analytics"
    registry = default_registry()
    written = {entry["name"] for entry in out["artifacts"]}  # type: ignore[index]
    assert written == set(registry.names(kind="analytics"))


def test_export_with_explicit_format_selection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    registry = default_registry()
    chosen = registry.names(kind="analytics")[0]
    code, out, err = _invoke(_base(db_path, out_path) + ["export", "--formats", chosen], capsys)
    assert code == 0, err
    assert out is not None
    assert [entry["name"] for entry in out["artifacts"]] == [chosen]  # type: ignore[index]


def test_export_is_idempotent_and_deterministic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, first, _ = _invoke(_base(db_path, out_path) + ["export"], capsys)
    assert code == 0
    out_dir = Path(str(first["output_dir"]))  # type: ignore[index]
    snapshot = {str(p.relative_to(out_dir)): p.read_bytes() for p in sorted(out_dir.rglob("*")) if p.is_file()}
    code, second, _ = _invoke(_base(db_path, out_path) + ["export"], capsys)
    assert code == 0
    strip = lambda entries: [{k: v for k, v in entry.items() if k != "written"} for entry in entries]  # noqa: E731
    assert strip(first["artifacts"]) == strip(second["artifacts"])  # type: ignore[index]
    assert all(entry["written"] is False for entry in second["artifacts"])  # type: ignore[index]
    for rel, data in snapshot.items():
        assert (out_dir / rel).read_bytes() == data


def test_export_honours_contained_custom_output_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["export", "--out", "deck/graph"], capsys)
    assert code == 0, err
    assert out is not None
    out_dir = Path(str(out["output_dir"]))
    assert out_dir.is_relative_to(out_path.resolve())
    assert (out_dir / default_registry().names(kind="format")[0]).is_file()


# ------------------------------------------------------------ strict failure


def test_export_rejects_unknown_format_with_usage_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["export", "--formats", "nope.json"], capsys)
    assert code == 2
    assert out is None
    assert err is not None
    assert err["error"]["code"] == "CLI_USAGE"  # type: ignore[index]
    assert "nope.json" in err["error"]["message"]  # type: ignore[index]


def test_export_rejects_empty_format_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, _, err = _invoke(_base(db_path, out_path) + ["export", "--formats", " ,"], capsys)
    assert code == 2
    assert err is not None and err["error"]["code"] == "CLI_USAGE"  # type: ignore[index]


def test_export_refuses_output_escape(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["export", "--out", "../../outside"], capsys)
    assert code == 2
    assert out is None
    assert err is not None and err["error"]["code"] == "CLI_USAGE"  # type: ignore[index]


def test_export_without_active_generation_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "soul.sqlite3"
    EventStore(db_path).close()
    out_path = tmp_path / "out"
    code, out, err = _invoke(_base(db_path, out_path) + ["export"], capsys)
    assert code == 1
    assert out is None
    assert err is not None
    assert "generation" in err["error"]["message"]  # type: ignore[index]


# ------------------------------------------------------- frozen ops boundary


def test_export_and_analytics_are_not_knowledge_or_http_ops() -> None:
    assert "export" not in KNOWLEDGE_OPERATIONS
    assert "analytics" not in KNOWLEDGE_OPERATIONS
    assert "export" not in OPS_OPERATIONS
    assert "analytics" not in OPS_OPERATIONS
    assert len(KNOWLEDGE_OPERATIONS) == 14


def test_export_and_analytics_do_not_touch_the_store(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    before = EventStore(db_path)
    try:
        count = int(before.execute_raw("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"])
    finally:
        before.close()
    code, _, _ = _invoke(_base(db_path, out_path) + ["export"], capsys)
    assert code == 0
    after = EventStore(db_path)
    try:
        assert int(after.execute_raw("SELECT COUNT(*) AS n FROM event_log").fetchone()["n"]) == count
    finally:
        after.close()
