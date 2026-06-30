"""Shared Win32 utility functions."""
import ctypes
import ctypes.wintypes
import logging
import os

logger = logging.getLogger(__name__)


def is_elevated() -> bool:
    """Return True if the current process runs with administrative privileges.

    Uses ``shell32.IsUserAnAdmin`` (deprecated by Microsoft but still the
    simplest reliable elevation check; the token-API alternative is heavier for
    no practical gain here). Returns False on non-Windows or if the Win32 API
    is unavailable, so callers can treat the hint as best-effort.
    """
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except (AttributeError, OSError):
        logger.debug("IsUserAnAdmin unavailable; assuming not elevated", exc_info=True)
        return False


# Appended to access-denied errors when the server isn't running elevated, so
# an opaque "Access is denied" carries an actionable fix. (TODO #14) Worded to
# fit both shell commands and filesystem operations.
ELEVATION_HINT = (
    "This may require an elevated (Administrator) terminal; "
    "the agent-windows server is not currently running elevated."
)


def elevation_hint() -> str | None:
    """Return the elevation hint string when NOT elevated, else ``None``.

    Lets callers enrich access-denied errors with actionable guidance without
    each repeating the ``is_elevated()`` check. Best-effort: on non-Windows or
    if the API is unavailable, ``is_elevated`` returns False, so the hint is
    shown — the safe default, since most MCP servers run non-elevated.
    """
    return None if is_elevated() else ELEVATION_HINT


def get_process_name(pid: int) -> str:
    """Get process executable name from PID via Win32 API."""
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_INSUFFICIENT_BUFFER = 122
    k32 = ctypes.windll.kernel32
    # Declare the prototypes so a 64-bit HANDLE round-trips untruncated. ctypes
    # otherwise defaults OpenProcess's return to c_int (32-bit), truncating a
    # handle with the high bit set — then CloseHandle runs on the truncated
    # pointer and silently leaks. (G4)
    k32.OpenProcess.restype = ctypes.wintypes.HANDLE
    k32.OpenProcess.argtypes = [
        ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD,
    ]
    k32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
    k32.QueryFullProcessImageNameW.argtypes = [
        ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD,
        ctypes.wintypes.LPWSTR, ctypes.POINTER(ctypes.wintypes.DWORD),
    ]
    # CloseHandle's argtype must be HANDLE too: now that OpenProcess returns a
    # real 64-bit HANDLE (restype above), passing it to a function whose
    # argtype defaults to c_int raises ``ctypes.ArgumentError: OverflowError``
    # for any handle value > 2**32 — out of the ``finally`` block here, which
    # would mask the real result. (G4)
    k32.CloseHandle.restype = ctypes.wintypes.BOOL
    k32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        # Start at MAX_PATH; a process image path can exceed it, so on
        # ERROR_INSUFFICIENT_BUFFER grow the buffer and retry (QueryFullProcessImageNameW
        # reports the required length via *size). The earlier code ignored the
        # return value and could use a truncated/empty buffer for long paths.
        capacity = 260
        buf = ctypes.create_unicode_buffer(capacity)
        size = ctypes.wintypes.DWORD(capacity)
        ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok and k32.GetLastError() == ERROR_INSUFFICIENT_BUFFER:
            capacity = max(size.value + 1, capacity * 2)
            buf = ctypes.create_unicode_buffer(capacity)
            size = ctypes.wintypes.DWORD(capacity)
            ok = k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value)
    except Exception:
        logger.debug("Failed to get process name for pid %d", pid, exc_info=True)
        return ""
    finally:
        k32.CloseHandle(handle)
