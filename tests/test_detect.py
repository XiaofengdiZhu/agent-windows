"""Tests for detect command: JSON parsing, coordinate conversion, annotation."""

import json
import os
import shutil
import tempfile

import pytest
from PIL import Image
from unittest.mock import patch, MagicMock

from agent_windows.core.grounding import (
    _parse_detection_json,
    _validate_targets,
    _save_detection_screenshot,
    detect_elements,
)


# ── JSON parsing tests ────────────────────────────────────────────────


class TestParseDetectionJson:
    """Test the 4-level JSON fallback parser."""

    def test_clean_json_array(self):
        response = '[{"label": "icon", "bbox": [10, 20, 30, 40]}]'
        result = _parse_detection_json(response)
        assert len(result) == 1
        assert result[0]["label"] == "icon"
        assert result[0]["bbox"] == [10, 20, 30, 40]

    def test_json_in_code_block(self):
        response = '```json\n[{"label": "btn", "bbox": [1, 2, 3, 4]}]\n```'
        result = _parse_detection_json(response)
        assert len(result) == 1
        assert result[0]["label"] == "btn"

    def test_code_block_without_language_tag(self):
        response = '```\n[{"label": "x", "bbox": [1, 2, 3, 4]}]\n```'
        result = _parse_detection_json(response)
        assert len(result) == 1

    def test_json_with_preamble(self):
        response = 'I found the following elements:\n[{"label": "a", "bbox": [1, 2, 3, 4]}]'
        result = _parse_detection_json(response)
        assert len(result) == 1

    def test_empty_array(self):
        result = _parse_detection_json("[]")
        assert result == []

    def test_empty_array_in_code_block(self):
        result = _parse_detection_json("```json\n[]\n```")
        assert result == []

    def test_no_json_returns_empty(self):
        result = _parse_detection_json("no json here at all")
        assert result == []

    def test_malformed_json_returns_empty(self):
        result = _parse_detection_json("[{broken json}]")
        assert result == []

    def test_multiple_targets(self):
        data = [
            {"label": "a", "bbox": [1, 2, 3, 4]},
            {"label": "b", "bbox": [5, 6, 7, 8]},
            {"label": "c", "bbox": [9, 10, 11, 12]},
        ]
        result = _parse_detection_json(json.dumps(data))
        assert len(result) == 3

    def test_float_bbox_values_converted_to_int(self):
        response = '[{"label": "f", "bbox": [1.5, 2.7, 3.1, 4.9]}]'
        result = _parse_detection_json(response)
        assert result[0]["bbox"] == [1, 2, 3, 4]

    def test_skips_entries_without_label(self):
        response = '[{"bbox": [1, 2, 3, 4]}, {"label": "ok", "bbox": [5, 6, 7, 8]}]'
        result = _parse_detection_json(response)
        assert len(result) == 1
        assert result[0]["label"] == "ok"

    def test_skips_entries_without_bbox(self):
        response = '[{"label": "no bbox"}, {"label": "ok", "bbox": [5, 6, 7, 8]}]'
        result = _parse_detection_json(response)
        assert len(result) == 1

    def test_skips_non_dict_entries(self):
        response = '[42, "string", {"label": "ok", "bbox": [1, 2, 3, 4]}]'
        result = _parse_detection_json(response)
        assert len(result) == 1

    def test_label_with_bracket_in_preamble(self):
        """Level 3 should handle labels containing ] character."""
        response = 'Results: [{"label": "]", "bbox": [1, 2, 3, 4]}]'
        result = _parse_detection_json(response)
        assert len(result) == 1
        assert result[0]["label"] == "]"

    def test_label_with_brackets_in_preamble(self):
        response = 'Found: [{"label": "[OK]", "bbox": [1, 2, 3, 4]}]'
        result = _parse_detection_json(response)
        assert len(result) == 1
        assert result[0]["label"] == "[OK]"


class TestValidateTargets:
    def test_valid_targets(self):
        raw = [
            {"label": "a", "bbox": [1, 2, 3, 4]},
            {"label": "b", "bbox": [5, 6, 7, 8]},
        ]
        result = _validate_targets(raw)
        assert len(result) == 2

    def test_skip_non_dict(self):
        raw = ["string", 42, {"label": "ok", "bbox": [1, 2, 3, 4]}]
        result = _validate_targets(raw)
        assert len(result) == 1

    def test_skip_wrong_bbox_length(self):
        raw = [
            {"label": "short", "bbox": [1, 2]},
            {"label": "ok", "bbox": [1, 2, 3, 4]},
        ]
        result = _validate_targets(raw)
        assert len(result) == 1


# ── Core function tests (mocked) ──────────────────────────────────────


class TestDetectElements:
    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_basic_detection(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test-model", "engine": "openai", "width": 1000, "height": 1000}
        }
        mock_api.return_value = '[{"label": "icon", "bbox": [100, 200, 150, 250]}]'
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        result = detect_elements("test icon")

        assert result["count"] == 1
        assert len(result["targets"]) == 1
        t = result["targets"][0]
        assert t["label"] == "icon"
        assert t["index"] == 1
        # Coordinates should be physical pixels (converted from model space)
        assert "x1" in t["box"]
        assert "y1" in t["box"]
        assert "x2" in t["box"]
        assert "y2" in t["box"]
        # x1 < x2, y1 < y2
        assert t["box"]["x1"] <= t["box"]["x2"]
        assert t["box"]["y1"] <= t["box"]["y2"]

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_multiple_targets(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        mock_api.return_value = json.dumps([
            {"label": "a", "bbox": [10, 20, 30, 40]},
            {"label": "b", "bbox": [50, 60, 70, 80]},
            {"label": "c", "bbox": [90, 100, 110, 120]},
        ])
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        result = detect_elements("targets")
        assert result["count"] == 3
        assert result["targets"][0]["index"] == 1
        assert result["targets"][2]["index"] == 3

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_zero_targets(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        mock_api.return_value = "[]"
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        result = detect_elements("nothing")
        assert result["count"] == 0
        assert result["targets"] == []

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_model_override(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "default", "engine": "openai", "width": 1000, "height": 1000}
        }
        mock_api.return_value = "[]"
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        detect_elements("test", model="gpt-4o")

        # Verify model override in API call config
        args, kwargs = mock_api.call_args
        config_used = args[2]
        assert config_used["model"] == "gpt-4o"

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_max_targets_passed_to_prompt(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        mock_api.return_value = "[]"
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        detect_elements("test", max_targets=5)

        # Verify prompt contains max_targets value
        args, kwargs = mock_api.call_args
        prompt_used = args[1]
        assert "5" in prompt_used

    @patch("agent_windows.core.grounding.load_config")
    def test_disabled_raises(self, mock_config):
        mock_config.return_value = {"vlm": {"enabled": False}}
        with pytest.raises(RuntimeError, match="disabled"):
            detect_elements("test")

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_inverted_bbox_auto_swap(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        # Model returns inverted box: x2 < x1, y2 < y1
        mock_api.return_value = '[{"label": "inv", "bbox": [200, 300, 100, 200]}]'
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        result = detect_elements("test")
        assert result["count"] == 1
        b = result["targets"][0]["box"]
        assert b["x1"] <= b["x2"]
        assert b["y1"] <= b["y2"]

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_bbox_normalized_after_clamp(self, mock_config, mock_api, mock_capture):
        """A box whose two corners clamp to the same edge could be re-inverted
        by the per-corner clamp (model_to_physical clamps each corner
        independently). Re-normalize in physical space so x1<x2, y1<y2 always."""
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        # Corners far outside the image: both x's clamp near the right edge, but
        # at slightly different model→physical ratios the order could flip.
        mock_api.return_value = '[{"label": "edge", "bbox": [9990, 9990, 9995, 9995]}]'
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        result = detect_elements("test")
        assert result["count"] == 1
        b = result["targets"][0]["box"]
        assert b["x1"] <= b["x2"], "box must be normalized after physical clamp"
        assert b["y1"] <= b["y2"]
        # And stayed in bounds.
        assert 0 <= b["x1"] and b["x2"] < 1920
        assert 0 <= b["y1"] and b["y2"] < 1080

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_max_targets_truncation(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        # Model returns 5 targets, but max_targets=2
        targets = [{"label": f"t{i}", "bbox": [i*10, i*10, i*10+5, i*10+5]} for i in range(5)]
        mock_api.return_value = json.dumps(targets)
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        result = detect_elements("test", max_targets=2)
        assert result["count"] == 2

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_negative_max_targets_clamped(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        mock_api.return_value = '[{"label": "x", "bbox": [1, 2, 3, 4]}]'
        mock_capture.return_value = Image.new("RGB", (1920, 1080))

        result = detect_elements("test", max_targets=-1)
        # Clamped to 1, should return 1 target
        assert result["count"] == 1

    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_image_path(self, mock_config, mock_api):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        mock_api.return_value = "[]"

        tmpdir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmpdir, "test.png")
        Image.new("RGB", (100, 100), color="red").save(tmp_path, format="PNG")

        try:
            result = detect_elements("test", image_path=tmp_path)
            assert result["count"] == 0
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    @patch("agent_windows.core.grounding.load_config")
    def test_image_path_not_found_raises(self, mock_config):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai"}
        }
        with pytest.raises(ValueError, match="not found"):
            detect_elements("test", image_path="/nonexistent/image.png")

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_output_path(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000}
        }
        mock_api.return_value = "[]"
        mock_capture.return_value = Image.new("RGB", (100, 100))

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "output.png")
            result = detect_elements("test", output_path=out_path)
            assert os.path.exists(out_path)

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_custom_prompt(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {
                "enabled": True,
                "model": "test",
                "engine": "openai",
                "width": 1000,
                "height": 1000,
                "prompts": {"detect": "CUSTOM: {description} max={max_targets}"},
            }
        }
        mock_api.return_value = "[]"
        mock_capture.return_value = Image.new("RGB", (100, 100))

        detect_elements("my target")

        args, kwargs = mock_api.call_args
        prompt = args[1]
        assert prompt.startswith("CUSTOM:")
        assert "my target" in prompt


# ── Annotation tests ──────────────────────────────────────────────────


class TestSaveDetectionScreenshot:
    def test_creates_file(self):
        img = Image.new("RGB", (200, 200))
        targets = [{"index": 1, "label": "box", "box": {"x1": 10, "y1": 10, "x2": 50, "y2": 50}}]

        tmpdir = tempfile.mkdtemp()
        try:
            out = os.path.join(tmpdir, "marked.png")
            path = _save_detection_screenshot(img, targets, output_path=out)
            assert path.exists()
            assert path.stat().st_size > 0
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_zero_targets_creates_plain_copy(self):
        img = Image.new("RGB", (100, 100), color="blue")

        tmpdir = tempfile.mkdtemp()
        try:
            out = os.path.join(tmpdir, "empty.png")
            path = _save_detection_screenshot(img, [], output_path=out)
            assert path.exists()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_multiple_targets_different_colors(self):
        img = Image.new("RGB", (300, 300))
        targets = [
            {"index": 1, "label": "a", "box": {"x1": 10, "y1": 10, "x2": 50, "y2": 50}},
            {"index": 2, "label": "b", "box": {"x1": 60, "y1": 60, "x2": 100, "y2": 100}},
        ]

        tmpdir = tempfile.mkdtemp()
        try:
            out = os.path.join(tmpdir, "multi.png")
            path = _save_detection_screenshot(img, targets, output_path=out)
            assert path.exists()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Router handler tests ──────────────────────────────────────────────


class TestDetectRouterHandler:
    def test_missing_description_raises(self):
        from agent_windows.daemon.router import HANDLERS
        handler = HANDLERS["detect"]
        with pytest.raises(ValueError, match="description"):
            handler({})

    def test_handler_output_format(self):
        from agent_windows.daemon.router import HANDLERS

        with patch("agent_windows.core.grounding.detect_elements") as mock_detect:
            mock_detect.return_value = {
                "description": "icons",
                "count": 1,
                "targets": [{"index": 1, "label": "pdf", "box": {"x1": 10, "y1": 20, "x2": 30, "y2": 40}}],
                "screenshot_path": "/tmp/test.png",
            }

            handler = HANDLERS["detect"]
            result = handler({"description": "icons"})

            assert "## Detection Result" in result["output"]
            assert "Count: 1" in result["output"]
            assert 'label="pdf"' in result["output"]
            assert result["count"] == 1
            assert len(result["targets"]) == 1

    def test_handler_zero_targets_format(self):
        from agent_windows.daemon.router import HANDLERS

        with patch("agent_windows.core.grounding.detect_elements") as mock_detect:
            mock_detect.return_value = {
                "description": "nothing",
                "count": 0,
                "targets": [],
                "screenshot_path": "/tmp/empty.png",
            }

            handler = HANDLERS["detect"]
            result = handler({"description": "nothing"})

            assert "Count: 0" in result["output"]
            assert "No targets found" in result["output"]
