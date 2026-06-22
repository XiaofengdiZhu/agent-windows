"""Tests for core/input_controller.py: key-name parsing, shortcut parsing,
coordinate normalization, and mouse button validation.

These exercise the pure-ish parsing/validation logic. The actual Win32 calls
(``user32.keybd_event``, ``mouse_event``, ``SetCursorPos`` ...) are mocked so the
tests stay deterministic and don't move the real cursor.
"""

import pytest
from unittest.mock import patch

from agent_windows.core import input_controller as ic


# ── Key-name resolution: KEY_ALIASES ─────────────────────────────────


class TestKeyAliases:
    """KEY_ALIASES normalizes user-facing names to canonical SPECIAL_KEYS names."""

    @pytest.mark.parametrize("alias,canonical", [
        ("ctrl", "Ctrl"), ("control", "Ctrl"),
        ("alt", "Alt"),
        ("shift", "Shift"),
        ("win", "Win"), ("windows", "Win"), ("super", "Win"),
        ("enter", "Enter"), ("return", "Enter"),
        ("tab", "Tab"),
        ("esc", "Esc"), ("escape", "Esc"),
        ("back", "Back"), ("backspace", "Back"),
        ("del", "Delete"), ("delete", "Delete"),
        ("pgup", "PageUp"), ("page_up", "PageUp"), ("pageup", "PageUp"),
        ("pgdn", "PageDown"), ("page_down", "PageDown"), ("pagedown", "PageDown"),
        ("prtsc", "PrintScreen"), ("printscreen", "PrintScreen"),
        ("menu", "Apps"), ("context", "Apps"), ("apps", "Apps"),
    ])
    def test_alias_resolves(self, alias, canonical):
        assert ic.KEY_ALIASES[alias] == canonical

    def test_function_keys_covered(self):
        for i in range(1, 13):
            assert ic.KEY_ALIASES[f"f{i}"] == f"F{i}"

    def test_no_uppercase_aliases(self):
        """All alias keys are stored lowercase so .lower() lookups always hit."""
        for k in ic.KEY_ALIASES:
            assert k == k.lower(), f"alias {k!r} must be lowercase"


# ── SPECIAL_KEYS → VK code mapping ───────────────────────────────────


class TestSpecialKeys:
    @pytest.mark.parametrize("name,vk", [
        ("Ctrl", ic.VK_CONTROL), ("Shift", ic.VK_SHIFT),
        ("Alt", ic.VK_MENU), ("Win", ic.VK_LWIN),
        ("Enter", ic.VK_RETURN), ("Tab", ic.VK_TAB),
        ("Esc", ic.VK_ESCAPE), ("Back", ic.VK_BACK),
        ("Delete", ic.VK_DELETE), ("Insert", ic.VK_INSERT),
        ("Home", ic.VK_HOME), ("End", ic.VK_END),
        ("PageUp", ic.VK_PRIOR), ("PageDown", ic.VK_NEXT),
        ("Space", ic.VK_SPACE),
    ])
    def test_canonical_special_key_codes(self, name, vk):
        assert ic.SPECIAL_KEYS[name] == vk

    def test_function_keys_in_special_keys(self):
        assert ic.SPECIAL_KEYS["F1"] == ic.VK_F1
        assert ic.SPECIAL_KEYS["F12"] == ic.VK_F12
        for i in range(2, 12):
            assert ic.SPECIAL_KEYS[f"F{i}"] == 0x70 + (i - 1)

    def test_oem_keys_map_shifted_and_unshifted_to_same_code(self):
        """'=' / '+' both press VK_OEM_PLUS; '/' / '?' both VK_OEM_2, etc."""
        pairs = [
            ("=", "+", 0xBB), ("-", "_", 0xBD),
            (",", "<", 0xBC), (".", ">", 0xBE),
            ("/", "?", 0xBF), (";", ":", 0xBA),
            ("'", '"', 0xDE), ("[", "{", 0xDB),
            ("]", "}", 0xDD), ("\\", "|", 0xDC),
            ("`", "~", 0xC0),
        ]
        for plain, shifted, vk in pairs:
            assert ic.SPECIAL_KEYS[plain] == vk
            assert ic.SPECIAL_KEYS[shifted] == vk


# ── Extended-key classification ──────────────────────────────────────


class TestExtendedKeys:
    @pytest.mark.parametrize("vk", [ic.VK_HOME, ic.VK_END, ic.VK_PRIOR, ic.VK_NEXT,
                                     ic.VK_LEFT, ic.VK_UP, ic.VK_RIGHT, ic.VK_DOWN,
                                     ic.VK_INSERT, ic.VK_DELETE])
    def test_nav_edit_keys_are_extended(self, vk):
        assert ic._is_extended_key(vk) is True

    @pytest.mark.parametrize("vk", [ic.VK_CONTROL, ic.VK_SHIFT, ic.VK_RETURN,
                                     ic.VK_TAB, ic.VK_SPACE, ic.VK_F1])
    def test_modifiers_and_others_not_extended(self, vk):
        assert ic._is_extended_key(vk) is False


# ── Coordinate normalization ─────────────────────────────────────────


class TestNormalize:
    def test_origin(self):
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 1920, 1080)):
            assert ic._normalize(0, 0) == (0, 0)

    def test_far_corner(self):
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 1920, 1080)):
            # (1920-1) * 65535 // (1920-1) == 65535
            nx, ny = ic._normalize(1919, 1079)
            assert nx == 65535
            assert ny == 65535

    def test_multi_monitor_offset(self):
        """Negative virtual-screen origin (left monitor) shifts coordinates."""
        with patch.object(ic, "_get_virtual_screen", return_value=(-1920, 0, 3840, 1080)):
            # x=0 in pixel space is at offset 1920 into a 3840-wide virtual screen.
            nx, _ = ic._normalize(0, 0)
            assert nx == 1920 * 65535 // (3840 - 1)

    def test_zero_width_does_not_divide_by_zero(self):
        """max(vw-1, 1) guard prevents ZeroDivisionError on degenerate screens."""
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 1, 1)):
            nx, ny = ic._normalize(5, 5)
            assert nx == 5 * 65535  # // 1
            assert ny == 5 * 65535

    def test_virtual_screen_reads_four_metrics(self):
        """_get_virtual_screen queries SM_X/YVIRTUALSCREEN + SM_CX/CYVIRTUALSCREEN."""
        with patch.object(ic.user32, "GetSystemMetrics", return_value=100) as m:
            vl, vt, vw, vh = ic._get_virtual_screen()
            assert (vl, vt, vw, vh) == (100, 100, 100, 100)
            assert list(m.call_args_list[i][0][0] for i in range(4)) == [76, 77, 78, 79]


# ── Mouse button validation ──────────────────────────────────────────


class TestMouseButtonValidation:
    @pytest.mark.parametrize("fn", [ic.mouse_down, ic.mouse_up])
    @pytest.mark.parametrize("bad", ["", "LEFT", "x", "left-right", None])
    def test_invalid_button_raises(self, fn, bad):
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 100, 100)):
            with pytest.raises(ValueError, match="Invalid button"):
                fn(10, 10, button=bad)

    @pytest.mark.parametrize("fn", [ic.mouse_down, ic.mouse_up])
    @pytest.mark.parametrize("button", ["left", "right", "middle"])
    def test_valid_button_sends_one_event(self, fn, button):
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 100, 100)), \
             patch.object(ic.user32, "SetCursorPos") as mock_set, \
             patch.object(ic, "_send_mouse_event") as mock_evt:
            fn(50, 50, button=button)
        mock_set.assert_called_once_with(50, 50)
        mock_evt.assert_called_once()  # down OR up — single event each

    @pytest.mark.parametrize("button,expected", [
        ("left", ic.MOUSE_LEFTDOWN), ("right", ic.MOUSE_RIGHTDOWN), ("middle", ic.MOUSE_MIDDLEDOWN),
    ])
    def test_mouse_down_sends_correct_button_flag(self, button, expected):
        """A regression swapping the button dict would silently send LEFTDOWN."""
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 100, 100)), \
             patch.object(ic.user32, "SetCursorPos"), \
             patch.object(ic, "_send_mouse_event") as mock_evt:
            ic.mouse_down(50, 50, button=button)
        flags = mock_evt.call_args.args[0]
        assert flags & expected
        assert flags & ic.MOUSE_ABSOLUTE

    @pytest.mark.parametrize("button,expected", [
        ("left", ic.MOUSE_LEFTUP), ("right", ic.MOUSE_RIGHTUP), ("middle", ic.MOUSE_MIDDLEUP),
    ])
    def test_mouse_up_sends_correct_button_flag(self, button, expected):
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 100, 100)), \
             patch.object(ic.user32, "SetCursorPos"), \
             patch.object(ic, "_send_mouse_event") as mock_evt:
            ic.mouse_up(50, 50, button=button)
        flags = mock_evt.call_args.args[0]
        assert flags & expected
        assert flags & ic.MOUSE_ABSOLUTE


# ── hover MOUSE_MOVE event ───────────────────────────────────────────


class TestHover:
    def test_emits_mouse_move_absolute_event(self):
        """hover()'s whole point vs move_to: an extra MOUSE_MOVE|MOUSE_ABSOLUTE
        mouse_event so WM_MOUSEMOVE reaches the target window."""
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 100, 100)), \
             patch.object(ic.user32, "SetCursorPos") as mock_set, \
             patch.object(ic, "_send_mouse_event") as mock_evt, \
             patch.object(ic.time, "sleep"):
            ic.hover(30, 40, wait_time=0)
        mock_set.assert_called_once_with(30, 40)
        mock_evt.assert_called_once()
        flags = mock_evt.call_args.args[0]
        assert flags & ic.MOUSE_MOVE
        assert flags & ic.MOUSE_ABSOLUTE


# ── shortcut() parsing & event sequencing ────────────────────────────


class TestShortcutParsing:
    """shortcut() resolves names, holds modifiers, presses the final key,
    then releases modifiers in reverse. We mock the low-level press/release
    senders so we can assert ordering without touching the real keyboard."""

    def _captured(self):
        """Return (shortcut_fn, log) where log records ('press'|'send'|'release', vk)."""
        log: list[tuple[str, int]] = []

        def fake_press(vk):
            log.append(("press", vk))

        def fake_release(vk):
            log.append(("release", vk))

        def fake_send(vk, scan=0):
            log.append(("send", vk))

        return fake_press, fake_release, fake_send, log

    def test_single_key(self):
        press, release, send, log = self._captured()
        with patch.object(ic, "_press_key", press), patch.object(ic, "_release_key", release), \
             patch.object(ic, "_send_vk_key", send):
            ic.shortcut("enter")
        assert log == [("send", ic.VK_RETURN)]

    def test_modifier_combo_releases_in_reverse(self):
        press, release, send, log = self._captured()
        with patch.object(ic, "_press_key", press), patch.object(ic, "_release_key", release), \
             patch.object(ic, "_send_vk_key", send):
            ic.shortcut("ctrl+shift+s")
        # hold Ctrl, hold Shift, send S, release Shift, release Ctrl
        assert log == [
            ("press", ic.VK_CONTROL),
            ("press", ic.VK_SHIFT),
            ("send", ord("S")),
            ("release", ic.VK_SHIFT),
            ("release", ic.VK_CONTROL),
        ]

    def test_aliases_resolved(self):
        """Aliases like 'control'/'return'/'pgdn' route to canonical VKs."""
        press, release, send, log = self._captured()
        with patch.object(ic, "_press_key", press), patch.object(ic, "_release_key", release), \
             patch.object(ic, "_send_vk_key", send):
            ic.shortcut("control+return")
        assert log[0] == ("press", ic.VK_CONTROL)
        assert log[1] == ("send", ic.VK_RETURN)

    def test_single_character_uppercased(self):
        """A bare letter resolves to its uppercase ASCII code."""
        press, release, send, log = self._captured()
        with patch.object(ic, "_press_key", press), patch.object(ic, "_release_key", release), \
             patch.object(ic, "_send_vk_key", send):
            ic.shortcut("a")
        assert log == [("send", ord("A"))]

    def test_canonical_name_without_alias(self):
        """A canonical SPECIAL_KEYS name passed verbatim (no alias lookup) still
        resolves — KEY_ALIASES.get(part.lower(), part) misses 'Home'/'F4', then
        the raw name hits SPECIAL_KEYS. This exercises a different branch than
        the alias path."""
        press, release, send, log = self._captured()
        with patch.object(ic, "_press_key", press), patch.object(ic, "_release_key", release), \
             patch.object(ic, "_send_vk_key", send):
            ic.shortcut("Home")
            ic.shortcut("F4")
        assert log == [("send", ic.VK_HOME), ("send", 0x73)]

    def test_unknown_key_raises(self):
        with patch.object(ic, "_press_key"), patch.object(ic, "_release_key"), \
             patch.object(ic, "_send_vk_key"):
            with pytest.raises(ValueError, match="Unknown key"):
                ic.shortcut("ctrl+notakey")

    def test_whitespace_around_parts_stripped(self):
        press, release, send, log = self._captured()
        with patch.object(ic, "_press_key", press), patch.object(ic, "_release_key", release), \
             patch.object(ic, "_send_vk_key", send):
            ic.shortcut("  ctrl + c  ")
        assert log == [("press", ic.VK_CONTROL), ("send", ord("C")), ("release", ic.VK_CONTROL)]

    def test_oem_shifted_char(self):
        """A shifted OEM char like '?' resolves to its SPECIAL_KEYS entry."""
        press, release, send, log = self._captured()
        with patch.object(ic, "_press_key", press), patch.object(ic, "_release_key", release), \
             patch.object(ic, "_send_vk_key", send):
            ic.shortcut("?")
        assert log == [("send", 0xBF)]


# ── Low-level key senders: _send_vk_key / _press_key / _release_key ──


class TestSendVkKey:
    """Drive the real _send_vk_key/_press_key/_release_key bodies (not patched)
    so the extended-flag + scan-resolution + keybd_event down/up logic runs."""

    def test_send_vk_key_emits_down_then_up(self):
        with patch.object(ic.user32, "keybd_event") as mock_kb, \
             patch.object(ic.user32, "MapVirtualKeyA", return_value=0x42), \
             patch.object(ic.time, "sleep"):
            ic._send_vk_key(ic.VK_RETURN)
        # exactly one down (no KEY_UP) and one up (KEY_UP set)
        assert mock_kb.call_count == 2
        down_flags = mock_kb.call_args_list[0].args[2]
        up_flags = mock_kb.call_args_list[1].args[2]
        assert not down_flags & ic.KEY_UP
        assert up_flags & ic.KEY_UP
        # scan comes from MapVirtualKeyA when not overridden
        assert mock_kb.call_args_list[0].args[1] == 0x42

    def test_send_vk_key_extended_key_sets_flag(self):
        """Home is an extended key → KEY_EXTENDED must be OR'd into both events."""
        with patch.object(ic.user32, "keybd_event") as mock_kb, \
             patch.object(ic.user32, "MapVirtualKeyA", return_value=0x47), \
             patch.object(ic.time, "sleep"):
            ic._send_vk_key(ic.VK_HOME)
        for call in mock_kb.call_args_list:
            assert call.args[2] & ic.KEY_EXTENDED
        assert mock_kb.call_args_list[0].args[0] == ic.VK_HOME

    def test_send_vk_key_explicit_scan_skips_mapvirtualkey(self):
        with patch.object(ic.user32, "keybd_event"), \
             patch.object(ic.user32, "MapVirtualKeyA") as mock_map, \
             patch.object(ic.time, "sleep"):
            ic._send_vk_key(ic.VK_SPACE, scan=99)
        mock_map.assert_not_called()

    def test_press_and_release_hold_semantics(self):
        """_press_key emits only a down; _release_key emits only an up."""
        with patch.object(ic.user32, "keybd_event") as mock_kb, \
             patch.object(ic.user32, "MapVirtualKeyA", return_value=1), \
             patch.object(ic.time, "sleep"):
            ic._press_key(ic.VK_SHIFT)
            assert mock_kb.call_count == 1
            assert not mock_kb.call_args_list[0].args[2] & ic.KEY_UP
            ic._release_key(ic.VK_SHIFT)
            assert mock_kb.call_count == 2
            assert mock_kb.call_args_list[1].args[2] & ic.KEY_UP


# ── type_text & Unicode input path ───────────────────────────────────


class TestTypeText:
    def test_send_unicode_char_emits_unicode_down_and_up(self):
        with patch.object(ic, "_send_input") as mock_si, \
             patch.object(ic.time, "sleep"):
            ic._send_unicode_char("A")
        assert mock_si.call_count == 1
        inputs = mock_si.call_args.args
        assert len(inputs) == 2  # down + up
        down, up = inputs
        assert down.union.ki.wVk == 0
        assert down.union.ki.wScan == ord("A")
        assert down.union.ki.dwFlags & ic.KEY_UNICODE
        assert not down.union.ki.dwFlags & ic.KEY_UP
        assert up.union.ki.dwFlags & ic.KEY_UNICODE
        assert up.union.ki.dwFlags & ic.KEY_UP


# ── INPUT struct dedup (QUAL-07) ─────────────────────────────────────


class TestInputStructDedup:
    """input_controller reuses the ctypes INPUT/MOUSEINPUT/KEYBDINPUT/_INPUTUnion
    from uia.enums instead of redefining them. Lock that the four names are the
    SAME objects as the vendored ones (no silent local re-definition) and that
    sizeof(INPUT) is unchanged — the vendored union adds a HARDWAREINPUT branch,
    but MOUSEINPUT is the largest member so the union (and INPUT) size is
    identical. A regression that re-introduced a local struct or that drifted
    the field layout would fail here."""

    def test_structs_are_the_uia_enums_objects(self):
        from agent_windows.uia.enums import (
            INPUT as UiaInput,
            MOUSEINPUT as UiaMouse,
            KEYBDINPUT as UiaKeybd,
            _INPUTUnion as UiaUnion,
        )
        # Same class objects — not parallel re-definitions.
        assert ic.INPUT is UiaInput
        assert ic.MOUSEINPUT is UiaMouse
        assert ic.KEYBDINPUT is UiaKeybd
        assert ic._INPUTUnion is UiaUnion

    def test_sizeof_INPUT_unchanged(self):
        """sizeof(INPUT) must be 40 — the value before and after the dedup.
        MOUSEINPUT (32B) is the largest union member; HARDWAREINPUT (8B) and
        KEYBDINPUT (24B) don't grow the union, so the vendored 3-branch union
        has the same size as the old 2-branch one."""
        import ctypes
        assert ctypes.sizeof(ic.INPUT) == 40
        assert ctypes.sizeof(ic._INPUTUnion) == 32  # == max member (MOUSEINPUT)

    def test_mouse_and_key_helpers_build_valid_inputs(self):
        """The 3 private helpers construct INPUT via the imported structs with
        the mi/ki union branches — both exist in the vendored 3-branch union."""
        m = ic._mouse_input(100, 200, 0, 0x0002)
        assert m.type == ic.INPUT_MOUSE
        assert m.union.mi.dx == 100
        assert m.union.mi.dy == 200
        k = ic._key_input(0x0D, 0, 0)
        assert k.type == ic.INPUT_KEYBOARD
        assert k.union.ki.wVk == 0x0D

    def test_type_text_one_pair_per_char(self):
        with patch.object(ic, "_send_unicode_char") as mock_char, \
             patch.object(ic.time, "sleep"):
            ic.type_text("abc", interval=0, wait_time=0)
        assert [c.args[0] for c in mock_char.call_args_list] == ["a", "b", "c"]

    def test_type_text_handles_multibyte(self):
        """CJK chars go through the Unicode path; verify the scan code is the
        full codepoint (not low-byte-truncated)."""
        with patch.object(ic, "_send_input") as mock_si, \
             patch.object(ic.time, "sleep"):
            ic._send_unicode_char("你")
        inputs = mock_si.call_args.args
        assert inputs[0].union.ki.wScan == 0x4F60  # ord('你')

    def test_type_text_at_clicks_clears_then_types(self):
        with patch.object(ic, "click") as mock_click, \
             patch.object(ic, "shortcut") as mock_short, \
             patch.object(ic, "_send_vk_key") as mock_vk, \
             patch.object(ic, "type_text") as mock_type, \
             patch.object(ic.time, "sleep"):
            ic.type_text_at("hi", 10, 20, clear=True, enter=True)
        mock_click.assert_called_once_with(10, 20)
        mock_short.assert_called_once_with("ctrl+a")
        # VK_DELETE then text then VK_RETURN
        assert mock_vk.call_args_list[0].args[0] == ic.VK_DELETE
        mock_type.assert_called_once_with("hi")
        assert mock_vk.call_args_list[-1].args[0] == ic.VK_RETURN

    # ── caret_position (TODO #16) ──────────────────────────────────────

    def test_move_caret_start_sends_home(self):
        with patch.object(ic, "_send_vk_key") as m_vk:
            ic.move_caret("start")
        m_vk.assert_called_once_with(ic.VK_HOME)

    def test_move_caret_end_sends_end(self):
        with patch.object(ic, "_send_vk_key") as m_vk:
            ic.move_caret("end")
        m_vk.assert_called_once_with(ic.VK_END)

    def test_move_caret_idle_sends_nothing(self):
        with patch.object(ic, "_send_vk_key") as m_vk:
            ic.move_caret("idle")
        m_vk.assert_not_called()

    def test_type_text_at_caret_start_presses_home_before_clear(self):
        """Order must be click → Home → clear(Ctrl+A+Del) → type → Enter."""
        with patch.object(ic, "click") as m_click, \
             patch.object(ic, "shortcut") as m_short, \
             patch.object(ic, "_send_vk_key") as m_vk, \
             patch.object(ic, "type_text") as m_type, \
             patch.object(ic.time, "sleep"):
            ic.type_text_at("hi", 10, 20, clear=True, enter=True, caret_position="start")
        m_click.assert_called_once_with(10, 20)
        # First vk call is Home (caret), then VK_DELETE (clear), last is RETURN
        assert m_vk.call_args_list[0].args[0] == ic.VK_HOME
        assert m_vk.call_args_list[1].args[0] == ic.VK_DELETE
        assert m_vk.call_args_list[-1].args[0] == ic.VK_RETURN
        m_type.assert_called_once_with("hi")

    def test_type_text_at_caret_idle_backward_compatible(self):
        """Default caret_position=idle must not inject any extra vk call (backward compat)."""
        with patch.object(ic, "click"), \
             patch.object(ic, "_send_vk_key") as m_vk, \
             patch.object(ic, "type_text"), \
             patch.object(ic.time, "sleep"):
            ic.type_text_at("hi", 10, 20)  # no caret_position kwarg
        # idle → no Home/End; with clear=False/enter=False there are zero vk calls
        m_vk.assert_not_called()


# ── click() parameters ───────────────────────────────────────────────


class TestClick:
    def test_double_click_sends_two_downup_pairs(self):
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 100, 100)), \
             patch.object(ic.user32, "SetCursorPos"), \
             patch.object(ic, "_send_mouse_event") as mock_evt:
            ic.click(10, 10, double=True, wait_time=0)
        # 2 clicks × (down + up) = 4 events
        assert mock_evt.call_count == 4

    def test_right_click_uses_right_flags(self):
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 100, 100)), \
             patch.object(ic.user32, "SetCursorPos"), \
             patch.object(ic, "_send_mouse_event") as mock_evt:
            ic.click(10, 10, right=True, wait_time=0)
        flags = [c.args[0] for c in mock_evt.call_args_list]
        assert all(f & ic.MOUSE_RIGHTDOWN or f & ic.MOUSE_RIGHTUP for f in flags)

    def test_left_click_uses_left_flags(self):
        with patch.object(ic, "_get_virtual_screen", return_value=(0, 0, 100, 100)), \
             patch.object(ic.user32, "SetCursorPos"), \
             patch.object(ic, "_send_mouse_event") as mock_evt:
            ic.click(10, 10, right=False, double=False, wait_time=0)
        flags = [c.args[0] for c in mock_evt.call_args_list]
        assert all(f & ic.MOUSE_LEFTDOWN or f & ic.MOUSE_LEFTUP for f in flags)


# ── scroll_at() direction ────────────────────────────────────────────


class TestScrollAt:
    def test_down_is_negative_delta(self):
        with patch.object(ic.user32, "SetCursorPos"), \
             patch.object(ic, "_send_mouse_event") as mock_evt:
            ic.scroll_at(10, 10, direction="down", amount=2, wait_time=0)
        deltas = [c.args[3] for c in mock_evt.call_args_list]
        assert all(d == -ic.WHEEL_DELTA for d in deltas)
        assert len(deltas) == 2

    def test_up_is_positive_delta(self):
        with patch.object(ic.user32, "SetCursorPos"), \
             patch.object(ic, "_send_mouse_event") as mock_evt:
            ic.scroll_at(10, 10, direction="up", amount=3, wait_time=0)
        deltas = [c.args[3] for c in mock_evt.call_args_list]
        assert all(d == ic.WHEEL_DELTA for d in deltas)
        assert len(deltas) == 3
