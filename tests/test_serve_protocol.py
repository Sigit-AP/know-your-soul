"""Wire-protocol tests for the superpower server (SUB-100).

The daemon speaks newline-delimited JSON-RPC 2.0 (the codegraph transport
shape). These tests pin the frame codec, the request envelope validation,
the MCP handshake negotiation and the typed error taxonomy — all
deterministic and fail-closed.
"""

from __future__ import annotations

import io

import pytest

from know_your_soul.serve import rpc as protocol

# --------------------------------------------------------------------- codec


class TestEncodeDecode:
    def test_roundtrip_preserves_payload(self) -> None:
        message = {"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {"a": [1, 2]}}
        decoded = protocol.decode_message(protocol.encode_message(message))
        assert decoded == message

    def test_encoded_frame_ends_with_newline(self) -> None:
        assert protocol.encode_message({"a": 1}).endswith(b"\n")

    def test_encode_is_canonical(self) -> None:
        one = protocol.encode_message({"b": 1, "a": 2})
        two = protocol.encode_message({"a": 2, "b": 1})
        assert one == two

    def test_encode_rejects_non_mapping(self) -> None:
        with pytest.raises(protocol.ProtocolError):
            protocol.encode_message([1, 2, 3])  # type: ignore[arg-type]

    def test_decode_rejects_invalid_json(self) -> None:
        with pytest.raises(protocol.ProtocolError) as info:
            protocol.decode_message(b"{not json}\n")
        assert info.value.code == protocol.MALFORMED_REQUEST

    def test_decode_rejects_non_object(self) -> None:
        for payload in (b"[1,2]\n", b'"x"\n', b"42\n", b"null\n"):
            with pytest.raises(protocol.ProtocolError) as info:
                protocol.decode_message(payload)
            assert info.value.code == protocol.MALFORMED_REQUEST

    def test_decode_rejects_blank_frame(self) -> None:
        with pytest.raises(protocol.ProtocolError):
            protocol.decode_message(b"\n")


class TestFrameReader:
    def test_reads_frames_in_order(self) -> None:
        stream = io.BytesIO(b'{"id":1}\n{"id":2}\n')
        reader = protocol.FrameReader(stream)
        assert reader.read() == {"id": 1}
        assert reader.read() == {"id": 2}

    def test_clean_eof_returns_none(self) -> None:
        reader = protocol.FrameReader(io.BytesIO(b""))
        assert reader.read() is None

    def test_eof_after_frames_returns_none(self) -> None:
        reader = protocol.FrameReader(io.BytesIO(b'{"id":1}\n'))
        assert reader.read() == {"id": 1}
        assert reader.read() is None

    def test_truncated_final_frame_is_typed(self) -> None:
        reader = protocol.FrameReader(io.BytesIO(b'{"id":1}'))
        with pytest.raises(protocol.ProtocolError) as info:
            reader.read()
        assert info.value.code == protocol.MALFORMED_REQUEST

    def test_oversized_frame_is_typed(self) -> None:
        payload = b'{"data":"' + b"x" * 4096 + b'"}\n'
        reader = protocol.FrameReader(io.BytesIO(payload), max_bytes=1024)
        with pytest.raises(protocol.ProtocolError) as info:
            reader.read()
        assert info.value.code == protocol.PAYLOAD_TOO_LARGE

    def test_oversized_stream_without_newline_is_typed(self) -> None:
        reader = protocol.FrameReader(io.BytesIO(b"x" * 4096), max_bytes=1024)
        with pytest.raises(protocol.ProtocolError) as info:
            reader.read()
        assert info.value.code == protocol.PAYLOAD_TOO_LARGE

    def test_write_message_appends_newline(self) -> None:
        sink = io.BytesIO()
        protocol.write_message(sink, {"ok": True})
        assert sink.getvalue() == b'{"ok":true}\n'


# ------------------------------------------------------------------ envelope


class TestParseRequest:
    def test_minimal_request(self) -> None:
        envelope = protocol.parse_request({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert envelope.method == "ping"
        assert envelope.id == 1
        assert envelope.params == {}

    def test_string_id_accepted(self) -> None:
        envelope = protocol.parse_request({"jsonrpc": "2.0", "id": "abc", "method": "ping"})
        assert envelope.id == "abc"

    def test_notification_has_no_id(self) -> None:
        envelope = protocol.parse_request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert envelope.id is None
        assert envelope.is_notification

    def test_request_with_id_is_not_notification(self) -> None:
        envelope = protocol.parse_request({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert not envelope.is_notification

    def test_missing_method_rejected(self) -> None:
        with pytest.raises(protocol.ProtocolError) as info:
            protocol.parse_request({"jsonrpc": "2.0", "id": 1})
        assert info.value.code == protocol.MALFORMED_REQUEST

    def test_non_string_method_rejected(self) -> None:
        with pytest.raises(protocol.ProtocolError) as info:
            protocol.parse_request({"jsonrpc": "2.0", "id": 1, "method": 42})
        assert info.value.code == protocol.MALFORMED_REQUEST

    def test_wrong_jsonrpc_version_rejected(self) -> None:
        with pytest.raises(protocol.ProtocolError) as info:
            protocol.parse_request({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        assert info.value.code == protocol.MALFORMED_REQUEST

    def test_unknown_envelope_key_rejected(self) -> None:
        with pytest.raises(protocol.ProtocolError) as info:
            protocol.parse_request({"jsonrpc": "2.0", "id": 1, "method": "ping", "shell": "sh"})
        assert info.value.code == protocol.MALFORMED_REQUEST

    def test_non_object_params_rejected(self) -> None:
        with pytest.raises(protocol.ProtocolError) as info:
            protocol.parse_request({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": [1]})
        assert info.value.code == protocol.MALFORMED_REQUEST

    def test_float_id_rejected(self) -> None:
        with pytest.raises(protocol.ProtocolError):
            protocol.parse_request({"jsonrpc": "2.0", "id": 1.5, "method": "ping"})

    def test_bool_id_rejected(self) -> None:
        with pytest.raises(protocol.ProtocolError):
            protocol.parse_request({"jsonrpc": "2.0", "id": True, "method": "ping"})


class TestResponses:
    def test_result_response_shape(self) -> None:
        frame = protocol.result_response(3, {"ok": True})
        assert frame == {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}

    def test_error_response_shape(self) -> None:
        frame = protocol.error_response(3, protocol.SERVER_BUSY, "busy", {"retry_after_ms": 50})
        assert frame["jsonrpc"] == "2.0"
        assert frame["id"] == 3
        assert frame["error"]["code"] == protocol.SERVER_BUSY
        assert frame["error"]["data"]["retry_after_ms"] == 50

    def test_error_response_without_data_omits_key(self) -> None:
        frame = protocol.error_response(None, protocol.MALFORMED_REQUEST, "bad")
        assert "data" not in frame["error"]

    def test_protocol_error_carries_code_and_data(self) -> None:
        error = protocol.ProtocolError(protocol.SERVER_BUSY, "queue full", {"retry_after_ms": 25})
        assert error.code == protocol.SERVER_BUSY
        assert error.data == {"retry_after_ms": 25}
        assert "queue full" in str(error)


# ----------------------------------------------------------------- handshake


class TestHandshake:
    def test_known_version_is_echoed(self) -> None:
        for version in ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"):
            assert protocol.negotiate_protocol_version(version) == version

    def test_unknown_version_falls_back_to_latest_handshake(self) -> None:
        assert protocol.negotiate_protocol_version("2999-01-01") == "2025-11-25"

    def test_non_string_version_falls_back(self) -> None:
        assert protocol.negotiate_protocol_version(None) == "2025-11-25"
        assert protocol.negotiate_protocol_version(7) == "2025-11-25"

    def test_initialize_result_shape(self) -> None:
        result = protocol.initialize_result(
            {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "c", "version": "1"}}
        )
        assert set(result) == {"protocolVersion", "capabilities", "serverInfo"}
        assert result["protocolVersion"] == "2025-03-26"
        assert set(result["capabilities"]) == {"tools", "resources"}
        assert result["serverInfo"]["name"] == "know-your-soul"
        assert isinstance(result["serverInfo"]["version"], str)
        assert result["serverInfo"]["version"]

    def test_initialize_result_matches_sdk_wire_shape(self) -> None:
        """Byte-shape parity with the real mcp_server initialize result."""
        result = protocol.initialize_result({"protocolVersion": "2026-07-28"})
        assert result == {
            "protocolVersion": "2025-11-25",
            "capabilities": {
                "resources": {"listChanged": False, "subscribe": False},
                "tools": {"listChanged": False},
            },
            "serverInfo": {"name": "know-your-soul", "version": result["serverInfo"]["version"]},
        }

    def test_initialize_without_params_uses_latest_handshake(self) -> None:
        result = protocol.initialize_result(None)
        assert result["protocolVersion"] == "2025-11-25"


class TestToolListing:
    def test_seventeen_tools_sorted(self) -> None:
        result = protocol.tool_listing_result()
        names = [tool["name"] for tool in result["tools"]]
        assert len(names) == 17  # 16 frozen-era tools + 1 contract-v4 extraction verb (SUB-106)
        assert names == sorted(names)

    def test_tool_entries_carry_name_description_schema(self) -> None:
        for tool in protocol.tool_listing_result()["tools"]:
            assert set(tool) >= {"name", "description", "inputSchema"}
            assert tool["inputSchema"].get("additionalProperties") is False

    def test_no_tool_exposes_project_path(self) -> None:
        for tool in protocol.tool_listing_result()["tools"]:
            assert "project_path" not in tool["inputSchema"].get("properties", {})

    def test_listing_is_deterministic(self) -> None:
        assert protocol.tool_listing_result() == protocol.tool_listing_result()

    def test_listing_matches_mcp_server_surface(self) -> None:
        from know_your_soul.mcp_server import EXPECTED_TOOLS

        names = tuple(tool["name"] for tool in protocol.tool_listing_result()["tools"])
        assert names == EXPECTED_TOOLS
