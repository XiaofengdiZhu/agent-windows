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

# Whether the CLI-side telemetry switch has been resolved for this process.
# Env > config; no flag for the CLI path (there are too many subcommands to add
# one to each). Resolved once, lazily, on the first dispatch — reading the
# config file every command would be wasteful. (TODO #9)
_telemetry_configured = False


def _ensure_telemetry_configured() -> None:
    """Resolve the CLI-side telemetry switch once (env > [telemetry] config).

    Idempotent: the first caller reads config + env and applies the boolean to
    ``telemetry.configure``; subsequent calls are a no-op. Local-only, default
    off. Lazy import of config/telemetry so the CLI import-latency guard (M-12)
    stays green — these are stdlib-only modules but keeping them lazy matches the
    project's discipline of never importing at dispatcher module top.
    """
    global _telemetry_configured
    if _telemetry_configured:
        return
    import os
    from agent_windows.telemetry import _parse_telemetry_enabled, configure
    try:
        from agent_windows.utils.config import load_config
        cfg = (load_config().get("telemetry") or {}).get("enabled", False)
    except Exception:
        cfg = False
    configure(_parse_telemetry_enabled(
        False, os.environ.get("AGENT_WINDOWS_TELEMETRY"), cfg))
    _telemetry_configured = True


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

    _ensure_telemetry_configured()

    # Local-only telemetry: time the tool call and record it (best-effort, off
    # by default). ``time`` is stdlib and the record import is lazy so this
    # module keeps the CLI import-latency guard (M-12) green. ``method`` is the
    # tool/method name; source="cli" covers both the daemon and --no-daemon
    # paths below. (TODO #9)
    import time
    start = time.perf_counter()
    success = True
    error_type: str | None = None
    try:
        if no_daemon:
            return _dispatch_no_daemon(method, params)
        return call_daemon(method, params, debug=debug)
    except BaseException as exc:
        success = False
        error_type = type(exc).__name__
        raise
    finally:
        from agent_windows.telemetry import record_tool_call
        record_tool_call(
            "cli", method, int((time.perf_counter() - start) * 1000),
            success, error_type)


def _dispatch_no_daemon(method: str, params: dict[str, Any] | None) -> dict[str, Any]:
    """Execute a method in-process (no daemon), mirroring daemon error codes.

    Direct execution mode (bypass daemon). Still routes through ``route_request``
    so handler errors map to the same JSON-RPC error codes
    (INVALID_PARAMS / WAIT_TIMEOUT / METHOD_NOT_FOUND /
    DAEMON_INTERNAL_ERROR) as the daemon path, and surface via the same
    ``unwrap_response`` — instead of leaking raw tracebacks or ad-hoc exits
    (M-11). Factored out of ``dispatch`` so the telemetry try/finally above can
    wrap both paths uniformly.
    """
    import ctypes
    import pythoncom

    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        logger.debug("DPI awareness setup failed (non-critical)", exc_info=True)
    # QUAL3-11: guard CoInitialize so a COM init failure surfaces as a clear
    # error instead of an uncaught traceback out of the no-daemon path.
    try:
        pythoncom.CoInitialize()
    except Exception as e:
        raise RuntimeError(f"COM initialization failed in no-daemon mode: {e}") from e
    try:
        from agent_windows.daemon.router import route_request
        from agent_windows.ipc.protocol import JSONRPCRequest

        request = JSONRPCRequest(id=1, method=method, params=params)
        return unwrap_response(route_request(request))
    finally:
        pythoncom.CoUninitialize()


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

    # Write to stdout, bypassing Windows' GBK console codepage via the raw
    # buffer with UTF-8. Robust to streams that have no ``.buffer`` (IDLE, test
    # captures, redirected text streams) and to characters the console codepage
    # can't encode: the echo fallback coerces through the stream's own encoding
    # with ``errors="replace"`` instead of raising ``UnicodeEncodeError`` (L-15).
    out = sys.stdout
    buf = getattr(out, "buffer", None)
    if buf is not None:
        try:
            buf.write(text.encode("utf-8", errors="replace"))
            buf.write(b"\n")
            buf.flush()
            return
        except Exception:
            logger.debug("stdout.buffer write failed; falling back to echo", exc_info=True)
    enc = getattr(out, "encoding", None) or "utf-8"
    # Coerce through the stream's own codepage first: every char it can't
    # encode becomes '?', so the subsequent typer.echo can't raise
    # UnicodeEncodeError. Crash-proof by construction ('?' is encodable
    # everywhere).
    typer.echo(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def _cmd(ctx: typer.Context, method: str, **params: Any) -> None:
    """Dispatch ``method`` to the daemon and print the result.

    The shared two-line tail every dispatch-backed CLI command repeats (H-11):
    package params, call the daemon, print. ``**params`` accepts inline kwargs
    (``_cmd(ctx, "click", x=x, y=y)``) or a pre-built dict
    (``_cmd(ctx, "type", **params)``), so commands that assemble params
    conditionally keep their logic verbatim. ``params`` is always a dict
    (``{}`` for no-arg commands), matching what every command passed before —
    never ``None``.
    """
    print_result(dispatch(method, params, ctx), ctx)
