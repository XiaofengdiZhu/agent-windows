"""Tests for core/notifier.py — Windows toast notification via PowerShell WinRT.

These are pure-function tests: subprocess.run is patched so no real PowerShell
or toast is shown. The focus is the security boundary (XML escaping, app_id
injection guard), the DoS bounds, and the output shape — not the Win32 behavior.
"""

from unittest.mock import patch, MagicMock

import pytest

from agent_windows.core import notifier
from agent_windows.core.notifier import (
    send_notification,
    _xml_escape,
    _build_toast_xml,
    _build_ps_command,
    DEFAULT_APP_ID,
    MAX_TITLE_LEN,
    MAX_MESSAGE_LEN,
    MAX_APP_ID_LEN,
)


def _ok_run(stdout="", stderr="", returncode=0):
    """A completed subprocess.run result double."""
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


# ── _xml_escape ─────────────────────────────────────────────────────


class TestXmlEscaping:
    def test_ampersand_escaped(self):
        assert _xml_escape("a & b") == "a &amp; b"

    def test_angle_brackets_escaped(self):
        assert _xml_escape("<tag>") == "&lt;tag&gt;"

    def test_quotes_escaped(self):
        assert _xml_escape('"q"') == "&quot;q&quot;"
        assert _xml_escape("it's") == "it&apos;s"

    def test_injection_payload_neutralized(self):
        """A payload that tries to close the text node and inject a node is
        inert: the angle brackets become entities so it renders as literal text."""
        xml = _build_toast_xml("ok", "</text><script>alert(1)</script>")
        assert "<script>alert(1)</script>" not in xml
        assert "&lt;/text&gt;&lt;script&gt;" in xml

    def test_plain_text_unchanged(self):
        assert _xml_escape("Hello World") == "Hello World"

    def test_illegal_control_chars_stripped(self):
        """XML 1.0 forbids most C0 control codes (except \\t \\n \\r); they're
        stripped so LoadXml doesn't throw with a cryptic error."""
        assert _xml_escape("a\x00b\x07c") == "abc"
        assert _xml_escape("a\tb\nc") == "a\tb\nc"  # tab/newline kept


# ── template structure ──────────────────────────────────────────────


class TestTemplateStructure:
    def test_toast_xml_has_two_text_nodes(self):
        xml = _build_toast_xml("Title", "Body")
        assert xml.count("<text>") == 2
        assert "<text>Title</text>" in xml
        assert "<text>Body</text>" in xml
        assert 'template="ToastGeneric"' in xml

    def test_ps_command_loads_winrt_and_shows(self):
        cmd = _build_ps_command("my.app")
        # WinRT type loads
        assert "ToastNotificationManager" in cmd
        assert "Windows.Data.Xml.Dom.XmlDocument" in cmd
        # app_id in a single-quoted assignment
        assert "$appId = 'my.app'" in cmd
        # XML read from stdin via $input (NOT interpolated), then toast shown
        assert "$input -join" in cmd
        assert "$notifier.Show($toast)" in cmd


# ── injection (SEC-05 pattern, the main security test) ──────────────


class TestNoPowerShellInterpolation:
    """The toast XML rides on stdin; the command is a CONSTANT independent of
    title/message. A $() payload in the user's text can't execute as PowerShell
    because PowerShell never parses it as code (only as LoadXml input)."""

    def test_command_constant_regardless_of_title(self):
        """The -Command argv is byte-identical no matter what title/message is."""
        with patch("subprocess.run", return_value=_ok_run()) as m1:
            send_notification("nice title", "ok")
        with patch("subprocess.run", return_value=_ok_run()) as m2:
            send_notification("$(Remove-Item -Recurse C:\\)", "$env:PATH")
        cmd1 = m1.call_args.args[0]
        cmd2 = m2.call_args.args[0]
        assert cmd1 == cmd2  # command unchanged by hostile content

    def test_hostile_payload_on_stdin_not_in_command(self):
        """$()-laden text is on stdin (as escaped XML text), never in -Command."""
        hostile = "$(Get-Process) $evil"
        with patch("subprocess.run", return_value=_ok_run()) as m_run:
            send_notification(hostile, hostile)
        kwargs = m_run.call_args.kwargs
        argv = m_run.call_args.args[0]
        # The hostile text is in the stdin input (as XML text nodes — $ and ( are
        # not XML-special so they ride through unescaped, which is safe because
        # PowerShell parses this as LoadXml data, not code).
        assert kwargs["input"].count(hostile) == 2  # title + message nodes
        command_str = argv[-1]
        assert "$(" not in command_str
        assert "Get-Process" not in command_str

    def test_xml_pipes_via_input_kwarg(self):
        """The XML is passed as the `input=` kwarg (stdin), not built into argv."""
        with patch("subprocess.run", return_value=_ok_run()) as m_run:
            send_notification("T", "M")
        kwargs = m_run.call_args.kwargs
        assert "input" in kwargs
        assert "<toast>" in kwargs["input"]


# ── length bounds (DoS) ─────────────────────────────────────────────


class TestLengthBounds:
    def test_overlong_title_rejected(self):
        with pytest.raises(ValueError, match="title exceeds"):
            send_notification("x" * (MAX_TITLE_LEN + 1), "ok")

    def test_overlong_message_rejected(self):
        with pytest.raises(ValueError, match="message exceeds"):
            send_notification("ok", "x" * (MAX_MESSAGE_LEN + 1))

    def test_overlong_app_id_rejected(self):
        with pytest.raises(ValueError, match="app_id exceeds"):
            send_notification("ok", "ok", app_id="x" * (MAX_APP_ID_LEN + 1))

    def test_title_at_limit_ok(self):
        with patch("subprocess.run", return_value=_ok_run()):
            send_notification("x" * MAX_TITLE_LEN, "ok")  # no raise


# ── app_id injection guard ──────────────────────────────────────────


class TestAppIdValidation:
    def test_single_quote_rejected(self):
        """A single quote breaks out of the PS single-quoted app_id assignment —
        reject it outright rather than escaping."""
        with pytest.raises(ValueError, match="single quote"):
            send_notification("T", "M", app_id="a'b")

    def test_default_app_id_used_when_omitted(self):
        with patch("subprocess.run", return_value=_ok_run()) as m_run:
            r = send_notification("T", "M")
        assert r["app_id"] == DEFAULT_APP_ID
        command_str = m_run.call_args.args[0][-1]
        assert DEFAULT_APP_ID in command_str

    def test_custom_app_id_forwarded(self):
        with patch("subprocess.run", return_value=_ok_run()) as m_run:
            send_notification("T", "M", app_id="my.custom.app")
        command_str = m_run.call_args.args[0][-1]
        assert "$appId = 'my.custom.app'" in command_str


# ── send_notification output shape + error paths ────────────────────


class TestSendNotification:
    def test_success_sent_true(self):
        with patch("subprocess.run", return_value=_ok_run(returncode=0)):
            r = send_notification("Hello", "World")
        assert r["sent"] is True
        assert r["exit_code"] == 0
        assert "Hello" in r["output"]
        assert "World" in r["output"]
        for key in ("output", "exit_code", "stdout", "stderr", "app_id", "sent"):
            assert key in r

    def test_nonzero_exit_sent_false_with_stderr(self):
        """A failing PowerShell (e.g. invalid AUMID) doesn't raise — the toast
        may still have shown. sent=False + stderr surfaced for diagnosis."""
        with patch("subprocess.run",
                   return_value=_ok_run(stderr="boom", returncode=1)):
            r = send_notification("T", "M")
        assert r["sent"] is False
        assert r["exit_code"] == 1
        assert "boom" in r["output"]

    def test_timeout_raises_timeout_error(self):
        """Timeout → TimeoutError (NOT RuntimeError) so the router maps it to
        WAIT_TIMEOUT (-1003), not the generic DAEMON_INTERNAL_ERROR."""
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=30)):
            with pytest.raises(TimeoutError, match="timed out"):
                send_notification("T", "M")

    def test_missing_powershell_raises_runtime_error(self):
        with patch("subprocess.run", side_effect=FileNotFoundError()):
            with pytest.raises(RuntimeError, match="PowerShell not found"):
                send_notification("T", "M")

    def test_non_string_inputs_rejected(self):
        with pytest.raises(ValueError, match="must be strings"):
            send_notification(123, "ok")  # type: ignore[arg-type]

    def test_uses_powershell_not_pwsh(self):
        """WinRT toast types are unavailable in pwsh 7+; verify `powershell`
        is the executable."""
        with patch("subprocess.run", return_value=_ok_run()) as m_run:
            send_notification("T", "M")
        cmd = m_run.call_args.args[0]
        assert cmd[0] == "powershell"
        assert "-NoProfile" in cmd
        assert "-NonInteractive" in cmd
