"""agent-windows MCP server subpackage.

Exposes ``build_server`` and ``run`` for the CLI ``mcp serve`` command. The
adapter helpers (``_call``/``_as_bool``/``xy_or_selector``) are intentionally
NOT re-exported at the package top level: re-exporting ``_call`` here would
shadow the submodule ``agent_windows.mcp._call`` (Python binds the imported
name over the submodule attribute), breaking ``import agent_windows.mcp._call``
and module-attribute access. Import them from ``agent_windows.mcp._call``
directly instead.
"""
from agent_windows.mcp.server import AuthKeyMiddleware, build_server, reset_server, run

__all__ = ["AuthKeyMiddleware", "build_server", "reset_server", "run"]
