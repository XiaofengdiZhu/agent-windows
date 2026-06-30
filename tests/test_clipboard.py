"""Direct unit tests for core/clipboard.py.

The clipboard now uses the native win32clipboard API (TODO #17) instead of
spawning powershell. These tests lock the Open/Get/Set/Close flow, the
open-retry behavior, the empty-vs-non-text distinction, the keyboard
(Ctrl+C/V) mode, and the QUAL-08 error chaining — all without touching the
real clipboard or sending real keystrokes.
"""
from unittest.mock import patch, MagicMock
import sys

import pytest

from agent_windows.core import clipboard as cb


def _win32clip():
    """A mock win32clipboard module: OpenClipboard/CloseClipboard/etc as MagicMocks."""
    mod = MagicMock()
    mod.CF_UNICODETEXT = 13
    return mod


# ── get_clipboard ─────────────────────────────────────────────────────


class TestGetClipboard:
    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_returns_unicode_text(self, mock_open):
        """CF_UNICODETEXT available → returns the raw text (unmutated), preview stripped."""
        w32c = _win32clip()
        w32c.IsClipboardFormatAvailable.return_value = True
        w32c.GetClipboardData.return_value = "hello world\n"
        mock_open.return_value = w32c
        result = cb.get_clipboard()
        # The ``text`` field carries the EXACT clipboard contents — not stripped.
        # Stripping would silently drop code indentation / passwords / tokens.
        assert result["text"] == "hello world\n"
        # The human-facing preview is stripped for readability.
        assert "hello world" in result["output"]
        w32c.GetClipboardData.assert_called_once_with(w32c.CF_UNICODETEXT)
        w32c.CloseClipboard.assert_called_once()

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_returns_text_with_significant_whitespace_intact(self, mock_open):
        """G3: leading/trailing whitespace that carries meaning (code indent,
        a trailing newline, surrounding spaces on a token) must survive in the
        ``text`` field. Only the empty-check and the preview are stripped."""
        w32c = _win32clip()
        w32c.IsClipboardFormatAvailable.return_value = True
        # e.g. a copied code line with 4-space indent.
        w32c.GetClipboardData.return_value = "    return x\n"
        mock_open.return_value = w32c
        result = cb.get_clipboard()
        assert result["text"] == "    return x\n"  # raw, unmutated
        assert "return x" in result["output"]

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_non_text_clipboard_reports_non_text(self, mock_open):
        """No CF_UNICODETEXT available (e.g. an image) → 'non-text', no text key."""
        w32c = _win32clip()
        w32c.IsClipboardFormatAvailable.return_value = False
        mock_open.return_value = w32c
        result = cb.get_clipboard()
        assert result["output"] == "## Clipboard\n(Empty or non-text content)"
        assert "text" not in result
        w32c.GetClipboardData.assert_not_called()
        w32c.CloseClipboard.assert_called_once()

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_empty_text_clipboard_reports_empty(self, mock_open):
        """CF_UNICODETEXT available but blank → 'Empty' (distinct from non-text)."""
        w32c = _win32clip()
        w32c.IsClipboardFormatAvailable.return_value = True
        w32c.GetClipboardData.return_value = "   \n  "  # whitespace-only → empty
        mock_open.return_value = w32c
        result = cb.get_clipboard()
        assert result["output"] == "## Clipboard\n(Empty)"

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_close_called_even_on_error(self, mock_open):
        """CloseClipboard runs in a finally — even if GetClipboardData raises."""
        w32c = _win32clip()
        w32c.IsClipboardFormatAvailable.return_value = True
        w32c.GetClipboardData.side_effect = OSError("boom")
        mock_open.return_value = w32c
        with pytest.raises(RuntimeError, match="Failed to read clipboard"):
            cb.get_clipboard()
        w32c.CloseClipboard.assert_called_once()  # finally ran

    @patch("agent_windows.core.input_controller.shortcut")
    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_keyboard_true_sends_ctrl_c_first(self, mock_open, mock_shortcut):
        """keyboard=True simulates Ctrl+C before reading the clipboard."""
        w32c = _win32clip()
        w32c.IsClipboardFormatAvailable.return_value = True
        w32c.GetClipboardData.return_value = "x"
        mock_open.return_value = w32c
        cb.get_clipboard(keyboard=True)
        mock_shortcut.assert_called_once_with("ctrl+c")


# ── set_clipboard ─────────────────────────────────────────────────────


class TestSetClipboard:
    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_empties_then_sets_text(self, mock_open):
        """set calls EmptyClipboard then SetClipboardData(CF_UNICODETEXT, text)."""
        w32c = _win32clip()
        mock_open.return_value = w32c
        result = cb.set_clipboard("hello")
        w32c.EmptyClipboard.assert_called_once()
        w32c.SetClipboardData.assert_called_once_with(w32c.CF_UNICODETEXT, "hello")
        w32c.CloseClipboard.assert_called_once()
        assert "hello" in result["output"]

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_text_set_verbatim_no_interpolation_risk(self, mock_open):
        """TODO #17 + former SEC-05: with the native API there's no command string,
        so hostile text is passed to SetClipboardData verbatim — no injection
        surface at all. The text reaches the API unchanged."""
        hostile = "'; Get-Process; '$evil\r\nback`tick${var}"
        w32c = _win32clip()
        mock_open.return_value = w32c
        cb.set_clipboard(hostile)
        # The exact string reaches SetClipboardData — nothing is parsed.
        assert w32c.SetClipboardData.call_args.args == (w32c.CF_UNICODETEXT, hostile)

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_close_called_even_on_set_error(self, mock_open):
        """CloseClipboard runs in a finally — even if SetClipboardData raises."""
        w32c = _win32clip()
        w32c.SetClipboardData.side_effect = OSError("denied")
        mock_open.return_value = w32c
        with pytest.raises(RuntimeError, match="Failed to set clipboard"):
            cb.set_clipboard("x")
        w32c.CloseClipboard.assert_called_once()

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_failure_raises_runtime_error_with_chain(self, mock_open):
        """QUAL-08 lock: _open_clipboard failures already chain; re-raised as-is."""
        mock_open.side_effect = RuntimeError("locked")
        with pytest.raises(RuntimeError, match="locked"):
            cb.set_clipboard("x")

    @patch("agent_windows.core.input_controller.shortcut")
    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_keyboard_true_sends_ctrl_v_after(self, mock_open, mock_shortcut):
        """keyboard=True simulates Ctrl+V AFTER setting the clipboard."""
        w32c = _win32clip()
        mock_open.return_value = w32c
        result = cb.set_clipboard("x", keyboard=True)
        mock_shortcut.assert_called_once_with("ctrl+v")
        assert "Keyboard: simulated Ctrl+V" in result["output"]

    @patch("agent_windows.core.input_controller.shortcut")
    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_keyboard_paste_skipped_when_set_fails(self, mock_open, mock_shortcut):
        """If the clipboard set fails, Ctrl+V is NOT sent — paste runs only after
        a successful set (ordering lock)."""
        w32c = _win32clip()
        w32c.SetClipboardData.side_effect = OSError("denied")
        mock_open.return_value = w32c
        with pytest.raises(RuntimeError):
            cb.set_clipboard("x", keyboard=True)
        mock_shortcut.assert_not_called()

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_success_output_truncates_long_text(self, mock_open):
        w32c = _win32clip()
        mock_open.return_value = w32c
        long = "x" * 200
        result = cb.set_clipboard(long)
        assert "..." in result["output"]
        assert str(long) not in result["output"]  # truncated, not full

    @patch("agent_windows.core.clipboard._open_clipboard")
    def test_success_output_no_truncation_for_short_text(self, mock_open):
        w32c = _win32clip()
        mock_open.return_value = w32c
        result = cb.set_clipboard("hi")
        assert "..." not in result["output"]
        assert "hi" in result["output"]


# ── _open_clipboard retry behavior ────────────────────────────────────


class TestOpenClipboardRetry:
    def test_retries_until_success(self):
        """OpenClipboard fails a few times (another app holds it) then succeeds."""
        w32c = _win32clip()
        w32c.OpenClipboard.side_effect = [OSError("locked"), OSError("locked"), None]
        # _open_clipboard imports win32clipboard locally, so patch sys.modules.
        with patch.dict(sys.modules, {"win32clipboard": w32c}), \
                patch("time.sleep"):  # don't really sleep
            result = cb._open_clipboard()
        assert result is w32c
        assert w32c.OpenClipboard.call_count == 3

    def test_gives_up_after_retries_with_chain(self):
        """Persistent failure → RuntimeError chained to the last error."""
        w32c = _win32clip()
        w32c.OpenClipboard.side_effect = OSError("locked forever")
        with patch.dict(sys.modules, {"win32clipboard": w32c}), \
                patch("time.sleep"), \
                patch("agent_windows.core.clipboard._OPEN_RETRIES", 2):
            with pytest.raises(RuntimeError, match="Could not open clipboard") as exc_info:
                cb._open_clipboard()
        assert isinstance(exc_info.value.__cause__, OSError)
