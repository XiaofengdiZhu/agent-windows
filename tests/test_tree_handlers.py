"""Tests for daemon tree handlers (snapshot --window/--active scoping)."""
import pytest
from unittest.mock import patch
from agent_windows.daemon.router import HANDLERS
from agent_windows.core.tree import WindowInfo


def _win(title="App", process="app.exe", handle=1, active=False):
    return WindowInfo(
        title=title, process_name=process, handle=handle,
        bbox=None, is_active=active, class_name="", status="",
        is_browser=False,
    )


class TestSnapshotWindowScope:
    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.traverse_tree")
    @patch("agent_windows.core.tree.resolve_window")
    def test_window_forwards_to_resolve_and_traverse(
            self, mock_resolve, mock_traverse, mock_render, mock_cfg):
        win = _win(title="Notepad", process="notepad.exe", handle=12345)
        mock_resolve.return_value = (win, None)
        mock_traverse.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "RENDERED"
        mock_cfg.return_value = {}

        out = HANDLERS["snapshot"]({"window": "note"})

        mock_resolve.assert_called_once_with(value="note", active=False)
        _, kwargs = mock_traverse.call_args
        assert kwargs.get("only_window") is win
        assert "## Window:" in out["output"]
        assert "Notepad" in out["output"]
        assert "notepad.exe" in out["output"]
        assert "RENDERED" in out["output"]

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.traverse_tree")
    @patch("agent_windows.core.tree.resolve_window")
    def test_active_forwards(self, mock_resolve, mock_traverse, mock_render, mock_cfg):
        win = _win(title="FG", process="fg.exe", handle=1, active=True)
        mock_resolve.return_value = (win, None)
        mock_traverse.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "R"
        mock_cfg.return_value = {}

        HANDLERS["snapshot"]({"active": True})

        mock_resolve.assert_called_once_with(value=None, active=True)

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.traverse_tree")
    @patch("agent_windows.core.tree.resolve_window")
    def test_warning_prepended_before_body(
            self, mock_resolve, mock_traverse, mock_render, mock_cfg):
        win = _win(title="calc — main", process="calc.exe", handle=1)
        mock_resolve.return_value = (win, '⚠ Matched 2 windows by title "calc"')
        mock_traverse.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "BODY"
        mock_cfg.return_value = {}

        out = HANDLERS["snapshot"]({"window": "calc"})

        assert "## Window:" in out["output"]
        assert '⚠ Matched 2 windows' in out["output"]
        assert out["output"].index("## Window:") < out["output"].index("BODY")

    @patch("agent_windows.utils.config.load_config")
    @patch("agent_windows.core.tree.render_snapshot")
    @patch("agent_windows.core.tree.traverse_tree")
    def test_no_window_no_header(self, mock_traverse, mock_render, mock_cfg):
        mock_traverse.return_value = {"semantic_tree": "", "interactive_elements": []}
        mock_render.return_value = "PLAIN"
        mock_cfg.return_value = {}

        out = HANDLERS["snapshot"]({})

        assert "## Window:" not in out["output"]
        _, kwargs = mock_traverse.call_args
        assert kwargs.get("only_window") is None

    def test_window_and_active_mutually_exclusive(self):
        with pytest.raises(ValueError, match="mutually exclusive"):
            HANDLERS["snapshot"]({"window": "x", "active": True})

    @patch("agent_windows.core.tree.resolve_window")
    def test_resolve_not_found_propagates_as_value_error(self, mock_resolve):
        mock_resolve.side_effect = ValueError("Window not found: zzz")
        with pytest.raises(ValueError, match="Window not found"):
            HANDLERS["snapshot"]({"window": "zzz"})
