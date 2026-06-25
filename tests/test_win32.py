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
