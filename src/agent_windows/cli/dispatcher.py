"""Command dispatcher: sends CLI commands to daemon via IPC.

All CLI commands go through this dispatcher which:
1. Packages the command as a JSON-RPC request
2. Sends it to the daemon
3. Returns/formats the result
"""

import logging
import sys
from typing import Any

import typer

logger = logging.getLogger(__name__)

from agent_windows.ipc.client import call_daemon, unwrap_response


def dispatch(
    method: str,
    params: dict[str, Any] | None = None,
    ctx: typer.Context | None = None,
) -> dict[str, Any]:
    """Send a command to the daemon and return the result.

    Args:
        method: Command method name (e.g. "screenshot", "click").
        params: Command parameters.
        ctx: Typer context (for --no-daemon, --format, --debug flags).

    Returns:
        The result dict from the daemon.
    """
    no_daemon = False
    debug = False
    fmt = "text"

    if ctx and ctx.obj:
        no_daemon = ctx.obj.get("no_daemon", False)
        debug = ctx.obj.get("debug", False)
        fmt = ctx.obj.get("format", "text")

    if no_daemon:
        # Direct execution mode (bypass daemon). Still route through
        # route_request so handler errors map to the same JSON-RPC error codes
        # (INVALID_PARAMS / WAIT_TIMEOUT / METHOD_NOT_FOUND /
        # DAEMON_INTERNAL_ERROR) as the daemon path, and surface via the same
        # unwrap_response — instead of leaking raw tracebacks or ad-hoc exits
        # (M-11).
        import ctypes
        import pythoncom

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            logger.debug("DPI awareness setup failed (non-critical)", exc_info=True)
        pythoncom.CoInitialize()
        try:
            from agent_windows.daemon.router import route_request
            from agent_windows.ipc.protocol import JSONRPCRequest

            request = JSONRPCRequest(id=1, method=method, params=params)
            return unwrap_response(route_request(request))
        finally:
            pythoncom.CoUninitialize()

    return call_daemon(method, params, no_daemon=no_daemon, debug=debug)


def print_result(result: dict[str, Any], ctx: typer.Context | None = None) -> None:
    """Print a result dict as formatted text or JSON.

    Handles Windows console encoding (GBK) gracefully by writing directly
    to stdout buffer with UTF-8 encoding.
    """
    fmt = "text"
    if ctx and ctx.obj:
        fmt = ctx.obj.get("format", "text")

    if fmt == "json":
        import json
        text = json.dumps(result, ensure_ascii=False, indent=2)
    else:
        output = result.get("output")
        if output:
            text = output
        else:
            lines = []
            for key, value in result.items():
                if key == "output":
                    continue
                lines.append(f"{key}: {value}")
            text = "\n".join(lines)

    # Write to stdout buffer with UTF-8, bypassing Windows GBK console encoding
    try:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace"))
        sys.stdout.buffer.write(b"\n")
        sys.stdout.buffer.flush()
    except Exception:
        # Fallback: let typer handle it (may fail on non-UTF-8 characters)
        typer.echo(text)
