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
