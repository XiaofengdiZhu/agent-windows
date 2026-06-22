"""Tests for daemon/handlers/input.py and system.py: handler parameter
extraction and forwarding to the core functions.

These handlers are thin adapters: they read params (require()/get()), forward
to a core function, and wrap the result in a markdown ``output`` dict. The core
functions are patched out so we test the *plumbing*, not the Win32 behavior
(already covered by test_input_controller.py / test_app_manager.py).
"""

import time
from unittest.mock import patch

import pytest

from agent_windows.daemon.router import HANDLERS
from agent_windows.core import notifier


# ── input.py handlers ────────────────────────────────────────────────


class TestClickHandler:
    def test_forwards_left_click(self):
        with patch("agent_windows.core.input_controller.click") as mock_click:
            r = HANDLERS["click"]({"x": 10, "y": 20})
        mock_click.assert_called_once_with(10, 20, right=False, double=False)
        assert "Position: (10, 20)" in r["output"]
        assert "Button: left" in r["output"]
        assert "Double: False" in r["output"]

    def test_forwards_right_double(self):
        with patch("agent_windows.core.input_controller.click") as mock_click:
            r = HANDLERS["click"]({"x": 1, "y": 2, "right": True, "double": True})
        mock_click.assert_called_once_with(1, 2, right=True, double=True)
        assert "Button: right" in r["output"]

    def test_missing_x_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'x'"):
            HANDLERS["click"]({"y": 5})


class TestDblClickHandler:
    def test_always_passes_double_true(self):
        with patch("agent_windows.core.input_controller.click") as mock_click:
            r = HANDLERS["dblclick"]({"x": 5, "y": 6})
        mock_click.assert_called_once_with(5, 6, right=False, double=True)
        assert "Double Click" in r["output"]


class TestHoverHandler:
    def test_forwards_position(self):
        with patch("agent_windows.core.input_controller.hover") as mock_hover:
            r = HANDLERS["hover"]({"x": 7, "y": 8})
        mock_hover.assert_called_once_with(7, 8)
        assert "Hover" in r["output"]


class TestMouseDownUpHandlers:
    def test_mouse_down_defaults_left(self):
        with patch("agent_windows.core.input_controller.mouse_down") as m:
            HANDLERS["mouse_down"]({"x": 1, "y": 2})
        m.assert_called_once_with(1, 2, button="left")

    def test_mouse_down_passes_button(self):
        with patch("agent_windows.core.input_controller.mouse_down") as m:
            HANDLERS["mouse_down"]({"x": 1, "y": 2, "button": "right"})
        m.assert_called_once_with(1, 2, button="right")

    def test_mouse_up_defaults_left(self):
        with patch("agent_windows.core.input_controller.mouse_up") as m:
            HANDLERS["mouse_up"]({"x": 3, "y": 4})
        m.assert_called_once_with(3, 4, button="left")

    def test_mouse_up_passes_button(self):
        with patch("agent_windows.core.input_controller.mouse_up") as m:
            HANDLERS["mouse_up"]({"x": 3, "y": 4, "button": "middle"})
        m.assert_called_once_with(3, 4, button="middle")


class TestTypeHandler:
    def test_plain_type(self):
        with patch("agent_windows.core.input_controller.type_text") as m:
            r = HANDLERS["type"]({"text": "hello"})
        m.assert_called_once_with("hello")
        assert "Text: hello" in r["output"]
        assert "Enter: False" in r["output"]

    def test_type_truncates_long_text_in_output(self):
        long = "x" * 100
        with patch("agent_windows.core.input_controller.type_text"):
            r = HANDLERS["type"]({"text": long})
        assert "x" * 50 in r["output"]
        assert "..." in r["output"]

    def test_type_with_at_position_routes_to_type_text_at(self):
        with patch("agent_windows.core.input_controller.type_text_at") as m_at, \
             patch("agent_windows.core.input_controller.type_text") as m_plain:
            HANDLERS["type"]({"text": "hi", "at_x": 10, "at_y": 20, "clear": True, "enter": True})
        m_at.assert_called_once_with("hi", 10, 20, clear=True, enter=True, caret_position="idle")
        m_plain.assert_not_called()

    def test_type_asymmetric_at_coords_falls_back_to_plain(self):
        """The guard requires BOTH at_x and at_y; supplying only one must route
        to plain type_text (the lone coord is silently ignored). Pinning
        current intent so a future change to the guard is a conscious one."""
        with patch("agent_windows.core.input_controller.type_text_at") as m_at, \
             patch("agent_windows.core.input_controller.type_text") as m_plain:
            HANDLERS["type"]({"text": "hi", "at_x": 10})  # no at_y
        m_at.assert_not_called()
        m_plain.assert_called_once_with("hi")

    def test_type_clear_without_position_runs_shortcut_and_delete(self):
        """clear=True (no at_x/at_y) issues Ctrl+A + VK_DELETE before typing."""
        with patch("agent_windows.core.input_controller.type_text"), \
             patch("agent_windows.core.input_controller.shortcut") as m_sc, \
             patch("agent_windows.core.input_controller._send_vk_key") as m_vk, \
             patch.object(time, "sleep"):
            HANDLERS["type"]({"text": "hi", "clear": True})
        m_sc.assert_called_once_with("ctrl+a")
        # first vk call is VK_DELETE (0x2E)
        assert m_vk.call_args_list[0].args[0] == 0x2E

    def test_type_enter_without_position_sends_return(self):
        with patch("agent_windows.core.input_controller.type_text"), \
             patch("agent_windows.core.input_controller._send_vk_key") as m_vk, \
             patch.object(time, "sleep"):
            HANDLERS["type"]({"text": "hi", "enter": True})
        assert m_vk.call_args_list[-1].args[0] == 0x0D  # VK_RETURN

    def test_text_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["type"]({"text": 123})

    # ── caret_position (TODO #16) ──────────────────────────────────────

    def test_type_invalid_caret_rejected(self):
        """A bad caret_position surfaces as INVALID_PARAMS, not a silent no-op."""
        with pytest.raises(ValueError, match="caret_position must be"):
            HANDLERS["type"]({"text": "hi", "caret_position": "middle"})

    def test_type_caret_at_position_forwarded(self):
        """caret_position is forwarded to type_text_at when at_x/at_y given."""
        with patch("agent_windows.core.input_controller.type_text_at") as m_at:
            HANDLERS["type"]({
                "text": "hi", "at_x": 10, "at_y": 20, "caret_position": "end",
            })
        m_at.assert_called_once_with("hi", 10, 20, clear=False, enter=False, caret_position="end")

    def test_type_caret_start_without_position_sends_home(self):
        """No at_x/at_y: caret_position=start still presses Home in the focused field."""
        with patch("agent_windows.core.input_controller.type_text"), \
             patch("agent_windows.core.input_controller._send_vk_key") as m_vk, \
             patch.object(time, "sleep"):
            HANDLERS["type"]({"text": "hi", "caret_position": "start"})
        assert m_vk.call_args_list[0].args[0] == 0x24  # VK_HOME

    def test_type_caret_end_without_position_sends_end(self):
        with patch("agent_windows.core.input_controller.type_text"), \
             patch("agent_windows.core.input_controller._send_vk_key") as m_vk, \
             patch.object(time, "sleep"):
            HANDLERS["type"]({"text": "hi", "caret_position": "end"})
        assert m_vk.call_args_list[0].args[0] == 0x23  # VK_END

    def test_type_caret_idle_without_position_sends_no_vk(self):
        """idle is a true no-op: no Home/End, so no _send_vk_key at all."""
        with patch("agent_windows.core.input_controller.type_text"), \
             patch("agent_windows.core.input_controller._send_vk_key") as m_vk, \
             patch.object(time, "sleep"):
            HANDLERS["type"]({"text": "hi", "caret_position": "idle"})
        m_vk.assert_not_called()

    def test_type_caret_null_treated_as_idle(self):
        """A present-but-None caret_position is treated as the default 'idle'
        (matching require()'s None-as-missing convention), not rejected."""
        with patch("agent_windows.core.input_controller.type_text"), \
             patch("agent_windows.core.input_controller._send_vk_key") as m_vk, \
             patch.object(time, "sleep"):
            r = HANDLERS["type"]({"text": "hi", "caret_position": None})
        m_vk.assert_not_called()
        assert "Caret: idle" in r["output"]

    def test_type_caret_non_string_rejected(self):
        """A non-string caret_position (int/bool) must surface as INVALID_PARAMS,
        not crash with TypeError as a DAEMON_INTERNAL_ERROR. The `in` membership
        check is what keeps this safe — pin it so a refactor to match/case or a
        dict lookup doesn't silently regress."""
        with pytest.raises(ValueError, match="caret_position must be"):
            HANDLERS["type"]({"text": "hi", "caret_position": 1})
        with pytest.raises(ValueError, match="caret_position must be"):
            HANDLERS["type"]({"text": "hi", "caret_position": True})

    def test_type_caret_start_before_clear_on_no_coords_path(self):
        """No-coords path: caret (Home) must precede clear (VK_DELETE), mirroring
        the at-coords type_text_at ordering."""
        with patch("agent_windows.core.input_controller.type_text"), \
             patch("agent_windows.core.input_controller.shortcut"), \
             patch("agent_windows.core.input_controller._send_vk_key") as m_vk, \
             patch.object(time, "sleep"):
            HANDLERS["type"]({"text": "hi", "clear": True, "caret_position": "start"})
        assert m_vk.call_args_list[0].args[0] == 0x24  # VK_HOME before…
        assert m_vk.call_args_list[1].args[0] == 0x2E  # …VK_DELETE


class TestScrollHandler:
    def test_defaults(self):
        with patch("agent_windows.core.input_controller.scroll_at") as m:
            r = HANDLERS["scroll"]({"x": 1, "y": 2})
        m.assert_called_once_with(1, 2, direction="down", amount=3)
        assert "Direction: down" in r["output"]

    def test_custom(self):
        with patch("agent_windows.core.input_controller.scroll_at") as m:
            HANDLERS["scroll"]({"x": 1, "y": 2, "direction": "up", "amount": 5})
        m.assert_called_once_with(1, 2, direction="up", amount=5)


class TestMoveHandler:
    def test_forwards(self):
        with patch("agent_windows.core.input_controller.move_to") as m:
            HANDLERS["move"]({"x": 9, "y": 9})
        m.assert_called_once_with(9, 9)


class TestDragHandler:
    def test_defaults_left(self):
        with patch("agent_windows.core.input_controller.drag") as m:
            HANDLERS["drag"]({"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        m.assert_called_once_with(1, 2, 3, 4, right=False)

    def test_right(self):
        with patch("agent_windows.core.input_controller.drag") as m:
            HANDLERS["drag"]({"x1": 1, "y1": 2, "x2": 3, "y2": 4, "right": True})
        m.assert_called_once_with(1, 2, 3, 4, right=True)

    def test_missing_coord_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'x2'"):
            HANDLERS["drag"]({"x1": 1, "y1": 2, "y2": 4})


class TestShortcutHandler:
    def test_forwards_keys(self):
        with patch("agent_windows.core.input_controller.shortcut") as m:
            r = HANDLERS["shortcut"]({"keys": "ctrl+c"})
        m.assert_called_once_with("ctrl+c")
        assert "Keys: ctrl+c" in r["output"]

    def test_keys_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["shortcut"]({"keys": ["ctrl", "c"]})


# ── selector (@eN) → coordinate resolution (TODO #13) ────────────────


class TestSelectorResolution:
    """Mouse/keyboard handlers accept a `selector` (@eN) that resolves to the
    element's bbox center, overriding x/y. resolve_element_center is patched
    so no real UIA runs."""

    _CENTER = (200, 300)

    def _patch_center(self):
        return patch("agent_windows.core.tree.resolve_element_center",
                     return_value=self._CENTER)

    def test_click_selector_overrides_coords(self):
        """selector present → resolve_element_center drives the click; x/y ignored."""
        with self._patch_center() as m_res, \
                patch("agent_windows.core.input_controller.click") as m_click:
            HANDLERS["click"]({"selector": "@e3", "x": 999, "y": 999})
        m_res.assert_called_once_with("@e3")
        m_click.assert_called_once_with(200, 300, right=False, double=False)

    def test_click_selector_without_xy_works(self):
        with self._patch_center(), \
                patch("agent_windows.core.input_controller.click") as m_click:
            HANDLERS["click"]({"selector": "@e3"})
        m_click.assert_called_once_with(200, 300, right=False, double=False)

    def test_click_xy_path_untouched_when_no_selector(self):
        """No selector → the legacy x/y path; resolve_element_center not called."""
        with self._patch_center() as m_res, \
                patch("agent_windows.core.input_controller.click") as m_click:
            HANDLERS["click"]({"x": 10, "y": 20})
        m_res.assert_not_called()
        m_click.assert_called_once_with(10, 20, right=False, double=False)

    @pytest.mark.parametrize("handler,core_fn,extra", [
        ("dblclick", "click", {}),
        ("hover", "hover", {}),
        ("move", "move_to", {}),
        ("scroll", "scroll_at", {"direction": "down", "amount": 3}),
    ])
    def test_every_coord_handler_supports_selector(self, handler, core_fn, extra):
        """Each coordinate-taking handler honors selector over x/y."""
        with self._patch_center(), \
                patch(f"agent_windows.core.input_controller.{core_fn}") as m_core:
            HANDLERS[handler]({"selector": "@e2"})
        # First two positional args are the resolved center coords.
        assert m_core.call_args.args[:2] == self._CENTER

    def test_mouse_down_up_support_selector(self):
        with self._patch_center(), \
                patch("agent_windows.core.input_controller.mouse_down") as m_dn, \
                patch("agent_windows.core.input_controller.mouse_up") as m_up:
            HANDLERS["mouse_down"]({"selector": "@e4", "button": "right"})
            HANDLERS["mouse_up"]({"selector": "@e4", "button": "right"})
        m_dn.assert_called_once_with(200, 300, button="right")
        m_up.assert_called_once_with(200, 300, button="right")

    def test_selector_resolution_error_propagates(self):
        """A bad/missing selector surfaces its error (ValueError → INVALID_PARAMS),
        not a silent fallback to x/y."""
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=ValueError("Invalid selector: 'bad'")):
            with pytest.raises(ValueError, match="Invalid selector"):
                HANDLERS["click"]({"selector": "bad"})

    def test_selector_missing_element_propagates(self):
        """An out-of-range @eN (IndexError from resolve_element) surfaces as-is —
        the handler does not swallow it into a generic error."""
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=IndexError("Element @e99 not found")):
            with pytest.raises(IndexError, match="not found"):
                HANDLERS["click"]({"selector": "@e99"})


class TestTypeAtSelector:
    """type --at-selector (@eN) resolves to a click point, overriding --at."""

    _CENTER = (200, 300)

    def _patch_center(self):
        return patch("agent_windows.core.tree.resolve_element_center",
                     return_value=self._CENTER)

    def test_at_selector_routes_to_type_text_at(self):
        with self._patch_center() as m_res, \
                patch("agent_windows.core.input_controller.type_text_at") as m_at, \
                patch("agent_windows.core.input_controller.type_text") as m_plain:
            HANDLERS["type"]({"text": "hi", "at_selector": "@e3"})
        m_res.assert_called_once_with("@e3")
        m_at.assert_called_once_with("hi", 200, 300, clear=False, enter=False,
                                     caret_position="idle")
        m_plain.assert_not_called()

    def test_at_selector_overrides_at_xy(self):
        """If both at_selector and at_x/at_y given, selector wins (no x/y call)."""
        with self._patch_center(), \
                patch("agent_windows.core.input_controller.type_text_at") as m_at:
            HANDLERS["type"]({"text": "hi", "at_selector": "@e3",
                              "at_x": 999, "at_y": 999})
        # type_text_at gets the resolved center, NOT 999,999
        assert m_at.call_args.args[1:3] == self._CENTER

    def test_no_at_no_selector_is_plain_type(self):
        """Without at_selector or at_x/at_y, falls to the focused-field path."""
        with self._patch_center() as m_res, \
                patch("agent_windows.core.input_controller.type_text") as m_plain, \
                patch("agent_windows.core.input_controller.type_text_at") as m_at:
            HANDLERS["type"]({"text": "hi"})
        m_res.assert_not_called()
        m_at.assert_not_called()
        m_plain.assert_called_once_with("hi")

    def test_at_selector_error_propagates(self):
        """A bad at_selector surfaces its error (different branch from _coords,
        so pin it independently)."""
        with patch("agent_windows.core.tree.resolve_element_center",
                   side_effect=ValueError("Invalid selector: 'bad'")):
            with pytest.raises(ValueError, match="Invalid selector"):
                HANDLERS["type"]({"text": "hi", "at_selector": "bad"})


# ── system.py handlers ───────────────────────────────────────────────


class TestAppLaunchHandler:
    def test_forwards_name_and_args(self):
        with patch("agent_windows.core.app_manager.launch_app", return_value={"output": "ok", "pid": 7}) as m:
            r = HANDLERS["app_launch"]({"name_or_path": "notepad", "args": ["a.txt"]})
        m.assert_called_once_with("notepad", ["a.txt"])
        assert r == {"output": "ok", "pid": 7}

    def test_args_none_passed_through(self):
        with patch("agent_windows.core.app_manager.launch_app", return_value={"output": "ok"}) as m:
            HANDLERS["app_launch"]({"name_or_path": "notepad"})
        m.assert_called_once_with("notepad", None)

    def test_missing_name_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'name_or_path'"):
            HANDLERS["app_launch"]({})


class TestAppSwitchHandler:
    def test_forwards_title(self):
        with patch("agent_windows.core.app_manager.switch_window", return_value={"output": "ok"}) as m:
            HANDLERS["app_switch"]({"name_or_title": "notes"})
        m.assert_called_once_with("notes")


class TestAppResizeHandler:
    def test_forwards_all(self):
        with patch("agent_windows.core.app_manager.resize_window", return_value={"output": "ok"}) as m:
            HANDLERS["app_resize"]({"name_or_title": "w", "width": 800, "height": 600, "x": 10, "y": 20})
        m.assert_called_once_with("w", 800, 600, 10, 20)

    def test_position_defaults_to_negative(self):
        with patch("agent_windows.core.app_manager.resize_window", return_value={"output": "ok"}) as m:
            HANDLERS["app_resize"]({"name_or_title": "w", "width": 800, "height": 600})
        m.assert_called_once_with("w", 800, 600, -1, -1)

    def test_missing_width_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'width'"):
            HANDLERS["app_resize"]({"name_or_title": "w", "height": 600})


class TestAppListHandler:
    def test_calls_list_windows(self):
        with patch("agent_windows.core.app_manager.list_windows", return_value={"output": "x", "count": 0}) as m:
            r = HANDLERS["app_list"]({})
        m.assert_called_once_with()
        assert r == {"output": "x", "count": 0}


class TestShellHandler:
    def test_forwards_command(self):
        with patch("agent_windows.core.shell_runner.run_shell", return_value={"output": "done"}) as m:
            HANDLERS["shell"]({"command": "Get-Date"})
        m.assert_called_once_with("Get-Date")

    def test_command_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["shell"]({"command": 5})


class TestFsHandlers:
    def test_fs_read_forwards_path(self):
        with patch("agent_windows.core.filesystem.read_file", return_value={"output": "c"}) as m:
            HANDLERS["fs_read"]({"path": r"C:\x"})
        # Bare call forwards offset=None, limit=None (TODO #4 optional params).
        m.assert_called_once_with(r"C:\x", offset=None, limit=None)

    def test_fs_read_forwards_offset_limit(self):
        with patch("agent_windows.core.filesystem.read_file", return_value={"output": "c"}) as m:
            HANDLERS["fs_read"]({"path": r"C:\x", "offset": 5, "limit": 10})
        m.assert_called_once_with(r"C:\x", offset=5, limit=10)

    def test_fs_write_forwards_path_and_content(self):
        with patch("agent_windows.core.filesystem.write_file", return_value={"output": "c"}) as m:
            HANDLERS["fs_write"]({"path": r"C:\x", "content": "hi"})
        m.assert_called_once_with(r"C:\x", "hi", append=False)

    def test_fs_write_content_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["fs_write"]({"path": "p", "content": 1})

    def test_fs_list_forwards_path(self):
        with patch("agent_windows.core.filesystem.list_dir", return_value={"output": "c"}) as m:
            HANDLERS["fs_list"]({"path": r"C:\x"})
        m.assert_called_once_with(r"C:\x", pattern=None, recursive=False, show_hidden=False)


class TestClipboardHandlers:
    def test_get_uses_config_method_default(self, mock_config):
        """keyboard flag defaults from config['clipboard']['method']; here 'system' → False."""
        with patch("agent_windows.core.clipboard.get_clipboard", return_value={"output": "c"}) as m:
            HANDLERS["clipboard_get"]({})
        m.assert_called_once_with(keyboard=False)

    def test_get_config_keyboard_method_defaults_true(self, mock_config):
        """When config clipboard.method == 'keyboard', the default flag is True."""
        mock_config.return_value["clipboard"]["method"] = "keyboard"
        with patch("agent_windows.core.clipboard.get_clipboard", return_value={"output": "c"}) as m:
            HANDLERS["clipboard_get"]({})
        m.assert_called_once_with(keyboard=True)

    def test_set_config_keyboard_method_defaults_true(self, mock_config):
        mock_config.return_value["clipboard"]["method"] = "keyboard"
        with patch("agent_windows.core.clipboard.set_clipboard", return_value={"output": "c"}) as m:
            HANDLERS["clipboard_set"]({"text": "hi"})
        m.assert_called_once_with("hi", keyboard=True)

    def test_get_explicit_keyboard_flag_overrides_config(self, mock_config):
        with patch("agent_windows.core.clipboard.get_clipboard", return_value={"output": "c"}) as m:
            HANDLERS["clipboard_get"]({"keyboard": True})
        m.assert_called_once_with(keyboard=True)

    def test_set_forwards_text_and_flag(self, mock_config):
        with patch("agent_windows.core.clipboard.set_clipboard", return_value={"output": "c"}) as m:
            HANDLERS["clipboard_set"]({"text": "hi"})
        m.assert_called_once_with("hi", keyboard=False)

    def test_set_explicit_keyboard(self, mock_config):
        with patch("agent_windows.core.clipboard.set_clipboard", return_value={"output": "c"}) as m:
            HANDLERS["clipboard_set"]({"text": "hi", "keyboard": True})
        m.assert_called_once_with("hi", keyboard=True)

    def test_set_text_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["clipboard_set"]({"text": 5})


class TestProcessHandlers:
    def test_process_list(self):
        with patch("agent_windows.core.process_manager.list_processes", return_value={"output": "c"}) as m:
            HANDLERS["process_list"]({})
        m.assert_called_once_with()

    def test_process_kill_accepts_str(self):
        with patch("agent_windows.core.process_manager.kill_process", return_value={"output": "c"}) as m:
            HANDLERS["process_kill"]({"pid_or_name": "notepad"})
        m.assert_called_once_with("notepad")

    def test_process_kill_accepts_int(self):
        with patch("agent_windows.core.process_manager.kill_process", return_value={"output": "c"}) as m:
            HANDLERS["process_kill"]({"pid_or_name": 1234})
        m.assert_called_once_with(1234)

    def test_process_kill_rejects_list(self):
        with pytest.raises(ValueError, match="must be str or int"):
            HANDLERS["process_kill"]({"pid_or_name": [1, 2]})

    def test_process_kill_missing_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'pid_or_name'"):
            HANDLERS["process_kill"]({})


# ── notify handler (TODO #10) ────────────────────────────────────────


class TestNotifyHandler:
    def test_notify_forwards_title_message(self):
        with patch("agent_windows.core.notifier.send_notification",
                   return_value={"output": "ok", "sent": True}) as m:
            HANDLERS["notify"]({"title": "T", "message": "M"})
        m.assert_called_once_with("T", "M", app_id=notifier.DEFAULT_APP_ID)

    def test_notify_forwards_custom_app_id(self):
        with patch("agent_windows.core.notifier.send_notification",
                   return_value={"output": "ok", "sent": True}) as m:
            HANDLERS["notify"]({"title": "T", "message": "M", "app_id": "my.app"})
        m.assert_called_once_with("T", "M", app_id="my.app")

    def test_notify_missing_title_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'title'"):
            HANDLERS["notify"]({"message": "M"})

    def test_notify_missing_message_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'message'"):
            HANDLERS["notify"]({"title": "T"})
