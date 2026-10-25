"""Runtime boundary tests: no model, no provider, no socket, no sampling.

Task 1 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``,
extended at Task 10 to cover the full post-integration runtime surface
(``mcp_server``, ``cli``, ``http_api``, ``scoring``, ``review``,
``improvement``, ``scheduler``, ``relearn``, ``security``, ``observability``
and ``backup``) in both the forbidden-import scan and the socket-denial
guards, plus the two matrix contract tests owned by this gate (rows 36 and
40).

Every assertion here is about a *runtime surface*, not a document: the guard
module (``soul_knowledge.runtime_guard``) fails closed, so a violation raises
instead of warning. The tests exercise the guards against synthetic hostile
inputs, against the real installed environment, and inside a scrubbed child
process, and they statically scan product sources for forbidden imports.
"""

from __future__ import annotations

import ast
import importlib.metadata as importlib_metadata
import json
import os
import re
import socket
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Any, cast

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from soul_knowledge import runtime_guard

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# RFC 5737 TEST-NET-1: never routed, so a probe can only fail by being denied.
TEST_NET = ("192.0.2.1", 9)


def _clean_mcp_server():
    """A real MCP SDK server registering exactly ``tools`` + ``resources``.

    This is the capability evidence ``verify_runtime`` observes: the families are
    read from the handlers the server actually registers, not from the guard's
    own allowlist.
    """
    from mcp.server.lowlevel import Server

    async def _handler(context: Any, params: Any) -> Any:  # pragma: no cover - never invoked
        return None

    handler = cast(Any, _handler)
    return Server("soul-knowledge", on_list_tools=handler, on_list_resources=handler)


def _unix_roundtrip(name: str = "roundtrip") -> None:
    """Prove AF_UNIX sockets still work while a socket guard may be active."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"soul-guard-{name}-{os.getpid()}.sock")
    try:
        server.bind(listener)
        server.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(listener)
            connection, _ = server.accept()
            try:
                connection.sendall(b"ok")
                assert client.recv(2) == b"ok"
            finally:
                connection.close()
        finally:
            client.close()
    finally:
        server.close()
        if os.path.exists(listener):
            os.unlink(listener)


# ------------------------------------------------------------------ python


def test_guard_requires_python_314() -> None:
    assert runtime_guard.REQUIRED_PYTHON == (3, 14)
    assert runtime_guard.SUPPORTED_PYTHON_RANGE == ">=3.14,<3.15"
    runtime_guard.assert_supported_python()
    for wrong in ((3, 10), (3, 13), (3, 15)):
        with pytest.raises(runtime_guard.PythonVersionError):
            runtime_guard.assert_supported_python(wrong)


# ------------------------------------------------------ declared boundary


def test_guard_declares_the_rejected_import_surface() -> None:
    for module in ("openai", "anthropic", "tiktoken", "boto3", "transformers", "chromadb", "faiss"):
        assert module in runtime_guard.FORBIDDEN_IMPORT_ROOTS, module
    for module in (
        "graphify.llm",
        "graphify.serve",
        "graphify.prs",
        "graphify.transcribe",
        "graphify.ingest",
        "graphify.mcp_ingest",
        "graphify.exporters.graphdb",
    ):
        assert module in runtime_guard.FORBIDDEN_IMPORT_ROOTS, module


def test_guard_declares_the_rejected_distribution_surface() -> None:
    for distribution in (
        "openai",
        "anthropic",
        "boto3",
        "tiktoken",
        "transformers",
        "sentence-transformers",
        "chromadb",
        "faiss-cpu",
        "graspologic-native",
        "neo4j",
        "falkordb",
        "faster-whisper",
        "yt-dlp",
    ):
        assert distribution in runtime_guard.FORBIDDEN_DISTRIBUTIONS, distribution
    assert runtime_guard.GRAPHIFY_PIN == "graphifyy[mcp]==0.9.95"


def test_every_graphify_provider_extra_is_forbidden() -> None:
    """The guard list must stay ahead of Graphify's own provider extras."""
    provider_extras = {"openai", "anthropic", "gemini", "kimi", "ollama", "bedrock"}
    metadata = importlib_metadata.distribution("graphifyy").metadata
    checked = 0
    for requirement in metadata.get_all("Requires-Dist") or []:
        match = re.search(r'extra\s*==\s*"([^"]+)"', requirement)
        if not match or match.group(1) not in provider_extras:
            continue
        name = canonicalize_name(Requirement(requirement).name)
        checked += 1
        assert name in runtime_guard.FORBIDDEN_DISTRIBUTIONS, f"Graphify provider extra {name} is not guarded"
    assert checked >= 6, f"expected Graphify provider extras to be present, saw {checked}"


# ------------------------------------------------------------- credentials


def test_provider_credentials_are_detected_and_refused() -> None:
    env = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "OPENAI_API_KEY": "sk-test",
        "ANTHROPIC_API_KEY": "sk-test",
        "AWS_SECRET_ACCESS_KEY": "aws",
        "HF_TOKEN": "hf",
        "TIKTOKEN_CACHE_DIR": "/tmp/tiktoken",
    }
    assert set(runtime_guard.provider_credentials(env)) == {
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "HF_TOKEN",
        "TIKTOKEN_CACHE_DIR",
    }
    with pytest.raises(runtime_guard.ProviderCredentialError):
        runtime_guard.assert_no_provider_credentials(env)


def test_scrubbed_environment_keeps_process_essentials_only() -> None:
    env = {
        "PATH": "/usr/bin",
        "HOME": "/root",
        "LANG": "C.UTF-8",
        "XDG_CACHE_HOME": "/root/.cache",
        "OPENAI_API_KEY": "sk-test",
        "HERMES_CUSTOM_VENDOR_AI_API_KEY": "vendor",
        "AWS_SESSION_TOKEN": "aws",
        "HF_HOME": "/root/.cache/huggingface",
        "TIKTOKEN_CACHE_DIR": "/tmp/tiktoken",
        "OLLAMA_HOST": "http://127.0.0.1:11434",
        "TRANSFORMERS_CACHE": "/root/.cache/transformers",
    }
    scrubbed = runtime_guard.scrubbed_env(env)
    assert scrubbed == {"PATH": "/usr/bin", "HOME": "/root", "LANG": "C.UTF-8", "XDG_CACHE_HOME": "/root/.cache"}
    assert runtime_guard.provider_credentials(scrubbed) == {}
    assert runtime_guard.model_asset_env(scrubbed) == {}
    assert runtime_guard.removed_credential_names(env) == (
        "AWS_SESSION_TOKEN",
        "HERMES_CUSTOM_VENDOR_AI_API_KEY",
        "HF_HOME",
        "OLLAMA_HOST",
        "OPENAI_API_KEY",
        "TIKTOKEN_CACHE_DIR",
        "TRANSFORMERS_CACHE",
    )


def test_scrubbed_environment_explicitly_can_keep_one_variable() -> None:
    env = {"PATH": "/usr/bin", "GRAPHIFY_OUT": "/tmp/out", "OPENAI_API_KEY": "sk-test"}
    scrubbed = runtime_guard.scrubbed_env(env, allow=("GRAPHIFY_OUT",))
    assert scrubbed == {"PATH": "/usr/bin", "GRAPHIFY_OUT": "/tmp/out"}


def test_deterministic_environment_pins_hash_seed() -> None:
    env = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-test"}
    deterministic = runtime_guard.deterministic_env(env)
    assert deterministic["PYTHONHASHSEED"] == "0"
    assert runtime_guard.provider_credentials(deterministic) == {}


def test_live_environment_is_scrubbed_before_it_reaches_a_child_process() -> None:
    live = dict(os.environ)
    scrubbed = runtime_guard.scrubbed_env(live)
    assert runtime_guard.provider_credentials(scrubbed) == {}
    assert runtime_guard.model_asset_env(scrubbed) == {}
    assert all(key in runtime_guard.SAFE_ENV_KEYS or key.startswith("XDG_") for key in scrubbed)
    assert set(runtime_guard.removed_credential_names(live)) == set(runtime_guard.provider_credentials(live))


def test_model_asset_configuration_is_refused() -> None:
    with pytest.raises(runtime_guard.ProviderCredentialError):
        runtime_guard.assert_no_model_asset_env({"PATH": "/usr/bin", "HF_HOME": "/root/.cache/huggingface"})


# ---------------------------------------------------------- import guard


def test_forbidden_imports_fail_closed_and_allowed_imports_work() -> None:
    with runtime_guard.import_guard():
        with pytest.raises(runtime_guard.ForbiddenImportError):
            importlib_import("graphify.llm")
        with pytest.raises(runtime_guard.ForbiddenImportError):
            importlib_import("graphify.serve")
        with pytest.raises(runtime_guard.ForbiddenImportError):
            importlib_import("openai")
        with pytest.raises(runtime_guard.ForbiddenImportError):
            importlib_import("graphify.prs")
        # Deterministic primitives the product does use stay importable.
        assert importlib_import("graphify") is not None
        assert importlib_import("graphify.cache") is not None
        assert importlib_import("networkx") is not None
        assert importlib_import("soul_knowledge.runtime_guard") is not None


def test_import_guard_detects_modules_that_are_already_loaded() -> None:
    guard = runtime_guard.import_guard()
    with guard:
        fake = type(sys)("openai")
        sys.modules["openai"] = fake
        try:
            with pytest.raises(runtime_guard.ForbiddenImportError):
                guard.assert_not_loaded()
            with pytest.raises(runtime_guard.ForbiddenImportError):
                guard.assert_clean()
        finally:
            del sys.modules["openai"]


def test_import_guard_is_removed_after_use() -> None:
    finders_before = list(sys.meta_path)
    with runtime_guard.import_guard():
        assert list(sys.meta_path) != finders_before
    assert list(sys.meta_path) == finders_before


def test_assert_no_forbidden_imports_probes_every_blocked_root() -> None:
    runtime_guard.assert_no_forbidden_imports()


# --------------------------------------------------------------- sockets


def test_outbound_tcp_connections_are_denied() -> None:
    with runtime_guard.deny_outbound_sockets():
        with pytest.raises(runtime_guard.OutboundSocketDenied):
            socket.create_connection(TEST_NET, timeout=0.05)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(runtime_guard.OutboundSocketDenied):
                sock.connect(TEST_NET)
            with pytest.raises(runtime_guard.OutboundSocketDenied):
                sock.connect_ex(TEST_NET)
        finally:
            sock.close()
        datagram = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            with pytest.raises(runtime_guard.OutboundSocketDenied):
                datagram.sendto(b"x", TEST_NET)
        finally:
            datagram.close()


def test_outbound_http_clients_are_denied() -> None:
    with runtime_guard.deny_outbound_sockets():
        with pytest.raises(runtime_guard.OutboundSocketDenied):
            urllib.request.urlopen("http://192.0.2.1/", timeout=0.05)


def test_local_unix_sockets_stay_usable() -> None:
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"soul-guard-{os.getpid()}.sock")
    try:
        server.bind(listener)
        server.listen(1)
        with runtime_guard.deny_outbound_sockets():
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(listener)
            connection, _ = server.accept()
            connection.sendall(b"ok")
            assert client.recv(2) == b"ok"
            client.close()
            connection.close()
    finally:
        server.close()
        if os.path.exists(listener):
            os.unlink(listener)


def test_socket_guard_restores_the_original_api() -> None:
    original_connect = socket.socket.connect
    with runtime_guard.deny_outbound_sockets():
        assert socket.socket.connect is not original_connect
    assert socket.socket.connect is original_connect


def test_socket_guard_install_is_reference_counted_and_reentrant() -> None:
    """A nested install must not overwrite the real originals (task-1 fix)."""
    original_connect = socket.socket.connect
    outer = runtime_guard.install_socket_guard()
    inner = runtime_guard.install_socket_guard()
    try:
        assert socket.socket.connect is not original_connect
        with pytest.raises(runtime_guard.OutboundSocketDenied):
            socket.create_connection(TEST_NET, timeout=0.05)
        inner.uninstall()
        # The outer install still holds the patch and the true originals.
        assert socket.socket.connect is not original_connect
        assert runtime_guard._SOCKET_ORIGINALS, "nested uninstall emptied the originals map"
        _unix_roundtrip("nested")
        with pytest.raises(runtime_guard.OutboundSocketDenied):
            socket.create_connection(TEST_NET, timeout=0.05)
    finally:
        inner.uninstall()
        outer.uninstall()
    assert socket.socket.connect is original_connect
    assert runtime_guard._SOCKET_ORIGINALS == {}


def test_verify_runtime_is_safe_inside_an_active_socket_guard() -> None:
    """verify_runtime's nested probe must not corrupt an active guard (task-1 fix)."""
    original_connect = socket.socket.connect
    with runtime_guard.deny_outbound_sockets():
        report = runtime_guard.verify_runtime(mcp_server=_clean_mcp_server())
        assert report["outbound_sockets_denied"] is True
        assert socket.socket.connect is not original_connect
        assert runtime_guard._SOCKET_ORIGINALS, "verify_runtime emptied the originals map"
        # AF_UNIX must still work after verify_runtime.
        _unix_roundtrip("after-verify")
    assert socket.socket.connect is original_connect
    assert runtime_guard._SOCKET_ORIGINALS == {}


def test_af_unix_sockets_still_work_after_install_verify_uninstall() -> None:
    """Explicit regression: install -> verify_runtime -> AF_UNIX -> uninstall."""
    original_connect = socket.socket.connect
    guard = runtime_guard.install_socket_guard()
    try:
        runtime_guard.verify_runtime(mcp_server=_clean_mcp_server())
        _unix_roundtrip("install-verify")
    finally:
        guard.uninstall()
    assert socket.socket.connect is original_connect
    assert runtime_guard._SOCKET_ORIGINALS == {}


# ---------------------------------------------------------------- sampling


def test_allowed_mcp_capabilities_are_exactly_tools_and_resources() -> None:
    assert runtime_guard.ALLOWED_MCP_CAPABILITIES == {"tools", "resources"}
    runtime_guard.assert_mcp_capabilities({"tools": {}, "resources": {}})
    for violation in (
        {"tools": {}, "resources": {}, "sampling": {}},
        {"tools": {}, "prompts": {}},
        {"tools": {}, "logging": {}},
        {"tools": {}, "completions": {}},
        {"tools": {}},
        {"resources": {}},
        {},
    ):
        with pytest.raises(runtime_guard.CapabilityViolation):
            runtime_guard.assert_mcp_capabilities(violation)


def test_fresh_mcp_sdk_server_registers_no_sampling_handler() -> None:
    from mcp.server.lowlevel import Server

    server = Server("soul-knowledge")
    assert runtime_guard.forbidden_mcp_methods(server) == []
    runtime_guard.assert_no_sampling_handler(server)


def test_a_sampling_handler_is_refused() -> None:
    class Hostile:
        _request_handlers = {"sampling/createMessage": object(), "prompts/list": object()}

    assert runtime_guard.forbidden_mcp_methods(Hostile()) == ["prompts/list", "sampling/createMessage"]
    with pytest.raises(runtime_guard.CapabilityViolation):
        runtime_guard.assert_no_sampling_handler(Hostile())


def test_a_verifiable_handler_registry_is_required() -> None:
    class Opaque:
        pass

    with pytest.raises(runtime_guard.CapabilityViolation):
        runtime_guard.assert_no_sampling_handler(Opaque())


def test_observed_capabilities_come_from_the_server_registry_not_the_allowlist() -> None:
    assert runtime_guard.observed_mcp_capabilities(_clean_mcp_server()) == {"tools": {}, "resources": {}}

    class SamplingServer:
        _request_handlers = {"tools/list": object(), "resources/list": object(), "sampling/createMessage": object()}

    observed = runtime_guard.observed_mcp_capabilities(SamplingServer())
    assert set(observed) == {"resources", "sampling", "tools"}
    with pytest.raises(runtime_guard.CapabilityViolation):
        runtime_guard.assert_mcp_capabilities(observed)


def test_empty_server_advertises_no_allowed_capability() -> None:
    from mcp.server.lowlevel import Server

    observed = runtime_guard.observed_mcp_capabilities(Server("soul-knowledge"))
    assert observed == {}
    with pytest.raises(runtime_guard.CapabilityViolation):
        runtime_guard.assert_mcp_capabilities(observed)


# --------------------------------------------------------- static surface


def _product_sources() -> list[Path]:
    files = sorted(SRC.rglob("*.py"))
    assert files, "the product package must exist"
    return files


def test_product_sources_import_no_forbidden_module() -> None:
    offenders: list[str] = []
    for path in _product_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if runtime_guard.is_forbidden_module(name):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports {name}")
    assert offenders == [], offenders


def test_product_sources_never_scrape_the_graphify_cli() -> None:
    """The adapter must use Graphify as a library, never shell out and parse text."""
    offenders: list[str] = []
    for path in _product_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name not in {"run", "call", "check_call", "check_output", "Popen"}:
                continue
            literals = [
                argument.value
                for argument in node.args
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
            if any("graphify" in literal for literal in literals):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} shells out to graphify")
    assert offenders == [], offenders


# --------------------------------------------------- scrubbed subprocess


def test_guards_hold_inside_a_scrubbed_child_process(tmp_path: Path) -> None:
    script = tmp_path / "probe.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import json, os, socket, sys
            sys.path.insert(0, {str(SRC)!r})
            from soul_knowledge import runtime_guard as guard

            report = {{"credentials": sorted(guard.provider_credentials(os.environ))}}
            report["hash_seed"] = os.environ.get("PYTHONHASHSEED")
            with guard.deny_outbound_sockets():
                try:
                    socket.create_connection(("192.0.2.1", 9), timeout=0.05)
                    report["socket_denied"] = False
                except guard.OutboundSocketDenied:
                    report["socket_denied"] = True
            with guard.import_guard():
                try:
                    import graphify.llm  # noqa: F401
                    report["import_denied"] = False
                except guard.ForbiddenImportError:
                    report["import_denied"] = True
            from mcp.server.lowlevel import Server

            async def _handler(context, params):
                return None

            _server = Server("soul-knowledge", on_list_tools=_handler, on_list_resources=_handler)
            report["report"] = guard.verify_runtime(mcp_server=_server)
            print(json.dumps(report))
            """
        ),
        encoding="utf-8",
    )
    env = runtime_guard.deterministic_env(
        {**os.environ, "OPENAI_API_KEY": "sk-injected", "TIKTOKEN_CACHE_DIR": "/tmp/tiktoken"}
    )
    env["PYTHONPATH"] = str(SRC)
    completed = subprocess.run(
        [sys.executable, str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["credentials"] == []
    assert payload["hash_seed"] == "0"
    assert payload["socket_denied"] is True
    assert payload["import_denied"] is True
    assert payload["report"]["status"] == "ok"


# ------------------------------------------------------------ verification


def test_verify_runtime_reports_a_clean_boundary() -> None:
    report = runtime_guard.verify_runtime(mcp_server=_clean_mcp_server())
    assert report["status"] == "ok"
    assert report["python"] == "3.14"
    assert report["forbidden_distributions"] == []
    assert report["forbidden_entry_points"] == []
    assert report["outbound_sockets_denied"] is True
    assert report["forbidden_imports_blocked"] is True
    assert report["mcp_capabilities"] == ["resources", "tools"]
    assert report["mcp_capability_evidence"] == "server_registry"
    assert report["pinned_entry_point_boundary"] == ["graphifyy:console_scripts:graphify-mcp=graphify.serve:_main"]
    assert report["graphify_pin"] == "graphifyy[mcp]==0.9.95"
    json.dumps(report)


def test_verify_runtime_accepts_an_explicit_capability_snapshot() -> None:
    report = runtime_guard.verify_runtime(mcp_capabilities={"tools": {}, "resources": {}})
    assert report["mcp_capabilities"] == ["resources", "tools"]
    assert report["mcp_capability_evidence"] == "capability_snapshot"


def test_verify_runtime_fails_closed_without_capability_evidence() -> None:
    with pytest.raises(runtime_guard.CapabilityViolation):
        runtime_guard.verify_runtime()


def test_verify_runtime_fails_closed_when_a_server_exposes_sampling() -> None:
    class HostileServer:
        _request_handlers = {
            "tools/list": object(),
            "resources/list": object(),
            "sampling/createMessage": object(),
        }

    with pytest.raises(runtime_guard.CapabilityViolation):
        runtime_guard.verify_runtime(mcp_server=HostileServer())


def test_verify_runtime_fails_closed_on_a_capability_snapshot_with_extras() -> None:
    with pytest.raises(runtime_guard.CapabilityViolation):
        runtime_guard.verify_runtime(mcp_capabilities={"tools": {}, "resources": {}, "prompts": {}})
    with pytest.raises(runtime_guard.CapabilityViolation):
        runtime_guard.verify_runtime(mcp_server=_clean_mcp_server(), mcp_capabilities={"tools": {}, "logging": {}})


def test_verify_runtime_fails_closed_on_a_dirty_environment() -> None:
    dirty = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-test"}
    with pytest.raises(runtime_guard.ProviderCredentialError):
        runtime_guard.verify_runtime(env=dirty, strict_env=True, mcp_server=_clean_mcp_server())
    with pytest.raises(runtime_guard.ForbiddenDependencyError):
        runtime_guard.verify_runtime(dirty_distributions={"openai": "2.0.0"}, mcp_server=_clean_mcp_server())


def test_entry_point_policy_fails_closed_on_the_pinned_wheel_script() -> None:
    """The pinned wheel's dormant `graphify-mcp` is rejected by policy (task-1 fix)."""
    offenders = runtime_guard.forbidden_entry_points()
    assert offenders == ["graphifyy:console_scripts:graphify-mcp=graphify.serve:_main"], offenders
    with pytest.raises(runtime_guard.ForbiddenDependencyError):
        runtime_guard.assert_no_forbidden_entry_points()
    # Only the recorded pinned-wheel boundary is tolerated by the relaxed view.
    assert runtime_guard.blocking_forbidden_entry_points() == []


def test_assert_no_forbidden_distributions_pass_on_this_environment() -> None:
    runtime_guard.assert_no_forbidden_distributions()
    runtime_guard.assert_no_forbidden_entry_points(allow_pinned_boundary=True)


# ================================================== Task 10: full runtime surface
#
# The Task 10 plan modifies this file to cover every post-integration runtime
# module in the forbidden-import scan and under the socket-denial guard, and
# to carry the two matrix contract tests owned by the security/operations
# gate (rows 36 and 40 of docs/research/graphify-feature-matrix.md).

RUNTIME_SURFACE_MODULES = (
    "mcp_server",
    "cli",
    "http_api",
    "scoring",
    "review",
    "improvement",
    "scheduler",
    "relearn",
    "security",
    "observability",
    "backup",
)


def test_forbidden_import_scan_covers_the_full_runtime_surface() -> None:
    """Every named runtime module exists and imports no forbidden module."""
    offenders: list[str] = []
    for module in RUNTIME_SURFACE_MODULES:
        path = SRC / "soul_knowledge" / f"{module}.py"
        assert path.is_file(), f"{module}.py missing — the runtime surface changed"
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if runtime_guard.is_forbidden_module(name):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports {name}")
    assert offenders == [], offenders


def test_runtime_surface_runs_with_outbound_sockets_denied(tmp_path: Path) -> None:
    """The whole post-integration surface works with the socket guard installed.

    Every operation below is local: SQLite, files under tmp_path and in-process
    calls. If any module secretly needed the network, the guard would turn it
    into a hard failure instead of a silent leak.
    """
    from soul_knowledge import backup as backup_module
    from soul_knowledge import cli, http_api, mcp_server, observability, scheduler, security
    from soul_knowledge.contracts import (
        ApplyImprovementRequest,
        EvaluateRequest,
        ProposeImprovementRequest,
        QueryRequest,
        ReviewRequest,
    )
    from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, Relation, RelationKind
    from soul_knowledge.improvement import DiffOp, DiffOperation
    from soul_knowledge.projection import GraphProjector
    from soul_knowledge.relearn import RelearnEngine
    from soul_knowledge.service import KnowledgeService
    from soul_knowledge.store import EventStore

    assert scheduler.DueReviewScheduler is not None  # exercised below via review(include_due=True)
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
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
                source_version_id="sv-guard",
            ),
        ),
        idempotency_key="guard:upsert:KROOT",
    )
    for domain in PrimaryDomain:
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id=domain.value,
                domain=domain,
                node=Node(
                    node_id=domain.value,
                    parent_id="KROOT",
                    kind=NodeKind.PRIMARY,
                    domain=domain,
                    summary=domain.title,
                    provenance=("soul://roots",),
                    source_version_id="sv-guard",
                ),
            ),
            idempotency_key=f"guard:upsert:{domain.value}",
        )
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-gates",
            domain=PrimaryDomain.P1,
            node=Node(
                node_id="n-gates",
                parent_id="P1",
                kind=NodeKind.CHILD,
                domain=PrimaryDomain.P1,
                summary="Run the gates before commit",
                context_tags=("gates",),
                provenance=("soul://corpus/lesson.md",),
                source_version_id="sv-guard",
            ),
        ),
        idempotency_key="guard:upsert:n-gates",
    )
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id="P1",
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.PARENT_OF, source_node_id="P1", target_node_id="n-gates"),
        ),
        idempotency_key="guard:rel:p1:n-gates",
    )

    with runtime_guard.deny_outbound_sockets():
        projector.rebuild()
        service = KnowledgeService(store, projector)
        # retrieval + service
        service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
        # scoring
        service.evaluate(EvaluateRequest(node_id="n-gates", include_descendants=False))
        # review (+ the due-review scheduler through include_due)
        service.review(ReviewRequest(node_id="n-gates", include_due=True))
        # improvement
        proposal = service.propose_improvement(
            ProposeImprovementRequest(
                node_id="n-gates",
                diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="Guarded"),),
                rationale="socket guard probe",
            )
        )
        service.apply_improvement(ApplyImprovementRequest(proposal_id=proposal.proposal.proposal_id))
        # relearn (read-side scope)
        engine = RelearnEngine(store)
        assert engine.quarantined_evidence_ids() == frozenset()
        # security
        sanitized = security.sanitize_text("\x1b[31mEVIL\x1b[0m\u202e\u200bclean")
        assert "\x1b" not in sanitized and "\u202e" not in sanitized and "\u200b" not in sanitized
        security.resolve_within_root(tmp_path, "out")
        # observability
        health = observability.check_health(store)
        assert health.ok, health
        observability.collect_store_metrics(store, observability.MetricsRegistry())
        # backup
        backup_receipt = backup_module.backup_store(store, tmp_path / "backup.sqlite3")
        assert (tmp_path / "backup.sqlite3").is_file(), backup_receipt
        # cli
        cli.build_parser()
        # http_api (the local API dispatch on the same store)
        stack = http_api.build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            http_api.dispatch_text(
                stack, "query", json.dumps({"purpose": "run the gates", "domain": "P1", "token_budget": 512})
            )
        finally:
            stack.close()
        # mcp_server
        server = mcp_server.build_server(service, graph_root=tmp_path / "out")
        runtime_guard.assert_no_sampling_handler(server)
        # …while the guard still denies any outbound connection mid-flight.
        with pytest.raises(runtime_guard.OutboundSocketDenied):
            socket.create_connection(TEST_NET, timeout=0.05)


def test_no_db_sink_and_no_credentials_on_argv() -> None:
    """Matrix row 36: DB push sinks and credential-on-argv paths stay excluded.

    Graphify's GraphML/SVG exports may be published into a generation, but the
    database push sinks and every path that could place credentials on a
    command line are excluded and asserted absent.
    """
    # The Graphify DB-push exporter is a forbidden import root…
    assert runtime_guard.is_forbidden_module("graphify.exporters.graphdb")
    # …and the sink driver distributions are forbidden in the locked environment.
    for distribution in ("neo4j", "falkordb", "psycopg", "psycopg2"):
        assert runtime_guard.is_forbidden_distribution(distribution), distribution
    # The product never spawns a process, so no credential can ride an argv,
    # and no source references a DB sink endpoint.
    offenders: list[str] = []
    for path in _product_sources():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.split(".")[0] == "subprocess" for name in names):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno} imports subprocess")
        for sink in ("neo4j://", "bolt://", "falkor://", "postgresql://"):
            if sink in source:
                offenders.append(f"{path.relative_to(REPO_ROOT)} references {sink}")
    assert offenders == [], offenders


def test_containment_caps_and_generation_secret_scan(tmp_path: Path) -> None:
    """Matrix row 40: containment, size caps, sanitization, generation secret scan."""
    import hashlib

    from soul_knowledge import security
    from soul_knowledge.domain import PrimaryDomain, canonical_json
    from soul_knowledge.importer import SeedImporter
    from soul_knowledge.projection import GraphProjector
    from soul_knowledge.read_model_validator import ReadModelValidator
    from soul_knowledge.store import EventStore

    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_text("# Alpha\n\n## Lesson\n\nContainment matters.\n", encoding="utf-8")
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    projector = GraphProjector(store, output_root=tmp_path / "out")
    receipt = projector.rebuild()
    generation = Path(receipt.generation_dir)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))

    # Containment: inside resolves, escapes fail closed.
    security.resolve_within_root(tmp_path, "out")
    with pytest.raises(security.PathEscapeError):
        security.resolve_within_root(generation, "..")
    with pytest.raises(security.PathEscapeError):
        security.resolve_within_root(generation, "/etc/passwd")
    # Size caps: per-payload/per-generation ceilings fail closed.
    assert security.ensure_text_size("ok", max_bytes=16) <= 16
    with pytest.raises(security.OversizedPayloadError):
        security.ensure_text_size("x" * 64, max_bytes=16)
    with pytest.raises(security.OversizedPayloadError):
        security.ensure_payload_size({"blob": "y" * 8192}, max_bytes=128)
    # Sanitization: control, bidi and zero-width never survive.
    sanitized = security.sanitize_text("\x1b[31mEVIL\x1b[0m\u202e\u200bclean")
    assert "\x1b" not in sanitized and "\u202e" not in sanitized and "\u200b" not in sanitized

    # The published generation passes the validator…
    validator = ReadModelValidator()
    report = validator.validate(generation, manifest)
    assert report.ok, report.errors
    # …and a secret-shaped string in any artifact fails the generation closed.
    graph_path = generation / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["summary"] = "api_key = 'sk-live-0123456789abcdef'"
    graph_path.write_text(canonical_json(graph), encoding="utf-8")
    tampered = dict(manifest)
    tampered["artifacts"] = dict(manifest["artifacts"])
    tampered["artifacts"]["graph.json"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    (generation / "manifest.json").write_text(canonical_json(tampered) + "\n", encoding="utf-8")
    report = validator.validate(generation, tampered)
    assert not report.ok
    assert any("secret" in error.lower() for error in report.errors), report.errors


def importlib_import(name: str):  # noqa: ANN201 - small local helper
    import importlib

    return importlib.import_module(name)
