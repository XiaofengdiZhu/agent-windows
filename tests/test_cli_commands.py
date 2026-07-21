"""Tests for the Typer CLI command modules: argument parsing, param assembly,
and the inline logic in batch / wait / config / daemon commands.

Uses typer.testing.CliRunner to invoke the real registered app, with `_cmd`
(the dispatch sink) and `dispatch`/`print_result` patched so no daemon/IPC
happens. test_dispatcher.py already covers dispatch()/print_result() directly;
test_shell.py covers the core shell_runner. This file covers the CLI layer.
"""

import json
import sys
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from agent_windows.cli.main import app, main, _preprocess_argv, _COORD_CMDS

runner = CliRunner()


def _cmd_calls(mock_cmd):
    """Extract (method, params) from each _cmd call as a list of tuples."""
    out = []
    for c in mock_cmd.call_args_list:
        args = c.args
        # _cmd(ctx, method, **params) → args[0]=ctx, args[1]=method
        method = args[1] if len(args) > 1 else None
        params = dict(c.kwargs)
        out.append((method, params))
    return out


# ── input command param parsing ──────────────────────────────────────


class TestInputCommands:
    @patch("agent_windows.cli.commands.input._cmd")
    def test_click_forwards_coords_and_flags(self, mock_cmd):
        result = runner.invoke(app, ["click", "100", "200", "--right", "--double"])
        assert result.exit_code == 0
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "click"
        assert params == {"x": 100, "y": 200, "right": True, "double": True}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_click_defaults(self, mock_cmd):
        runner.invoke(app, ["click", "1", "2"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"x": 1, "y": 2, "right": False, "double": False}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_type_with_at_option_parses_coords(self, mock_cmd):
        """--at '10,20' → at_x/at_y ints."""
        runner.invoke(app, ["type", "hello", "--at", "10,20"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["at_x"] == 10
        assert params["at_y"] == 20
        assert params["text"] == "hello"

    @patch("agent_windows.cli.commands.input._cmd")
    def test_type_without_at_omits_coords(self, mock_cmd):
        runner.invoke(app, ["type", "hi", "--enter", "--clear"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert "at_x" not in params
        assert params == {"text": "hi", "enter": True, "clear": True, "caret_position": "idle"}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_type_caret_option_forwarded(self, mock_cmd):
        """--caret start maps to caret_position=start param (TODO #16)."""
        runner.invoke(app, ["type", "hi", "--caret", "start"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["caret_position"] == "start"

    @patch("agent_windows.cli.commands.input._cmd")
    def test_type_at_ignored_when_not_two_parts(self, mock_cmd):
        """A malformed --at (not exactly 'x,y') is silently ignored."""
        runner.invoke(app, ["type", "hi", "--at", "onlyone"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert "at_x" not in params

    @patch("agent_windows.cli.commands.input._cmd")
    def test_mouse_down_button_option(self, mock_cmd):
        runner.invoke(app, ["mouse-down", "5", "6", "--button", "middle"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "mouse_down"
        assert params == {"x": 5, "y": 6, "button": "middle"}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_scroll_direction_and_amount(self, mock_cmd):
        runner.invoke(app, ["scroll", "1", "2", "--direction", "up", "--amount", "7"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"x": 1, "y": 2, "direction": "up", "amount": 7}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_drag_four_coords(self, mock_cmd):
        runner.invoke(app, ["drag", "1", "2", "3", "4", "--right"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"x1": 1, "y1": 2, "x2": 3, "y2": 4, "right": True}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_shortcut_keys(self, mock_cmd):
        runner.invoke(app, ["shortcut", "ctrl+c"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"keys": "ctrl+c"}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_key_down_with_hold(self, mock_cmd):
        runner.invoke(app, ["key-down", "a", "--hold", "0.5"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "key_down"
        assert params == {"keys": "a", "hold": 0.5}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_key_down_without_hold_omits_it(self, mock_cmd):
        """No --hold → hold omitted (stays pressed; pair with key-up)."""
        runner.invoke(app, ["key-down", "ctrl+shift+a"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "key_down"
        assert params == {"keys": "ctrl+shift+a"}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_key_up(self, mock_cmd):
        runner.invoke(app, ["key-up", "ctrl+shift+a"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "key_up"
        assert params == {"keys": "ctrl+shift+a"}

    # ── --selector / -s (TODO #13) ─────────────────────────────────────

    @patch("agent_windows.cli.commands.input._cmd")
    def test_click_selector_overrides_coords(self, mock_cmd):
        """--selector @eN sends selector param; no x/y in params."""
        result = runner.invoke(app, ["click", "--selector", "@e3", "--right"])
        assert result.exit_code == 0
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"selector": "@e3", "right": True, "double": False}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_click_short_flag_selector(self, mock_cmd):
        """-s is the short alias for --selector."""
        runner.invoke(app, ["click", "-s", "@e2"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["selector"] == "@e2"
        assert "x" not in params

    @patch("agent_windows.cli.commands.input._cmd")
    def test_click_coords_still_work_without_selector(self, mock_cmd):
        """Backward compat: bare X Y still produces x/y params."""
        runner.invoke(app, ["click", "100", "200"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"x": 100, "y": 200, "right": False, "double": False}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_click_missing_coords_and_selector_errors(self, mock_cmd):
        """Neither coords nor selector → CLI usage error (non-zero exit)."""
        result = runner.invoke(app, ["click"])
        assert result.exit_code != 0
        mock_cmd.assert_not_called()

    @patch("agent_windows.cli.commands.input._cmd")
    def test_click_one_coord_errors(self, mock_cmd):
        """Only one coordinate (forgot the second) → usage error, not a call."""
        result = runner.invoke(app, ["click", "100"])
        assert result.exit_code != 0
        mock_cmd.assert_not_called()

    @patch("agent_windows.cli.commands.input._cmd")
    def test_scroll_selector(self, mock_cmd):
        runner.invoke(app, ["scroll", "-s", "@e5", "--direction", "up"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"selector": "@e5", "direction": "up", "amount": 3}

    @patch("agent_windows.cli.commands.input._cmd")
    def test_type_at_selector(self, mock_cmd):
        """--at-selector sends at_selector param; no at_x/at_y."""
        runner.invoke(app, ["type", "hi", "--at-selector", "@e3"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["at_selector"] == "@e3"
        assert "at_x" not in params
        assert params["text"] == "hi"


# ── negative coordinates (C1: fallback CLI accepts negative coords) ───


class TestNegativeCoordinates:
    """Multi-monitor coordinates go negative (a window at x=-1920). Click/Typer
    reject a bare `-1` token as "No such option" before the command ever runs.
    The fix is argv preprocessing in ``main()`` (a marker prefix so Click sees a
    non-option token) + a custom ``parser`` that strips the marker.

    These tests exercise BOTH layers — they call ``main()`` with a monkeypatched
    ``sys.argv`` (the real entry path) rather than ``runner.invoke(app, ...)``,
    because the preprocessor runs *before* the Typer app parses, and CliRunner
    bypasses it by passing args directly to ``app.main``.
    """

    def _run(self, argv_rest, module):
        """Invoke main() with sys.argv=[prog, *argv_rest], _cmd patched in module.

        Returns (result_exit_code, list_of_(method, params)). main() calls
        app() which on error raises SystemExit(code); we capture it.
        """
        calls: list = []

        def capture(ctx, method, **params):
            calls.append((method, params))
            return ""

        with patch(module + "._cmd", side_effect=capture):
            old = sys.argv
            sys.argv = ["agent-windows-python"] + argv_rest
            code = 0
            try:
                main()
            except SystemExit as e:
                code = e.code if isinstance(e.code, int) else 1
            finally:
                sys.argv = old
        return code, calls

    # ── preprocessing unit tests (pure function, no app) ───────────────

    def test_preprocess_whitelist_only(self):
        """Only the 9 coord commands get negative tokens rewritten."""
        for cmd in ("click", "scroll", "move", "dblclick", "hover",
                    "mouse-down", "mouse-up", "drag", "highlight"):
            assert cmd in _COORD_CMDS, f"{cmd} missing from whitelist"
        # rewritten: leading "-<digits>" → marker-prefixed
        out = _preprocess_argv(["aw", "click", "-1", "-1"])
        assert out == ["aw", "click", "\x01-1", "\x01-1"]

    def test_preprocess_leaves_positive_coords_untouched(self):
        """Positive coordinates (no leading dash) pass through verbatim."""
        out = _preprocess_argv(["aw", "click", "100", "200"])
        assert out == ["aw", "click", "100", "200"]

    def test_preprocess_skips_non_whitelisted_commands(self):
        """config/mcp/fs/shell and unknown commands are untouched, even with
        a leading-`-<digits>` token (a future command's own concern)."""
        for argv in (["aw", "config", "show", "-1"],
                     ["aw", "fs", "read", "-1"],
                     ["aw", "shell", "-1"]):
            assert _preprocess_argv(list(argv)) == list(argv)

    def test_preprocess_keeps_options_after_negative_coords(self):
        """`click -1 -1 --right` → both coords marked, --right preserved."""
        out = _preprocess_argv(["aw", "click", "-1", "-1", "--right"])
        assert out == ["aw", "click", "\x01-1", "\x01-1", "--right"]

    def test_preprocess_stops_at_double_dash(self):
        """A literal `--` ends option processing; tokens after it (Click's own
        raw-positional mechanism) must NOT be marked again — Click already
        treats everything after `--` as a positional."""
        out = _preprocess_argv(["aw", "click", "--", "-1", "-1"])
        assert out == ["aw", "click", "--", "-1", "-1"]

    def test_preprocess_does_not_touch_option_values(self):
        """A leading-`-<digits>` that is the *value* of a value-taking option
        on a whitelisted command is still rewritten — but Click consumes it as
        the option's value before the parser runs, so the marker is harmless
        (the option's own converter receives the marked string). In practice no
        whitelisted coord command has a value-taking option, so this is moot.
        The key invariant: the rewrite is scoped to whitelisted commands only,
        so `screenshot --monitor -1` is never touched."""
        out = _preprocess_argv(["aw", "screenshot", "--monitor", "-1"])
        assert out == ["aw", "screenshot", "--monitor", "-1"]

    # ── end-to-end: negative coords reach the handler as ints ──────────

    def test_click_negative_coords_forwarded(self):
        """`click -1 -1` → _cmd receives x=-1, y=-1 (regression: was exit 2)."""
        code, calls = self._run(["click", "-1", "-1"], "agent_windows.cli.commands.input")
        assert code == 0, "negative click must not exit 2 (Click option-reject)"
        method, params = calls[0]
        assert method == "click"
        assert params["x"] == -1 and params["y"] == -1
        assert isinstance(params["x"], int)  # marker stripped, not leaked

    def test_click_negative_coords_mixed_with_flags(self):
        """Flags after negative coords are still parsed as flags, not coords."""
        code, calls = self._run(["click", "-1", "-1", "--right", "--double"],
                                "agent_windows.cli.commands.input")
        assert code == 0
        _, params = calls[0]
        assert params == {"x": -1, "y": -1, "right": True, "double": True}

    def test_click_negative_coords_after_option_reach_parser(self):
        """`click -1 -1 --right -100`: the negative `-100` after the option is
        NOT swallowed by --right (a flag, no value). It reaches coords as a 3rd
        positional, which _xy_or_selector rejects with a clear usage error
        (len != 2). The point: preprocessing let it through Click without the
        old "No such option: -1" — the error is now the *command's* own
        "provide X Y" message, not Click's option-reject."""
        code, calls = self._run(["click", "-1", "-1", "--right", "-100"],
                                "agent_windows.cli.commands.input")
        # _xy_or_selector sees 3 coords → BadParameter → non-zero, but NOT the
        # old Click "No such option" (that would also be code 2, so assert the
        # message differs to prove preprocessing ran).
        assert code != 0
        # _cmd was never reached (the error is at param assembly)
        assert calls == []

    def test_all_seven_list_coord_commands_accept_negatives(self):
        """Each list[int]-coords command (click/scroll/move/dblclick/hover/
        mouse-down/mouse-up) accepts negative X Y."""
        for cmd in ("click", "scroll", "move", "dblclick", "hover",
                    "mouse-down", "mouse-up"):
            code, calls = self._run([cmd, "-1", "-1"],
                                    "agent_windows.cli.commands.input")
            assert code == 0, f"{cmd} -1 -1 exited {code}"
            _, params = calls[0]
            assert params["x"] == -1 and params["y"] == -1, f"{cmd} got {params}"

    def test_drag_four_negative_coords(self):
        """drag takes 4 separate int params; all can be negative (multi-mon)."""
        code, calls = self._run(["drag", "-1920", "0", "-100", "100"],
                                "agent_windows.cli.commands.input")
        assert code == 0
        _, params = calls[0]
        assert params == {"x1": -1920, "y1": 0, "x2": -100, "y2": 100,
                          "right": False}

    def test_highlight_negative_coords(self):
        """highlight (in observe.py) takes 4 int params; multi-mon negatives ok."""
        code, calls = self._run(["highlight", "-10", "-10", "10", "10"],
                                "agent_windows.cli.commands.observe")
        assert code == 0
        _, params = calls[0]
        assert params == {"x1": -10, "y1": -10, "x2": 10, "y2": 10}

    # ── non-regression: positive coords & options still work via main() ─

    def test_positive_coords_still_work_via_main(self):
        """main() preprocessing is a no-op for positive coords."""
        code, calls = self._run(["click", "100", "200"],
                                "agent_windows.cli.commands.input")
        assert code == 0
        _, params = calls[0]
        assert params["x"] == 100 and params["y"] == 200

    def test_selector_still_works_via_main(self):
        """--selector/-s path is unaffected by preprocessing."""
        code, calls = self._run(["click", "-s", "@e2"],
                                "agent_windows.cli.commands.input")
        assert code == 0
        _, params = calls[0]
        assert params["selector"] == "@e2"
        assert "x" not in params

    def test_non_coord_command_untouched_via_main(self):
        """A non-whitelisted command runs through main() unchanged."""
        code, calls = self._run(["shortcut", "ctrl+c"],
                                "agent_windows.cli.commands.input")
        assert code == 0
        _, params = calls[0]
        assert params == {"keys": "ctrl+c"}

    # ── --help is not broken by the custom parser ──────────────────────

    def test_help_still_works(self):
        """The custom parser must not break `--help` (it runs on real args only)."""
        old = sys.argv
        sys.argv = ["agent-windows-python", "click", "--help"]
        code = 0
        try:
            main()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
        finally:
            sys.argv = old
        assert code == 0


# ── observe/screenshot param assembly ────────────────────────────────


class TestObserveCommands:
    @patch("agent_windows.cli.commands.observe._cmd")
    def test_observe_defaults_to_snapshot_and_screenshot(self, mock_cmd):
        """No flags → snapshot=True, screenshot=True (the default branch)."""
        runner.invoke(app, ["observe"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"snapshot": True, "screenshot": True, "ground": ""}

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_observe_explicit_flags(self, mock_cmd):
        runner.invoke(app, ["observe", "--snapshot", "--ground", "the button"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["snapshot"] is True
        assert params["screenshot"] is False
        assert params["ground"] == "the button"

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_screenshot_bbox_parsed_to_ints(self, mock_cmd):
        runner.invoke(app, ["screenshot", "--bbox", "10,20,30,40"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["bbox"] == [10, 20, 30, 40]

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_screenshot_selector_included_when_given(self, mock_cmd):
        runner.invoke(app, ["screenshot", "--selector", "@e3"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["selector"] == "@e3"

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_screenshot_defaults_no_selector_no_bbox(self, mock_cmd):
        runner.invoke(app, ["screenshot"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert "selector" not in params
        assert "bbox" not in params
        assert params == {"output": "", "monitor": -1}

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_screenshot_annotate_flag(self, mock_cmd):
        """--annotate sets use_annotation and forwards grid params (TODO #14)."""
        runner.invoke(app, ["screenshot", "--annotate", "--grid-w", "2", "--grid-h", "3"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["use_annotation"] is True
        assert params["width_reference_line"] == 2
        assert params["height_reference_line"] == 3

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_screenshot_annotate_without_grid(self, mock_cmd):
        """--annotate alone forwards just use_annotation (no grid keys)."""
        runner.invoke(app, ["screenshot", "--annotate"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["use_annotation"] is True
        assert "width_reference_line" not in params
        assert "height_reference_line" not in params

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_highlight_only_includes_non_default_options(self, mock_cmd):
        """color defaults to 'red' and is omitted unless overridden; others added."""
        runner.invoke(app, ["highlight", "0", "0", "10", "10", "--label", "L", "--color", "blue"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"x1": 0, "y1": 0, "x2": 10, "y2": 10, "label": "L", "color": "blue"}

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_highlight_default_color_omitted(self, mock_cmd):
        runner.invoke(app, ["highlight", "0", "0", "1", "1"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert "color" not in params  # default 'red' not forwarded

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_diff_baseline_and_threshold(self, mock_cmd):
        runner.invoke(app, ["diff", "/a.png", "--compare", "/b.png", "--threshold", "50"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"baseline": "/a.png", "threshold": 50, "compare": "/b.png"}

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_is_visible_routes_to_element_state(self, mock_cmd):
        runner.invoke(app, ["is", "visible", "@e3"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "element_state"
        assert params == {"selector": "@e3", "check": "visible"}

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_is_enabled_routes_to_element_state(self, mock_cmd):
        runner.invoke(app, ["is", "enabled", "@e5"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"selector": "@e5", "check": "enabled"}


# ── find-image command (template matching) ───────────────────────────


class TestFindImageCommand:
    @patch("agent_windows.cli.commands.observe._cmd")
    def test_find_image_forwards_method_and_params(self, mock_cmd):
        result = runner.invoke(app, ["find-image", "tpl.png",
                                     "--in", "src.png", "--bbox", "1,2,3,4",
                                     "--tolerance", "5", "--grayscale", "--all"])
        assert result.exit_code == 0
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "find_image"
        assert params == {"image": "tpl.png", "source": "src.png",
                          "bbox": [1, 2, 3, 4], "tolerance": 5,
                          "grayscale": True, "all": True}

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_find_image_omits_optionals_when_default(self, mock_cmd):
        """Bare `find-image tpl.png` sends only image + tolerance(=0)."""
        runner.invoke(app, ["find-image", "tpl.png"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"image": "tpl.png", "tolerance": 0}

    @patch("agent_windows.cli.commands.observe._cmd")
    def test_find_image_short_tolerance_flag(self, mock_cmd):
        """-t is the short alias for --tolerance."""
        runner.invoke(app, ["find-image", "tpl.png", "-t", "8"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["tolerance"] == 8


# ── system commands (app/fs/clipboard/process) ───────────────────────


class TestSystemCommands:
    @patch("agent_windows.cli.commands.system._cmd")
    def test_app_launch_passes_args(self, mock_cmd):
        runner.invoke(app, ["app", "launch", "notepad", "file.txt"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "app_launch"
        assert params == {"name_or_path": "notepad", "args": ["file.txt"]}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_app_launch_no_args_defaults_empty_list(self, mock_cmd):
        runner.invoke(app, ["app", "launch", "notepad"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["args"] == []

    @patch("agent_windows.cli.commands.system._cmd")
    def test_app_resize_required_width_height(self, mock_cmd):
        runner.invoke(app, ["app", "resize", "win", "--width", "800", "--height", "600"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"name_or_title": "win", "width": 800, "height": 600, "x": -1, "y": -1}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_app_resize_missing_width_errors(self, mock_cmd):
        """--width/--height are required options."""
        result = runner.invoke(app, ["app", "resize", "win", "--height", "600"])
        assert result.exit_code != 0
        mock_cmd.assert_not_called()

    @patch("agent_windows.cli.commands.system._cmd")
    def test_fs_read_write_list(self, mock_cmd):
        runner.invoke(app, ["fs", "read", "/p"])
        runner.invoke(app, ["fs", "write", "/p", "content"])
        runner.invoke(app, ["fs", "list", "/d"])
        calls = _cmd_calls(mock_cmd)
        assert calls[0] == ("fs_read", {"path": "/p"})
        assert calls[1] == ("fs_write", {"path": "/p", "content": "content"})
        assert calls[2] == ("fs_list", {"path": "/d"})

    @patch("agent_windows.cli.commands.system._cmd")
    def test_clipboard_get_keyboard_flag_only_when_set(self, mock_cmd):
        """keyboard=None (option omitted) → not forwarded; --keyboard → True."""
        runner.invoke(app, ["clipboard", "get"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {}
        runner.invoke(app, ["clipboard", "get", "--keyboard"])
        _, params = _cmd_calls(mock_cmd)[1]
        assert params == {"keyboard": True}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_clipboard_set(self, mock_cmd):
        runner.invoke(app, ["clipboard", "set", "hello", "-k"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"text": "hello", "keyboard": True}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_process_kill(self, mock_cmd):
        runner.invoke(app, ["process", "kill", "notepad"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"pid_or_name": "notepad"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_process_kill_force(self, mock_cmd):
        runner.invoke(app, ["process", "kill", "notepad", "--force"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"pid_or_name": "notepad", "force": True}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_process_list_defaults_empty(self, mock_cmd):
        """Bare `process list` sends no params (bare-call compatibility)."""
        runner.invoke(app, ["process", "list"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "process_list"
        assert params == {}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_process_list_filters(self, mock_cmd):
        runner.invoke(app, ["process", "list", "--sort-by", "cpu",
                            "--limit", "10", "--name", "edge"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "process_list"
        assert params == {"sort_by": "cpu", "limit": 10, "name": "edge"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_notify_forwards_title_message(self, mock_cmd):
        runner.invoke(app, ["notify", "Hello", "World"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "notify"
        assert params == {"title": "Hello", "message": "World"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_notify_with_app_id(self, mock_cmd):
        runner.invoke(app, ["notify", "Hello", "World", "--app-id", "my.app"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"title": "Hello", "message": "World", "app_id": "my.app"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_registry_get(self, mock_cmd):
        runner.invoke(app, ["registry", "get", r"HKCU:\Soft", "Name"])
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "registry"
        assert params == {"mode": "get", "path": r"HKCU:\Soft", "name": "Name"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_registry_set_default_type(self, mock_cmd):
        runner.invoke(app, ["registry", "set", r"HKCU:\Soft", "Name", "val"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"mode": "set", "path": r"HKCU:\Soft",
                          "name": "Name", "value": "val", "type": "String"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_registry_set_custom_type(self, mock_cmd):
        runner.invoke(app, ["registry", "set", r"HKCU:\Soft", "Count", "42", "--type", "DWord"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["type"] == "DWord"
        assert params["value"] == "42"

    @patch("agent_windows.cli.commands.system._cmd")
    def test_registry_delete_with_name(self, mock_cmd):
        runner.invoke(app, ["registry", "delete", r"HKCU:\Soft", "Name"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"mode": "delete", "path": r"HKCU:\Soft", "name": "Name"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_registry_delete_without_name(self, mock_cmd):
        runner.invoke(app, ["registry", "delete", r"HKCU:\Soft"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"mode": "delete", "path": r"HKCU:\Soft"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_registry_list(self, mock_cmd):
        runner.invoke(app, ["registry", "list", r"HKCU:\Soft"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"mode": "list", "path": r"HKCU:\Soft"}

    @patch("agent_windows.cli.commands.system._cmd")
    def test_registry_set_multistring_parses_json(self, mock_cmd):
        """BUG-A fix: --type MultiString JSON-parses the value into a list
        before forwarding (the CLI boundary where string → typed value)."""
        runner.invoke(app, ["registry", "set", r"HKCU:\Soft", "Name",
                            '["a","b"]', "--type", "MultiString"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["value"] == ["a", "b"]
        assert params["type"] == "MultiString"

    def test_registry_set_multistring_rejects_non_array(self):
        result = runner.invoke(app, ["registry", "set", r"HKCU:\Soft", "Name",
                                     '"notarray"', "--type", "MultiString"])
        assert result.exit_code != 0

    @patch("agent_windows.cli.commands.system._cmd")
    def test_registry_delete_empty_name_targets_default_value(self, mock_cmd):
        """BUG-B fix: an explicit empty NAME targets the unnamed default value,
        not a whole-key delete. NAME omitted → whole key."""
        runner.invoke(app, ["registry", "delete", r"HKCU:\Soft", ""])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params == {"mode": "delete", "path": r"HKCU:\Soft", "name": ""}


# ── batch command: JSON source resolution + error paths ─────────────


class TestBatchCommand:
    @patch("agent_windows.cli.commands.system._cmd")
    def test_batch_from_argument(self, mock_cmd):
        cmds = [{"method": "click", "params": {"x": 1, "y": 2}}]
        result = runner.invoke(app, ["batch", json.dumps(cmds)])
        assert result.exit_code == 0
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "batch"
        assert params["commands"] == cmds
        assert params["stop_on_error"] is True
        assert params["delay"] == 0.0

    @patch("agent_windows.cli.commands.system._cmd")
    def test_batch_from_file(self, mock_cmd, tmp_path):
        cmds = [{"method": "screenshot"}]
        f = tmp_path / "cmds.json"
        f.write_text(json.dumps(cmds), encoding="utf-8")
        result = runner.invoke(app, ["batch", "--file", str(f), "--delay", "0.5", "--continue-on-error"])
        assert result.exit_code == 0
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["commands"] == cmds
        assert params["delay"] == 0.5
        assert params["stop_on_error"] is False

    @patch("agent_windows.cli.commands.system._cmd")
    def test_batch_from_stdin(self, mock_cmd):
        cmds = [{"method": "ping"}]
        result = runner.invoke(app, ["batch", "-"], input=json.dumps(cmds))
        assert result.exit_code == 0
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["commands"] == cmds

    def test_batch_no_source_errors(self):
        """No argument, no --file, no stdin → Exit(1) with a clear message."""
        result = runner.invoke(app, ["batch"])
        assert result.exit_code == 1
        assert "provide commands" in result.output

    def test_batch_invalid_json_errors(self):
        result = runner.invoke(app, ["batch", "not json"])
        assert result.exit_code == 1
        assert "invalid JSON" in result.output

    def test_batch_non_array_errors(self):
        """A valid JSON object (not an array) is rejected."""
        result = runner.invoke(app, ["batch", json.dumps({"method": "click"})])
        assert result.exit_code == 1
        assert "non-empty JSON array" in result.output

    def test_batch_empty_array_errors(self):
        result = runner.invoke(app, ["batch", "[]"])
        assert result.exit_code == 1
        assert "non-empty JSON array" in result.output

    @patch("agent_windows.cli.commands.system._cmd")
    def test_batch_file_takes_precedence_over_argument(self, mock_cmd, tmp_path):
        """--file wins over a positional argument."""
        f = tmp_path / "f.json"
        f.write_text(json.dumps([{"method": "from_file"}]), encoding="utf-8")
        runner.invoke(app, ["batch", json.dumps([{"method": "from_arg"}]), "--file", str(f)])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["commands"] == [{"method": "from_file"}]


# ── wait command: mode resolution + error paths ──────────────────────


class TestWaitCommand:
    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_plain_seconds_does_local_sleep(self, mock_print, mock_time):
        """A numeric target → local time.sleep, no daemon round-trip, then print."""
        result = runner.invoke(app, ["wait", "3"])
        assert result.exit_code == 0
        mock_time.sleep.assert_called_once_with(3.0)
        mock_print.assert_called_once()
        out = mock_print.call_args.args[0]["output"]
        assert "Slept 3" in out

    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_plain_seconds_zero_or_negative_errors(self, mock_print, mock_time):
        result = runner.invoke(app, ["wait", "0"])
        assert result.exit_code == 1
        assert "positive number" in result.output
        mock_time.sleep.assert_not_called()
        mock_print.assert_not_called()

    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_invalid_target_errors(self, mock_print, mock_time):
        """Neither @e, a number, nor --text → clear error."""
        result = runner.invoke(app, ["wait", "notanumber"])
        assert result.exit_code == 1
        assert "invalid target" in result.output

    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_text_and_target_mutually_exclusive(self, mock_print, mock_time):
        result = runner.invoke(app, ["wait", "@e3", "--text", "done"])
        assert result.exit_code == 1
        assert "cannot specify both" in result.output

    @patch("agent_windows.cli.commands.wait.dispatch", return_value={"found": True, "element": "Btn"})
    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_selector_mode_polls_until_found(self, mock_print, mock_time, mock_dispatch):
        """@e selector → one wait_check dispatch with {selector:...}, then print."""
        # time.time must return real floats so the f-string formatting works.
        mock_time.time.return_value = 1.0
        result = runner.invoke(app, ["wait", "@e3"])
        assert result.exit_code == 0
        mock_dispatch.assert_called_once()
        sent_params = mock_dispatch.call_args.args[1]
        assert sent_params == {"selector": "@e3"}
        out = mock_print.call_args.args[0]
        assert "@e3" in out["output"]
        assert out["selector"] == "@e3"

    @patch("agent_windows.cli.commands.wait.dispatch", return_value={"found": False})
    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_selector_timeout_errors(self, mock_print, mock_time, mock_dispatch):
        """Element never appears within --timeout → Exit(1) with message."""
        # time.time() progression: start, then each loop iteration past timeout.
        t = {"v": 0.0}
        mock_time.time.side_effect = [0.0, 100.0]  # start=0, then 100 (>timeout)
        result = runner.invoke(app, ["wait", "@e3", "--timeout", "5"])
        assert result.exit_code == 1
        assert "not found within 5" in result.output
        # the probe was actually dispatched (not just short-circuited to error)
        mock_dispatch.assert_called()

    @patch("agent_windows.utils.config.load_config",
           return_value={"wait": {"poll_interval": 0.25}})
    @patch("agent_windows.cli.commands.wait.dispatch",
           side_effect=[{"found": False}, {"found": True, "element": "X"}])
    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_poll_interval_defaults_from_config(self, mock_print, mock_time, mock_dispatch, mock_cfg):
        """When --interval is 0, poll_interval comes from config['wait']['poll_interval']
        and that value is what the loop sleeps with between probes."""
        # Two probes: start, then within-timeout for the 2nd.
        mock_time.time.side_effect = [0.0, 0.0, 0.0, 0.0]
        result = runner.invoke(app, ["wait", "@e3", "--timeout", "5"])
        assert result.exit_code == 0
        assert mock_dispatch.call_count == 2
        # the config poll_interval (0.25) reached time.sleep between the two probes
        assert any(c.args == (0.25,) for c in mock_time.sleep.call_args_list)

    @patch("agent_windows.cli.commands.wait.dispatch", return_value={"found": True, "matched_element": "X"})
    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_text_mode(self, mock_print, mock_time, mock_dispatch):
        mock_time.time.return_value = 1.0
        result = runner.invoke(app, ["wait", "--text", "done", "--timeout", "5"])
        assert result.exit_code == 0
        assert mock_dispatch.call_args.args[1] == {"text": "done"}


# ── config command: API-key masking ──────────────────────────────────


class TestConfigCommand:
    def test_show_masks_long_api_key(self):
        with patch("agent_windows.cli.commands.config_cmd.load_config",
                   return_value={"vlm": {"api_key": "sk-1234567890abcdef"}}):
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        # first 4 + "..." + last 4 (tail slice computation is part of the contract)
        assert "sk-1" in result.output
        assert "cdef" in result.output
        assert "1234567890abcdef" not in result.output  # full key not leaked

    def test_show_masks_short_api_key(self):
        with patch("agent_windows.cli.commands.config_cmd.load_config",
                   return_value={"vlm": {"api_key": "short"}}):
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "***" in result.output
        assert "short" not in result.output

    def test_show_no_api_key_no_mask(self):
        with patch("agent_windows.cli.commands.config_cmd.load_config",
                   return_value={"vlm": {"model": "x"}}):
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0

    def test_show_masks_oauth_client_secret(self):
        """oauth_client_secret is the same secret-class as the api key and must
        be masked too — otherwise `config show` leaks it to the terminal /
        screen captures."""
        with patch("agent_windows.cli.commands.config_cmd.load_config",
                   return_value={"security": {"oauth_client_secret": "super-secret-12345678"}}):
            result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0
        assert "super-secret-12345678" not in result.output  # full secret not leaked
        assert "***" in result.output or ("supe" in result.output and "5678" in result.output)

    @patch("agent_windows.cli.commands.config_cmd.set_config_value")
    def test_set_calls_set_config_value(self, mock_set):
        result = runner.invoke(app, ["config", "set", "vlm.model", "gpt-4o"])
        assert result.exit_code == 0
        mock_set.assert_called_once_with("vlm.model", "gpt-4o")
        assert "vlm.model = gpt-4o" in result.output

    def test_doctor_reports_config_errors(self, tmp_path, monkeypatch):
        """config doctor reports type errors and exits 1 before the vlm probe
        (a mistyped value would make load_config raise)."""
        import agent_windows.utils.config as cfg
        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None
        cfg_path.write_text('[mcp]\nport = true\n', encoding="utf-8")
        result = runner.invoke(app, ["config", "doctor"])
        assert result.exit_code == 1
        assert "mcp.port" in result.output
        assert "validation errors" in result.output.lower()

    def test_doctor_runs_when_valid(self, tmp_path, monkeypatch):
        """With a valid (or absent) config, doctor proceeds to the vlm probe."""
        import agent_windows.utils.config as cfg
        monkeypatch.setattr(cfg, "get_config_path", lambda: tmp_path / "nope.toml")
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None
        result = runner.invoke(app, ["config", "doctor"])
        assert result.exit_code == 0
        assert "Vision model configuration" in result.output


# ── daemon command: start/stop/status branches ───────────────────────


class TestDaemonCommand:
    # daemon_cmd lazy-imports subprocess / call_daemon / pipe_exists inside the
    # command functions, so patch at their source modules.
    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=False)
    @patch("subprocess.Popen")
    @patch("agent_windows.cli.commands.daemon_cmd.is_our_daemon", return_value=True)
    @patch("agent_windows.cli.commands.daemon_cmd.read_pid_file", return_value=(1234, 0.0))
    def test_start_already_running(self, mock_pid, mock_alive, mock_popen, mock_pipe):
        # pipe_exists stubbed False so the PID-file branch (which prints the
        # mocked PID 1234) is reached regardless of a real daemon on the host.
        result = runner.invoke(app, ["daemon", "start"])
        assert result.exit_code == 0
        assert "already running" in result.output
        assert "1234" in result.output
        mock_popen.assert_not_called()

    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=False)
    @patch("subprocess.Popen")
    @patch("agent_windows.cli.commands.daemon_cmd.is_our_daemon", return_value=False)
    @patch("agent_windows.cli.commands.daemon_cmd.read_pid_file", return_value=None)
    def test_start_spawns_when_not_running(self, mock_pid, mock_alive, mock_popen, mock_pipe):
        mock_popen.return_value = MagicMock(pid=5678)
        result = runner.invoke(app, ["daemon", "start"])
        assert result.exit_code == 0
        assert "5678" in result.output
        mock_popen.assert_called_once()

    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=False)
    @patch("subprocess.Popen")
    @patch("agent_windows.cli.commands.daemon_cmd.is_our_daemon", return_value=False)
    @patch("agent_windows.cli.commands.daemon_cmd.read_pid_file", return_value=None)
    def test_start_propagates_config_override(self, mock_pid, mock_alive, mock_popen, mock_pipe, tmp_path):
        # `daemon start --config X` must spawn the daemon with --config so it
        # reads the same file (TODO #21-1, review fix: this Python spawn path
        # previously omitted --config).
        mock_popen.return_value = MagicMock(pid=1)
        alt = tmp_path / "alt.toml"
        alt.write_text('[shell]\ntimeout = 5\n', encoding="utf-8")
        try:
            result = runner.invoke(app, ["--config", str(alt), "daemon", "start"])
            assert result.exit_code == 0, result.output
            cmd = mock_popen.call_args.args[0]
            assert "--config" in cmd
            assert cmd[cmd.index("--config") + 1] == str(alt)
        finally:
            from agent_windows.utils import config as cfg
            cfg.set_config_override(None)

    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=False)
    def test_stop_when_not_running(self, mock_pipe):
        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0
        assert "not running" in result.output

    @patch("agent_windows.ipc.client.call_daemon")
    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=True)
    def test_stop_sends_shutdown(self, mock_pipe, mock_call):
        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 0
        mock_call.assert_called_once_with("shutdown")
        assert "shutdown requested" in result.output

    @patch("agent_windows.ipc.client.call_daemon", side_effect=ConnectionError("pipe broke"))
    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=True)
    def test_stop_returns_nonzero_on_error(self, mock_pipe, mock_call):
        """A failed shutdown surfaces a non-zero exit (not the previous silent
        exit 0) so scripts/agents can detect the failure. The error is echoed
        to stderr."""
        result = runner.invoke(app, ["daemon", "stop"])
        assert result.exit_code == 1
        mock_call.assert_called_once_with("shutdown")
        assert "Error stopping daemon" in result.output

    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=True)
    @patch("agent_windows.cli.commands.daemon_cmd.is_our_daemon", return_value=True)
    @patch("agent_windows.cli.commands.daemon_cmd.read_pid_file", return_value=(1234, 0.0))
    @patch("agent_windows.cli.commands.daemon_cmd.read_version_file",
           return_value="0.1.0")  # matches agent_windows.__version__
    def test_status_running_matching_version(self, mock_ver, mock_pid, mock_alive, mock_pipe):
        result = runner.invoke(app, ["daemon", "status"])
        assert result.exit_code == 0
        assert result.exception is None
        assert "Status: running" in result.output
        # the recorded version matches the tool version → no mismatch warning
        assert "Version mismatch" not in result.output

    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=True)
    @patch("agent_windows.cli.commands.daemon_cmd.is_our_daemon", return_value=True)
    @patch("agent_windows.cli.commands.daemon_cmd.read_pid_file", return_value=(1234, 0.0))
    @patch("agent_windows.cli.commands.daemon_cmd.read_version_file", return_value="9.9.9")
    def test_status_version_mismatch_warns(self, mock_ver, mock_pid, mock_alive, mock_pipe):
        """A recorded daemon version that differs from the tool version surfaces
        the auto-restart mismatch warning."""
        result = runner.invoke(app, ["daemon", "status"])
        assert result.exit_code == 0
        assert "Version mismatch" in result.output
        assert "auto-restart" in result.output

    @patch("agent_windows.ipc.pipe.pipe_exists", return_value=False)
    @patch("agent_windows.cli.commands.daemon_cmd.is_our_daemon", return_value=False)
    @patch("agent_windows.cli.commands.daemon_cmd.read_pid_file", return_value=None)
    @patch("agent_windows.cli.commands.daemon_cmd.read_version_file", return_value=None)
    def test_status_stopped(self, mock_ver, mock_pid, mock_alive, mock_pipe):
        result = runner.invoke(app, ["daemon", "status"])
        assert result.exit_code == 0
        assert result.exception is None
        assert "Status: stopped" in result.output


# ── ping command: top-level daemon health check (C2) ──────────────────


class TestPingCommand:
    """`agent-windows ping` is a top-level command (the README and the native
    launcher expose it there). The Python fallback (agent-windows-python) was
    missing it entirely → "No such command 'ping'", exit 2. The fix registers
    it as top-level, dispatching to the daemon's policy-exempt ping handler."""

    @patch("agent_windows.cli.commands.daemon_cmd._cmd")
    def test_ping_dispatches_to_daemon_ping(self, mock_cmd):
        """`ping` calls _cmd(ctx, "ping") with no params."""
        result = runner.invoke(app, ["ping"])
        assert result.exit_code == 0
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "ping"
        assert params == {}

    @patch("agent_windows.cli.dispatcher.call_daemon",
           return_value={"status": "ok", "message": "pong"})
    def test_ping_renders_status_and_message(self, mock_call):
        """The daemon reply has no `output` key, so print_result renders the
        status/message fields directly (the no-output fallback path)."""
        result = runner.invoke(app, ["ping"])
        assert result.exit_code == 0
        assert "ok" in result.output
        assert "pong" in result.output

    @patch("agent_windows.cli.dispatcher.call_daemon",
           return_value={"status": "ok", "message": "pong"})
    def test_ping_format_json_dumps_full_dict(self, mock_call):
        """--format json (a global flag, before the command) dumps the whole
        reply dict, not just a rendered line."""
        result = runner.invoke(app, ["--format", "json", "ping"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed == {"status": "ok", "message": "pong"}

    @patch("agent_windows.cli.dispatcher.call_daemon",
           side_effect=ConnectionError("daemon unreachable"))
    def test_ping_daemon_down_exits_nonzero(self, mock_call):
        """A daemon-down connection error surfaces a clear message and a
        non-zero exit (2 — infrastructure failure), so a script/agent health
        check can distinguish 'daemon down' from success."""
        result = runner.invoke(app, ["ping"])
        assert result.exit_code == 2
        assert "daemon unreachable" in result.output

    @patch("agent_windows.ipc.client.call_daemon",
           return_value={"status": "ok", "message": "pong"})
    def test_ping_no_daemon_runs_in_process(self, mock_call):
        """--no-daemon bypasses call_daemon and runs the ping handler in-process
        via route_request — so call_daemon is NOT invoked, yet the reply still
        renders. (The daemon handler is policy-exempt, so this works without
        any tool authorization.)"""
        result = runner.invoke(app, ["--no-daemon", "ping"])
        assert result.exit_code == 0
        assert "pong" in result.output
        mock_call.assert_not_called()  # ran in-process, not via IPC

    def test_ping_listed_in_top_level_help(self):
        """`ping` must appear in the root command list (not buried under
        `daemon ping`), matching the README and the launcher."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "ping" in result.output


# ── multi-select / multi-edit commands (TODO #12) ─────────────────────


class TestMultiSelectCommand:
    @patch("agent_windows.cli.commands.multi._cmd")
    def test_locs_from_argument(self, mock_cmd):
        locs = [[100, 200], [300, 400]]
        result = runner.invoke(app, ["multi-select", json.dumps(locs)])
        assert result.exit_code == 0
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "multi_select"
        assert params["locs"] == locs
        assert params["press_ctrl"] is True

    @patch("agent_windows.cli.commands.multi._cmd")
    def test_no_ctrl_flag(self, mock_cmd):
        runner.invoke(app, ["multi-select", "[[1,2]]", "--no-ctrl"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["press_ctrl"] is False

    @patch("agent_windows.cli.commands.multi._cmd")
    def test_selectors_flag_sends_selectors_not_locs(self, mock_cmd):
        runner.invoke(app, ["multi-select", '["@e1","@e3"]', "--selectors"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["selectors"] == ["@e1", "@e3"]
        assert "locs" not in params

    @patch("agent_windows.cli.commands.multi._cmd")
    def test_from_file(self, mock_cmd, tmp_path):
        locs = [[5, 6]]
        f = tmp_path / "t.json"
        f.write_text(json.dumps(locs), encoding="utf-8")
        result = runner.invoke(app, ["multi-select", "--file", str(f)])
        assert result.exit_code == 0
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["locs"] == locs

    def test_no_source_errors(self):
        result = runner.invoke(app, ["multi-select"])
        assert result.exit_code == 1
        assert "provide JSON" in result.output

    def test_invalid_json_errors(self):
        result = runner.invoke(app, ["multi-select", "not json"])
        assert result.exit_code == 1
        assert "invalid JSON" in result.output


class TestMultiEditCommand:
    @patch("agent_windows.cli.commands.multi._cmd")
    def test_locs_from_argument(self, mock_cmd):
        locs = [[100, 200, "Alice"], [300, 400, "Bob"]]
        result = runner.invoke(app, ["multi-edit", json.dumps(locs)])
        assert result.exit_code == 0
        method, params = _cmd_calls(mock_cmd)[0]
        assert method == "multi_edit"
        assert params["locs"] == locs

    @patch("agent_windows.cli.commands.multi._cmd")
    def test_selectors_flag(self, mock_cmd):
        runner.invoke(app, ["multi-edit", '[["@e1","Alice"]]', "--selectors"])
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["selectors"] == [["@e1", "Alice"]]
        assert "locs" not in params

    @patch("agent_windows.cli.commands.multi._cmd")
    def test_from_stdin(self, mock_cmd):
        locs = [[1, 2, "x"]]
        result = runner.invoke(app, ["multi-edit", "-"], input=json.dumps(locs))
        assert result.exit_code == 0
        _, params = _cmd_calls(mock_cmd)[0]
        assert params["locs"] == locs

    def test_non_array_errors(self):
        result = runner.invoke(app, ["multi-edit", json.dumps({"a": 1})])
        assert result.exit_code == 1
        assert "must be an array" in result.output


class TestConfigGlobalFlag:
    """The --config global flag sets the override for in-process config reads."""

    def teardown_method(self, _method):
        from agent_windows.utils import config as cfg
        cfg.set_config_override(None)

    def test_config_flag_sets_override(self, tmp_path):
        # `config show` reads config via load_config() and prints the file path;
        # with --config pointing at an alternate file, that path must appear and
        # the override accessor must reflect it.
        from agent_windows.utils import config as cfg
        alt = tmp_path / "alt.toml"
        alt.write_text('[shell]\ntimeout = 77\n', encoding="utf-8")
        result = runner.invoke(app, ["--config", str(alt), "config", "show"])
        assert result.exit_code == 0, result.output
        assert cfg.get_config_override() == alt.resolve()
        # config show prints "# Config file: <path>" — confirms load_config()
        # read from the override, not the default.
        assert str(alt.resolve()) in result.output

    def test_no_config_flag_leaves_default(self):
        from agent_windows.utils import config as cfg
        cfg.set_config_override(None)
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.output
        assert cfg.get_config_override() is None
