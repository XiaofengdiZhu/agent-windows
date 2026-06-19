"""Tests for ask-vision command (parameter passing, no API calls)."""

import pytest
from unittest.mock import patch, MagicMock
from PIL import Image

from agent_windows.core.grounding import ask_vision


class TestAskVisionParams:
    """Test that ask_vision passes parameters correctly to the API."""

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_basic_prompt(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "gpt-4o-mini", "engine": "openai"}
        }
        mock_api.return_value = "I see a desktop with several icons."
        mock_capture.return_value = Image.new("RGB", (100, 100))

        result = ask_vision("Describe the screen")

        mock_api.assert_called_once()
        args, kwargs = mock_api.call_args
        assert args[1] == "Describe the screen"  # prompt
        assert kwargs.get("max_tokens") == 1024

        assert result["response"] == "I see a desktop with several icons."
        assert result["model_used"] == "gpt-4o-mini"

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_model_override(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "gpt-4o-mini", "engine": "openai"}
        }
        mock_api.return_value = "Response from custom model"
        mock_capture.return_value = Image.new("RGB", (100, 100))

        result = ask_vision("test prompt", model="gpt-4o")

        # Verify model override was applied
        args, kwargs = mock_api.call_args
        config_used = args[2]
        assert config_used["model"] == "gpt-4o"
        assert result["model_used"] == "gpt-4o"

    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_image_path(self, mock_config, mock_api):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "gpt-4o-mini", "engine": "openai"}
        }
        mock_api.return_value = "Image analysis"

        import tempfile, os
        # Create a small test image
        tmpdir = tempfile.mkdtemp()
        tmp_path = os.path.join(tmpdir, "test.png")
        Image.new("RGB", (100, 100), color="red").save(tmp_path, format="PNG")

        try:
            result = ask_vision("What is in this image?", image_path=tmp_path)
            # Verify the image was loaded and passed to the API
            args, kwargs = mock_api.call_args
            api_image = args[0]
            assert isinstance(api_image, Image.Image)
            assert result["response"] == "Image analysis"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    @patch("agent_windows.core.grounding.load_config")
    def test_disabled_raises(self, mock_config):
        mock_config.return_value = {
            "vlm": {"enabled": False}
        }
        with pytest.raises(RuntimeError, match="disabled"):
            ask_vision("test")

    @patch("agent_windows.core.screenshot.capture_screenshot")
    @patch("agent_windows.core.grounding.call_vision_model")
    @patch("agent_windows.core.grounding.load_config")
    def test_output_save(self, mock_config, mock_api, mock_capture):
        mock_config.return_value = {
            "vlm": {"enabled": True, "model": "gpt-4o-mini", "engine": "openai"}
        }
        mock_api.return_value = "response"
        mock_capture.return_value = Image.new("RGB", (100, 100), color="blue")

        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "output.png")
            result = ask_vision("test", output_path=out_path)
            assert os.path.exists(out_path)
            assert result["screenshot_path"] == out_path
