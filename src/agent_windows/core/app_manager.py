"""Application management: launch, switch, resize, list windows."""

import ctypes
import ctypes.wintypes
import logging
import os
import subprocess
from typing import Any

from agent_windows.utils.win32 import get_process_name

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32

# Window enumeration callback type (defined once at module level)
WNDENUMPROC = ctypes.WINFUNCTYPE(
    ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM,
)

# Cache of friendly-name → AppID/path from Get-StartApps (TODO #18). Built once
# per process; the Start Menu barely changes mid-session, and Get-StartApps is a
# ~100ms powershell call we don't want to repeat per launch.
_START_APPS_CACHE: dict[str, str] | None = None


def _get_start_apps() -> dict[str, str]:
    """Build a friendly-name → AppID/path map via Get-StartApps (TODO #18).

    Covers both UWP (Store) apps and Start-Menu shortcuts. ``Get-StartApps`` is
    the only reliable enumerator for UWP apps (which have no .lnk). Result is
    cached for the process lifetime. Returns ``{}`` (best-effort) on failure —
    callers fall back to the existing PATH/exe launch path.
    """
    global _START_APPS_CACHE
    if _START_APPS_CACHE is not None:
        return _START_APPS_CACHE

    apps: dict[str, str] = {}
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | ConvertTo-Csv -NoTypeInformation"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
        )
        if result.returncode == 0 and result.stdout.strip():
            import csv
            import io
            reader = csv.DictReader(io.StringIO(result.stdout.strip()))
            for row in reader:
                name = (row.get("Name") or "").strip()
                appid = (row.get("AppID") or "").strip()
                if name and appid and name.lower() not in apps:
                    apps[name.lower()] = appid
    except Exception:
        # Don't cache the failure: a transient powershell error (timeout, lock)
        # shouldn't permanently disable friendly-name launch for the session.
        # Leave _START_APPS_CACHE as None so the next launch retries.
        logger.debug("Get-StartApps failed; will retry next launch", exc_info=True)
        return {}

    _START_APPS_CACHE = apps
    return apps


def _resolve_friendly_name(name: str) -> str | None:
    """Resolve a friendly app name (e.g. "calculator") to a launchable target.

    Case-insensitive exact match first, then a substring match (we don't use
    fuzzy matching — see TODO #19, deliberately dependency-free). Returns the
    AppID/path (for UWP) or the resolved exe path, or None if no match. The
    caller decides how to launch based on the shape of the returned target.
    """
    apps = _get_start_apps()
    if not apps:
        return None
    key = name.lower()
    # Exact name match wins; else first substring match.
    if key in apps:
        return apps[key]
    for app_name, appid in apps.items():
        if key in app_name or app_name in key:
            return appid
    return None


def launch_app(name_or_path: str, args: list[str] | None = None) -> dict[str, Any]:
    """Launch an application by name or path.

    Tries the existing PATH/exe path first (bare names like "notepad", or any
    real path). On FileNotFoundError, falls back to Start-Menu friendly-name
    resolution (TODO #18) so "calculator" / "edge" / UWP Store apps launch by
    friendly name even when they aren't on PATH.
    """
    # L-03: refuse to launch a binary planted in credential storage or the
    # agent's own install/data tree. A bare PATH-resolved name (e.g. "notepad")
    # passes through; only sensitive *locations* are blocked. ``args`` are
    # program arguments, not executables, so they are intentionally unchecked.
    from agent_windows.core.filesystem import validate_executable_path
    validate_executable_path(name_or_path)

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
        # Friendly-name fallback (TODO #18): resolve via the Start Menu.
        target = _resolve_friendly_name(name_or_path)
        if not target:
            raise ValueError(f"Application not found: {name_or_path}")
        return _launch_start_app_target(name_or_path, target)


def _launch_start_app_target(friendly: str, target: str) -> dict[str, Any]:
    """Launch a Start-Menu-resolved target.

    ``target`` is either an AppID (UWP — no backslash, not an existing path) or
    a Win32 exe/.lnk path. UWP apps launch via the shell AppsFolder; Win32 paths
    launch directly (args still apply). We don't capture a reliable PID for UWP
    (the surrogate process differs), so report 0 and let the caller observe the
    window instead.
    """
    is_uwp = not ("\\" in target or os.path.exists(target))
    try:
        if is_uwp:
            # shell:AppsFolder\{appid} is the canonical UWP launch target.
            subprocess.Popen(
                ["explorer.exe", f"shell:AppsFolder\\{target}"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
            )
            return {"output": f"## App Launch\nApp: {friendly} (UWP)\nAppID: {target}", "pid": 0}
        # Win32 path: launch directly (validate the resolved location too).
        from agent_windows.core.filesystem import validate_executable_path
        validate_executable_path(target)
        proc = subprocess.Popen(
            [target],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
        )
        return {"output": f"## App Launch\nApp: {friendly}\nPath: {target}\nPID: {proc.pid}",
                "pid": proc.pid}
    except FileNotFoundError:
        raise ValueError(f"Application not found: {friendly}")


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

    def _cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc_name = get_process_name(pid.value).lower()
        if target in proc_name:
            result.value = hwnd
            return False
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return result.value or 0
