"""Tests for core window_manager: minimize/maximize/restore/close/focus.

P4.2 — direct window ops via Win32 ShowWindow/SetForegroundWindow/PostMessage,
replacing the indirect `shortcut win+down` convention. The Win32 layer is
mocked; resolve_window is mocked to feed a deterministic target. Each op
asserts the right primitive is called AND the post-state confirmation.
"""

from unittest.mock import MagicMock, patch

from agent_windows.core import window_manager as wm
from agent_windows.core.tree import WindowInfo


def _wi(handle: int = 100, title: str = "Notepad",
        process_name: str = "notepad.exe") -> WindowInfo:
    """A minimal WindowInfo — window_manager only reads .handle and .title."""
    return WindowInfo(title=title, process_name=process_name, handle=handle,
                      bbox=None, is_active=False)


class TestResolve:
    def test_empty_target_resolves_active_foreground(self):
        """No target → resolve_window(active=True) (the foreground window)."""
        with patch.object(wm, "resolve_window", return_value=(_wi(100), None)) as m:
            hwnd, title, warn = wm._resolve("")
        m.assert_called_once_with(active=True)
        assert hwnd == 100 and title == "Notepad" and warn is None

    def test_nonempty_target_resolves_by_value(self):
        """A target (HWND/title/process) → resolve_window(value=target)."""
        with patch.object(wm, "resolve_window", return_value=(_wi(200, "X"), None)) as m:
            hwnd, title, _ = wm._resolve("0xC8")
        m.assert_called_once_with(value="0xC8")
        assert hwnd == 200 and title == "X"

    def test_warning_propagates(self):
        """A multi-match warning from resolve_window flows through."""
        with patch.object(wm, "resolve_window", return_value=(_wi(100), "⚠ multi")):
            _, _, warn = wm._resolve("Notepad")
        assert warn == "⚠ multi"


class TestMinimize:
    def _run(self, iconic_after: bool):
        m32 = MagicMock()
        m32.IsIconic.return_value = iconic_after
        with patch.object(wm, "user32", m32), \
             patch.object(wm, "resolve_window", return_value=(_wi(100), None)):
            r = wm.minimize_window("Notepad")
        return r, m32

    def test_calls_show_window_minimize_and_confirms(self):
        r, m32 = self._run(iconic_after=True)
        m32.ShowWindow.assert_called_once_with(100, wm.SW.Minimize)
        assert r["minimized"] is True
        assert r["hwnd"] == 100
        assert "## Window Minimize" in r["output"]
        assert '"Notepad"' in r["output"]
        assert "Minimized: yes" in r["output"]

    def test_not_confirmed_reports_no(self):
        r, _ = self._run(iconic_after=False)
        assert r["minimized"] is False
        assert "Minimized: no" in r["output"]


class TestMaximize:
    def _run(self, zoomed_after: bool):
        m32 = MagicMock()
        m32.IsZoomed.return_value = zoomed_after
        with patch.object(wm, "user32", m32), \
             patch.object(wm, "resolve_window", return_value=(_wi(100), None)):
            return wm.maximize_window("Notepad"), m32

    def test_calls_show_window_maximize_and_confirms(self):
        r, m32 = self._run(zoomed_after=True)
        m32.ShowWindow.assert_called_once_with(100, wm.SW.Maximize)
        assert r["maximized"] is True
        assert "Maximized: yes" in r["output"]

    def test_not_confirmed_reports_no(self):
        r, _ = self._run(zoomed_after=False)
        assert r["maximized"] is False
        assert "Maximized: no" in r["output"]


class TestRestore:
    def _run(self, iconic: bool, zoomed: bool):
        m32 = MagicMock()
        m32.IsIconic.return_value = iconic
        m32.IsZoomed.return_value = zoomed
        with patch.object(wm, "user32", m32), \
             patch.object(wm, "resolve_window", return_value=(_wi(100), None)):
            return wm.restore_window("Notepad"), m32

    def test_calls_show_window_restore_and_confirms_normal(self):
        r, m32 = self._run(iconic=False, zoomed=False)
        m32.ShowWindow.assert_called_once_with(100, wm.SW.Restore)
        assert r["restored"] is True
        assert "Restored: yes" in r["output"]

    def test_still_iconic_not_restored(self):
        r, _ = self._run(iconic=True, zoomed=False)
        assert r["restored"] is False
        assert "Restored: no" in r["output"]

    def test_still_zoomed_not_restored(self):
        r, _ = self._run(iconic=False, zoomed=True)
        assert r["restored"] is False


class TestFocus:
    """Reuses the P0.4 pattern from app_manager.switch_window: restore-if-iconic,
    SetForegroundWindow, then re-read GetForegroundWindow to confirm (the BOOL
    return is advisory — focus-stealing prevention can make it lie)."""

    def _run(self, iconic_pre: bool, fg_hwnd: int):
        m32 = MagicMock()
        m32.IsIconic.return_value = iconic_pre
        m32.GetForegroundWindow.return_value = fg_hwnd
        with patch.object(wm, "user32", m32), \
             patch.object(wm, "resolve_window", return_value=(_wi(100), None)):
            return wm.focus_window("Notepad"), m32

    def test_restores_if_iconic_then_foregrounds_and_confirms(self):
        r, m32 = self._run(iconic_pre=True, fg_hwnd=100)
        m32.ShowWindow.assert_called_once_with(100, wm.SW.Restore)
        m32.SetForegroundWindow.assert_called_once_with(100)
        assert r["foreground_confirmed"] is True
        assert "Foreground: yes" in r["output"]

    def test_no_restore_if_not_iconic(self):
        _, m32 = self._run(iconic_pre=False, fg_hwnd=100)
        m32.ShowWindow.assert_not_called()
        m32.SetForegroundWindow.assert_called_once_with(100)

    def test_blocked_by_focus_stealing_reports_no(self):
        r, _ = self._run(iconic_pre=False, fg_hwnd=999)
        assert r["foreground_confirmed"] is False
        assert "Foreground: no" in r["output"]


class TestClose:
    """Graceful close via PostMessage(WM_CLOSE) — lets the app save / show a
    "save changes?" dialog. Confirmed gone via IsWindow(hwnd)==False. An app
    that intercepts WM_CLOSE (unsaved-work dialog) keeps the window → closed=no."""

    def _run(self, is_window_after: bool):
        m32 = MagicMock()
        m32.IsWindow.return_value = is_window_after
        with patch.object(wm, "user32", m32), \
             patch.object(wm, "resolve_window", return_value=(_wi(100), None)):
            return wm.close_window("Notepad"), m32

    def test_posts_wm_close_and_confirms_gone(self):
        r, m32 = self._run(is_window_after=False)
        m32.PostMessageW.assert_called_once_with(100, wm.WM_CLOSE, 0, 0)
        assert r["closed"] is True
        assert "Closed: yes" in r["output"]

    def test_app_intercepted_keeps_window_reports_no(self):
        r, _ = self._run(is_window_after=True)
        assert r["closed"] is False
        assert "Closed: no" in r["output"]

    def test_closes_after_brief_delay(self):
        """A window that vanishes shortly after WM_CLOSE (slow app, or a save
        modal dismissed a moment later) must still report closed=yes — poll
        IsWindow a few times rather than a single 100ms check that would
        false-negative on slow-to-close apps and make the agent over-react."""
        m32 = MagicMock()
        m32.IsWindow.side_effect = [True, True, False]  # gone on the 3rd check
        with patch.object(wm, "user32", m32), \
             patch.object(wm, "resolve_window", return_value=(_wi(100), None)), \
             patch("agent_windows.core.window_manager.time"):
            r = wm.close_window("Notepad")
        assert m32.IsWindow.call_count == 3
        assert r["closed"] is True
        assert "Closed: yes" in r["output"]


class TestWarningSurfacing:
    """A multi-match warning appears in output AND as a sibling (so a
    daemon-direct caller can detect ambiguity without parsing text)."""

    def test_warning_in_output_and_sibling(self):
        m32 = MagicMock()
        m32.IsIconic.return_value = True
        with patch.object(wm, "user32", m32), \
             patch.object(wm, "resolve_window", return_value=(_wi(100), "⚠ multi")):
            r = wm.minimize_window("X")
        assert "⚠ multi" in r["output"]
        assert r["warning"] == "⚠ multi"

    def test_no_warning_sibling_when_unique(self):
        m32 = MagicMock()
        m32.IsIconic.return_value = True
        with patch.object(wm, "user32", m32), \
             patch.object(wm, "resolve_window", return_value=(_wi(100), None)):
            r = wm.minimize_window("X")
        assert "warning" not in r
