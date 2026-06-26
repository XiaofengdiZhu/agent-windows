"""Shared adapter: MCP tool → daemon JSON-RPC call.

Every MCP tool is a thin wrapper that builds the same params dict the CLI
command builds and forwards it to the daemon via call_daemon. This module is
the single seam, so tools stay declarative and the daemon stays the single
source of truth for automation logic.
"""

from typing import Any

from agent_windows.ipc.client import call_daemon


def _as_bool(value: Any) -> bool:
    """Coerce a possibly-string bool to a real bool.

    Claude Desktop (and some MCP clients) serialize ``bool`` params as the
    strings ``"true"``/``"false"`` rather than JSON booleans. Mirrors
    reference/Windows-MCP's ``_as_bool`` so a tool's default-true flag still
    works regardless of how the client encoded it.
    """
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _call(method: str, **params: Any) -> dict[str, Any]:
    """Forward a daemon method call and return its result dict.

    ``None`` params are dropped: optional kwargs the caller didn't pass must
    not reach the daemon as ``null`` (some handlers treat a present-None as
    "explicitly empty"). A daemon error (``DaemonError`` — a ``RuntimeError``
    subclass carrying ``.code`` — from :func:`unwrap_response`) propagates
    unchanged: FastMCP surfaces it as a tool error, and the message already
    carries the JSON-RPC code. The MCP layer does not map ``.code`` itself;
    typed handling lives at the CLI boundary (cli/dispatcher._print_error).
    """
    clean = {k: v for k, v in params.items() if v is not None}
    return call_daemon(method, clean)


def xy_or_selector(loc: list[int] | None, selector: str | None) -> dict[str, Any]:
    """Resolve the common "x,y OR @eN selector" mouse-arg pattern.

    Exactly one must be given (matches ``cli/commands/input.py:_xy_or_selector``
    and ``daemon/handlers/input.py:_coords``: selector overrides coordinates).
    Returns ``{"selector": s}`` or ``{"x": .., "y": ..}`` ready to splat.
    """
    has_loc = loc is not None
    has_sel = bool(selector)
    if has_loc and has_sel:
        raise ValueError("Provide either loc ([x, y]) or selector (@eN), not both.")
    if not has_loc and not has_sel:
        raise ValueError("Either loc ([x, y]) or selector (@eN) is required.")
    if has_sel:
        return {"selector": selector}
    if len(loc) != 2:  # type: ignore[arg-type]
        raise ValueError("loc must be a list of exactly two integers [x, y]")
    return {"x": loc[0], "y": loc[1]}
