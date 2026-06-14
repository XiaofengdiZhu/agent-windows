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
    def test_parenthesized_format(self):
        assert parse_model_coordinates("(320, 240)") == (320, 240)

    def test_no_parens(self):
        assert parse_model_coordinates("320, 240") == (320, 240)

    def test_chinese_comma(self):
        assert parse_model_coordinates("(320，240)") == (320, 240)

    def test_fallback_first_two_numbers(self):
        assert parse_model_coordinates("the point is 100 200") == (100, 200)

    def test_no_numbers_returns_none(self):
        assert parse_model_coordinates("no coordinates here") is None

    def test_one_number_returns_none(self):
        assert parse_model_coordinates("42") is None

    def test_extra_numbers(self):
        # Structured pattern should match first
        assert parse_model_coordinates("width=50, point (320, 240)") == (320, 240)


class TestSinglePassGrounding:
    """H-07: single_pass mode does ONE vision API call (skips the fine pass)."""

    def _patches(self, single_pass_config=False):
        """Patch the network/disk calls and config so tests are hermetic."""
        return (
            patch(
                "agent_windows.core.grounding.load_config",
                return_value={"grounding": {"enabled": True, "single_pass": single_pass_config}},
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
        """single_pass=None reads grounding.single_pass from config (the
        'configurable default' requirement)."""
        from agent_windows.core.grounding import ground_element

        cfg_patch, call_patch, save_patch = self._patches(single_pass_config=True)
        with cfg_patch, call_patch as mock_call, save_patch:
            result = ground_element("a button", screenshot=Image.new("RGB", (1920, 1080)))
        assert mock_call.call_count == 1
        assert result["method"] == "visual-single-pass"
