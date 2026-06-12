"""Tests for wait command: sleep, element, and text modes."""

import pytest
import time
from unittest.mock import patch, MagicMock

from agent_windows.core.tree import ElementInfo, BoundingBox


def _make_element(name: str = "Button", control_type: str = "ButtonControl",
                  bbox: BoundingBox | None = None, is_enabled: bool = True,
                  is_offscreen: bool = False) -> ElementInfo:
    return ElementInfo(
        name=name,
        control_type=control_type,
        bbox=bbox or BoundingBox(100, 200, 300, 250),
        is_interactive=True,
        is_enabled=is_enabled,
        is_offscreen=is_offscreen,
    )


# ── wait: sleep mode ────────────────────────────────────────────────


def test_wait_sleep():
    """wait with seconds > 0 sleeps and returns success."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["wait"]
    with patch("agent_windows.daemon.router.time") as mock_time:
        mock_time.time.side_effect = [0.0, 0.3]  # start, after sleep
        result = handler({"seconds": 0.3})

    assert "Slept" in result["output"]
    assert "0.3" in result["output"]


def test_wait_sleep_zero():
    """wait with seconds=0 still works (no-op sleep)."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["wait"]
    result = handler({"seconds": 0.001})
    assert "Slept" in result["output"]


def test_wait_no_args():
    """wait with no seconds/selector/text raises ValueError."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["wait"]
    with pytest.raises(ValueError, match="provide seconds"):
        handler({})


# ── wait: element mode ──────────────────────────────────────────────


@patch("agent_windows.utils.config.load_config")
def test_wait_element_immediate(mock_config):
    """wait for element that already exists returns immediately."""
    from agent_windows.daemon.router import HANDLERS

    mock_config.return_value = {"wait": {"poll_interval": 0.5}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.resolve_element") as mock_resolve, \
         patch("agent_windows.daemon.router.time") as mock_time:
        mock_resolve.return_value = _make_element(name="Submit")
        mock_time.time.side_effect = [0.0, 0.0]  # start, after resolve
        result = handler({"selector": "@e3", "timeout": 5})

    assert "appeared" in result["output"]
    assert result["selector"] == "@e3"


@patch("agent_windows.utils.config.load_config")
def test_wait_element_not_found_timeout(mock_config):
    """wait for element that never appears raises TimeoutError."""
    from agent_windows.daemon.router import HANDLERS

    mock_config.return_value = {"wait": {"poll_interval": 0.01}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.resolve_element") as mock_resolve, \
         patch("agent_windows.daemon.router.time") as mock_time:
        mock_resolve.side_effect = IndexError("not found")
        # Simulate time passing: start=0, then past timeout on first check
        mock_time.time.side_effect = [0.0, 0.0, 0.0, 100.0]
        mock_time.sleep = MagicMock()

        with pytest.raises(TimeoutError, match="not found within"):
            handler({"selector": "@e99", "timeout": 0.1})


# ── wait: text mode ─────────────────────────────────────────────────


@patch("agent_windows.utils.config.load_config")
def test_wait_text_immediate(mock_config):
    """wait for text that already exists returns immediately."""
    from agent_windows.daemon.router import HANDLERS

    elements = [_make_element(name="提交成功")]
    mock_config.return_value = {"wait": {"poll_interval": 0.5}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.traverse_tree") as mock_traverse, \
         patch("agent_windows.daemon.router.time") as mock_time:
        mock_traverse.return_value = {"interactive_elements": elements}
        mock_time.time.side_effect = [0.0, 0.0]
        result = handler({"text": "成功", "timeout": 5})

    assert "appeared" in result["output"]
    assert result["text"] == "成功"
    assert result["matched_element"] == "提交成功"


@patch("agent_windows.utils.config.load_config")
def test_wait_text_not_found_timeout(mock_config):
    """wait for text that never appears raises TimeoutError."""
    from agent_windows.daemon.router import HANDLERS

    elements = [_make_element(name="Loading")]
    mock_config.return_value = {"wait": {"poll_interval": 0.01}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.traverse_tree") as mock_traverse, \
         patch("agent_windows.daemon.router.time") as mock_time:
        mock_traverse.return_value = {"interactive_elements": elements}
        # start=0, poll check at 100s (past timeout)
        mock_time.time.side_effect = [0.0, 100.0]
        mock_time.sleep = MagicMock()

        with pytest.raises(TimeoutError, match="not found within"):
            handler({"text": "Done", "timeout": 0.1})


# ── wait: interval parameter ────────────────────────────────────────


@patch("agent_windows.utils.config.load_config")
def test_wait_custom_interval(mock_config):
    """wait respects explicit interval parameter over config default."""
    from agent_windows.daemon.router import HANDLERS

    mock_config.return_value = {"wait": {"poll_interval": 0.5}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.resolve_element") as mock_resolve, \
         patch("agent_windows.daemon.router.time") as mock_time:
        mock_resolve.return_value = _make_element(name="OK")
        mock_time.time.side_effect = [0.0, 0.0]
        result = handler({"selector": "@e1", "timeout": 5, "interval": 0.1})

    assert "appeared" in result["output"]
    # sleep should NOT have been called (element found immediately)
    mock_time.sleep.assert_not_called()
