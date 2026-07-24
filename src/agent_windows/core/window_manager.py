"""Window management: minimize, maximize, restore, close, focus.

Direct Win32 ops (``ShowWindow`` / ``SetForegroundWindow`` /
``PostMessage(WM_CLOSE)``) by HWND/title/process-name — no target means the
foreground window — each with post-state confirmation. Replaces the indirect
``shortcut win+down`` convention (P4.2).

Reuses:
- :func:`resolve_window` (core.tree) for HWND/title/process/active resolution
- ``SW`` constants (uia.enums)
- the P0.4 foreground-blocked detection pattern (core.app_manager.switch_window)
"""
import ctypes
import ctypes.wintypes
import time
from typing import Any

from agent_windows.core.tree import resolve_window
from agent_windows.uia.enums import SW

user32 = ctypes.windll.user32
# HWND is pointer-sized; the default c_int restype truncates large handles on
# 64-bit, which would break the fg==hwnd foreground check in focus_window.
# IsWindow returns BOOL. (P0.4 — same restype fix as core.app_manager.)
user32.GetForegroundWindow.restype = ctypes.wintypes.HWND
user32.IsWindow.restype = ctypes.wintypes.BOOL

# EnumChildWindows callback signature (BOOL CALLBACK). Hosted as a module-level
# factory so enable_window can build a fresh pointer per call; EnumChildWindows
# must never reuse a ctypes callback object after it has been GC'd, but here the
# pointer is held alive by the call frame for the duration of the enumeration.
WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.HWND,
                                 ctypes.wintypes.LPARAM)

WM_CLOSE = 0x0010
# WM_CLOSE is asynchronous — the app receives it on its own message loop and
# may take a beat to tear down (or show a save-changes dialog). Poll IsWindow a
# few times instead of a single check so slow-to-close apps aren't misreported
# as "closed: no" while still winding down.
CLOSE_POLL_ATTEMPTS = 3
CLOSE_POLL_INTERVAL = 0.1


def _resolve(target: str) -> tuple[int, str, str | None]:
    """Resolve a target to ``(hwnd, title, warning)``.

    An empty target resolves to the foreground window (``resolve_window(
    active=True)``); otherwise the value is parsed as HWND / matched as a title
    or process-name substring by :func:`resolve_window`.
    """
    win, warn = (resolve_window(active=True) if not target
                 else resolve_window(value=target))
    return win.handle, win.title, warn


def _result(op: str, title: str, hwnd: int, state_line: str,
            warning: str | None, **siblings: Any) -> dict[str, Any]:
    """Build the uniform return dict: markdown ``output`` + ``hwnd`` + optional
    ``warning`` + op-specific state siblings (daemon-direct; MCP reads only
    ``output``)."""
    out = f'## Window {op}\nWindow: "{title}"\nHandle: {hwnd}\n{state_line}'
    if warning:
        out += f"\n{warning}"
    result: dict[str, Any] = {"output": out, "hwnd": hwnd}
    if warning:
        result["warning"] = warning
    result.update(siblings)
    return result


def minimize_window(target: str = "") -> dict[str, Any]:
    """Minimize a window (``ShowWindow(SW_MINIMIZE)``). Confirmed via IsIconic."""
    hwnd, title, warn = _resolve(target)
    user32.ShowWindow(hwnd, SW.Minimize)
    time.sleep(0.1)
    confirmed = bool(user32.IsIconic(hwnd))
    return _result("Minimize", title, hwnd,
                   "Minimized: yes" if confirmed else "Minimized: no",
                   warn, minimized=confirmed)


def maximize_window(target: str = "") -> dict[str, Any]:
    """Maximize a window (``ShowWindow(SW_MAXIMIZE)``). Confirmed via IsZoomed."""
    hwnd, title, warn = _resolve(target)
    user32.ShowWindow(hwnd, SW.Maximize)
    time.sleep(0.1)
    confirmed = bool(user32.IsZoomed(hwnd))
    return _result("Maximize", title, hwnd,
                   "Maximized: yes" if confirmed else "Maximized: no",
                   warn, maximized=confirmed)


def restore_window(target: str = "") -> dict[str, Any]:
    """Restore a window to Normal placement (``ShowWindow(SW_RESTORE)``).

    Confirmed via neither IsIconic nor IsZoomed (the Normal state).
    """
    hwnd, title, warn = _resolve(target)
    user32.ShowWindow(hwnd, SW.Restore)
    time.sleep(0.1)
    confirmed = not user32.IsIconic(hwnd) and not user32.IsZoomed(hwnd)
    return _result("Restore", title, hwnd,
                   "Restored: yes" if confirmed else "Restored: no",
                   warn, restored=confirmed)


def focus_window(target: str = "") -> dict[str, Any]:
    """Focus a window (restore-if-iconic, then ``SetForegroundWindow``).

    Uses the P0.4 pattern: ``SetForegroundWindow``'s BOOL is advisory —
    focus-stealing prevention can make it lie — so confirmation re-reads
    ``GetForegroundWindow`` and compares HWNDs.
    """
    hwnd, title, warn = _resolve(target)
    # Restore first if minimized — focus won't stick on an iconic window.
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW.Restore)
        time.sleep(0.1)
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    confirmed = (user32.GetForegroundWindow() == hwnd)
    state = ("Foreground: yes" if confirmed
             else "Foreground: no (blocked by focus-stealing prevention)")
    return _result("Focus", title, hwnd, state, warn,
                   foreground_confirmed=confirmed)


def close_window(target: str = "") -> dict[str, Any]:
    """Gracefully close a window (``PostMessage(WM_CLOSE)``).

    Lets the app save / show a "save changes?" dialog — the window-level polite
    close, distinct from ``process_kill`` (process-level ``taskkill``). No force
    flag: there is no clean window-level force API (``DestroyWindow`` is
    same-thread only). Confirmed gone via ``IsWindow(hwnd) == False``; an app
    that intercepts WM_CLOSE keeps the window → ``closed: no``.
    """
    hwnd, title, warn = _resolve(target)
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    gone = False
    for _ in range(CLOSE_POLL_ATTEMPTS):
        time.sleep(CLOSE_POLL_INTERVAL)
        if not bool(user32.IsWindow(hwnd)):
            gone = True
            break
    state = ("Closed: yes" if gone
             else "Closed: no (window remains — app may be prompting to save)")
    return _result("Close", title, hwnd, state, warn, closed=gone)


def _enable_subtree(hwnd: int) -> tuple[bool, int]:
    """Re-enable ``hwnd`` and any of its disabled descendants.

    Returns ``(root_was_disabled, children_reenabled)``. ``EnableWindow(hwnd,
    TRUE)`` the root only if it is currently disabled, then ``EnumChildWindows``
    walks the subtree — UWP/WinUI content is hosted in the frame's child
    windows, so a stranded disable can affect descendants too. Never disables
    anything; an already-enabled window (root or child) is left untouched.
    """
    root_was_disabled = not bool(user32.IsWindowEnabled(hwnd))
    children_reenabled = 0

    def _cb(child, _lparam):
        nonlocal children_reenabled
        if not user32.IsWindowEnabled(child):
            user32.EnableWindow(child, True)
            children_reenabled += 1
        return True

    if root_was_disabled:
        user32.EnableWindow(hwnd, True)
    user32.EnumChildWindows(hwnd, WNDENUMPROC(_cb), 0)
    return root_was_disabled, children_reenabled


def enable_window(target: str = "") -> dict[str, Any]:
    """Re-enable a window stranded disabled by an interrupted ``--background`` click.

    The EnableWindow-bypass ``finally`` restores the host window on a Python
    exception, but not on a process kill or a UIA Invoke that hangs — the window
    is left disabled (title-bar min/max/close still work, body buttons dead).
    This walks the root + children re-enabling anything disabled and reports
    how many it touched. Never disables. ``target`` resolves like the other
    window ops; no target = the foreground window.
    """
    hwnd, title, warn = _resolve(target)
    root_was_disabled, children_reenabled = _enable_subtree(hwnd)
    total = (1 if root_was_disabled else 0) + children_reenabled
    confirmed = bool(user32.IsWindowEnabled(hwnd))
    if total == 0:
        state = "Enabled: yes (already enabled)"
    else:
        parts = []
        if root_was_disabled:
            parts.append("root")
        if children_reenabled:
            parts.append(f"{children_reenabled} child window(s)")
        state = f"Re-enabled: {total} window(s) ({', '.join(parts)})"
    return _result("Enable", title, hwnd, state, warn,
                   enabled_count=total, enabled=confirmed)
