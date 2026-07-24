"""Window handlers: minimize/maximize/restore/close/focus (P4.2).

Thin wrappers over core.window_manager. The first four are non-destructive
(just return the core dict); window_close is destructive and runs under
_run_audited (audit trail), like process_kill.
"""
from typing import Any

from agent_windows.daemon.router import handler


def _target(params: dict) -> str:
    """Resolve the optional ``target`` parameter (HWND/title/process-name).

    Absent or None → "" (the foreground window, per core.window_manager).
    Non-str → ValueError → INVALID_PARAMS (-32602) via route_request.
    """
    target = params.get("target")
    if target is None:
        return ""
    if not isinstance(target, str):
        raise ValueError(
            f"parameter 'target' must be str, got {type(target).__name__}")
    return target


@handler("window_minimize")
def _window_minimize(params: dict) -> dict[str, Any]:
    """Minimize a window (no target = foreground window)."""
    from agent_windows.core.window_manager import minimize_window
    return minimize_window(_target(params))


@handler("window_maximize")
def _window_maximize(params: dict) -> dict[str, Any]:
    """Maximize a window (no target = foreground window)."""
    from agent_windows.core.window_manager import maximize_window
    return maximize_window(_target(params))


@handler("window_restore")
def _window_restore(params: dict) -> dict[str, Any]:
    """Restore a window to Normal placement (no target = foreground window)."""
    from agent_windows.core.window_manager import restore_window
    return restore_window(_target(params))


@handler("window_focus")
def _window_focus(params: dict) -> dict[str, Any]:
    """Focus a window (no target = foreground window)."""
    from agent_windows.core.window_manager import focus_window
    return focus_window(_target(params))


@handler("window_enable")
def _window_enable(params: dict) -> dict[str, Any]:
    """Re-enable a window stranded disabled by an interrupted --background click.

    Non-destructive recovery: re-enables the root + disabled descendants.
    ``target`` is the only parameter.
    """
    from agent_windows.core.window_manager import enable_window
    return enable_window(_target(params))


@handler("window_close")
def _window_close(params: dict) -> dict[str, Any]:
    """Gracefully close a window via PostMessage(WM_CLOSE).

    Audited (destructive): lets the app save / prompt. ``target`` is the only
    parameter, so it is the audit subset (no sensitive value to scrub).
    """
    from agent_windows.core.window_manager import close_window
    from agent_windows.daemon.handlers.system import _run_audited
    target = _target(params)
    return _run_audited("window_close", {"target": target}, close_window, target)
