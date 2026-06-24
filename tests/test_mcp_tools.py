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


import agent_windows.mcp.tools.vision as vis


def test_ground_fn_forwards_single_pass_as_bool():
    with patch.object(vis, "_call", return_value={"output": "g", "screenshot_path": "p"}) as m, \
         patch("pathlib.Path.read_bytes", return_value=_PNG):
        vis.ground_fn(description="taskbar", single_pass="true")
    sent = m.call_args.kwargs
    assert sent["single_pass"] is True
    assert sent["description"] == "taskbar"


def test_detect_fn_forwards_max_targets_and_image_path():
    with patch.object(vis, "_call", return_value={"output": "d", "screenshot_path": "p",
                                                  "count": 2, "targets": []}) as m, \
         patch("pathlib.Path.read_bytes", return_value=_PNG):
        out = vis.detect_fn(description="icons", max_targets=5, image=None, model=None, output=None)
    # *_fn forwards every kwarg to _call (the *_fn layer doesn't strip None —
    # _call itself does, and here _call is mocked). Assert method + the args
    # the test cares about; image/model/output are passed through as None.
    assert m.call_args.args == ("detect",)
    assert m.call_args.kwargs["description"] == "icons"
    assert m.call_args.kwargs["max_targets"] == 5
    assert isinstance(out, list)


def test_ask_vision_fn_requires_prompt():
    with pytest.raises(ValueError):
        vis.ask_vision_fn(prompt="")


def test_ground_fn_requires_description():
    with pytest.raises(ValueError):
        vis.ground_fn(description="")


import agent_windows.mcp.tools.input as inp


def test_click_fn_with_coords():
    with patch.object(inp, "_call", return_value={"output": "clicked"}) as m:
        out = inp.click_fn(loc=[10, 20], selector=None, right=True, double=False)
    m.assert_called_once_with("click", x=10, y=20, right=True, double=False)
    assert out == "clicked"


def test_click_fn_with_selector():
    with patch.object(inp, "_call", return_value={"output": "ok"}) as m:
        inp.click_fn(loc=None, selector="@e5", right=False, double=False)
    m.assert_called_once_with("click", selector="@e5", right=False, double=False)


def test_click_fn_requires_loc_or_selector():
    with pytest.raises(ValueError):
        inp.click_fn(loc=None, selector=None)


def test_type_fn_forwards_at_selector():
    with patch.object(inp, "_call", return_value={"output": "typed"}) as m:
        inp.type_fn(text="hi", at_selector="@e2")
    m.assert_called_once_with("type", text="hi", at_selector="@e2",
                              enter=False, clear=False, caret_position="idle")


def test_type_fn_forwards_at_coords():
    with patch.object(inp, "_call", return_value={"output": "typed"}) as m:
        inp.type_fn(text="hi", loc=[5, 6])
    m.assert_called_once_with("type", text="hi", at_x=5, at_y=6,
                              enter=False, clear=False, caret_position="idle")


def test_drag_fn_forwards_four_coords():
    """drag_fn forwards all four coordinates + the right flag."""
    with patch.object(inp, "_call", return_value={"output": "ok"}) as m:
        inp.drag_fn(x1=0, y1=1, x2=2, y2=3, right=True)
    m.assert_called_once_with("drag", x1=0, y1=1, x2=2, y2=3, right=True)


def test_scroll_fn_forwards_selector():
    with patch.object(inp, "_call", return_value={"output": "ok"}) as m:
        inp.scroll_fn(loc=None, selector="@e1", direction="up", amount=3)
    m.assert_called_once_with("scroll", selector="@e1", direction="up", amount=3)


import agent_windows.mcp.tools.system as sysmod


def test_app_launch_fn_forwards_args_list():
    with patch.object(sysmod, "_call", return_value={"output": "launched"}) as m:
        sysmod.app_launch_fn(name_or_path="notepad", args=["a.txt"])
    m.assert_called_once_with("app_launch", name_or_path="notepad", args=["a.txt"])


def test_app_resize_fn_requires_width_height():
    with pytest.raises(ValueError):
        sysmod.app_resize_fn(name_or_title="x", width=0, height=0)


def test_fs_write_fn_requires_content():
    with pytest.raises(ValueError):
        sysmod.fs_write_fn(path="p", content=None)


def test_registry_fn_validates_mode():
    with pytest.raises(ValueError):
        sysmod.registry_fn(mode="bogus", path="HKCU:\\S")


def test_clipboard_set_fn_forwards_text():
    with patch.object(sysmod, "_call", return_value={"output": "set"}) as m:
        sysmod.clipboard_set_fn(text="hello")
    # *_fn forwards every kwarg to _call (_call strips None — but it's mocked
    # here). keyboard defaults to `_as_bool(False) or None` = None, passed through.
    m.assert_called_once_with("clipboard_set", text="hello", keyboard=None)


def test_process_kill_fn_accepts_int_pid():
    with patch.object(sysmod, "_call", return_value={"output": "killed"}) as m:
        sysmod.process_kill_fn(pid_or_name=1234)
    m.assert_called_once_with("process_kill", pid_or_name=1234)
