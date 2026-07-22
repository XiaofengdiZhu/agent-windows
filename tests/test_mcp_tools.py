"""Unit tests for the MCP adapter layer.

The daemon is mocked — these tests verify param shaping and the _call contract,
NOT real Windows automation (that's covered by the existing daemon tests).
"""
from unittest.mock import patch

import pytest

# `agent_windows.mcp._call` is a submodule (not re-exported at the package top
# level — see mcp/__init__.py), so this binds to the module and
# `_call._call` / `_call._as_bool` / `_call.xy_or_selector` resolve correctly.
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
    # scale/max_width/max_height default to None (TODO #15b); _call strips them.
    # use_annotation/width_reference_line/height_reference_line (TODO #14) also
    # default to None and are forwarded so _call can strip them.
    # display (TODO #4) defaults to None → no bbox conversion.
    m.assert_called_once_with("screenshot", output="C:\\s.png", monitor=0,
                              bbox=None, selector=None,
                              scale=None, max_width=None, max_height=None,
                              use_annotation=None,
                              width_reference_line=None,
                              height_reference_line=None)
    # FastMCP tools that return an image return a list [text, Image]
    assert isinstance(out, list)
    assert out[0].startswith("## Screenshot")


def test_screenshot_fn_display_converts_to_bbox():
    """display resolves to a bbox before _call (the daemon screenshot handler
    has no display param). Mirrors observe.py's conversion. (TODO #4)"""
    from agent_windows.core.tree import BoundingBox
    result = {"output": "## Screenshot", "path": "C:\\s.png",
              "width": 1, "height": 1, "coord_scale": 1.0}
    with patch.object(obs, "_call", return_value=result) as m, \
         patch("pathlib.Path.read_bytes", return_value=_PNG), \
         patch("agent_windows.core.display.get_display_union_bbox",
               return_value=BoundingBox(0, 0, 1920, 1080)):
        obs.screenshot_fn(display=[0])
    # display=[0] → union bbox → bbox list; never forwarded as `display`.
    sent = m.call_args.kwargs
    assert sent["bbox"] == [0, 0, 1920, 1080]
    assert "display" not in sent

    # display wins over an explicit bbox (matches observe semantics).
    with patch.object(obs, "_call", return_value=result) as m, \
         patch("pathlib.Path.read_bytes", return_value=_PNG), \
         patch("agent_windows.core.display.get_display_union_bbox",
               return_value=BoundingBox(100, 200, 1000, 2000)):
        obs.screenshot_fn(display=[1], bbox=[0, 0, 10, 10])
    assert m.call_args.kwargs["bbox"] == [100, 200, 1000, 2000]


def test_screenshot_fn_bad_display_raises_valueerror():
    """A bad display index raises ValueError in the MCP layer (NOT caught into
    a retryable string) — a bad index is a caller bug, not a transient failure,
    so "please try again" framing would mislead. (TODO #4) Pins this deliberate
    decision so a future refactor moving conversion into try/except is caught."""
    # get_display_union_bbox raises ValueError on an out-of-range index.
    with patch("agent_windows.core.display.get_display_union_bbox",
               side_effect=ValueError("monitor index out of range")):
        with pytest.raises(ValueError, match="out of range"):
            obs.screenshot_fn(display=[99])


def test_screenshot_fn_forwards_annotation_params():
    """use_annotation + grid params are forwarded verbatim to _call. (TODO #14)
    Daemon is mocked — this verifies the wiring, not the box drawing."""
    result = {"output": "## Screenshot\nPath: C:\\s.png\nAnnotated: 3/3",
              "path": "C:\\s.png", "width": 1, "height": 1, "coord_scale": 1.0}
    with patch.object(obs, "_call", return_value=result) as m, \
         patch("pathlib.Path.read_bytes", return_value=_PNG):
        out = obs.screenshot_fn(use_annotation=True,
                                width_reference_line=4,
                                height_reference_line=3)
    m.assert_called_once_with("screenshot", output=None, monitor=-1,
                              bbox=None, selector=None,
                              scale=None, max_width=None, max_height=None,
                              use_annotation=True,
                              width_reference_line=4,
                              height_reference_line=3)
    assert isinstance(out, list)
    assert out[0].startswith("## Screenshot")


def test_snapshot_fn_forwards_display():
    with patch.object(obs, "_call", return_value={"output": "tree"}) as m:
        out = obs.snapshot_fn(display=[0, 1])
    m.assert_called_once_with("snapshot", display=[0, 1])
    assert out == "tree"


def test_snapshot_fn_forwards_window():
    with patch("agent_windows.mcp.tools.observation._call") as m:
        obs.snapshot_fn(window="note")
    m.assert_called_once_with("snapshot", window="note")


def test_snapshot_fn_forwards_active():
    with patch("agent_windows.mcp.tools.observation._call") as m:
        obs.snapshot_fn(active=True)
    m.assert_called_once_with("snapshot", active=True)


def test_snapshot_fn_forwards_all_combined():
    # active=False is the default — it must NOT inject the key (mirrors the
    # CLI/handler: only a truthy active is sent).
    with patch("agent_windows.mcp.tools.observation._call") as m:
        obs.snapshot_fn(display=[0], window="note", active=False)
    m.assert_called_once_with("snapshot", display=[0], window="note")


def test_observe_fn_defaults_to_snapshot_plus_screenshot():
    with patch.object(obs, "_call", return_value={"output": "x"}) as m:
        out = obs.observe_fn()
    sent = m.call_args.kwargs
    assert sent["snapshot"] is True and sent["screenshot"] is True
    assert out == "x"


# ── error resilience (TODO #12): failures return retryable text, not raise ──


def test_screenshot_fn_returns_retryable_text_on_failure():
    """A daemon error must not crash the tool — return retryable [text] instead.
    (TODO #12)"""
    with patch.object(obs, "_call", side_effect=RuntimeError("backend not ready [-1999]")):
        out = obs.screenshot_fn()
    assert isinstance(out, list)
    assert len(out) == 1
    # retryable framing AND carries the underlying message (informative, not vague)
    assert "Please try again" in out[0]
    assert "backend not ready" in out[0]


def test_snapshot_fn_returns_retryable_text_on_failure():
    with patch.object(obs, "_call", side_effect=RuntimeError("UIA init failed [-1003]")):
        out = obs.snapshot_fn()
    assert isinstance(out, str)
    assert "Please try again" in out
    assert "UIA init failed" in out


def test_observe_fn_returns_retryable_text_on_failure():
    with patch.object(obs, "_call", side_effect=RuntimeError("observe error")):
        out = obs.observe_fn()
    assert isinstance(out, str)
    assert "Please try again" in out
    assert "observe error" in out


def test_get_text_fn_requires_selector():
    with pytest.raises(ValueError):
        obs.get_text_fn(selector="")


def test_element_state_fn_validates_check():
    with pytest.raises(ValueError):
        obs.element_state_fn(selector="@e1", check="bogus")


def test_cleanup_fn_returns_output():
    with patch.object(obs, "_call", return_value={"output": "done"}):
        assert obs.cleanup_screenshots_fn() == "done"


def test_list_displays_fn_returns_output():
    with patch.object(obs, "_call", return_value={"output": "## Displays (1)"}):
        assert obs.list_displays_fn() == "## Displays (1)"


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


def test_key_down_fn_forwards_hold():
    with patch.object(inp, "_call", return_value={"output": "ok"}) as m:
        out = inp.key_down_fn(keys="a", hold=0.5)
    m.assert_called_once_with("key_down", keys="a", hold=0.5)
    assert out == "ok"


def test_key_down_fn_omits_hold_when_none():
    """hold=None → hold not forwarded (keys stay pressed; pair with key_up)."""
    with patch.object(inp, "_call", return_value={"output": "ok"}) as m:
        inp.key_down_fn(keys="ctrl+a")
    m.assert_called_once_with("key_down", keys="ctrl+a")


def test_key_up_fn_forwards_keys():
    with patch.object(inp, "_call", return_value={"output": "ok"}) as m:
        inp.key_up_fn(keys="ctrl+a")
    m.assert_called_once_with("key_up", keys="ctrl+a")


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


def test_fs_delete_fn_forwards_permanent_dry_run():
    with patch.object(sysmod, "_call", return_value={"output": "ok"}) as m:
        sysmod.fs_delete_fn(path="p", permanent=True, dry_run=True)
    m.assert_called_once_with("fs_delete", path="p", recursive=False,
                              permanent=True, dry_run=True)


def test_fs_delete_fn_defaults():
    with patch.object(sysmod, "_call", return_value={"output": "ok"}) as m:
        sysmod.fs_delete_fn(path="p")
    m.assert_called_once_with("fs_delete", path="p", recursive=False,
                              permanent=False, dry_run=False)


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
    # *_fn forwards pid_or_name + force(_as_bool(False) or None → None) to _call
    m.assert_called_once_with("process_kill", pid_or_name=1234, force=None)


def test_process_kill_fn_coerces_force_string_true():
    """force='true' (string, as some MCP clients send) → force=True forwarded."""
    with patch.object(sysmod, "_call", return_value={"output": "force-killed"}) as m:
        sysmod.process_kill_fn(pid_or_name="notepad", force="true")
    m.assert_called_once_with("process_kill", pid_or_name="notepad", force=True)


def test_process_list_fn_defaults_forwarded():
    with patch.object(sysmod, "_call", return_value={"output": "list"}) as m:
        sysmod.process_list_fn()
    m.assert_called_once_with("process_list", sort_by="memory", limit=0, name=None)


def test_process_list_fn_forwards_filter_params():
    with patch.object(sysmod, "_call", return_value={"output": "list"}) as m:
        sysmod.process_list_fn(sort_by="cpu", limit=10, name="edge")
    m.assert_called_once_with("process_list", sort_by="cpu", limit=10, name="edge")


def test_lock_fn_returns_output():
    with patch.object(sysmod, "_call", return_value={"output": "## Lock\nWorkstation locked."}):
        assert sysmod.lock_fn() == "## Lock\nWorkstation locked."


def test_sleep_fn_returns_output():
    with patch.object(sysmod, "_call", return_value={"output": "## Sleep\nSystem suspended."}):
        assert sysmod.sleep_fn() == "## Sleep\nSystem suspended."


def test_shutdown_fn_forwards_delay_force():
    captured = {}
    def fake(method, **p):
        captured.update(method=method, **p)
        return {"output": "## Shutdown\n..."}
    with patch.object(sysmod, "_call", side_effect=fake):
        sysmod.shutdown_fn(delay=30, force=True)
    assert captured == {"method": "system_shutdown", "delay": 30, "force": True}


def test_restart_fn_forwards_delay_force():
    captured = {}
    def fake(method, **p):
        captured.update(method=method, **p)
        return {"output": "## Restart\n..."}
    with patch.object(sysmod, "_call", side_effect=fake):
        sysmod.restart_fn(delay=60, force=False)
    assert captured == {"method": "system_restart", "delay": 60, "force": False}


import agent_windows.mcp.tools.batch as bat


def test_batch_fn_requires_non_empty_commands():
    with pytest.raises(ValueError):
        bat.batch_fn(commands=[])


def test_batch_fn_forwards_shape():
    cmds = [{"method": "ping"}, {"method": "app_list"}]
    with patch.object(bat, "_call", return_value={"output": "ok"}) as m:
        bat.batch_fn(commands=cmds, delay=0.1, stop_on_error=False)
    m.assert_called_once_with("batch", commands=cmds, delay=0.1, stop_on_error=False)


# ── batch tool-filter enforcement (review #7) ──────────────────────────


def test_batch_fn_allows_all_when_no_server():
    """No MCP server built (unit tests, library use) → enforcement is a no-op,
    every method forwards. This is the default path when no mcp serve is running."""
    cmds = [{"method": "shell"}, {"method": "fs_delete"}]
    with patch("agent_windows.mcp.server.mcp_server_built", return_value=False), \
         patch.object(bat, "_call", return_value={"output": "ok"}) as m:
        bat.batch_fn(commands=cmds)
    m.assert_called_once_with("batch", commands=cmds, stop_on_error=True, delay=0.0)


def _server_with(allowed):
    """Patch a built MCP server whose post-filter registry is `allowed`."""
    from contextlib import ExitStack
    stack = ExitStack()
    stack.enter_context(patch("agent_windows.mcp.server.mcp_server_built", return_value=True))
    stack.enter_context(patch("agent_windows.mcp.server.registered_tool_names", return_value=set(allowed)))
    return stack


def test_batch_fn_blocks_excluded_method():
    """With an MCP filter that excludes 'shell' (it's not in allowed_tools), a
    batch sub-command calling method 'shell' is blocked before forwarding — the
    filter can't be bypassed via batch."""
    cmds = [{"method": "ping"}, {"method": "shell", "params": {}}]
    with _server_with({"ping", "app_list", "screenshot"}), \
         patch.object(bat, "_call", return_value={"output": "ok"}) as m:
        with pytest.raises(ValueError, match="blocked by the MCP tool filter"):
            bat.batch_fn(commands=cmds)
    m.assert_not_called()  # nothing forwarded when a command is blocked


def test_batch_fn_blocks_only_the_excluded_command():
    """The error names the offending method(s), so the caller knows which to drop."""
    cmds = [{"method": "ping"}, {"method": "fs_delete"}, {"method": "shell"}]
    with _server_with({"ping"}), \
         patch.object(bat, "_call", return_value={"output": "ok"}):
        with pytest.raises(ValueError) as ei:
            bat.batch_fn(commands=cmds)
    msg = str(ei.value)
    assert "fs_delete" in msg
    assert "shell" in msg
    assert "ping" not in msg  # the allowed method isn't named as blocked


def test_batch_fn_maps_renamed_method_for_filter():
    """Daemon method names that differ from MCP tool names are mapped before the
    filter check: excluding MCP tool 'ask_vision' blocks daemon method
    'ask-vision'. Without the map, the hyphen/underscore mismatch would let it
    through."""
    cmds = [{"method": "ask-vision", "params": {}}]
    with _server_with({"ping"}), \
         patch.object(bat, "_call", return_value={"output": "ok"}) as m:
        with pytest.raises(ValueError, match="ask-vision"):
            bat.batch_fn(commands=cmds)
    m.assert_not_called()


def test_batch_fn_renamed_method_allowed_when_not_excluded():
    """Conversely, when ask_vision IS allowed, the renamed method forwards fine."""
    cmds = [{"method": "ask-vision"}]
    with _server_with({"ping", "ask_vision"}), \
         patch.object(bat, "_call", return_value={"output": "ok"}) as m:
        bat.batch_fn(commands=cmds)
    m.assert_called_once_with("batch", commands=cmds, stop_on_error=True, delay=0.0)


def test_batch_fn_empty_registry_fails_closed():
    """An over-restrictive filter (--tools typo) empties the registry. Enforcement
    must fail CLOSED: block everything, NOT silently allow all. The gate is on
    whether a server is built, not on whether the allowed set is non-empty."""
    cmds = [{"method": "ping"}]
    with _server_with(set()), \
         patch.object(bat, "_call", return_value={"output": "ok"}) as m:
        with pytest.raises(ValueError, match="ping"):
            bat.batch_fn(commands=cmds)
    m.assert_not_called()


def test_multi_select_fn_requires_locs_or_selectors():
    with pytest.raises(ValueError):
        bat.multi_select_fn()


def test_multi_select_fn_rejects_both():
    with pytest.raises(ValueError):
        bat.multi_select_fn(locs=[[1, 2]], selectors=["@e1"])


def test_multi_edit_fn_forwards_locs():
    with patch.object(bat, "_call", return_value={"output": "ok"}) as m:
        bat.multi_edit_fn(locs=[[1, 2, "hi"]])
    # *_fn forwards locs + selectors(None default) verbatim (_call strips None;
    # it's mocked here). Assert method + locs; selectors passed through as None.
    m.assert_called_once_with("multi_edit", locs=[[1, 2, "hi"]], selectors=None)


import agent_windows.mcp.tools.control as ctl


def test_ping_fn_reads_daemon_status_and_message():
    # The real ping handler returns {"status","message"} with NO "output" key.
    # ping_fn must read those fields (not a hard-coded "pong" via a nonexistent
    # "output" key), so an unhealthy or richer daemon reply is reflected.
    with patch.object(ctl, "_call", return_value={"status": "ok", "message": "pong"}):
        assert ctl.ping_fn() == "ok: pong"
    # A non-default status/message propagates faithfully (regression: the old
    # .get("output","pong") always returned "pong", ignoring the daemon).
    with patch.object(ctl, "_call", return_value={"status": "degraded", "message": "slow"}):
        assert ctl.ping_fn() == "degraded: slow"
    # Missing message → just the status.
    with patch.object(ctl, "_call", return_value={"status": "ok"}):
        assert ctl.ping_fn() == "ok"


def test_wait_fn_sleep_mode_calls_sleep():
    with patch.object(ctl, "_call", return_value={"output": "ok"}) as m, \
         patch("time.sleep") as _s:
        ctl.wait_fn(seconds=2)
    # sleep mode: no wait_check calls
    m.assert_not_called()


def test_wait_fn_text_mode_polls_until_found():
    # first check: found=False; second: found=True
    with patch.object(ctl, "_call", side_effect=[{"found": False}, {"found": True}]) as m, \
         patch("time.sleep"):
        out = ctl.wait_fn(seconds=None, selector=None, text="Done", timeout=5, interval=0.01)
    assert m.call_count == 2
    # wait_fn forwards selector=None + text verbatim to the mocked _call.
    m.assert_called_with("wait_check", selector=None, text="Done")


def test_wait_fn_requires_some_arg():
    with pytest.raises(ValueError):
        ctl.wait_fn(seconds=0, selector=None, text="")


def test_wait_fn_rejects_seconds_with_selector():
    """seconds is mutually exclusive with selector/text — passing both would
    otherwise silently drop seconds and poll for the element. Mirrors the CLI
    `wait` (which errors "cannot specify both a target and --text")."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        ctl.wait_fn(seconds=2, selector="@e3")
    with pytest.raises(ValueError, match="mutually exclusive"):
        ctl.wait_fn(seconds=2, text="Done")


def test_wait_for_fn_polls_condition():
    with patch.object(ctl, "_call", side_effect=[{"found": False}, {"found": True}]) as m, \
         patch("time.sleep"):
        ctl.wait_for_fn(condition="text_exists", text="Done", window_name="",
                        timeout=5, interval=0.01)
    m.assert_called_with("wait_for_check", condition="text_exists", text="Done", window_name="")


def test_wait_fn_times_out_when_never_found():
    """wait_fn raises TimeoutError when the element never appears within timeout."""
    with patch.object(ctl, "_call", return_value={"found": False}) as m, \
         patch("time.sleep"), \
         pytest.raises(TimeoutError, match="not found"):
        ctl.wait_fn(seconds=None, selector="@e9", text=None, timeout=0.1, interval=0.01)
    # at least one poll happened
    assert m.call_count >= 1


def test_wait_for_fn_times_out_with_detail():
    """wait_for_fn surfaces the daemon's detail string on timeout."""
    with patch.object(ctl, "_call", return_value={"found": False, "detail": "no match"}) as m, \
         patch("time.sleep"), \
         pytest.raises(TimeoutError, match="no match"):
        ctl.wait_for_fn(condition="element_exists", text="X", window_name="",
                        timeout=0.1, interval=0.01)
    assert m.call_count >= 1


# ── _image_from_result edge paths ───────────────────────────────────────

def test_image_from_result_returns_text_only_when_path_missing():
    """No path key in the result → just [text], no Image (no crash)."""
    out = obs._image_from_result({"output": "no image here"})
    assert isinstance(out, list) and len(out) == 1
    assert out[0] == "no image here"


def test_image_from_result_returns_text_only_on_unreadable_path():
    """A path that can't be read (OSError) degrades gracefully to [text]."""
    with patch("pathlib.Path.read_bytes", side_effect=OSError("gone")):
        out = obs._image_from_result({"output": "x", "path": "C:\\missing.png"})
    assert isinstance(out, list) and len(out) == 1
    assert out[0] == "x"


def test_image_from_result_detects_jpeg_format():
    """A non-.png path yields a jpeg Image (not png)."""
    with patch("pathlib.Path.read_bytes", return_value=_PNG):
        out = obs._image_from_result({"output": "shot", "path": "C:\\s.jpg"})
    assert isinstance(out, list) and len(out) == 2
    img = out[1]
    # FastMCP Image stores the format as a private _format / derived _mime_type
    # (no public .format attribute). Assert the mime type the transport sees.
    assert img._mime_type == "image/jpeg"


def test_find_image_fn_forwards_to_daemon():
    from agent_windows.mcp.tools.observation import find_image_fn
    fake = {"output": "## Find Image\nCount: 1", "count": 1,
            "matches": [{"index": 1, "x": 5, "y": 6, "w": 3, "h": 3,
                         "center_x": 6, "center_y": 7, "score": 1.0}],
            "method": "template", "screenshot_path": ""}
    with patch("agent_windows.mcp.tools.observation._call",
               return_value=fake) as mock_call:
        out = find_image_fn(image="tpl.png", source="src.png", bbox=[1, 2, 3, 4],
                            tolerance=5, grayscale=True, all_matches=True)
    assert mock_call.call_args.args == ("find_image",)
    assert mock_call.call_args.kwargs == {
        "image": "tpl.png", "source": "src.png", "bbox": [1, 2, 3, 4],
        "tolerance": 5, "grayscale": True, "all": True,
    }
    assert isinstance(out, list)  # _image_from_result -> [text] (empty path: no Image)


def test_pixel_at_fn_forwards_to_daemon():
    from agent_windows.mcp.tools.observation import pixel_at_fn
    fake = {"output": "## Pixel At\nRGB(255, 0, 0) #ff0000",
            "rgb": [255, 0, 0], "hex": "#ff0000", "screenshot_path": ""}
    with patch("agent_windows.mcp.tools.observation._call",
               return_value=fake) as mock_call:
        out = pixel_at_fn(10, 20, match="#ff0000", tolerance=5)
    assert mock_call.call_args.args == ("pixel_at",)
    assert mock_call.call_args.kwargs == {
        "x": 10, "y": 20, "source": None, "sample": 1,
        "match": "#ff0000", "tolerance": 5, "grayscale": False, "output": None,
    }
    assert isinstance(out, list)  # _image_from_result -> [text] (empty path: no Image)


def test_register_all_registers_every_domain_on_a_fake_mcp():
    """register_all calls register() on every tool module with the given mcp."""
    from agent_windows.mcp.tools import register_all, observation, vision, input, \
        system, batch, control

    seen = []
    class FakeMCP:
        def tool(self, **kw):
            def deco(fn):
                seen.append(kw.get("name"))
                return fn
            return deco

    register_all(FakeMCP())
    # every expected tool name registered
    expected = {
        # observation
        "screenshot", "snapshot", "observe", "get_text", "element_state",
        "highlight", "diff", "cleanup_screenshots", "find_image", "pixel_at",
        "list_displays",
        # vision
        "ground", "detect", "ask_vision",
        # input
        "click", "dblclick", "hover", "move", "mouse_down", "mouse_up",
        "drag", "scroll", "type", "shortcut", "key_down", "key_up",
        "expand", "collapse",
        # system
        "app_launch", "app_switch", "app_resize", "app_list", "shell",
        "fs_read", "fs_write", "fs_list", "fs_copy", "fs_move", "fs_delete",
        "fs_search", "fs_info", "clipboard_get", "clipboard_set",
        "process_list", "process_kill", "notify", "registry",
        "lock", "sleep", "shutdown", "restart",
        # batch/multi
        "batch", "multi_select", "multi_edit",
        # control
        "ping", "wait", "wait_for",
    }
    missing = expected - set(seen)
    assert not missing, f"missing tools: {missing}"


class TestParamDescriptions:
    """TODO #15a: non-obvious params carry Field(description=...) so the agent
    can pick enum values. Locks the schema propagation through FastMCP."""

    @pytest.fixture(scope="class")
    def _tool(self):
        """Build a real FastMCP instance once and fetch the registry tool."""
        import asyncio
        from fastmcp import FastMCP
        from agent_windows.mcp.tools import register_all

        mcp = FastMCP("test")
        register_all(mcp)

        async def _get():
            return await mcp.get_tool("registry")

        return asyncio.run(_get())

    def test_registry_mode_has_description(self, _tool):
        """registry.mode carries a description explaining get/set/delete/list."""
        desc = _tool.parameters["properties"]["mode"].get("description", "")
        assert "get" in desc and "set" in desc and "delete" in desc and "list" in desc

    def test_registry_type_has_description(self, _tool):
        """registry.type carries a description listing the value types."""
        desc = _tool.parameters["properties"]["type"].get("description", "")
        assert "String" in desc and "DWord" in desc
