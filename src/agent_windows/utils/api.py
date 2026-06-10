"""Vision model API client for visual grounding.

Supports OpenAI-compatible API (covers most providers), Anthropic, and Google Gemini.
"""

import base64
import io
import logging
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)


def encode_image(image: Image.Image, format: str = "PNG", max_size: int = 0) -> str:
    """Encode a PIL Image to base64 string.

    Args:
        image: PIL Image to encode.
        format: Image format (PNG or JPEG).
        max_size: Optional max dimension for resizing before encoding.

    Returns:
        Base64-encoded string of the image.
    """
    if max_size > 0 and (image.width > max_size or image.height > max_size):
        scale = min(max_size / image.width, max_size / image.height)
        new_size = (int(image.width * scale), int(image.height * scale))
        image = image.resize(new_size, Image.LANCZOS)

    buf = io.BytesIO()
    image.save(buf, format=format, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def call_vision_model(
    image: Image.Image,
    prompt: str,
    config: dict[str, Any],
) -> str:
    """Call a vision model API with an image and prompt.

    Args:
        image: PIL Image to send.
        prompt: Text prompt for the model.
        config: Grounding config dict (engine, model, api_base, api_key, etc.).

    Returns:
        The model's text response.

    Raises:
        RuntimeError: If the API call fails.
    """
    engine = config.get("engine", "openai").lower()

    if engine in ("openai", "doubao", "gemini_compatible"):
        return _call_openai_compatible(image, prompt, config)
    elif engine == "anthropic":
        return _call_anthropic(image, prompt, config)
    elif engine == "gemini":
        return _call_gemini(image, prompt, config)
    else:
        # Default to OpenAI-compatible for unknown engines
        return _call_openai_compatible(image, prompt, config)


def _call_openai_compatible(image: Image.Image, prompt: str, config: dict) -> str:
    """Call an OpenAI-compatible vision API."""
    from openai import OpenAI

    api_key = config.get("api_key", "") or _get_api_key_env()
    client = OpenAI(
        api_key=api_key,
        base_url=config.get("api_base", "https://api.openai.com/v1"),
        timeout=float(config.get("timeout", 30)),
    )

    b64 = encode_image(image)
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
                            "url": f"data:image/png;base64,{b64}",
                        },
                    },
                ],
            }
        ],
        max_tokens=100,
        temperature=0.0,
    )

    return response.choices[0].message.content.strip()


def _call_anthropic(image: Image.Image, prompt: str, config: dict) -> str:
    """Call Anthropic Claude vision API."""
    import anthropic

    api_key = config.get("api_key", "") or _get_api_key_env()
    client = anthropic.Anthropic(api_key=api_key, timeout=float(config.get("timeout", 30)))

    b64 = encode_image(image)
    model = config.get("model", "claude-sonnet-4-6-20250514")

    response = client.messages.create(
        model=model,
        max_tokens=100,
        temperature=0.0,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }
        ],
    )

    return response.content[0].text.strip()


def _call_gemini(image: Image.Image, prompt: str, config: dict) -> str:
    """Call Google Gemini vision API."""
    import google.generativeai as genai

    api_key = config.get("api_key", "") or _get_api_key_env()
    genai.configure(api_key=api_key)

    model_name = config.get("model", "gemini-1.5-flash")
    model = genai.GenerativeModel(model_name)
    response = model.generate_content([prompt, image],
                                       generation_config=genai.GenerationConfig(temperature=0))
    return response.text.strip()


def _get_api_key_env() -> str:
    """Get API key from environment variable."""
    import os
    return os.environ.get("AGENT_WINDOWS_GROUNDING_API_KEY", "")
