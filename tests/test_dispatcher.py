"""Tests for CLI dispatcher."""
import json
import pytest
from unittest.mock import patch, MagicMock, call


class TestDispatch:
    """Test the dispatch function in cli/dispatcher.py."""

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_daemon_mode_calls_call_daemon(self, mock_call):
        """In daemon mode, dispatch should forward to call_daemon."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"output": "ok"}
        ctx = MagicMock()
        ctx.obj = {"no_daemon": False, "debug": False, "format": "text"}
        result = dispatch("ping", {}, ctx=ctx)
        mock_call.assert_called_once_with("ping", {})
        assert result == {"output": "ok"}

    @patch("agent_windows.daemon.router.HANDLERS", {"ping": lambda p: {"status": "ok"}})
    @patch("pythoncom.CoUninitialize")
    @patch("pythoncom.CoInitialize")
    @patch("ctypes.windll")
    def test_no_daemon_mode_calls_handler(self, mock_windll, mock_coInit, mock_coUninit):
        """In no-daemon mode, dispatch should call handler directly."""
        from agent_windows.cli.dispatcher import dispatch
        ctx = MagicMock()
        ctx.obj = {"no_daemon": True, "debug": False, "format": "text"}
        result = dispatch("ping", {}, ctx=ctx)
        assert result["status"] == "ok"

    @patch("agent_windows.daemon.router.HANDLERS", {})
    @patch("pythoncom.CoUninitialize")
    @patch("pythoncom.CoInitialize")
    @patch("ctypes.windll")
    def test_no_daemon_unknown_method_maps_to_method_not_found(self, mock_windll, mock_coInit, mock_coUninit):
        """M-11: in no-daemon mode, an unknown method surfaces as a RuntimeError
        carrying the METHOD_NOT_FOUND code (via route_request), matching the
        daemon path — not an ad-hoc typer.Exit."""
        from agent_windows.cli.dispatcher import dispatch
        ctx = MagicMock()
        ctx.obj = {"no_daemon": True, "debug": False, "format": "text"}
        with pytest.raises(RuntimeError) as exc_info:
            dispatch("nonexistent", {}, ctx=ctx)
        assert "-32601" in str(exc_info.value)  # METHOD_NOT_FOUND

    @patch(
        "agent_windows.daemon.router.HANDLERS",
        {"boom": MagicMock(side_effect=ValueError("missing required parameter 'x'"))},
    )
    @patch("pythoncom.CoUninitialize")
    @patch("pythoncom.CoInitialize")
    @patch("ctypes.windll")
    def test_no_daemon_handler_error_maps_to_invalid_params(self, mock_windll, mock_coInit, mock_coUninit):
        """M-11: in no-daemon mode, a handler ValueError surfaces as a RuntimeError
        carrying the INVALID_PARAMS code (via route_request) — not a raw
        traceback. Before the fix the handler was called directly, so the
        ValueError leaked instead of mapping to INVALID_PARAMS."""
        from agent_windows.cli.dispatcher import dispatch
        ctx = MagicMock()
        ctx.obj = {"no_daemon": True, "debug": False, "format": "text"}
        with pytest.raises(RuntimeError) as exc_info:
            dispatch("boom", {}, ctx=ctx)
        assert "-32602" in str(exc_info.value)  # INVALID_PARAMS
        assert "missing required parameter 'x'" in str(exc_info.value)

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_daemon_mode_passes_params(self, mock_call):
        """dispatch should pass params dict to call_daemon."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"status": "ok"}
        ctx = MagicMock()
        ctx.obj = {"no_daemon": False, "debug": True, "format": "json"}
        params = {"x": 100, "y": 200}
        result = dispatch("click", params, ctx=ctx)
        mock_call.assert_called_once_with("click", params)
        assert result == {"status": "ok"}

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_daemon_mode_defaults_params(self, mock_call):
        """When params is None, dispatch should pass None to call_daemon."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"status": "ok"}
        ctx = MagicMock()
        ctx.obj = {"no_daemon": False, "debug": False, "format": "text"}
        dispatch("ping", None, ctx=ctx)
        mock_call.assert_called_once_with("ping", None)

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_no_ctx_defaults_to_daemon_mode(self, mock_call):
        """Without ctx, dispatch should use daemon mode with defaults."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"status": "ok"}
        result = dispatch("ping")
        mock_call.assert_called_once_with("ping", None)
        assert result == {"status": "ok"}

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_empty_ctx_obj_defaults_to_daemon_mode(self, mock_call):
        """With ctx.obj as None, dispatch should default to daemon mode."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"status": "ok"}
        ctx = MagicMock()
        ctx.obj = None
        result = dispatch("ping", ctx=ctx)
        mock_call.assert_called_once_with("ping", None)


class TestPrintResult:
    """Test the print_result function."""

    def test_text_format_prints_output_key(self, capsys):
        """Text format should print the 'output' key value."""
        from agent_windows.cli.dispatcher import print_result
        print_result({"output": "Hello", "path": "/tmp/test.png"})
        captured = capsys.readouterr()
        assert "Hello" in captured.out

    def test_json_format_prints_full_dict(self, capsys):
        """JSON format should print the entire result dict as JSON."""
        from agent_windows.cli.dispatcher import print_result
        ctx = MagicMock()
        ctx.obj = {"format": "json"}
        print_result({"output": "test", "key": "value"}, ctx=ctx)
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["key"] == "value"

    def test_text_format_without_output_key_prints_kv_pairs(self, capsys):
        """Text format without 'output' key should print key: value lines."""
        from agent_windows.cli.dispatcher import print_result
        print_result({"status": "ok", "count": 5})
        captured = capsys.readouterr()
        assert "status: ok" in captured.out
        assert "count: 5" in captured.out

    def test_empty_result_no_crash(self, capsys):
        """A result dict with no printable keys (empty dict, or only a falsy
        'output') must print an empty line, not crash."""
        from agent_windows.cli.dispatcher import print_result
        # No keys at all → kv-pairs loop produces nothing → empty text
        print_result({})
        captured = capsys.readouterr()
        assert captured.out == "\n"


class TestStdoutRobustness:
    """L-15: print_result must never raise, however hostile stdout is."""

    def test_no_buffer_stream_still_outputs(self, monkeypatch):
        """A stdout replacement with no ``.buffer`` (IDLE, test captures) must
        still emit text via the echo fallback instead of AttributeError."""
        from agent_windows.cli.dispatcher import print_result

        class _NoBuffer:
            encoding = "utf-8"

            def __init__(self):
                self.parts = []

            def write(self, s):
                self.parts.append(s.decode("utf-8", "replace") if isinstance(s, bytes) else s)

            def flush(self):
                pass

            def isatty(self):
                return False

        fake = _NoBuffer()
        monkeypatch.setattr("sys.stdout", fake)
        print_result({"output": "hello"})  # must not raise
        assert "hello" in "".join(fake.parts)

    def test_broken_buffer_falls_back_to_echo(self, monkeypatch):
        """If the raw buffer write raises, the echo fallback must still emit."""
        from agent_windows.cli.dispatcher import print_result

        class _BadBuffer:
            def write(self, _b):
                raise OSError("broken pipe")

            def flush(self):
                pass

        class _Broken:
            encoding = "utf-8"

            def __init__(self):
                self.buffer = _BadBuffer()
                self.parts = []

            def write(self, s):
                self.parts.append(s.decode("utf-8", "replace") if isinstance(s, bytes) else s)

            def flush(self):
                pass

            def isatty(self):
                return False

        fake = _Broken()
        monkeypatch.setattr("sys.stdout", fake)
        print_result({"output": "recover"})  # must not raise
        assert "recover" in "".join(fake.parts)

    def test_unencodable_chars_coerced_not_raised(self, monkeypatch):
        """A character outside the console codepage must be replaced, not raise
        UnicodeEncodeError (the old typer.echo fallback crashed here)."""
        from agent_windows.cli.dispatcher import print_result

        class _AsciiNoBuffer:
            encoding = "ascii"

            def __init__(self):
                self.parts = []

            def write(self, s):
                self.parts.append(s.decode("utf-8", "replace") if isinstance(s, bytes) else s)

            def flush(self):
                pass

            def isatty(self):
                return False

        fake = _AsciiNoBuffer()
        monkeypatch.setattr("sys.stdout", fake)
        print_result({"output": "café ☕"})  # must not raise
        out = "".join(fake.parts)
        assert "caf" in out          # text still emitted
        assert "é" not in out        # non-ASCII coerced (replaced), not raised


class TestCmdErrorBoundary:
    """review #4: _cmd wraps dispatch in an error boundary. A daemon/connection
    error surfaces as a one-line message (or a JSON error object under
    --format json) with a non-zero exit, instead of an uncaught traceback."""

    def _ctx(self, fmt="text"):
        ctx = MagicMock()
        ctx.obj = {"no_daemon": False, "debug": False, "format": fmt}
        return ctx

    def test_daemon_error_text_mode_prints_message(self, capsys):
        """A DaemonError in text mode prints 'Error: <msg>'. An application-range
        code (-1001 ELEMENT_NOT_FOUND) exits 1."""
        import typer
        from agent_windows.cli.dispatcher import _cmd
        from agent_windows.ipc.client import DaemonError
        with patch("agent_windows.cli.dispatcher.dispatch",
                   side_effect=DaemonError(-1001, "no such element")):
            with pytest.raises(typer.Exit) as ei:
                _cmd(self._ctx("text"), "click", x=1, y=2)
        assert ei.value.exit_code == 1
        assert "no such element" in capsys.readouterr().out

    def test_protocol_error_exits_two(self, capsys):
        """A protocol-range code (-32602 INVALID_PARAMS) exits 2, NOT 1 — matching
        the Rust launcher's exit_code_for so both entry points agree. This is the
        key consistency property: a script can't get exit 1 (app error) from one
        binary and exit 2 (bad params) from the other for the same daemon error."""
        import typer
        from agent_windows.cli.dispatcher import _cmd
        from agent_windows.ipc.client import DaemonError
        with patch("agent_windows.cli.dispatcher.dispatch",
                   side_effect=DaemonError(-32602, "bad params")):
            with pytest.raises(typer.Exit) as ei:
                _cmd(self._ctx("text"), "click", x=1, y=2)
        assert ei.value.exit_code == 2

    def test_daemon_error_json_mode_emits_structured_object(self, capsys):
        """--format json on the error path emits {"error":..,"code":..} (previously
        --format json was silently dropped on errors and a traceback escaped)."""
        import typer
        from agent_windows.cli.dispatcher import _cmd
        from agent_windows.ipc.client import DaemonError
        with patch("agent_windows.cli.dispatcher.dispatch",
                   side_effect=DaemonError(-32602, "bad params")):
            with pytest.raises(typer.Exit) as ei:
                _cmd(self._ctx("json"), "click", x=1, y=2)
        assert ei.value.exit_code == 2  # protocol-range code → exit 2
        data = json.loads(capsys.readouterr().out)
        assert data == {"error": "bad params", "code": -32602}

    def test_connection_error_exits_two(self, capsys):
        """ConnectionError (daemon down) is a distinct infra failure → exit 2,
        separable from an app error (exit 1) so scripts can tell them apart."""
        import typer
        from agent_windows.cli.dispatcher import _cmd
        with patch("agent_windows.cli.dispatcher.dispatch",
                   side_effect=ConnectionError("daemon did not become ready")):
            with pytest.raises(typer.Exit) as ei:
                _cmd(self._ctx("text"), "ping")
        assert ei.value.exit_code == 2
        assert "daemon did not become ready" in capsys.readouterr().out

    def test_connection_error_json_mode(self, capsys):
        """ConnectionError under --format json: code is omitted (no JSON-RPC code)."""
        import typer
        from agent_windows.cli.dispatcher import _cmd
        with patch("agent_windows.cli.dispatcher.dispatch",
                   side_effect=ConnectionError("pipe broke")):
            with pytest.raises(typer.Exit) as ei:
                _cmd(self._ctx("json"), "ping")
        assert ei.value.exit_code == 2
        data = json.loads(capsys.readouterr().out)
        assert data == {"error": "pipe broke"}

    def test_unexpected_exception_not_swallowed(self):
        """Genuinely unexpected bugs must still propagate as tracebacks — the
        boundary catches only ConnectionError/DaemonError, not BaseException."""
        from agent_windows.cli.dispatcher import _cmd
        with patch("agent_windows.cli.dispatcher.dispatch",
                   side_effect=RuntimeError("unexpected")):
            # A plain RuntimeError (not a DaemonError) is NOT caught by the
            # boundary — only DaemonError/ConnectionError are.
            with pytest.raises(RuntimeError, match="unexpected"):
                _cmd(self._ctx("text"), "ping")

    def test_success_path_still_prints_result(self, capsys):
        """Regression guard for the _write_output/_format refactor: a successful
        dispatch must still reach print_result → stdout in both formats. The
        error-boundary tests all exercise the except branch; this locks the
        success branch end-to-end through _cmd."""
        from agent_windows.cli.dispatcher import _cmd
        with patch("agent_windows.cli.dispatcher.dispatch",
                   return_value={"output": "## Snapshot\nok"}):
            _cmd(self._ctx("text"), "snapshot")
        assert "## Snapshot" in capsys.readouterr().out

    def test_success_path_json(self, capsys):
        """Success path under --format json: the full result dict is emitted as JSON."""
        from agent_windows.cli.dispatcher import _cmd
        with patch("agent_windows.cli.dispatcher.dispatch",
                   return_value={"output": "ok", "count": 3}):
            _cmd(self._ctx("json"), "snapshot")
        data = json.loads(capsys.readouterr().out)
        assert data == {"output": "ok", "count": 3}
