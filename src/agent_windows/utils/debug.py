"""Global debug-state for agent-windows (TODO #21-3).

A single source of truth — the ``AGENT_WINDOWS_DEBUG`` environment variable —
read by :func:`is_debug` and written by :func:`enable_debug`. Because env vars
are inherited by subprocesses, a spawned daemon picks up debug mode without
the CLI having to thread a ``--debug`` boolean through every IPC layer.

Any module can gate verbose diagnostic output on :func:`is_debug` without
receiving a debug flag as a parameter.
"""
import logging
import os

_DEBUG_ENV = "AGENT_WINDOWS_DEBUG"
_TRUTHY = ("1", "true", "yes", "on")
_VERBOSE_LOGGERS = ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access")


def is_debug() -> bool:
    """Return True if debug mode is enabled via ``AGENT_WINDOWS_DEBUG``.

    Truthy values (case-insensitive): ``1``, ``true``, ``yes``, ``on``.
    Unset or any other value is False.
    """
    return os.getenv(_DEBUG_ENV, "").lower() in _TRUTHY


def enable_debug() -> None:
    """Enable debug mode by setting ``AGENT_WINDOWS_DEBUG=true``. Idempotent."""
    os.environ[_DEBUG_ENV] = "true"


def configure_logging(component: str = "") -> None:
    """When :func:`is_debug`, raise the root logger + verbose third-party
    loggers to ``DEBUG``; otherwise do nothing.

    Consolidates the inline logging config previously duplicated in
    ``mcp/server.py:run`` and provides it for the ``--no-daemon`` path (which
    previously had no debug logging at all). Idempotent: safe to call
    repeatedly; uses ``setLevel`` (no handler accumulation).

    NOT used by the daemon: ``daemon/server.py:_setup_logging`` has its own
    FileHandler+StreamHandler structure (writes daemon.log) and reads
    :func:`is_debug` for the level decision itself.

    Args:
        component: optional label for a one-time startup log line.
    """
    if not is_debug():
        return
    logging.getLogger().setLevel(logging.DEBUG)
    for name in _VERBOSE_LOGGERS:
        logging.getLogger(name).setLevel(logging.DEBUG)
