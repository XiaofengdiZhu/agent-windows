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
        mock_call.assert_called_once_with("ping", {}, no_daemon=False, debug=False)
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
    def test_no_daemon_unknown_method_exits(self, mock_windll, mock_coInit, mock_coUninit):
        """In no-daemon mode, unknown method should raise typer.Exit."""
        from agent_windows.cli.dispatcher import dispatch
        ctx = MagicMock()
        ctx.obj = {"no_daemon": True, "debug": False, "format": "text"}
        with pytest.raises(RuntimeError):
            dispatch("nonexistent", {}, ctx=ctx)

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_daemon_mode_passes_params(self, mock_call):
        """dispatch should pass params dict to call_daemon."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"status": "ok"}
        ctx = MagicMock()
        ctx.obj = {"no_daemon": False, "debug": True, "format": "json"}
        params = {"x": 100, "y": 200}
        result = dispatch("click", params, ctx=ctx)
        mock_call.assert_called_once_with("click", params, no_daemon=False, debug=True)
        assert result == {"status": "ok"}

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_daemon_mode_defaults_params(self, mock_call):
        """When params is None, dispatch should pass None to call_daemon."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"status": "ok"}
        ctx = MagicMock()
        ctx.obj = {"no_daemon": False, "debug": False, "format": "text"}
        dispatch("ping", None, ctx=ctx)
        mock_call.assert_called_once_with("ping", None, no_daemon=False, debug=False)

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_no_ctx_defaults_to_daemon_mode(self, mock_call):
        """Without ctx, dispatch should use daemon mode with defaults."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"status": "ok"}
        result = dispatch("ping")
        mock_call.assert_called_once_with("ping", None, no_daemon=False, debug=False)
        assert result == {"status": "ok"}

    @patch("agent_windows.cli.dispatcher.call_daemon")
    def test_empty_ctx_obj_defaults_to_daemon_mode(self, mock_call):
        """With ctx.obj as None, dispatch should default to daemon mode."""
        from agent_windows.cli.dispatcher import dispatch
        mock_call.return_value = {"status": "ok"}
        ctx = MagicMock()
        ctx.obj = None
        result = dispatch("ping", ctx=ctx)
        mock_call.assert_called_once_with("ping", None, no_daemon=False, debug=False)


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

    def test_none_result_no_crash(self, capsys):
        """Passing a dict without 'output' key should not crash."""
        from agent_windows.cli.dispatcher import print_result
        # print_result expects a dict, not None — test empty dict instead
        print_result({"output": ""})
        captured = capsys.readouterr()
        # empty output string is falsy, so it falls through to kv pairs
        # but there are no other keys, so result is empty string
