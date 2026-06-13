"""Tests for vision API client: image encoding and format resolution."""

import base64
import io

from PIL import Image

from agent_windows.utils.api import encode_image, _resolve_image_format


def _decode(b64: str) -> Image.Image:
    """Decode a base64 image string; exposes .format for assertions."""
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw))
    img.load()
    return img


class TestEncodeImage:
    def test_default_format_is_jpeg(self):
        """H-08: encode_image defaults to JPEG (was PNG)."""
        img = Image.new("RGB", (100, 100), (128, 64, 200))
        assert _decode(encode_image(img)).format == "JPEG"

    def test_png_when_requested(self):
        img = Image.new("RGB", (100, 100), (10, 20, 30))
        assert _decode(encode_image(img, format="PNG")).format == "PNG"

    def test_rgba_flattened_for_jpeg(self):
        """JPEG can't carry alpha — encode_image must flatten without error."""
        img = Image.new("RGBA", (40, 40), (255, 0, 0, 128))
        decoded = _decode(encode_image(img, format="JPEG"))
        assert decoded.mode == "RGB"
        assert decoded.format == "JPEG"


class TestResolveImageFormat:
    def test_default_jpeg(self):
        assert _resolve_image_format({}) == ("JPEG", 85)

    def test_png_passthrough(self):
        assert _resolve_image_format({"image_format": "png"})[0] == "PNG"

    def test_invalid_falls_back_to_jpeg(self):
        assert _resolve_image_format({"image_format": "webp"})[0] == "JPEG"
