"""Command dispatcher: sends CLI commands to daemon via IPC.

All CLI commands go through this dispatcher which:
1. Packages the command as a JSON-RPC request
2. Sends it to the daemon
3. Returns/formats the result
"""

import sys
from typing import Any

import typer

from agent_windows.ipc.client import call_daemon


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
        # Direct execution mode (bypass daemon)
        # Initialize COM + DPI for in-process execution
        import ctypes
        import pythoncom

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        pythoncom.CoInitialize()
        try:
            from agent_windows.daemon.router import HANDLERS

            handler_func = HANDLERS.get(method)
            if handler_func is None:
                typer.echo(f"Error: unknown method '{method}'", err=True)
                raise typer.Exit(1)
            return handler_func(params or {})
        finally:
            pythoncom.CoUninitialize()

    return call_daemon(method, params, no_daemon=no_daemon, debug=debug)


def print_result(result: dict[str, Any], ctx: typer.Context | None = None) -> None:
    """Print a result dict as formatted text or JSON."""
    fmt = "text"
    if ctx and ctx.obj:
        fmt = ctx.obj.get("format", "text")

    if fmt == "json":
        import json
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        # Text format: render the "output" field if present, else dump keys
        output = result.get("output")
        if output:
            typer.echo(output)
        else:
            # Fallback: render key-value pairs
            for key, value in result.items():
                if key == "output":
                    continue
                typer.echo(f"{key}: {value}")
