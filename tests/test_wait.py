"""Tests for wait command: sleep, element, and text modes."""

import pytest
import time
import typer
from unittest.mock import patch, MagicMock

from agent_windows.core.tree import ElementInfo, BoundingBox


# ── wait: sleep mode ────────────────────────────────────────────────


def test_wait_sleep():
    """wait with seconds > 0 sleeps and returns success."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["wait"]
    with patch("agent_windows.daemon.handlers.tree.time") as mock_time:
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
def test_wait_element_immediate(mock_config, make_element):
    """wait for element that already exists returns immediately."""
    from agent_windows.daemon.router import HANDLERS

    mock_config.return_value = {"wait": {"poll_interval": 0.5}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.resolve_element") as mock_resolve, \
         patch("agent_windows.daemon.handlers.tree.time") as mock_time:
        mock_resolve.return_value = make_element(name="Submit")
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
         patch("agent_windows.daemon.handlers.tree.time") as mock_time:
        mock_resolve.side_effect = IndexError("not found")
        # Simulate time passing: start=0, then past timeout on first check
        mock_time.time.side_effect = [0.0, 0.0, 0.0, 100.0]
        mock_time.sleep = MagicMock()

        with pytest.raises(TimeoutError, match="not found within"):
            handler({"selector": "@e99", "timeout": 0.1})


# ── wait: text mode ─────────────────────────────────────────────────


@patch("agent_windows.utils.config.load_config")
def test_wait_text_immediate(mock_config, make_element):
    """wait for text that already exists returns immediately."""
    from agent_windows.daemon.router import HANDLERS

    elements = [make_element(name="提交成功")]
    mock_config.return_value = {"wait": {"poll_interval": 0.5}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.get_tree_snapshot") as mock_traverse, \
         patch("agent_windows.daemon.handlers.tree.time") as mock_time:
        mock_traverse.return_value = {"interactive_elements": elements}
        mock_time.time.side_effect = [0.0, 0.0]
        result = handler({"text": "成功", "timeout": 5})

    assert "appeared" in result["output"]
    assert result["text"] == "成功"
    assert result["matched_element"] == "提交成功"


@patch("agent_windows.utils.config.load_config")
def test_wait_text_not_found_timeout(mock_config, make_element):
    """wait for text that never appears raises TimeoutError."""
    from agent_windows.daemon.router import HANDLERS

    elements = [make_element(name="Loading")]
    mock_config.return_value = {"wait": {"poll_interval": 0.01}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.get_tree_snapshot") as mock_traverse, \
         patch("agent_windows.daemon.handlers.tree.time") as mock_time:
        mock_traverse.return_value = {"interactive_elements": elements}
        # start=0, poll check at 100s (past timeout)
        mock_time.time.side_effect = [0.0, 100.0]
        mock_time.sleep = MagicMock()

        with pytest.raises(TimeoutError, match="not found within"):
            handler({"text": "Done", "timeout": 0.1})


# ── wait: interval parameter ────────────────────────────────────────


@patch("agent_windows.utils.config.load_config")
def test_wait_custom_interval(mock_config, make_element):
    """wait respects explicit interval parameter over config default."""
    from agent_windows.daemon.router import HANDLERS

    mock_config.return_value = {"wait": {"poll_interval": 0.5}, "tree": {}}
    handler = HANDLERS["wait"]

    with patch("agent_windows.core.tree.resolve_element") as mock_resolve, \
         patch("agent_windows.daemon.handlers.tree.time") as mock_time:
        mock_resolve.return_value = make_element(name="OK")
        mock_time.time.side_effect = [0.0, 0.0]
        result = handler({"selector": "@e1", "timeout": 5, "interval": 0.1})

    assert "appeared" in result["output"]
    # sleep should NOT have been called (element found immediately)
    mock_time.sleep.assert_not_called()


# ── wait_check: single non-blocking probe (M-28) ──────────────────────


@patch("agent_windows.utils.config.load_config")
def test_wait_check_element_found(mock_config, make_element):
    """wait_check for an existing element returns found=True, no sleep."""
    from agent_windows.daemon.router import HANDLERS

    mock_config.return_value = {"tree": {}}
    handler = HANDLERS["wait_check"]

    with patch("agent_windows.core.tree.resolve_element") as mock_resolve, \
         patch("agent_windows.daemon.handlers.tree.time") as mock_time:
        mock_resolve.return_value = make_element(name="Submit")
        result = handler({"selector": "@e3"})

    assert result["found"] is True
    assert result["element"] == "Submit"
    mock_time.sleep.assert_not_called()


@patch("agent_windows.utils.config.load_config")
def test_wait_check_element_not_found(mock_config):
    """wait_check for a missing element returns found=False (no exception)."""
    from agent_windows.daemon.router import HANDLERS

    mock_config.return_value = {"tree": {}}
    handler = HANDLERS["wait_check"]

    with patch("agent_windows.core.tree.resolve_element") as mock_resolve:
        mock_resolve.side_effect = IndexError("not found")
        result = handler({"selector": "@e99"})

    assert result["found"] is False


@patch("agent_windows.utils.config.load_config")
def test_wait_check_text_found(mock_config, make_element):
    """wait_check for present text returns found=True with the matched element."""
    from agent_windows.daemon.router import HANDLERS

    mock_config.return_value = {"tree": {}}
    handler = HANDLERS["wait_check"]

    with patch("agent_windows.core.tree.get_tree_snapshot") as mock_snap:
        mock_snap.return_value = {"interactive_elements": [make_element(name="提交成功")]}
        result = handler({"text": "成功"})

    assert result["found"] is True
    assert result["matched_element"] == "提交成功"


def test_wait_check_no_args():
    """wait_check with neither selector nor text raises ValueError."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["wait_check"]
    with pytest.raises(ValueError, match="selector or text is required"):
        handler({})


# ── CLI wait: client-side polling (M-28) ──────────────────────────────


def _ctx() -> MagicMock:
    """A minimal typer.Context-like mock with text-format obj."""
    ctx = MagicMock()
    ctx.obj = {"format": "text"}
    return ctx


@patch("agent_windows.utils.config.load_config")
def test_cli_wait_sleep_is_local(mock_config):
    """CLI sleep mode never hits the daemon."""
    from agent_windows.cli.commands.wait import wait

    mock_config.return_value = {"wait": {"poll_interval": 0.5}}
    ctx = _ctx()

    with patch("agent_windows.cli.commands.wait.time") as mock_time, \
         patch("agent_windows.cli.commands.wait.dispatch") as mock_dispatch, \
         patch("agent_windows.cli.commands.wait.print_result") as mock_print:
        wait(ctx, target="3", timeout=30, text="", interval=0)

    mock_dispatch.assert_not_called()
    mock_time.sleep.assert_called_once_with(3.0)
    assert "Slept 3.0s" in mock_print.call_args[0][0]["output"]


@patch("agent_windows.utils.config.load_config")
def test_cli_wait_element_polls_then_found(mock_config):
    """CLI polls wait_check and prints when the element appears."""
    from agent_windows.cli.commands.wait import wait

    mock_config.return_value = {"wait": {"poll_interval": 0.5}}
    ctx = _ctx()

    with patch("agent_windows.cli.commands.wait.time") as mock_time, \
         patch("agent_windows.cli.commands.wait.dispatch") as mock_dispatch, \
         patch("agent_windows.cli.commands.wait.print_result") as mock_print:
        mock_dispatch.return_value = {"found": True, "element": "Submit"}
        mock_time.time.side_effect = [0.0, 0.5]
        wait(ctx, target="@e3", timeout=5, text="", interval=0)

    mock_dispatch.assert_called_once_with("wait_check", {"selector": "@e3"}, ctx)
    mock_time.sleep.assert_not_called()
    out = mock_print.call_args[0][0]
    assert "appeared" in out["output"]
    assert out["element"] == "Submit"


@patch("agent_windows.utils.config.load_config")
def test_cli_wait_text_polls_then_found(mock_config):
    """CLI text-mode polling exercises the matched_element payload branch."""
    from agent_windows.cli.commands.wait import wait

    mock_config.return_value = {"wait": {"poll_interval": 0.5}}
    ctx = _ctx()

    with patch("agent_windows.cli.commands.wait.time") as mock_time, \
         patch("agent_windows.cli.commands.wait.dispatch") as mock_dispatch, \
         patch("agent_windows.cli.commands.wait.print_result") as mock_print:
        mock_dispatch.return_value = {"found": True, "matched_element": "提交成功"}
        mock_time.time.side_effect = [0.0, 0.5]
        wait(ctx, target="", timeout=5, text="成功", interval=0)

    mock_dispatch.assert_called_once_with("wait_check", {"text": "成功"}, ctx)
    out = mock_print.call_args[0][0]
    assert "appeared" in out["output"]
    assert out["text"] == "成功"
    assert out["matched_element"] == "提交成功"


@patch("agent_windows.utils.config.load_config")
def test_cli_wait_element_timeout_exits(mock_config):
    """CLI polls until local timeout, then exits cleanly (no daemon block)."""
    from agent_windows.cli.commands.wait import wait

    mock_config.return_value = {"wait": {"poll_interval": 0.01}}
    ctx = _ctx()

    with patch("agent_windows.cli.commands.wait.time") as mock_time, \
         patch("agent_windows.cli.commands.wait.dispatch") as mock_dispatch:
        mock_dispatch.return_value = {"found": False}
        mock_time.time.side_effect = [0.0, 0.0, 100.0]
        with pytest.raises(typer.Exit):
            wait(ctx, target="@e99", timeout=0.1, text="", interval=0)

    assert mock_dispatch.call_count == 2


def test_cli_wait_target_and_text_conflict():
    """Specifying both a target and --text is rejected before any dispatch."""
    from agent_windows.cli.commands.wait import wait

    ctx = _ctx()

    with patch("agent_windows.cli.commands.wait.dispatch") as mock_dispatch:
        with pytest.raises(typer.Exit):
            wait(ctx, target="@e3", timeout=30, text="foo", interval=0)

    mock_dispatch.assert_not_called()
