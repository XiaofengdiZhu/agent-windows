"""Tests for input-coordinate and param validation (SEC3-02, SEC3-06).

Locks the new validation paths:
- coordinates must be numeric and within the virtual screen → COORDINATE_OUT_OF_RANGE
- app_launch args must be a list[str] → INVALID_PARAMS
- app_resize width/height must be positive ints → INVALID_PARAMS

Also verifies route_request maps CoordinateRangeError to COORDINATE_OUT_OF_RANGE
(-1002), activating the previously-dead error code (QUAL3-05).
"""
import pytest
from unittest.mock import patch

from agent_windows.daemon.router import HANDLERS, route_request, CoordinateRangeError
from agent_windows.ipc.protocol import JSONRPCRequest, COORDINATE_OUT_OF_RANGE, INVALID_PARAMS


# Realistic virtual-screen rect used by the mocked validator.
_VSCREEN = (0, 0, 1920, 1080)


@pytest.fixture(autouse=True)
def _mock_vscreen(monkeypatch):
    """Pin the virtual screen to 1920x1080 so bounds checks are deterministic."""
    monkeypatch.setattr(
        "agent_windows.core.input_controller._get_virtual_screen",
        lambda: _VSCREEN,
    )


# ── coordinate validation via handlers ────────────────────────────────


class TestCoordinateValidation:
    def test_non_numeric_x_rejected(self):
        with pytest.raises(CoordinateRangeError, match="must be a number"):
            HANDLERS["click"]({"x": "abc", "y": 10})

    def test_non_numeric_y_rejected(self):
        with pytest.raises(CoordinateRangeError, match="must be a number"):
            HANDLERS["move"]({"x": 10, "y": [1, 2]})

    def test_bool_rejected_as_coordinate(self):
        """bool is an int subclass but must not slip through as 0/1 silently."""
        with pytest.raises(CoordinateRangeError):
            HANDLERS["click"]({"x": True, "y": 5})

    def test_far_out_of_bounds_rejected(self):
        with pytest.raises(CoordinateRangeError, match="outside the virtual screen"):
            HANDLERS["click"]({"x": 99999, "y": 99999})

    def test_negative_far_out_of_bounds_rejected(self):
        with pytest.raises(CoordinateRangeError, match="outside the virtual screen"):
            HANDLERS["move"]({"x": -5000, "y": 10})

    def test_in_bounds_passes_and_coerces_float(self):
        """A float in-range is truncated to int and forwarded (no error)."""
        with patch("agent_windows.core.input_controller.click") as mock_click:
            HANDLERS["click"]({"x": 100.7, "y": 200.3})
        mock_click.assert_called_once()
        assert mock_click.call_args.args[0] == 100  # truncated
        assert mock_click.call_args.args[1] == 200

    def test_edge_pixel_within_grace_passes(self):
        """The exact screen edge (1920) is within the 1px grace — accepted."""
        with patch("agent_windows.core.input_controller.move_to"):
            HANDLERS["move"]({"x": 1920, "y": 1080})  # must not raise

    @pytest.mark.parametrize("handler", ["click", "dblclick", "hover", "mouse_down", "mouse_up", "scroll", "move"])
    def test_every_mouse_handler_validates(self, handler):
        """Each coordinate-taking handler rejects an out-of-bounds point."""
        with pytest.raises(CoordinateRangeError):
            HANDLERS[handler]({"x": 50000, "y": 50000})

    def test_drag_validates_both_endpoints(self):
        with pytest.raises(CoordinateRangeError):
            HANDLERS["drag"]({"x1": 10, "y1": 10, "x2": 99999, "y2": 10})

    def test_type_at_coords_validated(self):
        with pytest.raises(CoordinateRangeError):
            HANDLERS["type"]({"text": "hi", "at_x": "no", "at_y": 5})


# ── multi-monitor / negative virtual-screen origin ────────────────────


class TestNegativeVirtualScreen:
    """A secondary monitor arranged to the left yields a negative virtual-screen
    left/top. The bounds grace must extend on the negative side and still reject
    wildly-off-screen values."""

    def test_negative_origin_coord_passes(self, monkeypatch):
        # Secondary monitor to the left: virtual screen spans -1920..1920
        monkeypatch.setattr(
            "agent_windows.core.input_controller._get_virtual_screen",
            lambda: (-1920, 0, 3840, 1080),
        )
        with patch("agent_windows.core.input_controller.move_to"):
            HANDLERS["move"]({"x": -100, "y": 50})  # on the left monitor — must pass

    def test_negative_origin_far_offscreen_rejected(self, monkeypatch):
        monkeypatch.setattr(
            "agent_windows.core.input_controller._get_virtual_screen",
            lambda: (-1920, 0, 3840, 1080),
        )
        with pytest.raises(CoordinateRangeError):
            HANDLERS["move"]({"x": -5000, "y": 50})  # well past the left edge


# ── route_request mapping → COORDINATE_OUT_OF_RANGE ──────────────────


class TestErrorCodeMapping:
    def test_coordinate_range_maps_to_specific_code(self):
        """A CoordinateRangeError surfaces as COORDINATE_OUT_OF_RANGE (-1002),
        not the generic INVALID_PARAMS — activating the previously-dead code."""
        resp = route_request(JSONRPCRequest(id=1, method="click", params={"x": 99999, "y": 99999}))
        assert resp.error["code"] == COORDINATE_OUT_OF_RANGE

    def test_missing_param_still_invalid_params(self):
        """A plain missing-param ValueError stays INVALID_PARAMS (unchanged)."""
        resp = route_request(JSONRPCRequest(id=1, method="click", params={"y": 5}))
        assert resp.error["code"] == INVALID_PARAMS


# ── app_launch args validation (SEC3-06) ──────────────────────────────


class TestAppLaunchArgs:
    def test_none_args_passes(self):
        with patch("agent_windows.core.app_manager.launch_app", return_value={"output": "ok"}) as m:
            HANDLERS["app_launch"]({"name_or_path": "notepad"})
        m.assert_called_once_with("notepad", None)

    def test_str_list_args_passes(self):
        with patch("agent_windows.core.app_manager.launch_app", return_value={"output": "ok"}) as m:
            HANDLERS["app_launch"]({"name_or_path": "app", "args": ["-v", "--foo"]})
        m.assert_called_once_with("app", ["-v", "--foo"])

    def test_non_list_args_rejected(self):
        with pytest.raises(ValueError, match="list of strings"):
            HANDLERS["app_launch"]({"name_or_path": "app", "args": "-v"})

    def test_list_with_non_string_rejected(self):
        with pytest.raises(ValueError, match="list of strings"):
            HANDLERS["app_launch"]({"name_or_path": "app", "args": ["-v", 123]})


# ── app_resize width/height validation ────────────────────────────────


class TestAppResizeValidation:
    def test_positive_ints_pass(self):
        with patch("agent_windows.core.app_manager.resize_window", return_value={"output": "ok"}) as m:
            HANDLERS["app_resize"]({"name_or_title": "w", "width": 800, "height": 600})
        m.assert_called_once_with("w", 800, 600, -1, -1)

    def test_zero_width_rejected(self):
        with pytest.raises(ValueError, match="positive integer"):
            HANDLERS["app_resize"]({"name_or_title": "w", "width": 0, "height": 600})

    def test_negative_height_rejected(self):
        with pytest.raises(ValueError, match="positive integer"):
            HANDLERS["app_resize"]({"name_or_title": "w", "width": 800, "height": -1})

    def test_non_int_width_rejected(self):
        with pytest.raises(ValueError, match="must be int"):
            HANDLERS["app_resize"]({"name_or_title": "w", "width": "big", "height": 600})

    def test_bool_width_rejected(self):
        """bool is an int subclass; width=True must not pass as 1."""
        with pytest.raises(ValueError, match="positive integer"):
            HANDLERS["app_resize"]({"name_or_title": "w", "width": True, "height": 600})


# ── type text length cap (SEC3-05) ────────────────────────────────────


class TestTypeTextLength:
    def test_text_under_cap_passes(self):
        from agent_windows.daemon.handlers import input as input_mod
        with patch("agent_windows.core.input_controller.type_text"):
            HANDLERS["type"]({"text": "x" * input_mod.MAX_TYPE_TEXT_LEN})

    def test_text_over_cap_rejected(self):
        """type_text sleeps per char (~0.02s); an oversized string would block the
        single STA worker for hours. Reject it instead. (SEC3-05)"""
        from agent_windows.daemon.handlers import input as input_mod
        with pytest.raises(ValueError, match="exceeds the"):
            HANDLERS["type"]({"text": "x" * (input_mod.MAX_TYPE_TEXT_LEN + 1)})


# ── key_down --hold cap (F11) ─────────────────────────────────────────


class TestKeyHoldRange:
    """key_down --hold sleeps for the duration on the single STA worker, so a
    runaway hold would tie the daemon up. Bounded by MAX_KEY_HOLD_SECONDS. (F11)"""

    def test_hold_at_cap_passes(self):
        """hold == MAX_KEY_HOLD_SECONDS (the inclusive upper bound) is accepted."""
        from agent_windows.daemon.handlers import input as input_mod
        with patch("agent_windows.core.input_controller.key_down") as m:
            HANDLERS["key_down"]({"keys": "a",
                                  "hold": input_mod.MAX_KEY_HOLD_SECONDS})
        assert m.call_args.kwargs["hold"] == float(input_mod.MAX_KEY_HOLD_SECONDS)

    def test_hold_just_over_cap_rejected(self):
        from agent_windows.daemon.handlers import input as input_mod
        with pytest.raises(ValueError, match="hold must be in"):
            HANDLERS["key_down"]({"keys": "a",
                                  "hold": input_mod.MAX_KEY_HOLD_SECONDS + 0.1})

    def test_hold_none_skips_validation(self):
        """No hold → no bound check, keys stay pressed (pair with key_up)."""
        with patch("agent_windows.core.input_controller.key_down") as m:
            HANDLERS["key_down"]({"keys": "a"})
        m.assert_called_once_with("a", hold=None)


# ── DoS caps read [input] from config (fallback to module constants) ──


class TestInputCapsConfigOverride:
    """type / key_down DoS caps read [input] from config, falling back to the
    module constants only when the section is absent."""

    def test_type_cap_reads_config(self):
        with patch("agent_windows.core.input_controller.type_text"), \
             patch("agent_windows.utils.config.load_config",
                   return_value={"input": {"type_text_len": 5}}):
            HANDLERS["type"]({"text": "x" * 5})  # at cap → ok
            with pytest.raises(ValueError, match="exceeds the"):
                HANDLERS["type"]({"text": "x" * 6})  # over → rejected

    def test_key_hold_cap_reads_config(self):
        with patch("agent_windows.core.input_controller.key_down"), \
             patch("agent_windows.utils.config.load_config",
                   return_value={"input": {"key_hold_seconds": 5}}):
            HANDLERS["key_down"]({"keys": "a", "hold": 5})  # at cap → ok
            with pytest.raises(ValueError, match="hold must be in"):
                HANDLERS["key_down"]({"keys": "a", "hold": 5.1})
