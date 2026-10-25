"""Runtime boundary tests: no model, no provider, no socket, no sampling.

Task 1 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``.

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

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from soul_knowledge import runtime_guard

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"

# RFC 5737 TEST-NET-1: never routed, so a probe can only fail by being denied.
TEST_NET = ("192.0.2.1", 9)


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
            report["report"] = guard.verify_runtime()
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
    report = runtime_guard.verify_runtime()
    assert report["status"] == "ok"
    assert report["python"] == "3.14"
    assert report["forbidden_distributions"] == []
    assert report["forbidden_entry_points"] == []
    assert report["outbound_sockets_denied"] is True
    assert report["forbidden_imports_blocked"] is True
    assert report["mcp_capabilities"] == ["resources", "tools"]
    assert report["graphify_pin"] == "graphifyy[mcp]==0.9.95"
    json.dumps(report)


def test_verify_runtime_fails_closed_on_a_dirty_environment() -> None:
    dirty = {"PATH": "/usr/bin", "OPENAI_API_KEY": "sk-test"}
    with pytest.raises(runtime_guard.ProviderCredentialError):
        runtime_guard.verify_runtime(env=dirty, strict_env=True)
    with pytest.raises(runtime_guard.ForbiddenDependencyError):
        runtime_guard.verify_runtime(dirty_distributions={"openai": "2.0.0"})


def test_assert_no_forbidden_entry_points_and_distributions_pass_on_this_environment() -> None:
    runtime_guard.assert_no_forbidden_entry_points()
    runtime_guard.assert_no_forbidden_distributions()


def importlib_import(name: str):  # noqa: ANN201 - small local helper
    import importlib

    return importlib.import_module(name)
