"""EnableWindow-bypass for no-focus-steal UIA pattern delivery.

Ports cua-driver's ``uia/fg_bypass.rs:96 run_with_uwp_bypass``: a UIA pattern
(Invoke/Toggle/Select) travels over the kernel accessibility channel, not the
input queue that ``EnableWindow`` gates — so wrapping the pattern call in
``EnableWindow(host, FALSE)…()…(host, TRUE)`` lets the action fire while the
host top-level window *cannot* become foreground. No focus steal, no flash.

Heavy imports (comtypes/uia) are kept inside function bodies so importing this
module (which ``daemon/router`` does at load) stays cheap.
"""

from __future__ import annotations

import ctypes
from typing import Callable, TypeVar

T = TypeVar("T")


class BackgroundUnavailableError(Exception):
    """Background delivery cannot land; the caller should retry foreground.

    Raised when the target exposes no Invoke/Toggle/Select pattern, no live
    control resolves, the host window has no HWND, or a pattern COM call fails.
    Surfaces as BACKGROUND_UNAVAILABLE (-1007) via ``route_request``.
    """


def _enable_window(hwnd: int, enable: bool) -> int:
    """Win32 ``EnableWindow``; returns the prior enabled state (nonzero=enabled)."""
    user32 = ctypes.windll.user32
    user32.EnableWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.EnableWindow.restype = ctypes.c_int
    return user32.EnableWindow(ctypes.c_void_p(hwnd), 1 if enable else 0)


def run_with_uia_bypass(host_hwnd: int, action: Callable[[], T]) -> T:
    """Run ``action`` with the host top-level window briefly disabled.

    ``EnableWindow(host, FALSE)`` → ``action()`` → ``EnableWindow(host, <prior>)``
    in a ``finally`` so the window is always restored, even if ``action`` raises.
    Disabling the host prevents it from becoming foreground while the UIA
    pattern (sent over the accessibility channel) fires.

    Args:
        host_hwnd: HWND of the top-level window owning the target element.
        action: zero-arg callable performing the UIA pattern call.

    Returns:
        Whatever ``action`` returns.
    """
    prev = _enable_window(host_hwnd, False)
    try:
        return action()
    finally:
        _enable_window(host_hwnd, bool(prev))


def _host_hwnd_of(control) -> int:
    """HWND of the top-level WindowControl ancestor of ``control``.

    Walks the UIA tree up via ``Control.GetAncestorControl`` to the nearest
    window and reads its ``NativeWindowHandle``. Raises
    ``BackgroundUnavailableError`` if there is no window ancestor or it has no
    HWND.
    """
    window = control.GetAncestorControl(
        lambda c, _depth: c.ControlTypeName == "WindowControl")
    if window is None:
        raise BackgroundUnavailableError(
            "target has no window ancestor; retry without --background")
    hwnd = window.NativeWindowHandle
    if not hwnd:
        raise BackgroundUnavailableError(
            "host window has no HWND; retry without --background")
    return hwnd


def _invoke_via_pattern(control) -> str:
    """Fire the first available of Invoke → Toggle → Select on ``control``.

    Returns the action name that fired. Raises ``BackgroundUnavailableError``
    if none is supported or a pattern COM call fails.
    """
    from _ctypes import COMError
    from agent_windows.uia.enums import PatternId

    try:
        invoke = control.GetPattern(PatternId.InvokePattern)
        if invoke is not None:
            invoke.Invoke()
            return "invoke"
        toggle = control.GetPattern(PatternId.TogglePattern)
        if toggle is not None:
            toggle.Toggle()
            return "toggle"
        select = control.GetPattern(PatternId.SelectionItemPattern)
        if select is not None:
            select.Select()
            return "select"
    except COMError as e:
        raise BackgroundUnavailableError(
            f"background pattern call failed: {e}") from e
    raise BackgroundUnavailableError(
        "element exposes no Invoke/Toggle/Select pattern; retry without --background")


def invoke_background(x: int, y: int, selector: str | None) -> dict:
    """Deliver a background click via UIA pattern inside an EnableWindow bypass.

    Args:
        x, y: screen coordinates (used to resolve a control when ``selector``
            is None, and for echo/verify by the caller).
        selector: ``@eN`` element ref (preferred); None ⇒ ``ControlFromPoint``.

    Returns:
        ``{"action": "invoke"|"toggle"|"select", "host_hwnd": <int>}``.

    Raises:
        BackgroundUnavailableError: no live control, no host HWND, no supported
            pattern, or a pattern COM failure.
    """
    from agent_windows.uia.controls import ControlFromPoint

    if selector:
        from agent_windows.core.tree import _resolve_live_control
        control = _resolve_live_control(selector)
    else:
        control = ControlFromPoint(x, y)
        if control is None:
            raise BackgroundUnavailableError(
                f"no UIA element at ({x}, {y}); retry without --background")

    host_hwnd = _host_hwnd_of(control)
    action = run_with_uia_bypass(host_hwnd, lambda: _invoke_via_pattern(control))
    return {"action": action, "host_hwnd": host_hwnd}
