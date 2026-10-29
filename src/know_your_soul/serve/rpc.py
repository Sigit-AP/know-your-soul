"""Wire protocol for the superpower server: newline-delimited JSON-RPC 2.0.

This is the same framing codegraph uses between proxy and daemon
(RESEARCH-ARCH section 1b, ``transport.ts``): one JSON object per line, in
both directions, over stdio (host↔proxy) and over the Unix socket
(proxy↔daemon). The handshake constants replicate the pinned MCP SDK
negotiation (``mcp.server.runner``: ``requested if requested in
HANDSHAKE_PROTOCOL_VERSIONS else LATEST_HANDSHAKE_VERSION``) so the proxy's
locally answered ``initialize`` is byte-equivalent to the daemon's and to
the reference ``know_your_soul.mcp_server`` surface.

Everything here is deterministic and fail-closed: malformed frames, unknown
envelope keys and oversized payloads raise :class:`ProtocolError` carrying a
typed JSON-RPC code; nothing is coerced or guessed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, BinaryIO

__all__ = [
    "ACTOR_REQUIRED",
    "DAEMON_UNAVAILABLE",
    "HANDSHAKE_PROTOCOL_VERSIONS",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "LATEST_HANDSHAKE_VERSION",
    "MALFORMED_REQUEST",
    "MAX_FRAME_BYTES",
    "METHOD_NOT_FOUND",
    "PAYLOAD_TOO_LARGE",
    "PROTOCOL_VERSION",
    "SCOPE_DENIED",
    "SERVER_BUSY",
    "SERVER_NAME",
    "SHUTTING_DOWN",
    "UNKNOWN_VERB",
    "FrameReader",
    "ProtocolError",
    "RequestEnvelope",
    "decode_message",
    "encode_message",
    "error_response",
    "initialize_result",
    "negotiate_protocol_version",
    "parse_request",
    "result_response",
    "server_version",
    "tool_listing_result",
    "write_message",
]

PROTOCOL_VERSION = "2.0"
SERVER_NAME = "know-your-soul"
MAX_FRAME_BYTES = 1_048_576

# The pinned MCP SDK (mcp==2.3.0, server/runner.py) negotiates against this
# closed set; anything else falls back to the latest handshake version.
HANDSHAKE_PROTOCOL_VERSIONS: tuple[str, ...] = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
LATEST_HANDSHAKE_VERSION = "2025-11-25"

# Standard JSON-RPC codes.
MALFORMED_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# Application codes (JSON-RPC reserves -32000..-32099 for servers).
SERVER_BUSY = -32001
SCOPE_DENIED = -32002
ACTOR_REQUIRED = -32003
UNKNOWN_VERB = -32004
DAEMON_UNAVAILABLE = -32005
PAYLOAD_TOO_LARGE = -32006
DEADLINE_EXCEEDED = -32007
SHUTTING_DOWN = -32008

_ENVELOPE_KEYS = frozenset({"jsonrpc", "id", "method", "params"})


class ProtocolError(RuntimeError):
    """A fail-closed wire violation carrying a typed JSON-RPC code."""

    def __init__(self, code: int, message: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = int(code)
        self.data = dict(data) if data is not None else None


# --------------------------------------------------------------------- codec


def _canonical(message: Mapping[str, Any]) -> str:
    return json.dumps(message, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def encode_message(message: Mapping[str, Any]) -> bytes:
    """One canonical-JSON frame, newline terminated."""
    if not isinstance(message, Mapping):
        raise ProtocolError(MALFORMED_REQUEST, f"cannot encode non-object message of type {type(message).__name__}")
    return _canonical(message).encode("utf-8") + b"\n"


def decode_message(data: bytes) -> dict[str, Any]:
    """Parse one frame strictly; anything but a JSON object is rejected."""
    text = data.decode("utf-8", errors="strict").strip()
    if not text:
        raise ProtocolError(MALFORMED_REQUEST, "blank frame")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ProtocolError(MALFORMED_REQUEST, f"invalid JSON frame: {error.msg}") from None
    if not isinstance(parsed, dict):
        raise ProtocolError(MALFORMED_REQUEST, f"frame must be a JSON object, got {type(parsed).__name__}")
    return parsed


class FrameReader:
    """Bounded newline-delimited frame reader over a binary stream."""

    def __init__(self, source: BinaryIO, *, max_bytes: int = MAX_FRAME_BYTES) -> None:
        self._source = source
        self._max_bytes = int(max_bytes)

    def read(self) -> dict[str, Any] | None:
        """The next frame, or None on a clean EOF at a frame boundary.

        ``readline(limit)`` returning a full unterminated buffer means the
        peer is streaming past the cap: that is rejected typed, never
        truncated-and-parsed.
        """
        line = self._source.readline(self._max_bytes + 1)
        if not line:
            return None
        if len(line) > self._max_bytes:
            raise ProtocolError(
                PAYLOAD_TOO_LARGE,
                f"frame exceeds the {self._max_bytes}-byte cap",
                {"max_frame_bytes": self._max_bytes},
            )
        if not line.endswith(b"\n"):
            raise ProtocolError(MALFORMED_REQUEST, "truncated frame: stream ended without a newline")
        return decode_message(line)


def write_message(sink: BinaryIO, message: Mapping[str, Any]) -> None:
    sink.write(encode_message(message))
    sink.flush()


# ------------------------------------------------------------------ envelope


@dataclass(frozen=True)
class RequestEnvelope:
    """A validated inbound request; ``id is None`` marks a notification."""

    id: str | int | None
    method: str
    params: Mapping[str, Any]

    @property
    def is_notification(self) -> bool:
        return self.id is None


def parse_request(obj: Mapping[str, Any]) -> RequestEnvelope:
    """Validate the outer JSON-RPC envelope; unknown keys fail closed."""
    unknown = set(obj) - _ENVELOPE_KEYS
    if unknown:
        raise ProtocolError(MALFORMED_REQUEST, f"unknown envelope keys: {sorted(unknown)}")
    if obj.get("jsonrpc") != PROTOCOL_VERSION:
        raise ProtocolError(MALFORMED_REQUEST, f"jsonrpc must be {PROTOCOL_VERSION!r}")
    method = obj.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolError(MALFORMED_REQUEST, "method must be a non-empty string")
    request_id = obj.get("id")
    if request_id is not None and not isinstance(request_id, (str, int)):
        raise ProtocolError(MALFORMED_REQUEST, f"id must be a string or integer, got {type(request_id).__name__}")
    if isinstance(request_id, bool):
        raise ProtocolError(MALFORMED_REQUEST, "id must be a string or integer, got bool")
    params = obj.get("params", {})
    if not isinstance(params, Mapping):
        raise ProtocolError(MALFORMED_REQUEST, f"params must be an object, got {type(params).__name__}")
    return RequestEnvelope(id=request_id, method=method, params=params)


def result_response(request_id: str | int | None, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": PROTOCOL_VERSION, "id": request_id, "result": dict(result)}


def error_response(
    request_id: str | int | None,
    code: int,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": int(code), "message": message}
    if data is not None:
        error["data"] = dict(data)
    return {"jsonrpc": PROTOCOL_VERSION, "id": request_id, "error": error}


# ----------------------------------------------------------------- handshake


def server_version() -> str:
    """The installed package version, mirroring ``mcp_server.build_server``."""
    try:
        from importlib.metadata import version

        return version("know-your-soul")
    except Exception:  # noqa: BLE001 - the version is informational only
        return "0.0.0"


def negotiate_protocol_version(requested: Any) -> str:
    """The pinned SDK rule: echo a known handshake version, else the latest."""
    if isinstance(requested, str) and requested in HANDSHAKE_PROTOCOL_VERSIONS:
        return requested
    return LATEST_HANDSHAKE_VERSION


def initialize_result(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``initialize`` result, byte-equivalent to the SDK server's answer.

    Capabilities are exactly ``tools`` + ``resources`` — no sampling, prompts,
    logging or completions — matching the frozen adapter contract.
    """
    requested = params.get("protocolVersion") if isinstance(params, Mapping) else None
    return {
        "protocolVersion": negotiate_protocol_version(requested),
        "capabilities": {
            "resources": {"listChanged": False, "subscribe": False},
            "tools": {"listChanged": False},
        },
        "serverInfo": {"name": SERVER_NAME, "version": server_version()},
    }


def tool_listing_result(max_token_budget: int | None = None) -> dict[str, Any]:
    """The ``tools/list`` result shared by daemon and proxy (identical by construction).

    The pinned SDK (mcp==2.3.0) stamps modern-era envelope keys
    (``cacheScope``/``resultType``/``ttlMs``) on the model but strips them on
    legacy-era wire answers; the negotiated handshake here is always a legacy
    handshake version, so the wire shape drops them.
    """
    from mcp import types

    from know_your_soul.contracts import DEFAULT_MAX_TOKEN_BUDGET
    from know_your_soul.mcp_server import _DESCRIPTIONS, EXPECTED_TOOLS, _tool_schemas

    schemas = _tool_schemas(max_token_budget if max_token_budget is not None else DEFAULT_MAX_TOKEN_BUDGET)
    dumped = types.ListToolsResult(
        tools=[
            types.Tool(name=name, description=_DESCRIPTIONS[name], input_schema=schemas[name])
            for name in EXPECTED_TOOLS
        ]
    ).model_dump(by_alias=True, exclude_none=True)
    for modern_key in ("cacheScope", "resultType", "ttlMs"):
        dumped.pop(modern_key, None)
    return dumped  # type: ignore[no-any-return]
