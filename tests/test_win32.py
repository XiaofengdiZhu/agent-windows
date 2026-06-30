"""Unit tests for utils/win32.py — the shared get_process_name and is_elevated
helpers.

get_process_name is shared by core/app_manager.py and core/tree.py (extracted in
the QUAL-03 dedup). It had no direct test — only indirect coverage via the
callers, which mock it out. This locks the Win32 plumbing: OpenProcess →
QueryFullProcessImageNameW → basename → CloseHandle, plus the failure paths.
(TEST3-04)

is_elevated wraps shell32.IsUserAnAdmin for elevation-aware hints (TODO #14).
"""
import os
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.utils import win32


def test_returns_basename_of_process_image(monkeypatch):
    """Happy path: OpenProcess → QueryFullProcessImageNameW → basename → CloseHandle."""
    kernel = MagicMock()
    kernel.OpenProcess.return_value = 12345  # nonzero handle
    # Fill the caller-provided buffer with a path; the helper takes the basename.
    def _fill(handle, flags, buf, size_ref):
        buf.value = r"C:\Windows\System32\notepad.exe"
        size_ref._obj.value = len(buf.value)
        return True
    kernel.QueryFullProcessImageNameW.side_effect = _fill
    monkeypatch.setattr(win32.ctypes.windll, "kernel32", kernel)

    assert win32.get_process_name(9999) == "notepad.exe"
    # Lock the exact Win32 call args — PROCESS_QUERY_LIMITED_INFORMATION (0x1000),
    # inherit=False, and the PID. A flag regression (e.g. 0x400) must fail here.
    kernel.OpenProcess.assert_called_once_with(0x1000, False, 9999)
    # QueryFullProcessImageNameW(handle, flags=0, buf, size_ref)
    assert kernel.QueryFullProcessImageNameW.call_args.args[0] == 12345
    assert kernel.QueryFullProcessImageNameW.call_args.args[1] == 0
    kernel.CloseHandle.assert_called_once_with(12345)


def test_returns_empty_string_when_open_process_fails(monkeypatch):
    """OpenProcess returns 0 (no such PID / no access) → "" without querying."""
    kernel = MagicMock()
    kernel.OpenProcess.return_value = 0
    monkeypatch.setattr(win32.ctypes.windll, "kernel32", kernel)

    assert win32.get_process_name(9999) == ""
    kernel.QueryFullProcessImageNameW.assert_not_called()
    kernel.CloseHandle.assert_not_called()  # nothing to close


def test_query_failure_returns_empty_and_still_closes_handle(monkeypatch):
    """If QueryFullProcessImageNameW raises, the helper logs, returns "" and
    still closes the handle (finally guarantees resource cleanup)."""
    kernel = MagicMock()
    kernel.OpenProcess.return_value = 777
    kernel.QueryFullProcessImageNameW.side_effect = OSError("access denied")
    monkeypatch.setattr(win32.ctypes.windll, "kernel32", kernel)

    assert win32.get_process_name(4321) == ""
    kernel.CloseHandle.assert_called_once_with(777)


def test_handles_path_without_directory(monkeypatch):
    """A bare executable name (no directory) is returned unchanged by basename."""
    kernel = MagicMock()
    kernel.OpenProcess.return_value = 1
    def _fill(handle, flags, buf, size_ref):
        buf.value = "explorer.exe"
        size_ref._obj.value = len(buf.value)
        return True
    kernel.QueryFullProcessImageNameW.side_effect = _fill
    monkeypatch.setattr(win32.ctypes.windll, "kernel32", kernel)

    assert win32.get_process_name(1) == "explorer.exe"


def test_grows_buffer_on_insufficient_buffer(monkeypatch):
    """A path longer than the initial 260-char buffer: the first call returns
    FALSE with ERROR_INSUFFICIENT_BUFFER (122) and reports the required length;
    the helper grows the buffer and retries, returning the basename. Locks the
    return-value check that was previously ignored."""
    kernel = MagicMock()
    kernel.OpenProcess.return_value = 12345
    # The real image path is longer than 260 chars (deeply nested install dir).
    long_path = r"C:\a" + (r"\deep" * 60) + r"\app.exe"
    required = len(long_path)

    calls = {"n": 0}

    def _fill(handle, flags, buf, size_ref):
        calls["n"] += 1
        if calls["n"] == 1:
            # Buffer too small: report FALSE + the required length via *size.
            size_ref._obj.value = required
            kernel.GetLastError.return_value = 122  # ERROR_INSUFFICIENT_BUFFER
            return False
        # Retry on the grown buffer succeeds.
        buf.value = long_path
        size_ref._obj.value = required
        return True

    kernel.QueryFullProcessImageNameW.side_effect = _fill
    monkeypatch.setattr(win32.ctypes.windll, "kernel32", kernel)

    assert win32.get_process_name(9999) == "app.exe"
    # Retried exactly once after the initial insufficient-buffer failure.
    assert kernel.QueryFullProcessImageNameW.call_count == 2
    # The second call's buffer must be at least as large as the required length.
    second_buf = kernel.QueryFullProcessImageNameW.call_args_list[1].args[2]
    assert len(second_buf) > required
    kernel.CloseHandle.assert_called_once_with(12345)


def test_non_insufficient_failure_returns_empty(monkeypatch):
    """If QueryFullProcessImageNameW returns FALSE for a reason other than
    ERROR_INSUFFICIENT_BUFFER (e.g. access revoked mid-call), the helper does
    NOT retry — it returns "" but still closes the handle."""
    kernel = MagicMock()
    kernel.OpenProcess.return_value = 42
    kernel.QueryFullProcessImageNameW.return_value = False
    kernel.GetLastError.return_value = 5  # ERROR_ACCESS_DENIED, not 122
    monkeypatch.setattr(win32.ctypes.windll, "kernel32", kernel)

    assert win32.get_process_name(7) == ""
    # No retry — a single query attempt.
    assert kernel.QueryFullProcessImageNameW.call_count == 1
    kernel.CloseHandle.assert_called_once_with(42)


def test_open_process_declares_handle_restype(monkeypatch):
    """G4: OpenProcess must declare restype=wintypes.HANDLE.

    Without it ctypes defaults the return to a 32-bit ``c_int``, truncating a
    64-bit HANDLE whose high bit is set — so a process handle that is "alive"
    (nonzero) but lives above 2^31 reads back as a wrong/negative value, and
    the subsequent CloseHandle runs on the truncated pointer (silent leak on a
    long-lived daemon). Declaring the prototype is the documented ctypes fix.
    """
    import ctypes
    import ctypes.wintypes

    kernel = MagicMock()
    kernel.OpenProcess.return_value = 12345

    def _fill(handle, flags, buf, size_ref):
        buf.value = "x.exe"
        size_ref._obj.value = 5
        return True

    kernel.QueryFullProcessImageNameW.side_effect = _fill
    monkeypatch.setattr(win32.ctypes.windll, "kernel32", kernel)

    win32.get_process_name(123)
    # The prototype MUST be set so a 64-bit HANDLE round-trips untruncated.
    assert kernel.OpenProcess.restype is ctypes.wintypes.HANDLE, (
        "OpenProcess.restype must be wintypes.HANDLE, not the c_int default — "
        "else 64-bit handles are truncated and CloseHandle leaks"
    )
    # And argtypes so ctypes marshals the args correctly (BOOL/DWORD/PID).
    assert kernel.OpenProcess.argtypes == [
        ctypes.wintypes.DWORD, ctypes.wintypes.BOOL, ctypes.wintypes.DWORD,
    ]
    # CloseHandle must accept HANDLE too: once OpenProcess returns a real 64-bit
    # HANDLE, passing it to a function whose argtype defaults to c_int raises
    # ctypes.ArgumentError (a value > 2**32 overflows), out of the finally in
    # get_process_name. (G4 — the wide-handle half of the fix.)
    ch = getattr(win32.ctypes.windll.kernel32, "CloseHandle")
    assert ch.argtypes and ch.argtypes[0] is ctypes.wintypes.HANDLE, (
        "CloseHandle.argtypes[0] must be wintypes.HANDLE — else a wide "
        "(>2**32) process handle overflows the c_int default and raises"
    )


# ── is_elevated: Win32 IsUserAnAdmin wrapper ───────────────────────────


class TestIsElevated:
    def test_admin_returns_true(self, monkeypatch):
        """IsUserAnAdmin() != 0 → True."""
        shell = MagicMock()
        shell.IsUserAnAdmin.return_value = 1
        monkeypatch.setattr(win32.ctypes.windll, "shell32", shell)
        assert win32.is_elevated() is True
        shell.IsUserAnAdmin.assert_called_once()

    def test_non_admin_returns_false(self, monkeypatch):
        """IsUserAnAdmin() == 0 → False."""
        shell = MagicMock()
        shell.IsUserAnAdmin.return_value = 0
        monkeypatch.setattr(win32.ctypes.windll, "shell32", shell)
        assert win32.is_elevated() is False

    def test_attribute_error_returns_false(self, monkeypatch):
        """If shell32 lacks IsUserAnAdmin (non-Windows edge) → False, no raise."""
        shell = MagicMock()
        shell.IsUserAnAdmin.side_effect = AttributeError("no shell32")
        monkeypatch.setattr(win32.ctypes.windll, "shell32", shell)
        assert win32.is_elevated() is False

    def test_os_error_returns_false(self, monkeypatch):
        """An OSError from the Win32 call → False, no raise."""
        shell = MagicMock()
        shell.IsUserAnAdmin.side_effect = OSError("win32 fault")
        monkeypatch.setattr(win32.ctypes.windll, "shell32", shell)
        assert win32.is_elevated() is False


# ── elevation_hint: actionable guidance when not elevated (TODO #14) ───


class TestElevationHint:
    def test_not_elevated_returns_hint_string(self, monkeypatch):
        """Non-admin → the hint string is returned (non-empty, actionable)."""
        shell = MagicMock()
        shell.IsUserAnAdmin.return_value = 0
        monkeypatch.setattr(win32.ctypes.windll, "shell32", shell)
        hint = win32.elevation_hint()
        assert hint is win32.ELEVATION_HINT
        assert "elevated" in hint.lower()

    def test_elevated_returns_none(self, monkeypatch):
        """Admin → no hint (the operation doesn't need elevation)."""
        shell = MagicMock()
        shell.IsUserAnAdmin.return_value = 1
        monkeypatch.setattr(win32.ctypes.windll, "shell32", shell)
        assert win32.elevation_hint() is None

    def test_unavailable_api_returns_hint(self, monkeypatch):
        """If IsUserAnAdmin is unavailable (non-Windows edge) → is_elevated
        returns False, so the hint is shown — the safe default, since most MCP
        servers run non-elevated."""
        shell = MagicMock()
        shell.IsUserAnAdmin.side_effect = AttributeError("no shell32")
        monkeypatch.setattr(win32.ctypes.windll, "shell32", shell)
        assert win32.elevation_hint() is win32.ELEVATION_HINT
