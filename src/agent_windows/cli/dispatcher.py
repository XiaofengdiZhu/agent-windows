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

from agent_windows.ipc.client import call_daemon, unwrap_response, DaemonError

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


def _stamp_caller_local(params: dict[str, Any] | None) -> dict[str, Any]:
    """Inject the daemon-internal ``__caller='local'`` envelope (CLI attribution).

    The audit engine attributes each destructive op to a caller; the CLI path
    is always the local operator, so we stamp ``__caller='local'`` here. The
    daemon router pops it before handler dispatch (handlers never observe it),
    and the no-daemon path's ``route_request`` does the same — mirroring the MCP
    edge (``mcp/_call.py``), which resolves the caller from the live ASGI scope.

    Idempotent and safe to call on both paths: ``dispatch`` stamps before
    forwarding (covering ``call_daemon``), and ``_dispatch_no_daemon`` stamps
    again because it is also reachable directly (e.g. tests), so the injection
    must not depend on passing through ``dispatch``.

    No ``core.audit`` import here — the value is just a string, and importing
    audit would regress the CLI import-latency guard (M-12). (audit)
    """
    if params is None:
        params = {}
    params["__caller"] = "local"
    return params


def dispatch(
    method: str,
    params: dict[str, Any] | None = None,
    ctx: typer.Context | None = None,
) -> dict[str, Any]:
    """Send a command to the daemon and return the result.

    Args:
        method: Command method name (e.g. "screenshot", "click").
        params: Command parameters.
        ctx: Typer context (for --no-daemon, --format flags).

    Returns:
        The result dict from the daemon.
    """
    no_daemon = False
    fmt = "text"

    if ctx and ctx.obj:
        no_daemon = ctx.obj.get("no_daemon", False)
        fmt = ctx.obj.get("format", "text")

    _ensure_telemetry_configured()
    params = _stamp_caller_local(params)

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
        return call_daemon(method, params)
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
    import pythoncom

    # _dispatch_no_daemon is also reachable directly (not only via dispatch),
    # so stamp __caller here too — the injection must not depend on having gone
    # through dispatch(). Idempotent with dispatch()'s stamp. (audit)
    params = _stamp_caller_local(params)

    try:
        from agent_windows.core.dpi import ensure_process_dpi_awareness
        result = ensure_process_dpi_awareness(2)
        if result.ok:
            logger.debug("DPI awareness: per-monitor (actual=%d) %s",
                         result.actual, result.detail)
        else:
            logger.warning(
                "DPI awareness mismatch: requested=2 actual=%d — "
                "coordinates may be virtualized (detail=%s)",
                result.actual, result.detail)
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


def _write_output(text: str) -> None:
    """Write ``text`` to stdout, GBK-safe (the shared print primitive).

    Bypasses Windows' GBK console codepage via the raw buffer with UTF-8.
    Robust to streams that have no ``.buffer`` (IDLE, test captures, redirected
    text streams) and to characters the console codepage can't encode: the echo
    fallback coerces through the stream's own encoding with ``errors="replace"``
    instead of raising ``UnicodeEncodeError`` (L-15). Used by both
    :func:`print_result` (success path) and the error boundary (error path) so
    ``--format json`` works identically on both.
    """
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


def _format(ctx: typer.Context | None) -> str:
    """Read the --format option from the Typer context (default 'text')."""
    if ctx and ctx.obj:
        return ctx.obj.get("format", "text")
    return "text"


def print_result(result: dict[str, Any], ctx: typer.Context | None = None) -> None:
    """Print a result dict as formatted text or JSON.

    Handles Windows console encoding (GBK) gracefully by writing directly
    to stdout buffer with UTF-8 encoding.
    """
    fmt = _format(ctx)

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

    _write_output(text)


def _exit_code_for_daemon_error(code: int) -> int:
    """Map a JSON-RPC error code to a process exit code.

    Mirrors the Rust launcher's ``exit_code_for`` (launcher/src/main.rs) so the
    Python and native entry points agree on exit semantics: protocol errors
    (the JSON-RPC 2.0 reserved range -32700..=-32000, e.g. PARSE_ERROR /
    INVALID_REQUEST / METHOD_NOT_FOUND / INVALID_PARAMS) exit 2; application
    errors (ELEMENT_NOT_FOUND / WAIT_TIMEOUT / PERMISSION_DENIED /
    DAEMON_INTERNAL_ERROR, the -1001..=-1999 range) exit 1. A script can thus
    tell "bad params / unknown method" (2) from "element not found / timed out"
    (1) regardless of which binary it invoked.
    """
    if -32700 <= code <= -32000:
        return 2  # protocol error
    return 1      # application error


def _print_error(exc: BaseException, ctx: typer.Context | None) -> None:
    """Surface a dispatch error honoring ``--format``, then exit non-zero.

    - ``ConnectionError`` (daemon unreachable / died during startup) → a clear
      one-line message, exit 2 (infrastructure failure, distinct from an app
      error so scripts/agents can tell "daemon down" from "command failed").
    - ``DaemonError`` (the daemon returned a JSON-RPC error) → the daemon's own
      message + code, exit code derived from ``.code`` via
      :func:`_exit_code_for_daemon_error` (protocol range → 2, app range → 1),
      matching the Rust launcher so both entry points agree.
    In ``--format json`` mode both emit a ``{"error": ..., "code": ...}`` object
    so JSON consumers get structured errors instead of a raw traceback (which
    previously escaped unwrapped from ``__main__`` and dropped the --format flag).
    Other exceptions are not caught here — let them propagate so genuinely
    unexpected bugs still surface as tracebacks during development.
    """
    fmt = _format(ctx)
    if isinstance(exc, ConnectionError):
        exit_code = 2
        message = str(exc) or "Could not connect to the daemon"
        code = None
    elif isinstance(exc, DaemonError):
        message = exc.message
        code = exc.code
        exit_code = _exit_code_for_daemon_error(code)
    else:  # pragma: no cover - unexpected; re-raised by the caller
        raise exc

    if fmt == "json":
        import json
        payload = {"error": message}
        if code is not None:
            payload["code"] = code
        _write_output(json.dumps(payload, ensure_ascii=False))
    else:
        _write_output(f"Error: {message}")
    raise typer.Exit(code=exit_code)


def _cmd(ctx: typer.Context, method: str, **params: Any) -> None:
    """Dispatch ``method`` to the daemon and print the result.

    The shared two-line tail every dispatch-backed CLI command repeats (H-11):
    package params, call the daemon, print. ``**params`` accepts inline kwargs
    (``_cmd(ctx, "click", x=x, y=y)``) or a pre-built dict
    (``_cmd(ctx, "type", **params)``), so commands that assemble params
    conditionally keep their logic verbatim. ``params`` is always a dict
    (``{}`` for no-arg commands), matching what every command passed before —
    never ``None``.

    Wraps dispatch in an error boundary: a daemon/connection error is surfaced
    as a one-line message (or a JSON error object under ``--format json``) with
    a non-zero exit, instead of an uncaught Python traceback. (review #4)
    """
    try:
        result = dispatch(method, params, ctx)
    except (ConnectionError, DaemonError) as exc:
        _print_error(exc, ctx)
        return  # typer.Exit raised above; unreachable, keeps type checkers calm
    print_result(result, ctx)
