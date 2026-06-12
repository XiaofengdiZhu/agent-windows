"""Shared Win32 utility functions."""
import ctypes
import ctypes.wintypes
import os


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
        return ""
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)
