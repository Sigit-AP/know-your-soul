"""Super Power Knowledge SOUL Graph: a deterministic, local-first knowledge system.

Standalone application package. It never loads model assets, provider SDKs,
credentials, embedding/vector backends, and never registers MCP sampling
handlers. See ``docs/adr/0001-standalone-deterministic-monolith.md``.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
