"""The thin MCP stdio proxy between a host agent and the superpower daemon.

Mirrors the codegraph proxy mode (RESEARCH-ARCH section 1b): the host sees a
plain MCP stdio server; the proxy answers the handshake locally
(``initialize`` / ``tools/list`` / ``ping`` — byte-identical to the daemon's
answers, from the shared :mod:`know_your_soul.serve.rpc` constants) and
forwards everything else over the daemon's Unix socket. The daemon is
auto-spawned when absent. A PPID watchdog (codegraph #277) kills the proxy
when its host dies — including ``kill -9``, where no stdin EOF is owed.

Per-agent audit: the proxy stamps ``params._meta.actor`` onto every
forwarded ``tools/call`` that does not carry one (its own configured actor,
default ``mcp-proxy``); a host-supplied actor is preserved. The daemon
independently rejects actor-less requests, so the audit contract holds even
for clients that bypass the proxy.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from collections.abc import Mapping
from typing import Any, BinaryIO

from know_your_soul.serve import rpc
from know_your_soul.serve.daemon_config import DaemonConfig
from know_your_soul.serve.superpower import ensure_daemon

__all__ = ["StdioProxy", "run_proxy"]

# Methods answered locally without a daemon round-trip (fast handshake, #172).
_LOCAL_METHODS = frozenset({"initialize", "ping", "tools/list"})

# Early-ppid capture (codegraph #1185): the parent pid is recorded at import
# time — the earliest possible moment in the exec'd proxy process — so a host
# that dies during interpreter startup still leaves a stale reference for the
# watchdog to compare against.
_EARLY_PPID = os.getppid()


class StdioProxy:
    """One stdio session (stdin/stdout) bridged to the daemon socket."""

    def __init__(
        self,
        config: DaemonConfig,
        *,
        stdin: BinaryIO | None = None,
        stdout: BinaryIO | None = None,
        actor: str | None = None,
        ppid: int | None = None,
    ) -> None:
        self._config = config
        self._stdin = stdin if stdin is not None else sys.stdin.buffer
        self._stdout = stdout if stdout is not None else sys.stdout.buffer
        self._actor = actor or config.proxy_actor
        self._ppid = ppid if ppid is not None else _EARLY_PPID
        self._socket: socket.socket | None = None
        self._socket_reader: rpc.FrameReader | None = None
        self._socket_lock = threading.Lock()  # send+read is one atomic round-trip
        self._dead = threading.Event()

    # --------------------------------------------------------------- lifecycle

    def run(self) -> int:
        """Serve until the host closes stdin or the PPID watchdog fires."""
        try:
            self._connect()
        except Exception:  # noqa: BLE001 - startup failure must not hang the host
            self._socket = None
        watchdog = threading.Thread(target=self._ppid_watch, name="superpower-ppid", daemon=True)
        watchdog.start()
        threading.Thread(target=self._lease_keepalive, name="superpower-lease", daemon=True).start()
        reader = rpc.FrameReader(self._stdin, max_bytes=self._config.max_frame_bytes)
        while True:
            try:
                frame = reader.read()
            except rpc.ProtocolError as error:
                self._send(rpc.error_response(None, error.code, str(error), error.data))
                return 2
            except ConnectionError, OSError:
                return 0
            if frame is None:
                return 0
            try:
                envelope = rpc.parse_request(frame)
            except rpc.ProtocolError as error:
                self._send(rpc.error_response(None, error.code, str(error), error.data))
                continue
            if envelope.is_notification:
                continue  # MCP lifecycle/cancel notifications: never answered
            response = self._handle(envelope)
            self._send(response)
            if self._dead.is_set():
                return 0

    # --------------------------------------------------------------- dispatch

    def _handle(self, envelope: rpc.RequestEnvelope) -> dict[str, Any]:
        method = envelope.method
        if method == "initialize":
            return rpc.result_response(envelope.id, rpc.initialize_result(envelope.params))
        if method == "ping":
            return rpc.result_response(envelope.id, {})
        if method == "tools/list":
            return rpc.result_response(envelope.id, rpc.tool_listing_result(self._config.max_token_budget))
        if method.startswith("daemon."):
            return rpc.error_response(envelope.id, rpc.SCOPE_DENIED, "admin methods are not exposed to MCP hosts")
        params = envelope.params
        if method == "tools/call":
            params = self._stamp_actor(params)
        forwarded = self._forward(envelope, params)
        if forwarded is not None:
            return forwarded
        return rpc.error_response(
            envelope.id,
            rpc.DAEMON_UNAVAILABLE,
            "the knowledge daemon is unavailable; the proxy could not reach or respawn it",
        )

    def _stamp_actor(self, params: Mapping[str, Any]) -> dict[str, Any]:
        stamped = dict(params)
        meta = stamped.get("_meta")
        meta_dict = dict(meta) if isinstance(meta, Mapping) else {}
        actor = meta_dict.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            meta_dict["actor"] = self._actor
        stamped["_meta"] = meta_dict
        return stamped

    # ------------------------------------------------------------- forwarding

    def _connect(self) -> None:
        result = ensure_daemon(self._config)
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.connect(str(result.socket_path))
        self._socket = connection
        self._socket_reader = rpc.FrameReader(connection.makefile("rb"), max_bytes=self._config.max_frame_bytes)

    def _forward(self, envelope: rpc.RequestEnvelope, params: Mapping[str, Any]) -> dict[str, Any] | None:
        """Forward with recovery: a dead daemon is respawned within a budget.

        A spawn under host load legitimately takes seconds, so the retry
        budget is time-based (spawn timeout + margin), not attempt-based.
        """
        deadline = time.monotonic() + self._config.spawn_timeout_seconds + 10.0
        while time.monotonic() < deadline:
            if self._socket is None or self._socket_reader is None:
                try:
                    self._connect()
                except Exception:  # noqa: BLE001 - retried until the deadline
                    time.sleep(0.2)
                    continue
            assert self._socket is not None and self._socket_reader is not None
            with self._socket_lock:  # heartbeat and forwards never interleave frames
                try:
                    message: dict[str, Any] = {"jsonrpc": "2.0", "id": envelope.id, "method": envelope.method}
                    if params:
                        message["params"] = dict(params)
                    self._socket.sendall(rpc.encode_message(message))
                    response = self._socket_reader.read()
                except ConnectionError, OSError, rpc.ProtocolError:
                    response = None
            if response is None:
                # Clean EOF or a broken pipe: the daemon is gone — drop the
                # connection and let the next pass respawn it.
                try:
                    self._socket.close()
                except OSError:
                    pass
                self._socket = None
                self._socket_reader = None
                time.sleep(0.1)
                continue
            return response
        return None

    def _lease_keepalive(self) -> None:
        """Renew the daemon session lease on the host's behalf (W7 #6).

        Real MCP hosts do not heartbeat; the proxy does it for them so a
        thinking agent never loses its slot between tool calls.
        """
        interval = max(0.2, self._config.session_lease_seconds / 3)
        while not self._dead.wait(interval):
            if self._socket is None or self._socket_reader is None:
                continue
            try:
                with self._socket_lock:
                    self._socket.sendall(
                        rpc.encode_message({"jsonrpc": "2.0", "id": "lease-hb", "method": "session.heartbeat"})
                    )
                    self._socket_reader.read()
            except ConnectionError, OSError, rpc.ProtocolError:
                continue  # the forward path owns recovery

    # ----------------------------------------------------------------- helpers

    def _send(self, message: Mapping[str, Any]) -> None:
        try:
            rpc.write_message(self._stdout, message)
        except OSError, BrokenPipeError:
            self._dead.set()

    def _ppid_watch(self) -> None:
        """Exit when the host process is gone (kill -9 reparents us).

        Reparenting to init (ppid 1) is itself proof of orphaning even when
        the early capture already observed the reparented parent.
        """
        while not self._dead.wait(self._config.ppid_poll_seconds):
            current = os.getppid()
            if current != self._ppid or current == 1:
                os._exit(0)


def run_proxy(
    config: DaemonConfig,
    *,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
    actor: str | None = None,
) -> int:
    return StdioProxy(config, stdin=stdin, stdout=stdout, actor=actor).run()
