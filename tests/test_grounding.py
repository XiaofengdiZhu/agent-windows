"""Tests for grounding: coordinate conversion utilities and the two-pass /
single-pass locate strategy (H-07)."""

import pytest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from agent_windows.utils.coordinates import (
    prepare_model_input,
    model_to_physical,
    physical_to_model,
    crop_around_point,
    parse_model_coordinates,
)


class TestPrepareModelInput:
    def test_preserves_aspect_ratio(self):
        img = Image.new("RGB", (1920, 1080))
        result, *_ = prepare_model_input(img, 1000, 1000)
        assert result.size == (1000, 1000)

    def test_square_image_fills_model_space(self):
        img = Image.new("RGB", (100, 100))
        result, *_ = prepare_model_input(img, 1000, 1000)
        # Should fill completely
        assert result.size == (1000, 1000)


class TestModelToPhysicalRoundTrip:
    # physical_to_model has no production caller — it exists as the symmetric
    # inverse of model_to_physical and is used HERE to verify that inverse via
    # round-trip (physical→model→physical ≈ identity). See coordinates.py. (QUAL3-06)
    def test_center_roundtrip(self):
        """Center of image should round-trip accurately."""
        src_w, src_h = 1920, 1080
        model_w, model_h = 1000, 1000

        # Physical center → model → physical
        px, py = src_w // 2, src_h // 2
        mx, my = physical_to_model(px, py, model_w, model_h, src_w, src_h)
        rx, ry = model_to_physical(mx, my, model_w, model_h, src_w, src_h)
        # Allow 1-2 pixel tolerance due to integer rounding
        assert abs(rx - px) <= 2
        assert abs(ry - py) <= 2

    def test_origin_roundtrip(self):
        src_w, src_h = 1920, 1080
        model_w, model_h = 1000, 1000
        mx, my = physical_to_model(0, 0, model_w, model_h, src_w, src_h)
        rx, ry = model_to_physical(mx, my, model_w, model_h, src_w, src_h)
        assert rx == 0
        assert ry == 0


class TestCropAroundPoint:
    def test_center_crop(self):
        img = Image.new("RGB", (1920, 1080))
        cropped, cx, cy = crop_around_point(img, 960, 540, 400, 300)
        assert cropped.size == (400, 300)
        assert cx == 960 - 200
        assert cy == 540 - 150

    def test_edge_clamp(self):
        img = Image.new("RGB", (1920, 1080))
        cropped, cx, cy = crop_around_point(img, 10, 10, 400, 300)
        assert cx == 0  # Clamped to left edge
        assert cy == 0  # Clamped to top edge

    def test_crop_larger_than_image(self):
        img = Image.new("RGB", (200, 100))
        cropped, cx, cy = crop_around_point(img, 100, 50, 400, 300)
        assert cropped.size == (200, 100)  # Clamped to image size


class TestParseModelCoordinates:
    def test_point_marker(self):
        # The format the prompts now request. Unambiguous — must match even
        # when prose contains other numbers.
        assert parse_model_coordinates("POINT:500,600") == (500, 600)

    def test_point_marker_ignores_prose_numbers(self):
        assert parse_model_coordinates("I see Section 3, Row 4. POINT:500,600") == (500, 600)

    def test_parenthesized_format(self):
        assert parse_model_coordinates("(320, 240)") == (320, 240)

    def test_parenthesized_beats_prose_pair(self):
        # No marker, but the parens mark the coordinate explicitly; the
        # incidental "Section 3, Row 4" pair must not win.
        assert parse_model_coordinates("Section 3, Row 4 at (320, 240)") == (320, 240)

    def test_last_pair_beats_earlier_incidental_pair(self):
        # Regression for the original bug: a prose pair ("Section 3, Row 4")
        # appearing BEFORE the real coordinate used to be grabbed. Now the LAST
        # pair wins (models put the answer last).
        assert parse_model_coordinates("Section 3, Row 4, point 500, 600") == (500, 600)

    def test_no_parens_single_pair(self):
        assert parse_model_coordinates("320, 240") == (320, 240)

    def test_chinese_comma(self):
        assert parse_model_coordinates("(320，240)") == (320, 240)

    def test_json_form(self):
        assert parse_model_coordinates('{"x": 500, "y": 600}') == (500, 600)

    def test_json_form_reversed_keys(self):
        """A model may emit {"y":..,"x":..} (keys out of order). x/y must be
        resolved by KEY, not capture position — otherwise the coordinate is
        silently swapped to a reflected, wrong-but-valid point."""
        assert parse_model_coordinates('{"y": 600, "x": 500}') == (500, 600)

    def test_float_coordinates_truncated(self):
        """A vision model may emit decimal coordinates (500.5). They must
        truncate to int via float→int, not mis-parse the fractional digits as a
        separate number (which previously gave (500, 5) for '500.5')."""
        assert parse_model_coordinates('{"x": 500.5, "y": 600}') == (500, 600)
        assert parse_model_coordinates("POINT:500.5,600.7") == (500, 600)
        assert parse_model_coordinates("(500.5, 600)") == (500, 600)

    def test_multiple_markers_takes_last(self):
        """A model that reasons aloud may emit several POINT markers; the LAST
        is its final answer (same rationale as the last-pair fallback). Taking
        the first would return a discarded guess."""
        assert parse_model_coordinates(
            "First guess POINT:100,200 — actually POINT:500,600"
        ) == (500, 600)

    def test_fallback_first_two_numbers(self):
        # No separators at all → first two integers.
        assert parse_model_coordinates("the point is 100 200") == (100, 200)

    def test_no_numbers_returns_none(self):
        assert parse_model_coordinates("no coordinates here") is None

    def test_one_number_returns_none(self):
        assert parse_model_coordinates("42") is None

    def test_extra_numbers_parens_win(self):
        # Parenthesized coordinate is preferred over incidental prose numbers.
        assert parse_model_coordinates("width=50, point (320, 240)") == (320, 240)


class TestSinglePassGrounding:
    """H-07: single_pass mode does ONE vision API call (skips the fine pass)."""

    def _patches(self, single_pass_config=False):
        """Patch the network/disk calls and config so tests are hermetic."""
        return (
            patch(
                "agent_windows.core.grounding.load_config",
                return_value={"vlm": {"enabled": True, "single_pass": single_pass_config}},
            ),
            patch(
                "agent_windows.core.grounding.call_vision_model",
                return_value="(500, 500)",
            ),
            patch(
                "agent_windows.core.grounding._save_marked_screenshot",
                return_value=Path("/tmp/grounding-test.png"),
            ),
        )

    def test_single_pass_param_makes_one_api_call(self):
        """single_pass=True does only the coarse call — the H-07 latency win."""
        from agent_windows.core.grounding import ground_element

        cfg_patch, call_patch, save_patch = self._patches(single_pass_config=False)
        with cfg_patch, call_patch as mock_call, save_patch:
            result = ground_element("a button", screenshot=Image.new("RGB", (1920, 1080)), single_pass=True)
        assert mock_call.call_count == 1, "single-pass must skip the fine API call"
        assert result["method"] == "visual-single-pass"

    def test_two_pass_default_makes_two_api_calls(self):
        """No override + config single_pass=False keeps the original two-pass path
        — the branch didn't regress the default behavior."""
        from agent_windows.core.grounding import ground_element

        cfg_patch, call_patch, save_patch = self._patches(single_pass_config=False)
        with cfg_patch, call_patch as mock_call, save_patch:
            ground_element("a button", screenshot=Image.new("RGB", (1920, 1080)))
        assert mock_call.call_count == 2

    def test_single_pass_defaults_from_config(self):
        """single_pass=None reads vlm.single_pass from config (the
        'configurable default' requirement)."""
        from agent_windows.core.grounding import ground_element

        cfg_patch, call_patch, save_patch = self._patches(single_pass_config=True)
        with cfg_patch, call_patch as mock_call, save_patch:
            result = ground_element("a button", screenshot=Image.new("RGB", (1920, 1080)))
        assert mock_call.call_count == 1
        assert result["method"] == "visual-single-pass"
