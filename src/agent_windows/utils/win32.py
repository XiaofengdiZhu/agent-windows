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


def get_process_name(pid: int) -> str:
    """Get process executable name from PID via Win32 API."""
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
        logger.debug("Failed to get process name for pid %d", pid, exc_info=True)
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
