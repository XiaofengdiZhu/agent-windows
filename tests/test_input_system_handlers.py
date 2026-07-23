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


@pytest.fixture(autouse=True)
def _neutralize_verify(monkeypatch):
    """verify helpers default to no-op so legacy click/hover/move/drag tests
    (which patch only the core op, not verify) stay deterministic and free of
    Hit/Focus/Drop/Slider lines — no real ControlFromPoint in CI. verify-ON
    behavior is exercised by TestVerifyState, which overrides these with canned
    data on the same (function-scoped) monkeypatch instance."""
    import agent_windows.core.tree as _tree
    monkeypatch.setattr(_tree, "hit_element_at", lambda x, y: None)
    monkeypatch.setattr(_tree, "foreground_window", lambda: {"hwnd": 0, "title": ""})
    monkeypatch.setattr(_tree, "range_percent_at", lambda x, y: None)
    monkeypatch.setattr(_tree, "focused_field_value", lambda: None)
    monkeypatch.setattr(_tree, "field_value_at", lambda x, y: None)


# ── input.py handlers ────────────────────────────────────────────────


class TestClickHandler:
    def test_forwards_left_click(self):
        with patch("agent_windows.core.input_controller.click") as mock_click:
            r = HANDLERS["click"]({"x": 10, "y": 20})
        mock_click.assert_called_once_with(10, 20, right=False, double=False)
        assert "Position: (10, 20)" in r["output"]
        # P2.2: default flags (left button, single click) are suppressed — only
        # non-defaults surface, so a plain left click echoes just the position.
        assert "Button:" not in r["output"]
        assert "Double:" not in r["output"]

    def test_forwards_right_double(self):
        with patch("agent_windows.core.input_controller.click") as mock_click:
            r = HANDLERS["click"]({"x": 1, "y": 2, "right": True, "double": True})
        mock_click.assert_called_once_with(1, 2, right=True, double=True)
        assert "Button: right" in r["output"]
        assert "Double: true" in r["output"]  # P2.2: non-default surfaced

    def test_missing_x_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'x'"):
            HANDLERS["click"]({"y": 5})


class TestDblClickHandler:
    def test_always_passes_double_true(self):
        with patch("agent_windows.core.input_controller.click") as mock_click:
            r = HANDLERS["dblclick"]({"x": 5, "y": 6})
        mock_click.assert_called_once_with(5, 6, right=False, double=True)
        assert "Double Click" in r["output"]
        assert "Button:" not in r["output"]  # P2.2: default left suppressed


class TestHoverHandler:
    def test_forwards_position(self):
        with patch("agent_windows.core.input_controller.hover") as mock_hover:
            r = HANDLERS["hover"]({"x": 7, "y": 8})
        mock_hover.assert_called_once_with(7, 8)
        assert "Hover" in r["output"]


class TestMouseDownUpHandlers:
    def test_mouse_down_defaults_left(self):
        with patch("agent_windows.core.input_controller.mouse_down") as m:
            r = HANDLERS["mouse_down"]({"x": 1, "y": 2})
        m.assert_called_once_with(1, 2, button="left")
        assert "Button:" not in r["output"]  # P2.2: default left suppressed

    def test_mouse_down_passes_button(self):
        with patch("agent_windows.core.input_controller.mouse_down") as m:
            r = HANDLERS["mouse_down"]({"x": 1, "y": 2, "button": "right"})
        m.assert_called_once_with(1, 2, button="right")
        assert "Button: right" in r["output"]  # P2.2: non-default surfaced

    def test_mouse_up_defaults_left(self):
        with patch("agent_windows.core.input_controller.mouse_up") as m:
            r = HANDLERS["mouse_up"]({"x": 3, "y": 4})
        m.assert_called_once_with(3, 4, button="left")
        assert "Button:" not in r["output"]  # P2.2: default left suppressed

    def test_mouse_up_passes_button(self):
        with patch("agent_windows.core.input_controller.mouse_up") as m:
            r = HANDLERS["mouse_up"]({"x": 3, "y": 4, "button": "middle"})
        m.assert_called_once_with(3, 4, button="middle")
        assert "Button: middle" in r["output"]  # P2.2: non-default surfaced


class TestTypeHandler:
    def test_plain_type(self):
        with patch("agent_windows.core.input_controller.type_text") as m:
            r = HANDLERS["type"]({"text": "hello"})
        m.assert_called_once_with("hello")
        assert "## Type" in r["output"]
        assert "Text:" not in r["output"]  # P3.2: echo removed (diff replaces it)
        assert "Enter:" not in r["output"]  # P2.2: default enter=False suppressed

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
        assert "Caret:" not in r["output"]  # P2.2: default idle suppressed

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


class TestTypeValueDiff:
    """Default-on post-op field-value diff for type (P0.2). Each test overrides
    the autouse no-op field-value helpers with canned data (same monkeypatch)."""

    def test_no_coords_reads_focused_pre_before_clear(self, monkeypatch):
        # pre must be read BEFORE clear_field (so --clear shows old→new, not empty→new)
        import agent_windows.core.tree as t
        order = []

        def fv():
            order.append("read")
            return {"value": "pre" if len(order) == 1 else "post", "is_password": False}
        monkeypatch.setattr(t, "focused_field_value", fv)
        with patch("agent_windows.core.input_controller.clear_field",
                   lambda: order.append("clear")), \
             patch("agent_windows.core.input_controller.type_text",
                   lambda s: order.append("type")), \
             patch("agent_windows.core.input_controller.move_caret"):
            HANDLERS["type"]({"text": "x", "clear": True})
        assert order.index("read") < order.index("clear")  # pre before clear

    def test_single_line_diff_appended_and_sibling(self, monkeypatch):
        import agent_windows.core.tree as t
        reads = iter([{"value": "foo", "is_password": False},
                      {"value": "foobar", "is_password": False}])
        monkeypatch.setattr(t, "focused_field_value", lambda: next(reads))
        with patch("agent_windows.core.input_controller.type_text"):
            r = HANDLERS["type"]({"text": "bar"})
        assert "Field value: foo → foobar" in r["output"]
        assert r["field_value"] == {"pre": "foo", "post": "foobar", "is_password": False}

    def test_password_hidden_no_value_leak(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "focused_field_value",
                            lambda: {"value": "secret", "is_password": True})
        with patch("agent_windows.core.input_controller.type_text"):
            r = HANDLERS["type"]({"text": "more"})
        assert "(password field, value hidden)" in r["output"]
        assert "secret" not in r["output"]
        assert r["field_value"]["is_password"] is True
        # password value must not leak into the sibling either
        assert r["field_value"]["pre"] is None and r["field_value"]["post"] is None

    def test_at_coords_uses_field_value_at(self, monkeypatch):
        import agent_windows.core.tree as t
        reads = iter([{"value": "old", "is_password": False},
                      {"value": "oldnew", "is_password": False}])
        monkeypatch.setattr(t, "field_value_at", lambda x, y: next(reads))
        monkeypatch.setattr(t, "focused_field_value", lambda: None)  # must NOT be used
        with patch("agent_windows.core.input_controller.type_text_at"):
            r = HANDLERS["type"]({"text": "new", "at_x": 10, "at_y": 20})
        assert "Field value: old → oldnew" in r["output"]
        assert r["field_value"] == {"pre": "old", "post": "oldnew", "is_password": False}

    def test_no_change_when_value_unchanged(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "focused_field_value",
                            lambda: {"value": "same", "is_password": False})
        with patch("agent_windows.core.input_controller.type_text"):
            r = HANDLERS["type"]({"text": "x"})
        assert "Field value: no change" in r["output"]

    def test_verify_off_no_diff_no_sibling(self, monkeypatch):
        monkeypatch.setattr("agent_windows.utils.config.load_config",
                            lambda: {"input": {"verify": False}})
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "focused_field_value",
                            lambda: {"value": "would-show", "is_password": False})
        with patch("agent_windows.core.input_controller.type_text"):
            r = HANDLERS["type"]({"text": "x"})
        assert "Field value" not in r["output"]
        assert "field_value" not in r

    def test_unreadable_field_no_block(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "focused_field_value", lambda: None)
        with patch("agent_windows.core.input_controller.type_text"):
            r = HANDLERS["type"]({"text": "x"})
        assert "Field value" not in r["output"]
        assert "field_value" not in r  # nothing readable → no sibling either

    def test_read_failure_never_breaks_type(self, monkeypatch):
        import agent_windows.core.tree as t

        def _boom():
            raise Exception("boom")
        monkeypatch.setattr(t, "focused_field_value", _boom)
        with patch("agent_windows.core.input_controller.type_text"):
            r = HANDLERS["type"]({"text": "x"})  # must not raise
        assert "## Type" in r["output"]
        assert "field_value" not in r


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

    def test_amount_clamped_to_max_in_output(self):
        """A runaway amount is clamped to MAX_SCROLL_STEPS before forwarding,
        and the output reports the clamped value with a note — not the raw
        input (which scroll_at would silently shrink, leaving the agent misled)."""
        with patch("agent_windows.core.input_controller.scroll_at") as m:
            r = HANDLERS["scroll"]({"x": 1, "y": 2, "amount": 5000})
        m.assert_called_once_with(1, 2, direction="down", amount=1000)
        assert "Amount: 1000" in r["output"]
        assert "clamped from 5000" in r["output"]


class TestMoveHandler:
    def test_forwards(self):
        with patch("agent_windows.core.input_controller.move_to") as m:
            HANDLERS["move"]({"x": 9, "y": 9})
        m.assert_called_once_with(9, 9)


class TestDragHandler:
    def test_defaults_left(self):
        with patch("agent_windows.core.input_controller.drag") as m:
            r = HANDLERS["drag"]({"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        m.assert_called_once_with(1, 2, 3, 4, right=False)
        assert "Button:" not in r["output"]  # P2.2: default left suppressed

    def test_right(self):
        with patch("agent_windows.core.input_controller.drag") as m:
            r = HANDLERS["drag"]({"x1": 1, "y1": 2, "x2": 3, "y2": 4, "right": True})
        m.assert_called_once_with(1, 2, 3, 4, right=True)
        assert "Button: right" in r["output"]  # P2.2: non-default surfaced

    def test_missing_coord_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'x2'"):
            HANDLERS["drag"]({"x1": 1, "y1": 2, "y2": 4})


class TestVerifyState:
    """Default-on post-op verify (P0.1/P0.3). Each test overrides the
    autouse no-op verify helpers with canned data (same monkeypatch instance)."""

    def test_click_appends_hit_and_focus(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
                            lambda x, y: {"control_type": "Button", "name": "OK"})
        monkeypatch.setattr(t, "foreground_window",
                            lambda: {"hwnd": 1, "title": "Settings"})
        with patch("agent_windows.core.input_controller.click"):
            r = HANDLERS["click"]({"x": 10, "y": 20})
        assert 'Hit: Button "OK"' in r["output"]
        assert 'Focus: "Settings"' in r["output"]

    def test_click_shows_none_when_no_hit(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at", lambda x, y: None)
        monkeypatch.setattr(t, "foreground_window",
                            lambda: {"hwnd": 1, "title": "X"})
        with patch("agent_windows.core.input_controller.click"):
            r = HANDLERS["click"]({"x": 1, "y": 2})
        assert "Hit: (none)" in r["output"]
        assert 'Focus: "X"' in r["output"]

    def test_click_skips_focus_when_title_empty(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
                            lambda x, y: {"control_type": "Button", "name": "OK"})
        monkeypatch.setattr(t, "foreground_window", lambda: {"hwnd": 0, "title": ""})
        with patch("agent_windows.core.input_controller.click"):
            r = HANDLERS["click"]({"x": 1, "y": 2})
        assert 'Hit: Button "OK"' in r["output"]
        assert "Focus:" not in r["output"]

    def test_click_verify_off_via_config(self, monkeypatch):
        monkeypatch.setattr("agent_windows.utils.config.load_config",
                            lambda: {"input": {"verify": False}})
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
                            lambda x, y: {"control_type": "B", "name": "n"})  # would show if on
        with patch("agent_windows.core.input_controller.click"):
            r = HANDLERS["click"]({"x": 1, "y": 2})
        assert "Hit:" not in r["output"]
        assert "Focus:" not in r["output"]

    def test_dblclick_verify(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
                            lambda x, y: {"control_type": "ListItem", "name": "Row"})
        monkeypatch.setattr(t, "foreground_window", lambda: {"hwnd": 1, "title": "App"})
        with patch("agent_windows.core.input_controller.click"):
            r = HANDLERS["dblclick"]({"x": 5, "y": 6})
        assert 'Hit: ListItem "Row"' in r["output"]
        assert 'Focus: "App"' in r["output"]

    def test_hover_and_move_verify(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "Box"})
        monkeypatch.setattr(t, "foreground_window", lambda: {"hwnd": 1, "title": "App"})
        with patch("agent_windows.core.input_controller.hover"):
            rh = HANDLERS["hover"]({"x": 1, "y": 2})
        with patch("agent_windows.core.input_controller.move_to"):
            rm = HANDLERS["move"]({"x": 3, "y": 4})
        assert 'Hit: Edit "Box"' in rh["output"]
        assert 'Hit: Edit "Box"' in rm["output"]

    def test_drag_appends_drop_and_slider(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
                            lambda x, y: {"control_type": "Edit", "name": "Volume"})
        monkeypatch.setattr(t, "range_percent_at", lambda x, y: 62)
        with patch("agent_windows.core.input_controller.drag"):
            r = HANDLERS["drag"]({"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        assert 'Drop: Edit "Volume"' in r["output"]
        assert "Slider: 62%" in r["output"]
        assert "Focus:" not in r["output"]  # drag has no Focus line

    def test_drag_no_slider_when_none(self, monkeypatch):
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
                            lambda x, y: {"control_type": "List", "name": "Items"})
        monkeypatch.setattr(t, "range_percent_at", lambda x, y: None)
        with patch("agent_windows.core.input_controller.drag"):
            r = HANDLERS["drag"]({"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        assert "Slider:" not in r["output"]
        assert 'Drop: List "Items"' in r["output"]

    def test_drag_verify_off_via_config(self, monkeypatch):
        monkeypatch.setattr("agent_windows.utils.config.load_config",
                            lambda: {"input": {"verify": False}})
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
                            lambda x, y: {"control_type": "X", "name": "y"})
        with patch("agent_windows.core.input_controller.drag"):
            r = HANDLERS["drag"]({"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        assert "Drop:" not in r["output"]
        assert "Slider:" not in r["output"]

    def test_verify_failure_never_breaks_op(self, monkeypatch):
        """Even if a verify helper raises, a successful op returns normally
        (the handler wraps verify in its own try/except)."""
        import agent_windows.core.tree as t

        def boom(x, y):
            raise RuntimeError("verify exploded")
        monkeypatch.setattr(t, "hit_element_at", boom)
        monkeypatch.setattr(t, "foreground_window", boom)
        with patch("agent_windows.core.input_controller.click"):
            r = HANDLERS["click"]({"x": 1, "y": 2})
        assert "Position: (1, 2)" in r["output"]  # op still reported
        assert "Hit:" not in r["output"]  # verify raised → no line, no crash


class TestShortcutHandler:
    def test_forwards_keys(self):
        with patch("agent_windows.core.input_controller.shortcut") as m:
            r = HANDLERS["shortcut"]({"keys": "ctrl+c"})
        m.assert_called_once_with("ctrl+c")
        assert "Keys: ctrl+c" in r["output"]

    def test_keys_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["shortcut"]({"keys": ["ctrl", "c"]})


class TestKeyDownUpHandlers:
    """key_down/key_up are keyboard half-events (mirror mouse_down/mouse_up).
    hold (optional, seconds) auto-releases; omit to keep pressed (pair key_up)."""

    def test_key_down_no_hold_stays_pressed(self):
        with patch("agent_windows.core.input_controller.key_down") as m:
            HANDLERS["key_down"]({"keys": "a"})
        m.assert_called_once_with("a", hold=None)

    def test_key_down_forwards_hold(self):
        with patch("agent_windows.core.input_controller.key_down") as m:
            HANDLERS["key_down"]({"keys": "ctrl+a", "hold": 0.5})
        m.assert_called_once_with("ctrl+a", hold=0.5)

    def test_key_up_forwards_keys(self):
        with patch("agent_windows.core.input_controller.key_up") as m:
            HANDLERS["key_up"]({"keys": "ctrl+a"})
        m.assert_called_once_with("ctrl+a")

    def test_key_down_hold_coerced_to_float(self):
        """A JSON int hold (1) is coerced to float before forwarding."""
        with patch("agent_windows.core.input_controller.key_down") as m:
            HANDLERS["key_down"]({"keys": "a", "hold": 1})
        assert m.call_args.kwargs["hold"] == 1.0

    def test_key_down_output_shape(self):
        with patch("agent_windows.core.input_controller.key_down"):
            r = HANDLERS["key_down"]({"keys": "a"})
        assert "Key Down" in r["output"]
        assert "Keys: a" in r["output"]
        assert "until key-up" in r["output"]

    def test_key_down_hold_over_limit_rejected(self):
        """hold > MAX_KEY_HOLD_SECONDS (60) → INVALID_PARAMS, not a 60s+ block."""
        with pytest.raises(ValueError, match="hold must be in"):
            HANDLERS["key_down"]({"keys": "a", "hold": 999})

    def test_key_down_hold_negative_rejected(self):
        with pytest.raises(ValueError, match="hold must be in"):
            HANDLERS["key_down"]({"keys": "a", "hold": -1})

    def test_key_down_hold_non_number_rejected(self):
        """A string hold a JSON client might send → INVALID_PARAMS, not a crash."""
        with pytest.raises(ValueError, match="hold must be a number"):
            HANDLERS["key_down"]({"keys": "a", "hold": "soon"})

    def test_key_down_keys_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["key_down"]({"keys": ["a", "b"]})


class TestExpandCollapseHandlers:
    """expand/collapse take ONLY a selector (@eN) — no coordinates — and forward
    to the core pattern-based functions. (TODO #12)"""

    def test_expand_forwards_selector(self):
        with patch("agent_windows.core.tree.expand_element",
                   return_value={"action": "expanded", "state": "expanded",
                                 "skipped": False}) as m:
            r = HANDLERS["expand"]({"selector": "@e5"})
        m.assert_called_once_with("@e5")
        assert "Element: @e5" in r["output"]
        assert "State: expanded" in r["output"]
        assert "Skipped: False" in r["output"]

    def test_expand_skipped_surfaces(self):
        with patch("agent_windows.core.tree.expand_element",
                   return_value={"action": "skipped", "state": "expanded",
                                 "skipped": True}) as m:
            r = HANDLERS["expand"]({"selector": "@e1"})
        m.assert_called_once_with("@e1")
        assert "Skipped: True" in r["output"]

    def test_expand_missing_selector_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'selector'"):
            HANDLERS["expand"]({})

    def test_collapse_forwards_selector(self):
        with patch("agent_windows.core.tree.collapse_element",
                   return_value={"action": "collapsed", "state": "collapsed",
                                 "skipped": False}) as m:
            r = HANDLERS["collapse"]({"selector": "@e2"})
        m.assert_called_once_with("@e2")
        assert "Element: @e2" in r["output"]
        assert "State: collapsed" in r["output"]

    def test_collapse_missing_selector_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'selector'"):
            HANDLERS["collapse"]({})


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


class TestTypeClickTargetEcho:
    """P3.2: click paths (--at / --at-selector) echo the resolved target coord
    and verify what was hit (P0.1 consistency with click/dblclick/hover/move).
    The Text: param echo is gone — P0.2's Field value diff reads the field's
    actual value untruncated, so the old 50-char echo was both redundant and
    lossy."""

    def test_at_xy_echoes_coord(self):
        with patch("agent_windows.core.input_controller.type_text_at"):
            r = HANDLERS["type"]({"text": "hi", "at_x": 10, "at_y": 20})
        assert "At: (10, 20)" in r["output"]
        assert "Text:" not in r["output"]

    def test_at_selector_echoes_selector_and_coord(self):
        with patch("agent_windows.core.tree.resolve_element_center",
                   return_value=(200, 300)), \
             patch("agent_windows.core.input_controller.type_text_at"):
            r = HANDLERS["type"]({"text": "hi", "at_selector": "@e3"})
        assert "At: @e3 (200, 300)" in r["output"]

    def test_plain_path_has_no_at_line(self):
        with patch("agent_windows.core.input_controller.type_text"):
            r = HANDLERS["type"]({"text": "hi"})
        assert "At:" not in r["output"]

    def test_click_path_hit_focus_verify(self, monkeypatch):
        # Override the autouse _neutralize_verify fixture (function-scoped) with
        # canned hit/focus data — same pattern as TestVerifyState.
        import agent_windows.core.tree as t
        monkeypatch.setattr(t, "hit_element_at",
            lambda x, y: {"control_type": "Edit", "name": "Box"})
        monkeypatch.setattr(t, "foreground_window",
            lambda: {"hwnd": 1, "title": "App"})
        with patch("agent_windows.core.input_controller.type_text_at"):
            r = HANDLERS["type"]({"text": "hi", "at_x": 10, "at_y": 20})
        assert 'Hit: Edit "Box"' in r["output"]
        assert 'Focus: "App"' in r["output"]

    def test_click_path_verify_off_keeps_coord_drops_hit_focus(self, monkeypatch):
        monkeypatch.setattr("agent_windows.utils.config.load_config",
            lambda: {"input": {"verify": False}})
        with patch("agent_windows.core.input_controller.type_text_at"):
            r = HANDLERS["type"]({"text": "hi", "at_x": 10, "at_y": 20})
        assert "At: (10, 20)" in r["output"]  # coord echo is unconditional
        assert "Hit:" not in r["output"]      # verify off → no hit/focus
        assert "Focus:" not in r["output"]


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
        # timeout omitted by the caller → forwarded as None (run_shell then
        # reads shell.timeout config). (TODO #13)
        m.assert_called_once_with("Get-Date", timeout=None)

    def test_forwards_timeout(self):
        """TODO #13: an explicit timeout param is forwarded to run_shell."""
        with patch("agent_windows.core.shell_runner.run_shell", return_value={"output": "done"}) as m:
            HANDLERS["shell"]({"command": "Get-Date", "timeout": 5})
        m.assert_called_once_with("Get-Date", timeout=5)

    def test_timeout_zero_normalized_to_none(self):
        """TODO #13: timeout=0 means 'use the config default' (0 would be an
        instant timeout), so the handler normalizes it to None — matching the
        CLI's --timeout 0 semantics and protecting the MCP/batch path (which
        can't apply the CLI's >0 guard)."""
        with patch("agent_windows.core.shell_runner.run_shell", return_value={"output": "done"}) as m:
            HANDLERS["shell"]({"command": "Get-Date", "timeout": 0})
        m.assert_called_once_with("Get-Date", timeout=None)

    def test_command_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["shell"]({"command": 5})

    def test_timeout_rejects_string(self):
        """A JSON/MCP client may pass timeout as a string; the handler must
        reject it (INVALID_PARAMS) rather than forwarding a bad type to
        run_shell, which would crash on arithmetic."""
        with pytest.raises(ValueError, match="timeout"):
            HANDLERS["shell"]({"command": "Get-Date", "timeout": "30"})

    def test_timeout_rejects_bool(self):
        """bool is an int subclass but not a real timeout; True == 1 would
        otherwise silently become a 1s timeout. Reject it explicitly."""
        with pytest.raises(ValueError, match="timeout"):
            HANDLERS["shell"]({"command": "Get-Date", "timeout": True})

    def test_timeout_accepts_float(self):
        with patch("agent_windows.core.shell_runner.run_shell", return_value={"output": "done"}) as m:
            HANDLERS["shell"]({"command": "Get-Date", "timeout": 1.5})
        m.assert_called_once_with("Get-Date", timeout=1.5)


class TestFsHandlers:
    def test_fs_read_forwards_path(self):
        with patch("agent_windows.core.filesystem.read_file", return_value={"output": "c"}) as m:
            HANDLERS["fs_read"]({"path": r"C:\x"})
        # Bare call forwards offset=None, limit=None, encoding=None (TODO #4/#20b).
        m.assert_called_once_with(r"C:\x", offset=None, limit=None, encoding=None)

    def test_fs_read_forwards_offset_limit(self):
        with patch("agent_windows.core.filesystem.read_file", return_value={"output": "c"}) as m:
            HANDLERS["fs_read"]({"path": r"C:\x", "offset": 5, "limit": 10})
        m.assert_called_once_with(r"C:\x", offset=5, limit=10, encoding=None)

    def test_fs_read_forwards_encoding(self):
        """TODO #20b: encoding param forwards to read_file."""
        with patch("agent_windows.core.filesystem.read_file", return_value={"output": "c"}) as m:
            HANDLERS["fs_read"]({"path": r"C:\x", "encoding": "gbk"})
        m.assert_called_once_with(r"C:\x", offset=None, limit=None, encoding="gbk")

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
    def test_process_list_defaults(self):
        with patch("agent_windows.core.process_manager.list_processes", return_value={"output": "c"}) as m:
            HANDLERS["process_list"]({})
        m.assert_called_once_with(sort_by="memory", limit=0, name=None)

    def test_process_list_forwards_params(self):
        with patch("agent_windows.core.process_manager.list_processes", return_value={"output": "c"}) as m:
            HANDLERS["process_list"]({"sort_by": "cpu", "limit": 5, "name": "edge"})
        m.assert_called_once_with(sort_by="cpu", limit=5, name="edge")

    def test_process_kill_accepts_str(self):
        with patch("agent_windows.core.process_manager.kill_process", return_value={"output": "c"}) as m:
            HANDLERS["process_kill"]({"pid_or_name": "notepad"})
        m.assert_called_once_with("notepad", force=False)

    def test_process_kill_accepts_int(self):
        with patch("agent_windows.core.process_manager.kill_process", return_value={"output": "c"}) as m:
            HANDLERS["process_kill"]({"pid_or_name": 1234})
        m.assert_called_once_with(1234, force=False)

    def test_process_kill_forwards_force(self):
        with patch("agent_windows.core.process_manager.kill_process", return_value={"output": "c"}) as m:
            HANDLERS["process_kill"]({"pid_or_name": "notepad", "force": True})
        m.assert_called_once_with("notepad", force=True)

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


# ── registry handler (TODO #11) ──────────────────────────────────────


class TestRegistryHandler:
    def test_get_mode_forwards(self):
        with patch("agent_windows.core.registry.get_value",
                   return_value={"output": "ok"}) as m:
            HANDLERS["registry"]({"mode": "get", "path": r"HKCU:\Soft", "name": "N"})
        m.assert_called_once_with(r"HKCU:\Soft", "N")

    def test_set_mode_forwards_type_default(self):
        with patch("agent_windows.core.registry.set_value",
                   return_value={"output": "ok"}) as m:
            HANDLERS["registry"]({"mode": "set", "path": "p", "name": "n", "value": "v"})
        m.assert_called_once_with("p", "n", "v", reg_type="String")

    def test_set_mode_forwards_custom_type(self):
        with patch("agent_windows.core.registry.set_value",
                   return_value={"output": "ok"}) as m:
            HANDLERS["registry"]({"mode": "set", "path": "p", "name": "n",
                                  "value": "1", "type": "DWord"})
        m.assert_called_once_with("p", "n", "1", reg_type="DWord")

    def test_delete_mode_with_name(self):
        with patch("agent_windows.core.registry.delete_entry",
                   return_value={"output": "ok"}) as m:
            HANDLERS["registry"]({"mode": "delete", "path": "p", "name": "n"})
        m.assert_called_once_with("p", name="n")

    def test_delete_mode_without_name(self):
        with patch("agent_windows.core.registry.delete_entry",
                   return_value={"output": "ok"}) as m:
            HANDLERS["registry"]({"mode": "delete", "path": "p"})
        m.assert_called_once_with("p", name=None)

    def test_delete_mode_empty_name_targets_default_value(self):
        """BUG-B fix: name='' targets the unnamed default value, not the whole
        key. Distinguished from name omitted (→ None → whole key)."""
        with patch("agent_windows.core.registry.delete_entry",
                   return_value={"output": "ok"}) as m:
            HANDLERS["registry"]({"mode": "delete", "path": "p", "name": ""})
        m.assert_called_once_with("p", name="")

    def test_list_mode(self):
        with patch("agent_windows.core.registry.list_key",
                   return_value={"output": "ok"}) as m:
            HANDLERS["registry"]({"mode": "list", "path": "p"})
        m.assert_called_once_with("p")

    def test_invalid_mode_rejected(self):
        with pytest.raises(ValueError, match="mode must be"):
            HANDLERS["registry"]({"mode": "bogus", "path": "p"})

    def test_missing_mode_raises(self):
        with pytest.raises(ValueError, match="missing required parameter 'mode'"):
            HANDLERS["registry"]({"path": "p"})
