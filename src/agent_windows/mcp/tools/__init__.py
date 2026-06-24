"""MCP tool modules — register_all(mcp) decorates every tool on a FastMCP instance.

Each module exposes plain ``*_fn`` callables (unit-tested directly) and a
``register(mcp)`` that decorates them as FastMCP tools with descriptions and
ToolAnnotations.
"""
from agent_windows.mcp.tools import (
    batch,
    control,
    input,
    observation,
    system,
    vision,
)

_MODULES = [observation, vision, input, system, batch, control]


def register_all(mcp) -> None:
    """Register every tool module on *mcp*."""
    for mod in _MODULES:
        mod.register(mcp)
