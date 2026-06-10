"""Tests for coordinate conversion utilities."""

import pytest
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
