"""Tests for screenshot handler image-path blacklist validation (L-05).

highlight/diff open user-supplied image paths via PIL; those paths must pass
the filesystem blacklist so the handlers can't be used to read past it.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image


def _tmp_image(tmp_path: Path) -> Path:
    """Create a tiny valid PNG and return its path."""
    p = tmp_path / "src.png"
    Image.new("RGB", (4, 4), (10, 20, 30)).save(p)
    return p


_BLACKLISTED = r"C:\Users\alice\.ssh\leak.png"


class TestHighlightImageBlacklist:
    def test_rejects_blacklisted_image(self):
        from agent_windows.daemon.router import HANDLERS

        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["highlight"]({"image": _BLACKLISTED})

    def test_accepts_valid_image(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        img = _tmp_image(tmp_path)
        with patch("agent_windows.core.screenshot.highlight_region") as mock_hr:
            mock_hr.return_value = "/fake/out.png"
            HANDLERS["highlight"]({
                "image": str(img), "x1": 0, "y1": 0, "x2": 1, "y2": 1,
            })
        mock_hr.assert_called_once()


class TestHighlightRegionsValidation:
    """G15: the highlight handler's list path must cap the region count (a
    multi-thousand-region call forces an image.copy() + per-region draw on the
    single STA worker — a CPU sink) and validate each region's coordinates
    (the single-region shortcut calls validate_coordinates, but the list path
    passed regions straight through unchecked)."""

    def test_too_many_regions_rejected_before_draw(self, tmp_path):
        """A region list over MAX_HIGHLIGHT_REGIONS is rejected (INVALID_PARAMS)
        before highlight_region runs — no image.copy()/draw CPU sink."""
        from agent_windows.daemon.router import HANDLERS
        from agent_windows.daemon.handlers.screenshot import MAX_HIGHLIGHT_REGIONS

        img = _tmp_image(tmp_path)
        too_many = [
            {"x1": 0, "y1": 0, "x2": 1, "y2": 1}
            for _ in range(MAX_HIGHLIGHT_REGIONS + 1)
        ]
        with patch("agent_windows.core.screenshot.highlight_region") as mock_hr:
            with pytest.raises(ValueError, match="regions"):
                HANDLERS["highlight"]({"image": str(img), "regions": too_many})
            mock_hr.assert_not_called()  # rejected before any draw work

    def test_exactly_cap_regions_accepted(self, tmp_path):
        """The cap is inclusive — exactly MAX_HIGHLIGHT_REGIONS is allowed."""
        from agent_windows.daemon.router import HANDLERS
        from agent_windows.daemon.handlers.screenshot import MAX_HIGHLIGHT_REGIONS

        img = _tmp_image(tmp_path)
        at_cap = [
            {"x1": 0, "y1": 0, "x2": 1, "y2": 1}
            for _ in range(MAX_HIGHLIGHT_REGIONS)
        ]
        with patch("agent_windows.core.screenshot.highlight_region") as mock_hr:
            mock_hr.return_value = "/fake/out.png"
            HANDLERS["highlight"]({"image": str(img), "regions": at_cap})
        mock_hr.assert_called_once()

    def test_list_path_validates_each_region_coords(self, tmp_path):
        """The list path must validate each region's coordinates, mirroring the
        single-region shortcut. An out-of-virtual-screen coordinate in a region
        is rejected (CoordinateRangeError → COORDINATE_OUT_OF_RANGE) before
        highlight_region runs."""
        from agent_windows.daemon.router import HANDLERS

        img = _tmp_image(tmp_path)
        regions = [
            {"x1": 0, "y1": 0, "x2": 1, "y2": 1},       # valid
            {"x1": -99999, "y1": -99999,                 # wildly out of range
             "x2": -99998, "y2": -99998},
        ]
        with patch("agent_windows.core.screenshot.highlight_region") as mock_hr:
            with pytest.raises(ValueError):
                HANDLERS["highlight"]({"image": str(img), "regions": regions})
            mock_hr.assert_not_called()

    def test_non_numeric_region_coord_rejected(self, tmp_path):
        """A non-numeric coordinate in a region is rejected (the single-region
        path does this via validate_coordinates; the list path must too)."""
        from agent_windows.daemon.router import HANDLERS

        img = _tmp_image(tmp_path)
        regions = [{"x1": "abc", "y1": 0, "x2": 1, "y2": 1}]
        with patch("agent_windows.core.screenshot.highlight_region") as mock_hr:
            with pytest.raises(ValueError):
                HANDLERS["highlight"]({"image": str(img), "regions": regions})
            mock_hr.assert_not_called()



class TestDiffImageBlacklist:
    def test_rejects_blacklisted_baseline(self):
        from agent_windows.daemon.router import HANDLERS

        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["diff"]({"baseline": _BLACKLISTED})

    def test_rejects_blacklisted_compare(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        baseline = _tmp_image(tmp_path)  # valid, non-blacklisted
        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["diff"]({"baseline": str(baseline), "compare": _BLACKLISTED})


class TestOutputWriteProtection:
    """L-05-W1: a user-supplied --output can't land in a protected directory."""

    # The agent's data dir (~/.agent-windows) is protected from user-directed writes.
    _BAD_OUTPUT = str(Path.home() / ".agent-windows" / "pwned.png")

    def test_screenshot_rejects_protected_output(self):
        from agent_windows.daemon.router import HANDLERS

        # Validation runs before capture_screenshot, so no screen access needed.
        with pytest.raises(ValueError, match="install tree"):
            HANDLERS["screenshot"]({"output": self._BAD_OUTPUT})

    def test_highlight_rejects_protected_output(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        img = _tmp_image(tmp_path)
        with pytest.raises(ValueError, match="install tree"):
            HANDLERS["highlight"]({
                "image": str(img), "x1": 0, "y1": 0, "x2": 1, "y2": 1,
                "output": self._BAD_OUTPUT,
            })

    def test_diff_rejects_protected_output(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        baseline = _tmp_image(tmp_path)
        with pytest.raises(ValueError, match="install tree"):
            HANDLERS["diff"]({"baseline": str(baseline), "output": self._BAD_OUTPUT})


# ── screenshot --annotate (TODO #14) ────────────────────────────────


class TestScreenshotAnnotation:
    """The screenshot handler's use_annotation flag overlays numbered detection
    boxes + optional grid from the UIA tree snapshot."""

    def _patch_capture(self):
        """capture_screenshot → a fresh 100x100 image so no real screen access."""
        return patch(
            "agent_windows.core.screenshot.capture_screenshot",
            return_value=Image.new("RGB", (100, 100), (50, 50, 50)),
        )

    def _patch_annotate(self, tmp_path):
        """annotate_screenshot → writes a real file (so the handler's Image.open
        reload succeeds) and records its call args."""
        out = tmp_path / "annotated.png"
        out.write_bytes(b"")  # placeholder; will be overwritten below

        def _fake(image, elements, grid_lines=None, **kw):
            # Write a real image so the handler's reload works.
            Image.new("RGB", image.size, (1, 2, 3)).save(out)
            return out

        return patch("agent_windows.core.screenshot.annotate_screenshot",
                     side_effect=_fake), out

    def test_use_annotation_calls_annotate_and_snapshot(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS
        from agent_windows.core.tree import ElementInfo, BoundingBox

        el = ElementInfo(name="b", control_type="ButtonControl",
                         bbox=BoundingBox(0, 0, 10, 10), is_interactive=True)
        with self._patch_capture(), \
                patch("agent_windows.core.tree.get_tree_snapshot",
                      return_value={"interactive_elements": [el]}), \
                patch("agent_windows.utils.config.load_config",
                      return_value={}), \
                patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                      return_value=(0, 0, 100, 100)), \
                self._patch_annotate(tmp_path)[0] as m_ann:
            r = HANDLERS["screenshot"]({"use_annotation": True})
        m_ann.assert_called_once()
        # First positional arg is the image, second is the elements list.
        assert m_ann.call_args.args[1] == [el]
        assert r["output"].count("Annotated: 1/1 element boxes") == 1

    def test_grid_params_forwarded(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        with self._patch_capture(), \
                patch("agent_windows.core.tree.get_tree_snapshot",
                      return_value={"interactive_elements": []}), \
                patch("agent_windows.utils.config.load_config",
                      return_value={}), \
                patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                      return_value=(0, 0, 100, 100)), \
                self._patch_annotate(tmp_path)[0] as m_ann:
            HANDLERS["screenshot"]({"use_annotation": True,
                                    "width_reference_line": 2,
                                    "height_reference_line": 3})
        assert m_ann.call_args.kwargs.get("grid_lines") == (2, 3)

    def test_negative_grid_rejected(self):
        from agent_windows.daemon.router import HANDLERS

        with self._patch_capture():
            with pytest.raises(ValueError, match="non-negative"):
                HANDLERS["screenshot"]({"use_annotation": True,
                                        "width_reference_line": -1})

    def test_string_true_enables_annotation(self, tmp_path):
        """Some MCP clients serialize bools as the string "true"; it must enable
        annotation just like a real bool True. (Mirrors _call._as_bool.)"""
        from agent_windows.daemon.router import HANDLERS

        with self._patch_capture(), \
                patch("agent_windows.core.tree.get_tree_snapshot",
                      return_value={"interactive_elements": []}), \
                patch("agent_windows.utils.config.load_config",
                      return_value={}), \
                patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                      return_value=(0, 0, 100, 100)), \
                self._patch_annotate(tmp_path)[0] as m_ann:
            HANDLERS["screenshot"]({"use_annotation": "true"})
        m_ann.assert_called_once()

    def test_string_false_does_not_enable_annotation(self, tmp_path):
        """A stringified "false" must NOT enable annotation — raw bool("false")
        would be True, which is the bug this guards against."""
        from agent_windows.daemon.router import HANDLERS

        with self._patch_capture(), \
                patch("agent_windows.core.screenshot.annotate_screenshot") as m_ann:
            HANDLERS["screenshot"]({"use_annotation": "false"})
        m_ann.assert_not_called()

    def test_bbox_overridden_with_warning(self, tmp_path):
        """Annotation needs the full screen; a --bbox/--selector is dropped with a
        warning rather than misaligning the boxes on a cropped capture."""
        from agent_windows.daemon.router import HANDLERS

        with self._patch_capture() as m_cap, \
                patch("agent_windows.core.tree.get_tree_snapshot",
                      return_value={"interactive_elements": []}), \
                patch("agent_windows.utils.config.load_config",
                      return_value={}), \
                patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                      return_value=(0, 0, 1920, 1080)), \
                self._patch_annotate(tmp_path)[0]:
            r = HANDLERS["screenshot"]({
                "use_annotation": True,
                "bbox": [0, 0, 50, 50],
            })
        # Annotation short-circuits the initial capture: exactly ONE capture
        # of the full virtual screen (no double capture), bbox ignored.
        assert m_cap.call_count == 1
        assert m_cap.call_args.kwargs.get("bbox") == (0, 0, 1920, 1080)
        assert "annotation requires full screen" in r["output"]
        # Region is reported as the full virtual screen, not a primary-monitor guess.
        assert r["region"] == [0, 0, 1920, 1080]

    def test_annotation_captures_full_virtual_screen(self, tmp_path):
        """Annotation must capture the WHOLE virtual screen (multi-display aware),
        not dxcam's primary-only default — boxes use virtual-desktop coords."""
        from agent_windows.daemon.router import HANDLERS

        captured_bboxes: list = []
        real_capture = Image.new("RGB", (100, 100), (50, 50, 50))

        def _spy(*a, **kw):
            captured_bboxes.append(kw.get("bbox"))
            return real_capture

        with patch("agent_windows.core.screenshot.capture_screenshot",
                   side_effect=_spy), \
                patch("agent_windows.core.tree.get_tree_snapshot",
                      return_value={"interactive_elements": []}), \
                patch("agent_windows.utils.config.load_config",
                      return_value={}), \
                patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                      return_value=(-1920, 0, 3840, 1080)), \
                self._patch_annotate(tmp_path)[0]:
            HANDLERS["screenshot"]({"use_annotation": True})
        # The capture bbox is the explicit virtual-screen edges, so multi-display
        # elements (e.g. negative origin) align with the image. The dxcam default
        # (primary-only) is bypassed because this bbox spans monitors.
        assert captured_bboxes == [(-1920, 0, 1920, 1080)]

    def test_annotation_reports_virtual_screen_region(self, tmp_path):
        """After annotation, Region reflects the full virtual screen even when the
        backend is dxcam (which would otherwise report primary-only)."""
        from agent_windows.daemon.router import HANDLERS

        with self._patch_capture(), \
                patch("agent_windows.core.tree.get_tree_snapshot",
                      return_value={"interactive_elements": []}), \
                patch("agent_windows.utils.config.load_config",
                      return_value={}), \
                patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                      return_value=(0, 0, 2560, 1440)), \
                patch("agent_windows.core.screenshot.get_capture_backend",
                      return_value="dxcam"), \
                self._patch_annotate(tmp_path)[0]:
            r = HANDLERS["screenshot"]({"use_annotation": True})
        # Not the dxcam primary-monitor guess; the true virtual-screen edges.
        assert r["region"] == [0, 0, 2560, 1440]

    def test_plain_annotate_no_extra_params(self, tmp_path):
        """screenshot --annotate with no bbox/selector/monitor: no warning, still
        a single full-virtual-screen capture."""
        from agent_windows.daemon.router import HANDLERS

        with self._patch_capture() as m_cap, \
                patch("agent_windows.core.tree.get_tree_snapshot",
                      return_value={"interactive_elements": []}), \
                patch("agent_windows.utils.config.load_config",
                      return_value={}), \
                patch("agent_windows.core.screenshot.get_virtual_screen_rect",
                      return_value=(0, 0, 1920, 1080)), \
                self._patch_annotate(tmp_path)[0]:
            r = HANDLERS["screenshot"]({"use_annotation": True})
        assert m_cap.call_count == 1
        assert "annotation requires full screen" not in r["output"]

    def test_no_annotation_flag_skips_annotate(self, tmp_path):
        """Without use_annotation, annotate_screenshot is never called."""
        from agent_windows.daemon.router import HANDLERS

        with self._patch_capture(), \
                patch("agent_windows.core.screenshot.annotate_screenshot") as m_ann:
            HANDLERS["screenshot"]({})
        m_ann.assert_not_called()


class TestPerCallScaleOverride:
    """TODO #15b: per-call scale/max_width/max_height override the config defaults."""

    def _patch_scaling(self):
        """Mock scale_screenshot to return the image unchanged but record its kwargs."""
        from agent_windows.core import screenshot as sc
        return patch.object(sc, "scale_screenshot", side_effect=lambda img, **kw: img)

    def test_per_call_scale_overrides_config(self):
        """Passing scale in params overrides the config value."""
        from agent_windows.daemon.router import HANDLERS
        with patch("agent_windows.utils.config.load_config",
                   return_value={"screenshot": {"scale": 1.0, "max_width": 0, "max_height": 0}}), \
                patch("agent_windows.core.screenshot.capture_screenshot",
                      return_value=Image.new("RGB", (100, 100))), \
                self._patch_scaling() as m_scale:
            HANDLERS["screenshot"]({"scale": 0.5})
        # scale=0.5 from params wins over config's 1.0
        assert m_scale.call_args.kwargs["scale"] == 0.5

    def test_per_call_max_dims_override_config(self):
        """Passing max_width/max_height in params overrides the config values."""
        from agent_windows.daemon.router import HANDLERS
        with patch("agent_windows.utils.config.load_config",
                   return_value={"screenshot": {"scale": 1.0, "max_width": 1920, "max_height": 1080}}), \
                patch("agent_windows.core.screenshot.capture_screenshot",
                      return_value=Image.new("RGB", (100, 100))), \
                self._patch_scaling() as m_scale:
            HANDLERS["screenshot"]({"max_width": 800, "max_height": 600})
        assert m_scale.call_args.kwargs["max_width"] == 800
        assert m_scale.call_args.kwargs["max_height"] == 600

    def test_omitted_params_fall_back_to_config(self):
        """When scale/max_* are omitted from params, the config defaults apply."""
        from agent_windows.daemon.router import HANDLERS
        with patch("agent_windows.utils.config.load_config",
                   return_value={"screenshot": {"scale": 0.75, "max_width": 1280, "max_height": 720}}), \
                patch("agent_windows.core.screenshot.capture_screenshot",
                      return_value=Image.new("RGB", (100, 100))), \
                self._patch_scaling() as m_scale:
            HANDLERS["screenshot"]({})
        assert m_scale.call_args.kwargs["scale"] == 0.75
        assert m_scale.call_args.kwargs["max_width"] == 1280
        assert m_scale.call_args.kwargs["max_height"] == 720


class TestScreenshotProfiling:
    """The screenshot handler emits per-stage timings (capture_ms / resize_ms) to
    the log ONLY under is_debug(), and never otherwise (TODO #3). The production
    hot path must pay nothing. capture_screenshot + scale_screenshot are patched
    so no real screen access happens."""

    def _run(self, debug_on, caplog):
        import logging
        from agent_windows.daemon.router import HANDLERS
        from agent_windows.daemon.handlers import screenshot as sc_handler
        with patch.object(sc_handler, "is_debug", return_value=debug_on), \
                patch("agent_windows.utils.config.load_config",
                      return_value={}), \
                patch("agent_windows.core.screenshot.capture_screenshot",
                      return_value=Image.new("RGB", (100, 100))), \
                patch("agent_windows.core.screenshot.scale_screenshot",
                      side_effect=lambda img, **kw: img), \
                patch("agent_windows.core.screenshot.get_capture_backend",
                      return_value="test"):
            with caplog.at_level(logging.INFO,
                                 logger=sc_handler.logger.name):
                HANDLERS["screenshot"]({})

    def test_profile_line_emitted_when_debug_on(self, caplog):
        self._run(True, caplog)
        profile_lines = [r for r in caplog.records
                         if "screenshot profile:" in r.getMessage()]
        assert len(profile_lines) == 1
        msg = profile_lines[0].getMessage()
        assert "capture_ms=" in msg
        assert "resize_ms=" in msg
        assert "backend=test" in msg
        assert "annotated=False" in msg

    def test_no_profile_line_when_debug_off(self, caplog):
        self._run(False, caplog)
        profile_lines = [r for r in caplog.records
                         if "screenshot profile:" in r.getMessage()]
        assert profile_lines == []  # production: zero profiling output


class TestFindImageHandler:
    """find_image handler: param forwarding + validation -> INVALID_PARAMS."""

    def test_forwards_params_and_best_match(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        tpl = _tmp_image(tmp_path)
        fake = {"count": 1, "matches": [{"index": 1, "x": 10, "y": 20, "w": 3,
                 "h": 3, "center_x": 11, "center_y": 21, "score": 1.0}],
                "method": "template", "truncated": False,
                "screenshot_path": "/fake/m.png"}
        with patch("agent_windows.core.image_match.find_image",
                   return_value=fake) as mock_fi:
            result = HANDLERS["find_image"]({
                "image": str(tpl), "tolerance": 5, "grayscale": True, "all": True,
            })
        mock_fi.assert_called_once_with(
            template=str(tpl), source=None, region=None,
            tolerance=5, grayscale=True, all_matches=True)
        assert result["count"] == 1
        assert result["method"] == "template"
        assert result["truncated"] is False

    def test_bbox_becomes_region(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        tpl = _tmp_image(tmp_path)
        with patch("agent_windows.core.image_match.find_image",
                   return_value={"count": 0, "matches": [], "method": "template",
                                 "truncated": False, "screenshot_path": ""}) as mock_fi:
            HANDLERS["find_image"]({"image": str(tpl), "bbox": [1, 2, 3, 4]})
        assert mock_fi.call_args.kwargs["region"] == (1, 2, 3, 4)

    def test_missing_image_rejected(self):
        from agent_windows.daemon.router import HANDLERS
        with pytest.raises(ValueError, match="image"):
            HANDLERS["find_image"]({})

    def test_tolerance_out_of_range_rejected(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS
        tpl = _tmp_image(tmp_path)
        with pytest.raises(ValueError, match="tolerance"):
            HANDLERS["find_image"]({"image": str(tpl), "tolerance": 999})

    def test_blacklisted_template_rejected(self):
        from agent_windows.daemon.router import HANDLERS
        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["find_image"]({"image": _BLACKLISTED})
