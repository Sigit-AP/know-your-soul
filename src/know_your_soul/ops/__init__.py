"""Operational (OPS) commands: operator-facing tooling, never knowledge ops.

Modules here back CLI subcommands such as ``export``/``analytics`` (ATM
F-D1). They are deliberately *not* part of the frozen fourteen-operation
knowledge surface and are never registered into ``http_api.OPS_OPERATIONS``
or the MCP tool list — they run only in the operator's own process.
"""
