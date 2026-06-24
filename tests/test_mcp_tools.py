"""Unit tests for the MCP adapter layer.

The daemon is mocked — these tests verify param shaping and the _call contract,
NOT real Windows automation (that's covered by the existing daemon tests).
"""
from unittest.mock import patch

import pytest

from agent_windows.mcp import _call


def test_as_bool_true_variants():
    assert _call._as_bool(True) is True
    assert _call._as_bool("true") is True
    assert _call._as_bool("True") is True


def test_as_bool_false_variants():
    assert _call._as_bool(False) is False
    assert _call._as_bool("false") is False
    assert _call._as_bool(None) is False


def test_as_bool_other_is_false():
    # anything not literally True/"true" is falsy (matches Windows-MCP's _as_bool)
    assert _call._as_bool(0) is False
    assert _call._as_bool("yes") is False


def test_call_forwards_method_and_params_and_returns_result():
    """_call builds a JSONRPCRequest-shaped call and returns the result dict."""
    with patch("agent_windows.mcp._call.call_daemon", return_value={"output": "ok", "path": "x"}) as m:
        result = _call._call("screenshot", output="foo.png", monitor=0)
    m.assert_called_once_with("screenshot", {"output": "foo.png", "monitor": 0})
    assert result == {"output": "ok", "path": "x"}


def test_call_strips_none_params():
    """Optional args omitted by the caller must not be sent as null."""
    with patch("agent_windows.mcp._call.call_daemon", return_value={"output": "ok"}) as m:
        _call._call("ground", description="taskbar", single_pass=None)
    m.assert_called_once_with("ground", {"description": "taskbar"})


def test_call_propagates_daemon_error():
    """A daemon RuntimeError surfaces unchanged so FastMCP reports it."""
    with patch("agent_windows.mcp._call.call_daemon", side_effect=RuntimeError("Daemon error [-32602]: bad")):
        with pytest.raises(RuntimeError, match="bad"):
            _call._call("click", x=1, y=2)


def test_xy_or_selector_requires_one():
    with pytest.raises(ValueError):
        _call.xy_or_selector(None, None)
    with pytest.raises(ValueError):
        _call.xy_or_selector([1, 2], "@e1")


def test_xy_or_selector_selector_wins():
    assert _call.xy_or_selector(None, "@e3") == {"selector": "@e3"}


def test_xy_or_selector_coords():
    assert _call.xy_or_selector([100, 200], None) == {"x": 100, "y": 200}


# ── Observation tools: call the plain functions, daemon mocked ─────────
import base64
from unittest.mock import patch, MagicMock

import agent_windows.mcp.tools.observation as obs


_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M8AAAMBAQDJ/pLvAAAAAElFTkSuQmCC"
)


def test_screenshot_fn_forwards_params_and_returns_image_list():
    result = {"output": "## Screenshot\nPath: C:\\s.png", "path": "C:\\s.png",
              "width": 1, "height": 1, "coord_scale": 1.0}
    with patch.object(obs, "_call", return_value=result) as m, \
         patch("pathlib.Path.read_bytes", return_value=_PNG):
        out = obs.screenshot_fn(output="C:\\s.png", monitor=0, bbox=None, selector=None)
    # screenshot_fn forwards every kwarg to _call (the *_fn layer doesn't strip
    # None — _call itself does, and here _call is mocked). Assert the method +
    # the explicit args; monitor/bbox/selector are passed through verbatim.
    m.assert_called_once_with("screenshot", output="C:\\s.png", monitor=0,
                              bbox=None, selector=None)
    # FastMCP tools that return an image return a list [text, Image]
    assert isinstance(out, list)
    assert out[0].startswith("## Screenshot")


def test_snapshot_fn_forwards_display():
    with patch.object(obs, "_call", return_value={"output": "tree"}) as m:
        out = obs.snapshot_fn(display=[0, 1])
    m.assert_called_once_with("snapshot", display=[0, 1])
    assert out == "tree"


def test_observe_fn_defaults_to_snapshot_plus_screenshot():
    with patch.object(obs, "_call", return_value={"output": "x"}) as m:
        out = obs.observe_fn()
    sent = m.call_args.kwargs
    assert sent["snapshot"] is True and sent["screenshot"] is True
    assert out == "x"


def test_get_text_fn_requires_selector():
    with pytest.raises(ValueError):
        obs.get_text_fn(selector="")


def test_element_state_fn_validates_check():
    with pytest.raises(ValueError):
        obs.element_state_fn(selector="@e1", check="bogus")


def test_cleanup_fn_returns_output():
    with patch.object(obs, "_call", return_value={"output": "done"}):
        assert obs.cleanup_screenshots_fn() == "done"
