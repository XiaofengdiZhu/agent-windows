"""Tests for core/app_manager.py: window finding, launch, switch, resize.

The Win32 calls (EnumWindows, SetForegroundWindow, GetWindowRect, subprocess)
are mocked. The interesting logic is: validate_executable_path delegation in
launch_app, the EnumWindows callback fuzzy-matching in _find_window_by_*,
and the not-found / position-default branches in switch/resize.
"""

import ctypes
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.core import app_manager as am


# ── launch_app ───────────────────────────────────────────────────────


class TestLaunchApp:
    def test_passes_validate_then_subprocess(self):
        fake_proc = MagicMock(pid=4242)
        with patch("agent_windows.core.filesystem.validate_executable_path") as mock_v, \
             patch("agent_windows.core.app_manager.subprocess.Popen", return_value=fake_proc) as mock_p:
            result = am.launch_app("notepad", args=["foo.txt"])
        mock_v.assert_called_once_with("notepad")
        assert mock_p.call_args.args[0] == ["notepad", "foo.txt"]
        assert result["pid"] == 4242
        assert "notepad" in result["output"]
        assert "4242" in result["output"]

    def test_launch_uses_detached_process_group_flags(self):
        """Children must be detached so the daemon doesn't hold the console."""
        fake_proc = MagicMock(pid=1)
        with patch("agent_windows.core.filesystem.validate_executable_path"), \
             patch("agent_windows.core.app_manager.subprocess.Popen", return_value=fake_proc) as mock_p:
            am.launch_app("notepad")
        flags = mock_p.call_args.kwargs["creationflags"]
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert flags & subprocess.DETACHED_PROCESS

    def test_stdio_redirected_to_devnull(self):
        fake_proc = MagicMock(pid=1)
        with patch("agent_windows.core.filesystem.validate_executable_path"), \
             patch("agent_windows.core.app_manager.subprocess.Popen", return_value=fake_proc) as mock_p:
            am.launch_app("notepad")
        kw = mock_p.call_args.kwargs
        assert kw["stdout"] is subprocess.DEVNULL
        assert kw["stderr"] is subprocess.DEVNULL
        assert kw["stdin"] is subprocess.DEVNULL

    def test_args_none_defaults_to_empty(self):
        fake_proc = MagicMock(pid=1)
        with patch("agent_windows.core.filesystem.validate_executable_path"), \
             patch("agent_windows.core.app_manager.subprocess.Popen", return_value=fake_proc) as mock_p:
            am.launch_app("notepad")
        assert mock_p.call_args.args[0] == ["notepad"]

    def test_validation_failure_propagates(self):
        """A blocked path (credential/install dir) raises ValueError, no Popen."""
        with patch("agent_windows.core.filesystem.validate_executable_path",
                   side_effect=ValueError("blocked")) as mock_v, \
             patch("agent_windows.core.app_manager.subprocess.Popen") as mock_p:
            with pytest.raises(ValueError, match="blocked"):
                am.launch_app(r"C:\Users\x\.ssh\evil.exe")
        mock_v.assert_called_once()
        mock_p.assert_not_called()

    def test_filenotfound_becomes_valueerror(self):
        with patch("agent_windows.core.filesystem.validate_executable_path"), \
             patch("agent_windows.core.app_manager.subprocess.Popen",
                   side_effect=FileNotFoundError), \
             patch("agent_windows.core.app_manager._resolve_friendly_name",
                   return_value=None):
            # The friendly-name fallback is patched to "no match" so this test
            # stays hermetic — it must not spawn a real Get-StartApps powershell.
            with pytest.raises(ValueError, match="Application not found"):
                am.launch_app("nonexistent-app-xyz")


# ── friendly-name launch (TODO #18) ─────────────────────────────────


class TestFriendlyNameLaunch:
    """TODO #18: launch by Start-Menu friendly name when the PATH/exe path fails."""

    def setup_method(self):
        # Each test starts with a clean cache.
        am._START_APPS_CACHE = None

    def _csv(self, rows):
        """Build a Get-StartApps CSV stdout from [(name, appid), ...]."""
        import io
        import csv
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Name", "AppID"])
        for n, a in rows:
            w.writerow([n, a])
        return buf.getvalue()

    def test_get_start_apps_caches_result(self):
        """Get-StartApps runs once; the second call reuses the cache (no 2nd subprocess)."""
        csv_out = self._csv([("Calculator", "Microsoft.Calculator_8wekyb3d8bbwe!App"),
                             ("Notepad", r"C:\Windows\System32\notepad.exe")])
        with patch("agent_windows.core.app_manager.subprocess.run",
                   return_value=MagicMock(stdout=csv_out, returncode=0)) as mock_run:
            am._get_start_apps()
            am._get_start_apps()
        assert mock_run.call_count == 1

    def test_get_start_apps_returns_empty_on_failure(self):
        """A failed powershell call → {} (best-effort), not a raise."""
        with patch("agent_windows.core.app_manager.subprocess.run",
                   side_effect=FileNotFoundError("no powershell")):
            assert am._get_start_apps() == {}

    def test_resolve_exact_name_match(self):
        """An exact (case-insensitive) friendly name resolves to its AppID."""
        am._START_APPS_CACHE = {"calculator": "Microsoft.Calculator_8wekyb3d8bbwe!App"}
        assert am._resolve_friendly_name("Calculator") == "Microsoft.Calculator_8wekyb3d8bbwe!App"
        assert am._resolve_friendly_name("CALCULATOR") == "Microsoft.Calculator_8wekyb3d8bbwe!App"

    def test_resolve_substring_match(self):
        """No exact match → a substring match ("calc" ⊂ "calculator")."""
        am._START_APPS_CACHE = {"calculator": "appid-calc", "edge": "appid-edge"}
        assert am._resolve_friendly_name("calc") == "appid-calc"

    def test_resolve_returns_none_when_no_map(self):
        """Empty/disabled Start Menu → None (caller raises 'not found')."""
        am._START_APPS_CACHE = {}
        assert am._resolve_friendly_name("anything") is None

    def test_launch_falls_back_to_friendly_name_on_filenotfound(self):
        """When the PATH/exe Popen raises FileNotFoundError, the friendly-name
        path resolves via the Start Menu and launches the resolved target."""
        am._START_APPS_CACHE = {"myapp": r"C:\Apps\myapp.exe"}
        fake_proc = MagicMock(pid=99)
        with patch("agent_windows.core.filesystem.validate_executable_path"), \
                patch("agent_windows.core.app_manager.subprocess.Popen",
                      side_effect=[FileNotFoundError, fake_proc]) as mock_p, \
                patch("agent_windows.core.app_manager._resolve_friendly_name",
                      return_value=r"C:\Apps\myapp.exe"):
            result = am.launch_app("myapp")
        # Second Popen call launched the resolved path directly.
        assert mock_p.call_args_list[1].args[0] == [r"C:\Apps\myapp.exe"]
        assert result["pid"] == 99

    def test_launch_uwp_uses_shell_appsfolder(self):
        """A UWP AppID (no backslash, not an existing path) launches via
        explorer.exe shell:AppsFolder\\{appid}."""
        appid = "Microsoft.Calculator_8wekyb3d8bbwe!App"
        with patch("agent_windows.core.app_manager.subprocess.Popen",
                   return_value=MagicMock(pid=0)) as mock_p, \
                patch("os.path.exists", return_value=False):
            result = am._launch_start_app_target("calculator", appid)
        argv = mock_p.call_args.args[0]
        assert argv[0] == "explorer.exe"
        assert argv[1] == f"shell:AppsFolder\\{appid}"
        assert result["pid"] == 0
        assert "UWP" in result["output"]

    def test_launch_win32_path_validates_resolved_location(self):
        """A Win32 path target is re-validated (L-03) before launching."""
        with patch("agent_windows.core.filesystem.validate_executable_path") as mock_v, \
                patch("agent_windows.core.app_manager.subprocess.Popen",
                      return_value=MagicMock(pid=7)), \
                patch("os.path.exists", return_value=True):
            am._launch_start_app_target("myapp", r"C:\Apps\myapp.exe")
        mock_v.assert_called_once_with(r"C:\Apps\myapp.exe")

    def test_launch_no_match_raises_not_found(self):
        """When neither PATH nor Start Menu resolves, raise 'not found'."""
        with patch("agent_windows.core.filesystem.validate_executable_path"), \
                patch("agent_windows.core.app_manager.subprocess.Popen",
                      side_effect=FileNotFoundError), \
                patch("agent_windows.core.app_manager._resolve_friendly_name",
                      return_value=None):
            with pytest.raises(ValueError, match="Application not found"):
                am.launch_app("no-such-app-zzz")

    def test_blocked_path_raises_without_friendly_name_fallback(self):
        """L-03: a blocked sensitive path raises from validate_executable_path
        BEFORE the friendly-name fallback is ever attempted."""
        with patch("agent_windows.core.filesystem.validate_executable_path",
                   side_effect=ValueError("blocked")), \
                patch("agent_windows.core.app_manager.subprocess.Popen") as mock_p, \
                patch("agent_windows.core.app_manager._resolve_friendly_name") as mock_resolve:
            with pytest.raises(ValueError, match="blocked"):
                am.launch_app(r"C:\Users\x\.ssh\evil.exe")
        mock_p.assert_not_called()
        mock_resolve.assert_not_called()  # fallback never reached

    def test_win32_target_filenotfound_raises(self):
        """A resolved Win32 path whose Popen then fails → ValueError (not a
        silent success). Locks the _launch_start_app_target FileNotFoundError branch."""
        with patch("agent_windows.core.filesystem.validate_executable_path"), \
                patch("agent_windows.core.app_manager.subprocess.Popen",
                      side_effect=FileNotFoundError), \
                patch("os.path.exists", return_value=True):
            with pytest.raises(ValueError, match="Application not found"):
                am._launch_start_app_target("myapp", r"C:\Apps\gone.exe")


# ── EnumWindows callback harness ─────────────────────────────────────


def _make_enum(*hwnds):
    """Return a fake EnumWindows that invokes the registered callback for each
    hwnd in order, stopping early if the callback returns False (the real
    callback returns False to stop enumeration on first match)."""
    def fake_enum(cb, lp):
        for hwnd in hwnds:
            if not cb(hwnd, lp):
                break
        return 1
    return fake_enum


def _set_title(monkeypatch, title):
    """Make GetWindowTextW fill `title` and GetWindowTextLengthW return its length."""
    monkeypatch.setattr(am.user32, "GetWindowTextLengthW", lambda h: len(title))

    def fill(h, buf, n):
        buf.value = title
        return len(title)
    monkeypatch.setattr(am.user32, "GetWindowTextW", fill)


def _fill_buf(title):
    """side_effect for GetWindowTextW that writes `title` into the unicode buffer."""
    def fill(h, buf, n):
        buf.value = title
        return len(title)
    return fill


# ── _find_window_by_title ────────────────────────────────────────────


class TestFindByTitle:
    def test_substring_case_insensitive_match(self, monkeypatch):
        monkeypatch.setattr(am.user32, "IsWindowVisible", lambda h: True)
        _set_title(monkeypatch, "Untitled - Notepad")
        monkeypatch.setattr(am.user32, "EnumWindows", _make_enum(101))
        assert am._find_window_by_title("NOTEPAD") == 101

    def test_partial_substring_match(self, monkeypatch):
        monkeypatch.setattr(am.user32, "IsWindowVisible", lambda h: True)
        _set_title(monkeypatch, "foo - bar")
        monkeypatch.setattr(am.user32, "EnumWindows", _make_enum(202))
        assert am._find_window_by_title("bar") == 202

    def test_stops_at_first_match(self, monkeypatch):
        """When two windows match, the first one wins (callback returns False)."""
        called = []

        def visible(h):
            called.append(h)
            return True

        monkeypatch.setattr(am.user32, "IsWindowVisible", visible)
        _set_title(monkeypatch, "notepad")
        monkeypatch.setattr(am.user32, "EnumWindows", _make_enum(11, 22, 33))
        assert am._find_window_by_title("notepad") == 11
        assert called == [11]  # never reached 22 or 33

    def test_skips_invisible_windows(self, monkeypatch):
        monkeypatch.setattr(am.user32, "IsWindowVisible", lambda h: False)
        _set_title(monkeypatch, "invisible")
        monkeypatch.setattr(am.user32, "EnumWindows", _make_enum(5))
        assert am._find_window_by_title("invisible") == 0

    def test_skips_empty_title(self, monkeypatch):
        monkeypatch.setattr(am.user32, "IsWindowVisible", lambda h: True)
        monkeypatch.setattr(am.user32, "GetWindowTextLengthW", lambda h: 0)
        monkeypatch.setattr(am.user32, "GetWindowTextW", lambda h, buf, n: None)
        monkeypatch.setattr(am.user32, "EnumWindows", _make_enum(6))
        assert am._find_window_by_title("anything") == 0

    def test_no_match_returns_zero(self, monkeypatch):
        monkeypatch.setattr(am.user32, "IsWindowVisible", lambda h: True)
        _set_title(monkeypatch, "Other")
        monkeypatch.setattr(am.user32, "EnumWindows", _make_enum(7))
        assert am._find_window_by_title("notepad") == 0


# ── _find_window_by_process ──────────────────────────────────────────


class TestFindByProcess:
    def _patches(self, monkeypatch, hwnd, proc_name):
        """Wire EnumWindows→single hwnd, IsWindowVisible True, and a
        GetWindowThreadProcessId that writes a fixed pid into the DWORD.

        ``p`` is the CArgObject returned by ``ctypes.byref(DWORD())``; reaching
        ``p._obj`` gives the underlying DWORD so ``.value =`` writes through.
        """
        monkeypatch.setattr(am.user32, "IsWindowVisible", lambda h: True)
        monkeypatch.setattr(am.user32, "EnumWindows", _make_enum(hwnd))

        def fake_get_pid(h, p):
            # p is ctypes.byref(DWORD); .value writes through
            p._obj.value = 1234
            return 1

        monkeypatch.setattr(am.user32, "GetWindowThreadProcessId", fake_get_pid)
        monkeypatch.setattr(am, "get_process_name", lambda pid: proc_name)

    def test_normalizes_exe_suffix(self, monkeypatch):
        """A bare 'notepad' query is matched against proc_name lowercased;
        target becomes 'notepad.exe'."""
        self._patches(monkeypatch, 200, "notepad.exe")
        assert am._find_window_by_process("notepad") == 200

    def test_strips_existing_exe_suffix(self, monkeypatch):
        """Querying 'notepad.exe' must not become 'notepad.exe.exe'."""
        self._patches(monkeypatch, 201, "notepad.exe")
        assert am._find_window_by_process("notepad.exe") == 201

    def test_case_insensitive(self, monkeypatch):
        self._patches(monkeypatch, 202, "CHROME.EXE")
        assert am._find_window_by_process("Chrome") == 202

    def test_no_match_returns_zero(self, monkeypatch):
        self._patches(monkeypatch, 203, "explorer.exe")
        assert am._find_window_by_process("notepad") == 0

    def test_skips_invisible_windows(self, monkeypatch):
        monkeypatch.setattr(am.user32, "IsWindowVisible", lambda h: False)
        monkeypatch.setattr(am.user32, "EnumWindows", _make_enum(204))

        def fake_get_pid(h, p):
            p._obj.value = 1234
            return 1

        monkeypatch.setattr(am.user32, "GetWindowThreadProcessId", fake_get_pid)
        monkeypatch.setattr(am, "get_process_name", lambda pid: "notepad.exe")
        assert am._find_window_by_process("notepad") == 0


# ── switch_window ────────────────────────────────────────────────────


class TestSwitchWindow:
    def test_not_found_raises(self):
        with patch.object(am, "_find_window_by_title", return_value=0), \
             patch.object(am, "_find_window_by_process", return_value=0):
            with pytest.raises(ValueError, match="Window not found"):
                am.switch_window("missing")

    def test_title_match_takes_precedence_over_process(self):
        with patch.object(am, "_find_window_by_title", return_value=100) as mt, \
             patch.object(am, "_find_window_by_process", return_value=200) as mp, \
             patch.object(am.user32, "IsIconic", return_value=False), \
             patch.object(am.user32, "SetForegroundWindow") as mock_fg, \
             patch.object(am.user32, "GetForegroundWindow", return_value=100), \
             patch.object(am.user32, "GetWindowTextLengthW", return_value=0), \
             patch.object(am.user32, "GetWindowTextW"), \
             patch("time.sleep"):
            am.switch_window("foo")
        mt.assert_called_once()
        mp.assert_not_called()  # short-circuited
        mock_fg.assert_called_once_with(100)

    def test_falls_back_to_process_match(self):
        with patch.object(am, "_find_window_by_title", return_value=0), \
             patch.object(am, "_find_window_by_process", return_value=300), \
             patch.object(am.user32, "IsIconic", return_value=False), \
             patch.object(am.user32, "SetForegroundWindow") as mock_fg, \
             patch.object(am.user32, "GetForegroundWindow", return_value=300), \
             patch.object(am.user32, "GetWindowTextLengthW", return_value=0), \
             patch.object(am.user32, "GetWindowTextW"), \
             patch("time.sleep"):
            am.switch_window("foo")
        mock_fg.assert_called_once_with(300)

    def test_minimized_window_restored_first(self):
        """IsIconic → ShowWindow(SW_RESTORE) before SetForegroundWindow."""
        with patch.object(am, "_find_window_by_title", return_value=50), \
             patch.object(am, "_find_window_by_process", return_value=0), \
             patch.object(am.user32, "IsIconic", return_value=True), \
             patch.object(am.user32, "ShowWindow") as mock_sw, \
             patch.object(am.user32, "SetForegroundWindow"), \
             patch.object(am.user32, "GetForegroundWindow", return_value=50), \
             patch.object(am.user32, "GetWindowTextLengthW", return_value=0), \
             patch.object(am.user32, "GetWindowTextW"), \
             patch("time.sleep"):
            am.switch_window("foo")
        mock_sw.assert_called_once_with(50, 9)  # SW_RESTORE = 9

    def test_output_message_includes_fetched_title(self):
        """The returned output embeds the window title fetched via
        GetWindowTextW. If that fetch block were deleted, this would fail."""
        with patch.object(am, "_find_window_by_title", return_value=100), \
             patch.object(am, "_find_window_by_process", return_value=0), \
             patch.object(am.user32, "IsIconic", return_value=False), \
             patch.object(am.user32, "SetForegroundWindow"), \
             patch.object(am.user32, "GetForegroundWindow", return_value=100), \
             patch.object(am.user32, "GetWindowTextLengthW", return_value=14), \
             patch.object(am.user32, "GetWindowTextW", side_effect=_fill_buf("Hello - Notes")), \
             patch("time.sleep"):
            result = am.switch_window("hello")
        assert result["output"].startswith("## App Switch")
        assert 'Window: "Hello - Notes"' in result["output"]
        assert "Handle: 100" in result["output"]

    def test_get_foreground_window_restype_is_hwnd(self):
        """HWND is pointer-sized; the default c_int restype truncates large
        handles on 64-bit, breaking the fg==hwnd foreground check. Lock the
        restype so the comparison is correct. (P0.4)"""
        import ctypes.wintypes
        assert am.user32.GetForegroundWindow.restype is ctypes.wintypes.HWND

    def test_foreground_confirmed_when_target_now_foreground(self):
        """SetForegroundWindow success + GetForegroundWindow re-reads the
        target → 'Foreground: yes' + foreground_confirmed True + hwnd sibling."""
        with patch.object(am, "_find_window_by_title", return_value=100), \
             patch.object(am, "_find_window_by_process", return_value=0), \
             patch.object(am.user32, "IsIconic", return_value=False), \
             patch.object(am.user32, "SetForegroundWindow", return_value=1), \
             patch.object(am.user32, "GetForegroundWindow", return_value=100), \
             patch.object(am.user32, "GetWindowTextLengthW", return_value=0), \
             patch.object(am.user32, "GetWindowTextW"), \
             patch("time.sleep"):
            r = am.switch_window("foo")
        assert "Foreground: yes" in r["output"]
        assert r["foreground_confirmed"] is True
        assert r["hwnd"] == 100

    def test_foreground_blocked_when_target_not_foreground(self):
        """Focus-stealing prevention (SetForegroundWindow returns 0 / target
        not actually foreground) → 'Foreground: no (blocked...)' + False."""
        with patch.object(am, "_find_window_by_title", return_value=100), \
             patch.object(am, "_find_window_by_process", return_value=0), \
             patch.object(am.user32, "IsIconic", return_value=False), \
             patch.object(am.user32, "SetForegroundWindow", return_value=0), \
             patch.object(am.user32, "GetForegroundWindow", return_value=999), \
             patch.object(am.user32, "GetWindowTextLengthW", return_value=0), \
             patch.object(am.user32, "GetWindowTextW"), \
             patch("time.sleep"):
            r = am.switch_window("foo")
        assert "Foreground: no" in r["output"]
        assert "blocked" in r["output"]
        assert r["foreground_confirmed"] is False
        assert r["hwnd"] == 100


# ── resize_window ────────────────────────────────────────────────────


class TestResizeWindow:
    def test_not_found_raises(self):
        with patch.object(am, "_find_window_by_title", return_value=0), \
             patch.object(am, "_find_window_by_process", return_value=0):
            with pytest.raises(ValueError, match="Window not found"):
                am.resize_window("missing", 800, 600)

    def test_uses_current_rect_when_position_omitted(self):
        """x/y default to -1 → fall back to window's current rect.left/top."""
        def fake_get_rect(h, r):
            r._obj.left = 120
            r._obj.top = 80
            return 1

        with patch.object(am, "_find_window_by_title", return_value=10), \
             patch.object(am, "_find_window_by_process", return_value=0), \
             patch.object(am.user32, "GetWindowRect", side_effect=fake_get_rect), \
             patch.object(am.user32, "SetWindowPos") as mock_sp:
            result = am.resize_window("win", 800, 600)
        # args: hwnd, hWndInsertAfter(0), x, y, w, h, flags
        assert mock_sp.call_args.args[0] == 10
        assert mock_sp.call_args.args[2:6] == (120, 80, 800, 600)
        assert mock_sp.call_args.args[6] == 0x0004  # SWP_NOZORDER
        # output message reflects the fallback position
        assert result["output"].startswith("## App Resize")
        assert "Size: 800x600" in result["output"]
        assert "Position: (120, 80)" in result["output"]

    def test_explicit_position_overrides_rect(self):
        def fake_get_rect(h, r):
            r._obj.left = 0
            r._obj.top = 0
            return 1

        with patch.object(am, "_find_window_by_title", return_value=10), \
             patch.object(am, "_find_window_by_process", return_value=0), \
             patch.object(am.user32, "GetWindowRect", side_effect=fake_get_rect), \
             patch.object(am.user32, "SetWindowPos") as mock_sp:
            am.resize_window("win", 640, 480, x=10, y=20)
        assert mock_sp.call_args.args[2:6] == (10, 20, 640, 480)

    def test_falls_back_to_process_match(self):
        def fake_get_rect(h, r):
            r._obj.left = 0
            r._obj.top = 0
            return 1

        with patch.object(am, "_find_window_by_title", return_value=0), \
             patch.object(am, "_find_window_by_process", return_value=77), \
             patch.object(am.user32, "GetWindowRect", side_effect=fake_get_rect), \
             patch.object(am.user32, "SetWindowPos") as mock_sp:
            am.resize_window("win", 100, 100)
        assert mock_sp.call_args.args[0] == 77


# ── list_windows ─────────────────────────────────────────────────────


class TestListWindows:
    def test_formats_markdown_table(self):
        win = MagicMock()
        win.title = "My Window"
        win.process_name = "app.exe"
        win.bbox = "0,0,100,200"
        with patch("agent_windows.core.tree.enumerate_windows", return_value=[win]):
            result = am.list_windows()
        assert result["count"] == 1
        assert "| # | Title | App | Position |" in result["output"]
        assert "My Window" in result["output"]
        assert "0,0,100,200" in result["output"]

    def test_truncates_long_titles(self):
        win = MagicMock()
        win.title = "X" * 200
        win.process_name = "app.exe"
        win.bbox = "0,0,1,1"
        with patch("agent_windows.core.tree.enumerate_windows", return_value=[win]):
            result = am.list_windows()
        # title cell is truncated to 50 chars
        assert "X" * 50 in result["output"]
        assert "X" * 51 not in result["output"]

    def test_handles_none_bbox(self):
        win = MagicMock()
        win.title = "Win"
        win.process_name = "app.exe"
        win.bbox = None
        with patch("agent_windows.core.tree.enumerate_windows", return_value=[win]):
            result = am.list_windows()
        assert "N/A" in result["output"]

    def test_empty_window_list_still_emits_headers(self):
        with patch("agent_windows.core.tree.enumerate_windows", return_value=[]):
            result = am.list_windows()
        assert result["count"] == 0
        # header + separator rows present even with no windows
        assert "| # | Title | App | Position |" in result["output"]
        assert "|---|-------|-----|----------|" in result["output"]
