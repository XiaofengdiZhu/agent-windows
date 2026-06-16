"""Direct unit tests for core/clipboard.py.

The handler-level tests in test_input_system_handlers.py mock clipboard
entirely, so the real PowerShell construction — in particular the SEC-05
stdin-piping injection fix and the QUAL-08 error-chaining — is otherwise
unexercised. This file locks those paths down without touching the real
clipboard or sending real keystrokes. (TEST3-02)
"""
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.core import clipboard as cb


def _completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Build a fake subprocess.CompletedProcess."""
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


# ── get_clipboard ─────────────────────────────────────────────────────


class TestGetClipboard:
    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_invokes_get_clipboard_command(self, mock_run):
        """argv is exactly ['powershell', '-NoProfile', '-Command', 'Get-Clipboard']."""
        mock_run.return_value = _completed(stdout="hello")
        cb.get_clipboard()
        argv = mock_run.call_args.args[0]
        assert argv == ["powershell", "-NoProfile", "-Command", "Get-Clipboard"]
        # capture_output + text + timeout + encoding all set
        kwargs = mock_run.call_args.kwargs
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        assert kwargs["timeout"] == 5
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"

    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_content_returned_with_text_key(self, mock_run):
        mock_run.return_value = _completed(stdout="hello world\n")
        result = cb.get_clipboard()
        assert result["text"] == "hello world"  # stripped
        assert "hello world" in result["output"]

    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_empty_clipboard_reports_empty(self, mock_run):
        mock_run.return_value = _completed(stdout="")
        result = cb.get_clipboard()
        assert result["output"] == "## Clipboard\n(Empty or non-text content)"
        assert "text" not in result  # no text key when empty

    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_failure_raises_runtime_error_with_chain(self, mock_run):
        """QUAL-08 lock: errors raise RuntimeError with exception chaining."""
        mock_run.side_effect = OSError("pipe broken")
        with pytest.raises(RuntimeError, match="Failed to read clipboard") as exc_info:
            cb.get_clipboard()
        assert isinstance(exc_info.value.__cause__, OSError)

    @patch("agent_windows.core.input_controller.shortcut")
    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_keyboard_true_sends_ctrl_c_first(self, mock_run, mock_shortcut):
        """keyboard=True simulates Ctrl+C before reading the clipboard."""
        mock_run.return_value = _completed(stdout="x")
        cb.get_clipboard(keyboard=True)
        mock_shortcut.assert_called_once_with("ctrl+c")


# ── set_clipboard ─────────────────────────────────────────────────────


class TestSetClipboard:
    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_text_piped_via_stdin_not_interpolated(self, mock_run):
        """SEC-05 regression lock: text is passed as `input=` to a CONSTANT
        command ($input | Set-Clipboard), never interpolated into the command
        string. This is the injection fix — a here-string build would embed the
        text and allow PowerShell metacharacters to execute."""
        hostile = "'; Get-Process; '$evil"
        mock_run.return_value = _completed(returncode=0)
        cb.set_clipboard(hostile)
        kwargs = mock_run.call_args.kwargs
        argv = mock_run.call_args.args[0]
        # The hostile text is on stdin, NOT in the command string
        assert kwargs["input"] == hostile
        assert argv[3] == "$input | Set-Clipboard"
        # The command is byte-identical regardless of input value
        assert hostile not in argv[3]

    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_command_constant_across_inputs(self, mock_run):
        """The PowerShell command string must not change with the text value —
        confirming no interpolation regardless of metacharacter content."""
        mock_run.return_value = _completed(returncode=0)
        inputs = [
            "plain text",
            "'; rm -rf /; '",                      # quotes + semicolon
            '$var = "x"\nGet-Date',                 # dollar + newline
            "back`tick",                            # backtick
            "line1\r\nline2\ttab",                  # CRLF + tab
        ]
        commands = set()
        for txt in inputs:
            cb.set_clipboard(txt)
            argv = mock_run.call_args.args[0]
            commands.add(argv[3])
            # each input is piped verbatim via stdin
            assert mock_run.call_args.kwargs["input"] == txt
        # exactly one constant command across all inputs
        assert commands == {"$input | Set-Clipboard"}

    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_nonzero_returncode_raises(self, mock_run):
        """QUAL-08 lock: a nonzero PowerShell exit surfaces as a chained
        RuntimeError, not a silent success."""
        mock_run.return_value = _completed(stderr="access denied", returncode=1)
        with pytest.raises(RuntimeError, match="Set-Clipboard failed") as exc_info:
            cb.set_clipboard("x")
        # the inner exception is preserved in the chain
        assert exc_info.value.__cause__ is not None

    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_subprocess_failure_raises_with_chain(self, mock_run):
        """A subprocess.run exception propagates as a chained RuntimeError."""
        mock_run.side_effect = FileNotFoundError("no powershell")
        with pytest.raises(RuntimeError, match="Failed to set clipboard") as exc_info:
            cb.set_clipboard("x")
        assert isinstance(exc_info.value.__cause__, FileNotFoundError)

    @patch("agent_windows.core.input_controller.shortcut")
    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_keyboard_true_sends_ctrl_v_after(self, mock_run, mock_shortcut):
        """keyboard=True simulates Ctrl+V AFTER setting the clipboard, and the
        output carries the 'Keyboard: simulated Ctrl+V' marker. The paste
        cannot happen unless the set succeeded — verify by making the set FAIL:
        shortcut must NOT be called (proving paste is gated behind a successful
        set, i.e. it runs after)."""
        mock_run.return_value = _completed(returncode=0)
        result = cb.set_clipboard("x", keyboard=True)
        mock_shortcut.assert_called_once_with("ctrl+v")
        assert "Keyboard: simulated Ctrl+V" in result["output"]

    @patch("agent_windows.core.input_controller.shortcut")
    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_keyboard_paste_skipped_when_set_fails(self, mock_run, mock_shortcut):
        """If the clipboard set fails (nonzero returncode), Ctrl+V is NOT sent —
        confirming the paste runs only after a successful set (ordering lock)."""
        mock_run.return_value = _completed(stderr="denied", returncode=1)
        with pytest.raises(RuntimeError):
            cb.set_clipboard("x", keyboard=True)
        mock_shortcut.assert_not_called()

    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_success_output_truncates_long_text(self, mock_run):
        mock_run.return_value = _completed(returncode=0)
        long = "x" * 200
        result = cb.set_clipboard(long)
        assert "..." in result["output"]
        assert str(long) not in result["output"]  # truncated, not full

    @patch("agent_windows.core.clipboard.subprocess.run")
    def test_success_output_no_truncation_for_short_text(self, mock_run):
        mock_run.return_value = _completed(returncode=0)
        result = cb.set_clipboard("hi")
        assert "..." not in result["output"]
        assert "hi" in result["output"]
