"""Vision model API client for visual grounding.

Supports OpenAI-compatible API (covers most providers), Anthropic, and Google Gemini.
"""

import base64
import io
import logging
import os
import threading
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

_client_cache: dict[str, Any] = {}
_client_lock = threading.Lock()


def encode_image(image: Image.Image, format: str = "JPEG", max_size: int = 0, quality: int = 85) -> str:
    """Encode a PIL Image to base64 string.

    Defaults to JPEG — a 1000x1000 frame is ~5-10x smaller than PNG while being
    visually indistinguishable to a vision model, cutting upload time and cost. (H-08)

    Args:
        image: PIL Image to encode.
        format: Image format ("JPEG" or "PNG"). Defaults to "JPEG".
        max_size: Optional max dimension for resizing before encoding.
        quality: JPEG quality (1-95). Ignored for PNG.

    Returns:
        Base64-encoded string of the image.
    """
    if max_size > 0 and (image.width > max_size or image.height > max_size):
        scale = min(max_size / image.width, max_size / image.height)
        new_size = (int(image.width * scale), int(image.height * scale))
        # BILINEAR is plenty for model input and much faster than LANCZOS. (H-05)
        image = image.resize(new_size, Image.BILINEAR)

    fmt = format.upper()
    if fmt == "JPG":
        fmt = "JPEG"

    # JPEG cannot encode alpha or palette modes — flatten to RGB first
    if fmt == "JPEG" and image.mode not in ("RGB", "L"):
        image = image.convert("RGB")

    buf = io.BytesIO()
    if fmt == "JPEG":
        image.save(buf, format="JPEG", quality=quality, optimize=True)
    else:
        image.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _resolve_image_format(config: dict[str, Any]) -> tuple[str, int]:
    """Resolve the vision API image format and JPEG quality from vlm config.

    Returns:
        (format, quality) where format is "JPEG" or "PNG".
    """
    image_format = str(config.get("image_format", "JPEG")).upper()
    if image_format == "JPG":
        image_format = "JPEG"
    if image_format not in ("JPEG", "PNG"):
        image_format = "JPEG"
    quality = int(config.get("jpeg_quality", 85))
    return image_format, quality


def call_vision_model(
    image: Image.Image,
    prompt: str,
    config: dict[str, Any],
    max_tokens: int = 100,
) -> str:
    """Call a vision model API with an image and prompt.

    Args:
        image: PIL Image to send.
        prompt: Text prompt for the model.
        config: Vision model config dict (engine, model, api_base, api_key, etc.).
        max_tokens: Maximum tokens in the model response (default 100 for grounding).

    Returns:
        The model's text response.

    Raises:
        RuntimeError: If the API call fails.
    """
    engine = config.get("engine", "openai").lower()

    # Resolve the API key ONCE here. The production caller passes the vlm
    # sub-config from load_config(), which already merges the env-var override
    # (config.py:183-186); for raw-config callers (tests), fall back to the env
    # var directly. This removes the per-backend duplication of the same
    # `config.get("api_key") or env` resolution.
    api_key = config.get("api_key") or os.environ.get("AGENT_WINDOWS_VLM_API_KEY", "")

    if engine in ("openai", "doubao", "gemini_compatible"):
        return _call_openai_compatible(image, prompt, config, api_key, max_tokens)
    elif engine == "anthropic":
        return _call_anthropic(image, prompt, config, api_key, max_tokens)
    elif engine == "gemini":
        return _call_gemini(image, prompt, config, api_key, max_tokens)
    else:
        # Default to OpenAI-compatible for unknown engines
        return _call_openai_compatible(image, prompt, config, api_key, max_tokens)


def _call_openai_compatible(image: Image.Image, prompt: str, config: dict[str, Any], api_key: str, max_tokens: int = 100) -> str:
    """Call an OpenAI-compatible vision API."""
    api_base = config.get("api_base", "https://api.openai.com/v1")
    cache_key = f"openai:{api_base}:{api_key}"
    with _client_lock:
        if cache_key not in _client_cache:
            from openai import OpenAI
            _client_cache[cache_key] = OpenAI(
                api_key=api_key,
                base_url=api_base,
                timeout=float(config.get("timeout", 30)),
            )
        client = _client_cache[cache_key]

    image_format, jpeg_quality = _resolve_image_format(config)
    b64 = encode_image(image, format=image_format, quality=jpeg_quality)
    model = config.get("model", "gpt-4o-mini")

    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{image_format.lower()};base64,{b64}",
                        },
                    },
                ],
            }
        ],
        max_tokens=max_tokens,
        temperature=0.0,
    )

    return response.choices[0].message.content.strip()


def _call_anthropic(image: Image.Image, prompt: str, config: dict[str, Any], api_key: str, max_tokens: int = 100) -> str:
    """Call Anthropic Claude vision API."""
    cache_key = f"anthropic:{api_key}"
    with _client_lock:
        if cache_key not in _client_cache:
            import anthropic
            _client_cache[cache_key] = anthropic.Anthropic(api_key=api_key, timeout=float(config.get("timeout", 30)))
        client = _client_cache[cache_key]

    image_format, jpeg_quality = _resolve_image_format(config)
    b64 = encode_image(image, format=image_format, quality=jpeg_quality)
    model = config.get("model", "claude-sonnet-4-20250514")

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=0.0,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": f"image/{image_format.lower()}",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    return response.content[0].text.strip()


def _call_gemini(image: Image.Image, prompt: str, config: dict[str, Any], api_key: str, max_tokens: int = 100) -> str:
    """Call Google Gemini vision API via the new google-genai SDK.

    Unlike the deprecated ``google.generativeai`` (which set credentials via a
    process-global ``genai.configure(api_key=...)``), the new SDK's
    ``genai.Client(api_key=...)`` binds credentials into the client OBJECT — so
    caching the client isolates per-key state with no global mutation. This
    matches the openai/anthropic callers and closes the multi-key/multi-thread
    race where two concurrent ``configure()`` calls would overwrite each other's
    global default credentials (D1). The client is model-agnostic, so the model
    name is passed per-call (not baked into the cache key).
    """
    from google import genai
    from google.genai import types

    # Cache key is api_key only: the Client is not bound to a model (unlike the
    # old GenerativeModel), so changing vlm.model reuses the same client and just
    # passes the new name to generate_content. Mirrors openai/anthropic keying.
    cache_key = f"gemini:{api_key}"
    with _client_lock:
        if cache_key not in _client_cache:
            _client_cache[cache_key] = genai.Client(api_key=api_key)
        client = _client_cache[cache_key]

    model_name = config.get("model", "gemini-2.0-flash")
    response = client.models.generate_content(
        model=model_name,
        contents=[prompt, image],
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=max_tokens,
        ),
    )
    return response.text.strip()
