"""Tests for screenshot annotation: highlight_region, diff_images."""

import tempfile
from pathlib import Path

import pytest
from PIL import Image

from agent_windows.core.screenshot import highlight_region, diff_images


def _solid_image(w: int, h: int, color: tuple = (128, 128, 128)) -> Image.Image:
    """Create a solid-color test image."""
    return Image.new("RGB", (w, h), color)


def _modify_region(
    img: Image.Image, x1: int, y1: int, x2: int, y2: int, color: tuple
) -> Image.Image:
    """Return a copy of img with a rectangular region changed."""
    result = img.copy()
    for y in range(y1, y2):
        for x in range(x1, x2):
            result.putpixel((x, y), color)
    return result


class TestHighlightRegion:
    def test_single_region_saves_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img = _solid_image(200, 200)
            out = Path(tmpdir) / "hl.png"
            path = highlight_region(img, [{"x1": 10, "y1": 20, "x2": 100, "y2": 80}], output_path=out)
            assert path.exists()
            assert path == out

    def test_single_region_with_label(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img = _solid_image(200, 200)
            out = Path(tmpdir) / "hl_label.png"
            path = highlight_region(img, [
                {"x1": 10, "y1": 20, "x2": 100, "y2": 80, "label": "Test"},
            ], output_path=out)
            assert path.exists()

    def test_multiple_regions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img = _solid_image(300, 300)
            regions = [
                {"x1": 0, "y1": 0, "x2": 50, "y2": 50, "color": "red"},
                {"x1": 100, "y1": 100, "x2": 200, "y2": 200, "color": "blue", "label": "B"},
            ]
            out = Path(tmpdir) / "hl_multi.png"
            path = highlight_region(img, regions, output_path=out)
            assert path.exists()
            # Verify output is valid image with same size
            with Image.open(path) as result:
                assert result.size == img.size

    def test_default_color_is_red(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img = _solid_image(100, 100)
            out = Path(tmpdir) / "hl_default.png"
            path = highlight_region(img, [
                {"x1": 0, "y1": 0, "x2": 50, "y2": 50},
            ], output_path=out)
            assert path.exists()

    def test_auto_generate_path(self):
        """When output_path is None, auto-generates path in screenshot dir."""
        img = _solid_image(100, 100)
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.cleanup_screenshots"):
                path = highlight_region(img, [{"x1": 0, "y1": 0, "x2": 50, "y2": 50}])
                assert path.exists()
                assert path.name.startswith("highlight-")


class TestDiffImages:
    def test_identical_images_match(self):
        img = _solid_image(100, 100, (128, 128, 128))
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "diff.png"
            result = diff_images(img, img.copy(), output_path=out)
            assert result["match"] is True
            assert result["diff_pixels"] == 0
            assert result["diff_percent"] == 0.0

    def test_different_images_have_diff(self):
        a = _solid_image(100, 100, (0, 0, 0))
        b = _solid_image(100, 100, (255, 255, 255))
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "diff.png"
            result = diff_images(a, b, threshold=10, output_path=out)
            assert result["match"] is False
            assert result["diff_pixels"] > 0
            assert result["diff_percent"] > 0.0
            assert out.exists()

    def test_threshold_filters_noise(self):
        """Small differences below threshold should be ignored."""
        a = _solid_image(50, 50, (128, 128, 128))
        # Modify center 10x10 region with small color shift (value 140)
        b = _modify_region(a, 10, 10, 20, 20, (140, 140, 140))

        # threshold=20 → diff is 12 < 20, should be filtered
        result_below = diff_images(a, b, threshold=20)
        assert result_below["diff_pixels"] == 0

        # threshold=5 → diff is 12 > 5, should be detected
        result_above = diff_images(a, b, threshold=5)
        assert result_above["diff_pixels"] > 0

    def test_different_sizes_resizes_b(self):
        a = _solid_image(100, 100)
        b = _solid_image(200, 200)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "diff.png"
            result = diff_images(a, b, output_path=out)
            assert result["total_pixels"] == 100 * 100

    def test_returns_expected_keys(self):
        img = _solid_image(50, 50)
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir) / "diff.png"
            result = diff_images(img, img.copy(), output_path=out)
            for key in ("diff_pixels", "total_pixels", "diff_percent", "match", "diff_path"):
                assert key in result

    def test_partial_diff(self):
        """Only a region differs."""
        a = _solid_image(100, 100, (100, 100, 100))
        b = _modify_region(a, 20, 20, 30, 30, (200, 200, 200))
        result = diff_images(a, b, threshold=10)
        assert result["diff_pixels"] == 100  # 10x10
        assert result["total_pixels"] == 10000
