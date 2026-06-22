"""Tests for screenshot annotation: highlight_region, diff_images."""

import tempfile
from pathlib import Path
from unittest.mock import patch

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
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.cleanup_screenshots"):
                path = highlight_region(img, [{"x1": 0, "y1": 0, "x2": 50, "y2": 50}])
                assert path.exists()
                assert path.name.startswith("highlight-")


class TestResamplingFilter:
    """H-05: saved screenshots use a config-driven resampling filter (default BILINEAR)."""

    def _filter(self, resampling):
        from agent_windows.core.screenshot import _get_resampling_filter
        cfg = {"screenshot": {"resampling": resampling} if resampling is not None else {}}
        with patch("agent_windows.core.screenshot.load_config", return_value=cfg):
            return _get_resampling_filter()

    def test_default_is_bilinear(self):
        assert self._filter(None) == Image.BILINEAR

    def test_lanczos(self):
        assert self._filter("LANCZOS") == Image.LANCZOS


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


# ── annotate_screenshot (TODO #14) ──────────────────────────────────


from agent_windows.core.screenshot import annotate_screenshot  # noqa: E402
from agent_windows.core.tree import ElementInfo, BoundingBox  # noqa: E402


def _el(x1, y1, x2, y2, name="x"):
    return ElementInfo(name=name, control_type="ButtonControl",
                       bbox=BoundingBox(x1, y1, x2, y2), is_interactive=True)


class TestAnnotateScreenshot:
    def test_draws_numbered_boxes_matching_snapshot_refs(self):
        """Each element with a bbox gets a box labeled @eN (1-based, matching the
        snapshot table refs the agent cross-references)."""
        img = _solid_image(200, 200)
        els = [_el(10, 10, 50, 50), _el(60, 60, 100, 100)]
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "a.png")
            annotate_screenshot(img, els, output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # A far corner stays the original solid color; the top-left
                # element region is no longer uniform (box + label drawn).
                assert marked.getpixel((199, 199)) == (128, 128, 128)
                assert marked.getpixel((10, 10)) != (128, 128, 128)

    def test_elements_without_bbox_skipped(self):
        """An element with bbox=None must be skipped, not crash. Its index still
        counts toward the @eN numbering (matches snapshot, which lists it too)."""
        img = _solid_image(100, 100)
        no_bbox = ElementInfo(name="nb", control_type="ButtonControl", bbox=None)
        els = [no_bbox, _el(10, 10, 50, 50)]
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "a.png")
            annotate_screenshot(img, els, output_path=out)  # must not raise

    def test_grid_lines_drawn(self):
        """grid_lines=(2, 2) draws 2 vertical + 2 horizontal lines + % labels."""
        img = _solid_image(300, 300)
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "g.png")
            annotate_screenshot(img, [], grid_lines=(2, 2), output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # A grid line at ~33% X (~100px) should have non-original pixels.
                assert marked.getpixel((100, 150)) != (128, 128, 128)

    def test_grid_zero_or_none_draws_no_grid(self):
        """grid_lines=None or (0, 0) skips the grid entirely (only boxes, if any)."""
        img = _solid_image(100, 100)
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "g.png")
            annotate_screenshot(img, [], grid_lines=None, output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # No boxes, no grid → image unchanged.
                assert marked.getpixel((50, 50)) == (128, 128, 128)

    def test_auto_generate_path(self):
        """No output_path → a timestamped file is created in the screenshot dir."""
        img = _solid_image(50, 50)
        tmpdir = tempfile.mkdtemp()
        with patch("agent_windows.core.screenshot.get_screenshot_save_dir",
                   return_value=Path(tmpdir)):
            path = annotate_screenshot(img, [_el(5, 5, 20, 20)])
        assert Path(path).exists()
        assert path.name.startswith("annotated-")

    def test_does_not_mutate_input_image(self):
        """annotate_screenshot copies the input; the caller's image is untouched."""
        img = _solid_image(80, 80)
        before = img.getpixel((10, 10))
        with tempfile.TemporaryDirectory() as d:
            annotate_screenshot(img, [_el(5, 5, 30, 30)],
                                output_path=str(Path(d) / "a.png"))
        assert img.getpixel((10, 10)) == before  # input not modified

    def test_offscreen_box_clamped_to_image(self):
        """An element extending past the image edge is clamped, not dropped —
        its box + @eN label stay visible on-canvas (multi-display virtual-screen
        coords where an element may extend beyond the capture)."""
        img = _solid_image(100, 100)
        # Element bottom-right at (150, 150) — beyond the 100x100 image.
        els = [_el(80, 80, 150, 150)]
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "c.png")
            annotate_screenshot(img, els, output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # Clamped corner (99, 99) now carries the box border/label.
                assert marked.getpixel((99, 99)) != (128, 128, 128)

    def test_box_fully_outside_image_skipped(self):
        """An element entirely outside the image contributes no pixels (no crash,
        no phantom box). Its index still advances the @eN numbering."""
        img = _solid_image(50, 50)
        els = [_el(200, 200, 300, 300), _el(5, 5, 20, 20)]
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "o.png")
            annotate_screenshot(img, els, output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # The on-image element (5,5) is still drawn.
                assert marked.getpixel((5, 5)) != (128, 128, 128)

    def test_grid_label_visible_at_last_line(self):
        """The last grid line (rounds to image width) still renders its % label
        on-canvas, not 1px past the edge."""
        img = _solid_image(100, 100)
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "g.png")
            annotate_screenshot(img, [], grid_lines=(1, 1), output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # Single vertical line at ~50% (x=50) — label "50%" must show.
                assert marked.getpixel((50, 0)) != (128, 128, 128)


class TestHighlightLabelRefactor:
    """highlight_region now uses the shared _draw_label helper (TODO #14 refactor).
    Verify labels still render after the extraction."""

    def test_label_still_drawn(self):
        """A labeled region renders both the outline and the label badge; the
        badge fill differs from the original solid color somewhere in the region."""
        img = _solid_image(100, 100)
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "h.png")
            highlight_region(img, [{"x1": 10, "y1": 10, "x2": 60, "y2": 60,
                                    "label": "BTN"}], output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # Scan the region's top-left band for any non-original pixel —
                # the box outline (red) and label badge must both be present.
                region_pixels = [marked.getpixel((x, y))
                                 for y in range(10, 30) for x in range(10, 30)]
        assert any(p != (128, 128, 128) for p in region_pixels)

    def test_label_badge_is_white_not_outline_color(self):
        """Regression: the label badge must be WHITE (legible for any outline
        color), not the outline color. A black outline with a black badge would
        make the label invisible — the pre-refactor behavior was white."""
        img = _solid_image(100, 100)
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "h.png")
            highlight_region(img, [{"x1": 10, "y1": 10, "x2": 60, "y2": 60,
                                    "label": "BTN", "color": "black"}],
                            output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # The badge sits just inside (10, 10). A white pixel must appear
                # there — if the badge were black it'd be invisible against the
                # box border and this assertion would fail.
                badge_pixels = [marked.getpixel((x, y))
                                for y in range(11, 18) for x in range(11, 25)]
        assert (255, 255, 255) in badge_pixels


class TestAnnotateEdgeCases:
    """Round-2 review hardening: clamp correctness, label anchor, dxcam bypass."""

    def test_grid_tiny_image_lines_in_bounds(self):
        """A 1px-wide image must not draw grid lines at negative coords — the
        `max(0, min(w-1, ...))` clamp keeps the line at x=0, never x=-1."""
        img = _solid_image(1, 50)
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "z.png")
            annotate_screenshot(img, [], grid_lines=(1, 0), output_path=out)  # no raise
            with Image.open(out) as marked:
                marked.load()
                # The single line lands at x=0 (clamped from min(0, round(0.5))=0).
                assert marked.getpixel((0, 25)) != (128, 128, 128)

    def test_label_anchored_at_clamped_corner(self):
        """Fix #1 label-anchor: an element whose top-left is OFF-canvas must still
        show its @eN badge at the clamped corner, not at the off-canvas origin."""
        img = _solid_image(100, 100)
        # Element top-left at (-20, -20): fully off-canvas origin, but the box
        # overlaps the image from (0,0). The @e1 label must land at (0,0), not (-20,-20).
        els = [_el(-20, -20, 50, 50)]
        with tempfile.TemporaryDirectory() as d:
            out = str(Path(d) / "la.png")
            annotate_screenshot(img, els, box_color="yellow", output_path=out)
            with Image.open(out) as marked:
                marked.load()
                # The badge sits at (0,0)+padding; pixel (1,1) must be non-gray
                # (badge background or text). If the label were anchored at
                # (-20,-20) it'd be off-canvas and (1,1) would stay gray.
                assert marked.getpixel((1, 1)) != (128, 128, 128)


class TestDxcamBypassForVirtualScreen:
    """Fix #2 guarantee: a virtual-screen bbox that spans monitors cannot be
    resolved by dxcam to a single output, so the backend chain falls through to
    mss/pillow (virtual-screen aware). This is the multi-display correctness
    property the annotation recapture relies on."""

    def test_spanning_bbox_unresolvable_by_dxcam(self):
        from agent_windows.core.screenshot import _resolve_dxcam_region
        from agent_windows.uia.core import Rect

        # Two side-by-side monitors (primary at 0,0; secondary to the left). The
        # virtual-screen bbox spans both → no single monitor fully contains it.
        monitors = [Rect(-1920, 0, 0, 1080), Rect(0, 0, 1920, 1080)]
        with patch("agent_windows.uia.core.GetMonitorsRect", return_value=monitors):
            result = _resolve_dxcam_region((-1920, 0, 1920, 1080))
        assert result is None

    def test_single_monitor_bbox_resolves_by_dxcam(self):
        """Contrast case: a bbox fully inside one monitor resolves (no bypass)."""
        from agent_windows.core.screenshot import _resolve_dxcam_region
        from agent_windows.uia.core import Rect

        monitors = [Rect(0, 0, 1920, 1080)]
        with patch("agent_windows.uia.core.GetMonitorsRect", return_value=monitors):
            result = _resolve_dxcam_region((100, 100, 500, 500))
        assert result is not None
        assert result[0] == 0  # output_idx 0

