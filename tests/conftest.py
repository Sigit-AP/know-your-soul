"""Shared fixtures for the know-your-soul test suite."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture()
def sock_dir() -> Iterator[Path]:
    """A short-path directory for AF_UNIX socket files.

    POSIX limits ``sockaddr_un.sun_path`` to ~104 bytes on macOS (108 on
    Linux); pytest's ``tmp_path`` on CI runners (e.g.
    ``/Users/runner/work/.../pytest-of-runner/pytest-N/test_name0/``)
    exceeds that and fails binds with ``OSError: AF_UNIX path too long``.
    Bind test sockets under a short ``/tmp/kys-*`` path instead. Only the
    socket file lives here — stores and projections stay on ``tmp_path``.
    """
    base = Path("/tmp") if Path("/tmp").is_dir() else Path(tempfile.gettempdir())
    path = Path(tempfile.mkdtemp(prefix="kys-", dir=base))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
