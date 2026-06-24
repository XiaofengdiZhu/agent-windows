"""Local-only tool-call telemetry (TODO #9).

Records each tool invocation (source, tool name, success/failure, wall-clock
duration) as a single line in ``~/.agent-windows/logs/telemetry.log``.

Privacy stance — this deliberately diverges from the reference implementation
(Windows-MCP ``PostHogAnalytics``), which is ON by default with a hardcoded
cloud API key and ships every ``tool_name``/error to ``us.i.posthog.com``:

- **Zero network**: writes a local file only. No PostHog, no OTel, no outbound
  request of any kind. Nothing to exfiltrate.
- **Off by default**: out of the box this module records nothing; the user must
  opt in (``--telemetry`` / ``AGENT_WINDOWS_TELEMETRY`` / ``[telemetry] enabled``).
- **Data minimization**: only ``timestamp | source | tool | ok/fail | duration_ms``
  (+ exception *type name* on failure) is logged. No arguments, no outputs, no
  error messages, no client info.
- **Reuse existing ACL**: the file lives under ``logs/`` (see
  ``utils.config.get_log_dir``), which is locked to the owner by an inheritable
  DACL (M-01) — the same protection ``shell_audit.log`` enjoys — so this module
  adds no ACL code.

This module is pure stdlib at module top so importing it never pulls pywin32 /
comtypes / PIL / openai — keeping the CLI import-latency guard (M-12) green. The
only non-stdlib dependency is ``agent_windows.utils.config.get_log_dir`` (itself
stdlib-only at import).
"""
from __future__ import annotations

import datetime
import functools
import inspect
import logging
import time
from typing import TYPE_CHECKING, Any

from agent_windows.utils.config import get_log_dir

if TYPE_CHECKING:
    # Avoid importing fastmcp at module top so this module is importable from the
    # CLI/dispatcher import chain without pulling heavy deps (M-12). The runtime
    # callers (server.py) already import fastmcp; the annotation here is nominal.
    from fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Max active log size before it rotates to telemetry.log.1 (matches the hand-rolled
# single-backup scheme already used by shell_audit.log; no logging.RotatingFileHandler).
_MAX_BYTES = 1 * 1024 * 1024  # 1 MiB

# Process-local on/off, resolved once at startup from the flag/env/config
# precedence in ``_parse_telemetry_enabled``. Default False = record nothing.
_enabled: bool = False


def _escape(field: str) -> str:
    """Escape ``|``, newline, tab and backslash so a field can't split a log line.

    Mirrors shell_audit.log's escaping (backslash first, then the chars it
    protects). A malicious tool name carrying a ``|`` or ``\n`` can't inject
    fake fields this way; tab/CR are escaped too so a value can't smuggle
    whitespace that misaligns the fixed ``|``-delimited columns.
    """
    return (field.replace("\\", "\\\\")
                 .replace("|", "\\|")
                 .replace("\r", "\\r")
                 .replace("\n", "\\n")
                 .replace("\t", "\\t"))


def _rotate_if_needed(path: Any) -> None:
    """Best-effort rotate the active log to ``.1`` once it exceeds the cap.

    A rotation failure is swallowed (debug only): it must never block the record
    write below, which is the whole point of telemetry.
    """
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
    except OSError:
        logger.debug("Telemetry log rotation skipped", exc_info=True)


def record_tool_call(
    source: str,
    tool: str,
    duration_ms: int,
    success: bool,
    error_type: str | None = None,
) -> None:
    """Append one telemetry line for a tool call.

    ``source`` is ``"mcp"`` or ``"cli"``; ``tool`` is the tool/method name (e.g.
    ``"screenshot"``). ``success`` False means the call raised; ``error_type`` is
    then the exception's class name (no message). Does nothing when telemetry is
    disabled. Best-effort: a write failure is logged at debug and never raised —
    telemetry must not break the tool path it observes.
    """
    if not _enabled:
        return
    ts = datetime.datetime.now().isoformat(timespec="milliseconds")
    status = "ok" if success else "fail"
    line = f"{ts} | {_escape(source)} | {_escape(tool)} | {status} | {duration_ms}"
    if not success and error_type:
        line += f" | {_escape(error_type)}"
    line += "\n"
    try:
        path = get_log_dir() / "telemetry.log"
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:  # noqa: BLE001 — telemetry is best-effort, never fatal
        logger.debug("Failed to write telemetry log", exc_info=True)


def _parse_telemetry_enabled(
    flag: bool,
    env_value: str | None,
    config_value: Any,
) -> bool:
    """Resolve the telemetry on/off flag with flag > env > config precedence.

    A truthy CLI flag wins; else the ``AGENT_WINDOWS_TELEMETRY`` env var (one of
    ``1``/``true``/``yes``, case-insensitive); else the ``[telemetry] enabled``
    config bool. Default False. Kept as a pure function for unit testing.
    """
    if flag:
        return True
    if env_value is not None and env_value.lower() in ("1", "true", "yes"):
        return True
    return bool(config_value)


def configure(enabled: bool) -> None:
    """Set the process-local telemetry switch (called once at startup).

    Kept separate from parsing so callers can resolve precedence then apply; the
    CLI resolves via ``_parse_telemetry_enabled`` and passes the result here.
    """
    global _enabled
    _enabled = bool(enabled)


def is_enabled() -> bool:
    """Return whether telemetry is currently recording (mainly for tests)."""
    return _enabled


def _wrap_one(tool: Any) -> None:
    """Wrap a single ``FunctionTool``'s ``.fn`` with telemetry recording.

    Idempotent: a tool already carrying ``__telemetry_wrapped__`` is left alone,
    so a repeated ``run()`` in the same process doesn't stack wrappers (each
    extra layer would re-time the inner ones and double-count). ``functools.wraps``
    preserves the original signature so FastMCP's pydantic type adapter still
    derives the same parameter schema (verified on fastmcp 3.4.2: wrapping leaves
    ``tool.parameters`` unchanged). The wrapper is async and awaits the result
    only if it's awaitable, so both sync and async tool functions are covered.
    """
    if getattr(tool.fn, "__telemetry_wrapped__", False):
        return
    name = tool.name
    orig = tool.fn

    @functools.wraps(orig)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        try:
            res = orig(*args, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            record_tool_call(
                "mcp", name, int((time.perf_counter() - start) * 1000), True)
            return res
        except BaseException as exc:  # incl. asyncio.CancelledError — record, re-raise
            record_tool_call(
                "mcp", name, int((time.perf_counter() - start) * 1000),
                False, type(exc).__name__)
            raise

    wrapper.__telemetry_wrapped__ = True
    tool.fn = wrapper


def apply_tool_telemetry(mcp: "FastMCP", enabled: bool) -> None:
    """Wrap every registered tool on *mcp* with telemetry (MCP source).

    No-op when ``enabled`` is False (no wrapping at all, zero overhead). Handles
    both FastMCP registry shapes — older ``_tool_manager._tools`` and 3.x
    ``_local_provider._components["tool:<name>@"]`` — exactly like
    ``server.apply_tool_filter``. Call this *after* ``apply_tool_filter`` so
    filtered-out tools aren't wrapped (they can't be called anyway).
    """
    if not enabled:
        return
    tool_mgr = getattr(mcp, "_tool_manager", None)
    tools_dict = getattr(tool_mgr, "_tools", None)
    if tools_dict is not None:
        # Older FastMCP: _tools is name → FunctionTool
        for tool in tools_dict.values():
            _wrap_one(tool)
        return
    provider = getattr(mcp, "_local_provider", None)
    components = getattr(provider, "_components", {})
    for key, tool in components.items():
        if isinstance(key, str) and key.startswith("tool:"):
            _wrap_one(tool)
