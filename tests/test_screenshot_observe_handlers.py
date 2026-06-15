"""Tests for daemon/handlers/screenshot.py and observe.py.

The existing test_screenshot_handler.py covers only the L-05 image-path
blacklist. This file covers the rest: selector→bbox resolution, scale-config
plumbing, the highlight single-region shortcut, the diff flow, cleanup stats,
and observe's combination/separation logic.

Core functions (capture_screenshot, scale_screenshot, save_and_cleanup,
highlight_region, diff_images, cleanup_screenshots, resolve_element) are
patched so no real screen/PIL work happens.
"""

from unittest.mock import patch, MagicMock

import pytest
from PIL import Image

from agent_windows.daemon.router import HANDLERS


def _fake_image(w=100, h=80):
    return Image.new("RGB", (w, h), (1, 2, 3))


# ── screenshot handler ───────────────────────────────────────────────


class TestScreenshotHandler:
    def test_basic_capture_returns_path_and_size(self):
        img = _fake_image(320, 200)
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img), \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="/out/a.png") as m_save, \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im):
            r = HANDLERS["screenshot"]({})
        m_save.assert_called_once()
        assert r["path"] == "/out/a.png"
        assert r["width"] == 320 and r["height"] == 200
        assert "## Screenshot" in r["output"]
        assert "Path: /out/a.png" in r["output"]
        assert "Size: 320x200" in r["output"]

    def test_capture_forwards_monitor_and_bbox(self):
        img = _fake_image()
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img) as m_cap, \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p"), \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im):
            HANDLERS["screenshot"]({"monitor": 1, "bbox": [10, 20, 30, 40]})
        m_cap.assert_called_once_with(monitor=1, bbox=(10, 20, 30, 40))

    def test_bbox_none_when_absent(self):
        img = _fake_image()
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img) as m_cap, \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p"), \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im):
            HANDLERS["screenshot"]({})
        m_cap.assert_called_once_with(monitor=-1, bbox=None)

    def test_selector_resolves_to_bbox(self):
        """An @e selector resolves via resolve_element; its bbox becomes the crop list."""
        img = _fake_image()
        el = MagicMock()
        el.bbox = MagicMock(left=1, top=2, right=3, bottom=4)
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img) as m_cap, \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p"), \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im), \
             patch("agent_windows.core.tree.resolve_element", return_value=el) as m_res:
            HANDLERS["screenshot"]({"selector": "@e:btn:Submit"})
        m_res.assert_called_once_with("@e:btn:Submit")
        m_cap.assert_called_once_with(monitor=-1, bbox=(1, 2, 3, 4))

    def test_selector_with_no_bbox_adds_warning(self):
        el = MagicMock()
        el.bbox = None
        img = _fake_image()
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img), \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p"), \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im), \
             patch("agent_windows.core.tree.resolve_element", return_value=el):
            r = HANDLERS["screenshot"]({"selector": "@e:x"})
        assert "selector unresolved" in r["output"]
        assert "has no bounding box" in r["output"]

    def test_selector_resolve_error_adds_warning(self):
        img = _fake_image()
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img), \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p"), \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im), \
             patch("agent_windows.core.tree.resolve_element", side_effect=ValueError("not found")):
            r = HANDLERS["screenshot"]({"selector": "@e:missing"})
        assert "selector unresolved: not found" in r["output"]

    def test_non_element_selector_ignored(self):
        """A selector that doesn't start with '@e' is NOT passed to resolve_element."""
        img = _fake_image()
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img) as m_cap, \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p"), \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im), \
             patch("agent_windows.core.tree.resolve_element") as m_res:
            HANDLERS["screenshot"]({"selector": "some text"})
        m_res.assert_not_called()
        m_cap.assert_called_once_with(monitor=-1, bbox=None)

    def test_scale_config_applied(self, mock_config):
        """scale_screenshot receives max_width/max_height/scale from config."""
        mock_config.return_value["screenshot"]["max_width"] = 50
        mock_config.return_value["screenshot"]["max_height"] = 40
        mock_config.return_value["screenshot"]["scale"] = 0.5
        img = _fake_image(100, 80)
        scaled = _fake_image(50, 40)
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img), \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p"), \
             patch("agent_windows.core.screenshot.scale_screenshot", return_value=scaled) as m_scale:
            r = HANDLERS["screenshot"]({})
        m_scale.assert_called_once_with(img, max_width=50, max_height=40, scale=0.5)
        assert r["width"] == 50 and r["height"] == 40  # from the scaled image

    def test_scale_failure_swallowed(self, mock_config):
        """A scaling exception is logged+swallowed; the unscaled image is saved."""
        img = _fake_image(64, 48)
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img), \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p") as m_save, \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=RuntimeError("boom")):
            r = HANDLERS["screenshot"]({})
        # the ORIGINAL image flows into save_and_cleanup (not None / not lost)
        assert m_save.call_args.args[0] is img
        assert r["width"] == 64  # fell back to original image

    def test_scale_config_defaults_when_keys_absent(self, mock_config):
        """Bare mock_config has no max_width/max_height keys → scale_screenshot
        receives the 0/0/1.0 defaults (proves cfg.get(..., default) resolves)."""
        img = _fake_image()
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img), \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="p"), \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im) as m_scale:
            HANDLERS["screenshot"]({})
        m_scale.assert_called_once_with(img, max_width=0, max_height=0, scale=1.0)

    def test_output_path_validated_before_capture(self):
        """validate_write_path runs (and may reject) before capture_screenshot."""
        img = _fake_image()
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=img) as m_cap, \
             patch("agent_windows.core.screenshot.save_and_cleanup", return_value="/safe/o.png"), \
             patch("agent_windows.core.screenshot.scale_screenshot", side_effect=lambda im, **k: im), \
             patch("agent_windows.core.filesystem.validate_write_path", return_value="/safe/o.png") as m_vw:
            HANDLERS["screenshot"]({"output": "o.png"})
        m_vw.assert_called_once_with("o.png")


# ── highlight handler ────────────────────────────────────────────────


class TestHighlightHandler:
    def test_single_region_shortcut_from_coords(self):
        """x1/y1/x2/y2 params build a single region with defaults."""
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=_fake_image()), \
             patch("agent_windows.core.screenshot.highlight_region", return_value="/o/h.png") as m_hr:
            r = HANDLERS["highlight"]({"x1": 1, "y1": 2, "x2": 3, "y2": 4})
        regions = m_hr.call_args.args[1]
        assert regions == [{"x1": 1, "y1": 2, "x2": 3, "y2": 4, "label": "", "color": "red"}]
        assert r["regions"] == 1
        assert "Regions: 1" in r["output"]

    def test_single_region_label_and_color(self):
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=_fake_image()), \
             patch("agent_windows.core.screenshot.highlight_region", return_value="p") as m_hr:
            HANDLERS["highlight"]({"x1": 0, "y1": 0, "x2": 1, "y2": 1, "label": "L", "color": "blue"})
        assert m_hr.call_args.args[1][0]["label"] == "L"
        assert m_hr.call_args.args[1][0]["color"] == "blue"

    def test_explicit_regions_list(self):
        """A provided `regions` list is used verbatim (no coord shortcut)."""
        regions = [{"x1": 0, "y1": 0, "x2": 1, "y2": 1}, {"x1": 5, "y1": 5, "x2": 6, "y2": 6}]
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=_fake_image()), \
             patch("agent_windows.core.screenshot.highlight_region", return_value="p") as m_hr:
            r = HANDLERS["highlight"]({"regions": regions})
        assert m_hr.call_args.args[1] == regions
        assert r["regions"] == 2

    def test_uses_provided_image_instead_of_capture(self, tmp_path):
        """When `image` is supplied, highlight opens it instead of capturing."""
        from PIL import Image as PILImage
        img_path = tmp_path / "src.png"
        PILImage.new("RGB", (4, 4), (0, 0, 0)).save(img_path)

        with patch("agent_windows.core.screenshot.capture_screenshot") as m_cap, \
             patch("agent_windows.core.screenshot.highlight_region", return_value="/o.png"), \
             patch("agent_windows.core.filesystem.validate_read_path", return_value=img_path):
            HANDLERS["highlight"]({"image": str(img_path), "x1": 0, "y1": 0, "x2": 1, "y2": 1})
        m_cap.assert_not_called()  # image path used instead

    def test_output_path_validated(self):
        with patch("agent_windows.core.screenshot.capture_screenshot", return_value=_fake_image()), \
             patch("agent_windows.core.screenshot.highlight_region", return_value="p"), \
             patch("agent_windows.core.filesystem.validate_write_path", return_value="/safe/o.png") as m_vw:
            HANDLERS["highlight"]({"x1": 0, "y1": 0, "x2": 1, "y2": 1, "output": "o.png"})
        m_vw.assert_called_once_with("o.png")


# ── diff handler ─────────────────────────────────────────────────────


class TestDiffHandler:
    def _two_images(self, tmp_path):
        from PIL import Image as PILImage
        a = tmp_path / "a.png"
        b = tmp_path / "b.png"
        PILImage.new("RGB", (4, 4), (0, 0, 0)).save(a)
        PILImage.new("RGB", (4, 4), (255, 255, 255)).save(b)
        return a, b

    def test_baseline_required(self):
        with pytest.raises(ValueError, match="missing required parameter 'baseline'"):
            HANDLERS["diff"]({})

    def test_baseline_must_be_str(self):
        with pytest.raises(ValueError, match="must be str"):
            HANDLERS["diff"]({"baseline": 123})

    def test_diff_with_explicit_compare(self, tmp_path):
        a, b = self._two_images(tmp_path)
        result = {"diff_pixels": 16, "total_pixels": 16, "diff_percent": 100.0,
                  "match": False, "diff_path": "/o/d.png"}
        with patch("agent_windows.core.screenshot.diff_images", return_value=result) as m_diff, \
             patch("agent_windows.core.filesystem.validate_read_path", side_effect=lambda p: p):
            r = HANDLERS["diff"]({"baseline": str(a), "compare": str(b), "threshold": 10})
        m_diff.assert_called_once()
        assert m_diff.call_args.kwargs["threshold"] == 10
        assert r["diff_pixels"] == 16
        assert "Diff pixels: 16 / 16" in r["output"]
        assert "Match: False" in r["output"]
        assert "Path: /o/d.png" in r["output"]

    def test_diff_captures_when_no_compare(self, tmp_path):
        a, _ = self._two_images(tmp_path)
        result = {"diff_pixels": 0, "total_pixels": 16, "diff_percent": 0.0,
                  "match": True, "diff_path": "/o/d.png"}
        with patch("agent_windows.core.screenshot.diff_images", return_value=result), \
             patch("agent_windows.core.screenshot.capture_screenshot", return_value=_fake_image()) as m_cap, \
             patch("agent_windows.core.filesystem.validate_read_path", side_effect=lambda p: p):
            HANDLERS["diff"]({"baseline": str(a)})
        m_cap.assert_called_once()  # compare image was captured

    def test_threshold_default(self, tmp_path):
        a, _ = self._two_images(tmp_path)
        result = {"diff_pixels": 0, "total_pixels": 16, "diff_percent": 0.0,
                  "match": True, "diff_path": "/o/d.png"}
        with patch("agent_windows.core.screenshot.diff_images", return_value=result) as m_diff, \
             patch("agent_windows.core.screenshot.capture_screenshot", return_value=_fake_image()), \
             patch("agent_windows.core.filesystem.validate_read_path", side_effect=lambda p: p):
            HANDLERS["diff"]({"baseline": str(a)})
        assert m_diff.call_args.kwargs["threshold"] == 30  # default

    def test_output_path_validated(self, tmp_path):
        a, _ = self._two_images(tmp_path)
        result = {"diff_pixels": 0, "total_pixels": 16, "diff_percent": 0.0,
                  "match": True, "diff_path": "/o/d.png"}
        with patch("agent_windows.core.screenshot.diff_images", return_value=result), \
             patch("agent_windows.core.screenshot.capture_screenshot", return_value=_fake_image()), \
             patch("agent_windows.core.filesystem.validate_read_path", side_effect=lambda p: p), \
             patch("agent_windows.core.filesystem.validate_write_path", return_value="/safe/o.png") as m_vw:
            HANDLERS["diff"]({"baseline": str(a), "output": "o.png"})
        m_vw.assert_called_once_with("o.png")


# ── cleanup_screenshots handler ──────────────────────────────────────


class TestCleanupHandler:
    def test_forwards_force_true(self):
        stats = {"deleted_by_age": 2, "deleted_by_size": 1, "total_kept": 5, "dir_size_mb": 12.3}
        with patch("agent_windows.core.screenshot.cleanup_screenshots", return_value=stats) as m:
            r = HANDLERS["cleanup_screenshots"]({})
        m.assert_called_once_with(force=True)
        assert "Deleted by age: 2" in r["output"]
        assert "Deleted by size: 1" in r["output"]
        assert "Remaining files: 5" in r["output"]
        assert "Directory size: 12.3 MB" in r["output"]
        assert r["deleted_by_age"] == 2

    def test_missing_stats_default_to_zero(self):
        """get(..., 0) means absent keys render as 0, not crash."""
        with patch("agent_windows.core.screenshot.cleanup_screenshots", return_value={}):
            r = HANDLERS["cleanup_screenshots"]({})
        assert "Deleted by age: 0" in r["output"]
        assert "Directory size: 0 MB" in r["output"]


# ── observe handler ──────────────────────────────────────────────────


class TestObserveHandler:
    def test_defaults_to_snapshot_and_screenshot(self):
        """No flags → both snapshot + screenshot run."""
        with patch.dict(HANDLERS, {
            "snapshot": MagicMock(return_value={"output": "SNAP"}),
            "screenshot": MagicMock(return_value={"output": "SHOT"}),
        }):
            r = HANDLERS["observe"]({})
        assert "SNAP" in r["output"]
        assert "SHOT" in r["output"]
        assert "---" in r["output"]  # separator between the two

    def test_snapshot_only(self):
        with patch.dict(HANDLERS, {"snapshot": MagicMock(return_value={"output": "SNAP"})}):
            r = HANDLERS["observe"]({"snapshot": True})
        assert r["output"] == "SNAP"

    def test_screenshot_only(self):
        with patch.dict(HANDLERS, {"screenshot": MagicMock(return_value={"output": "SHOT"})}):
            r = HANDLERS["observe"]({"screenshot": True})
        assert r["output"] == "SHOT"

    def test_separator_only_between_parts(self):
        """No leading/trailing '---' when a single part is requested."""
        with patch.dict(HANDLERS, {"snapshot": MagicMock(return_value={"output": "ONLY"})}):
            r = HANDLERS["observe"]({"snapshot": True})
        assert not r["output"].startswith("---")

    def test_ground_registered_appends(self):
        ground_mock = MagicMock(return_value={"output": "G"})
        with patch.dict(HANDLERS, {
            "snapshot": MagicMock(return_value={"output": "S"}),
            "screenshot": MagicMock(return_value={"output": "T"}),
            "ground": ground_mock,
        }):
            r = HANDLERS["observe"]({"ground": "the button"})
        assert "G" in r["output"]
        ground_mock.assert_called_once_with({"description": "the button"})

    def test_ground_unregistered_falls_back(self):
        """When 'ground' handler isn't registered, emit the not-implemented notice."""
        # clear=True isolates the set; keep observe + snapshot + screenshot, and
        # intentionally OMIT 'ground' so the fallback branch is taken.
        from agent_windows.daemon.handlers import observe as observe_mod
        with patch.dict(HANDLERS, {
            "observe": observe_mod._observe,
            "snapshot": MagicMock(return_value={"output": "S"}),
            "screenshot": MagicMock(return_value={"output": "T"}),
        }, clear=True):
            r = HANDLERS["observe"]({"ground": "x"})
        assert "Not yet implemented" in r["output"]

    def test_ground_only(self):
        with patch.dict(HANDLERS, {"ground": MagicMock(return_value={"output": "G"})}):
            r = HANDLERS["observe"]({"ground": "x"})
        assert r["output"] == "G"

    def test_ground_separator_positioning(self):
        """ground is the 2nd+ part → preceded by '---'."""
        with patch.dict(HANDLERS, {
            "snapshot": MagicMock(return_value={"output": "S"}),
            "ground": MagicMock(return_value={"output": "G"}),
        }):
            r = HANDLERS["observe"]({"snapshot": True, "ground": "x"})
        # S, then separator, then G
        assert r["output"].index("S") < r["output"].index("---") < r["output"].index("G")
