"""Application management: launch, switch, resize, list windows."""

import ctypes
import ctypes.wintypes
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32


def launch_app(name_or_path: str, args: list[str] | None = None) -> dict[str, Any]:
    """Launch an application by name or path."""
    cmd = [name_or_path] + (args or [])
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
        return {"output": f"## App Launch\nApp: {name_or_path}\nPID: {proc.pid}", "pid": proc.pid}
    except FileNotFoundError:
        raise ValueError(f"Application not found: {name_or_path}")


def switch_window(name_or_title: str) -> dict[str, Any]:
    """Switch to a window by title or process name (fuzzy match)."""
    import time

    hwnd = _find_window_by_title(name_or_title)
    if not hwnd:
        hwnd = _find_window_by_process(name_or_title)
    if not hwnd:
        raise ValueError(f"Window not found: {name_or_title}")

    # Restore if minimized
    SW_RESTORE = 9
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.1)

    # Bring to foreground
    user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)

    # Get window title
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)

    return {"output": f"## App Switch\nWindow: \"{buf.value}\"\nHandle: {hwnd}"}


def resize_window(name_or_title: str, width: int, height: int, x: int = -1, y: int = -1) -> dict[str, Any]:
    """Resize and optionally reposition a window."""
    hwnd = _find_window_by_title(name_or_title)
    if not hwnd:
        hwnd = _find_window_by_process(name_or_title)
    if not hwnd:
        raise ValueError(f"Window not found: {name_or_title}")

    # Get current rect
    rect = ctypes.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))

    new_x = x if x >= 0 else rect.left
    new_y = y if y >= 0 else rect.top

    SWP_NOZORDER = 0x0004
    user32.SetWindowPos(hwnd, 0, new_x, new_y, width, height, SWP_NOZORDER)

    return {"output": f"## App Resize\nWindow: \"{name_or_title}\"\nSize: {width}x{height}\nPosition: ({new_x}, {new_y})"}


def list_windows() -> dict[str, Any]:
    """List all visible top-level windows."""
    from agent_windows.core.tree import enumerate_windows

    windows = enumerate_windows()
    lines = ["## App List", "| # | Title | App | Position |", "|---|-------|-----|----------|"]
    for i, w in enumerate(windows, 1):
        pos = str(w.bbox) if w.bbox else "N/A"
        lines.append(f"| {i} | {w.title[:50]} | {w.process_name} | {pos} |")

    return {"output": "\n".join(lines), "count": len(windows)}


def _find_window_by_title(title: str) -> int:
    """Find a window handle by fuzzy title match."""
    target = title.lower()
    result = ctypes.c_void_p(0)

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM,
    )

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if target in buf.value.lower():
            result.value = hwnd
            return False  # Stop enumeration
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return result.value or 0


def _find_window_by_process(name: str) -> int:
    """Find a window handle by process name match."""
    target = name.lower().replace(".exe", "") + ".exe"
    result = ctypes.c_void_p(0)

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM,
    )

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc_name = _get_process_name(pid.value).lower()
        if target in proc_name:
            result.value = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return result.value or 0


def _get_process_name(pid: int) -> str:
    """Get process executable name from PID."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.wintypes.DWORD(260)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value)
    except Exception:
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
