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
    return base64.b64encode(
        encode_image_bytes(image, format=format, max_size=max_size, quality=quality)
    ).decode("utf-8")


def encode_image_bytes(image: Image.Image, format: str = "JPEG", max_size: int = 0, quality: int = 85) -> bytes:
    """Encode a PIL Image to RAW (un-base64'd) bytes.

    Shares the exact resize/flatten/save logic with :func:`encode_image`; the only
    difference is the final base64 step is skipped. Needed by the Gemini backend,
    which sends image bytes via ``types.Part.from_bytes`` (inline_data) rather than
    a base64 data URL — so Gemini benefits from the same JPEG compression / user
    ``image_format`` & ``jpeg_quality`` config as the OpenAI/Anthropic backends.
    (G19a — previously Gemini sent the uncompressed PIL image directly.)

    Args:
        image: PIL Image to encode.
        format: Image format ("JPEG" or "PNG"). Defaults to "JPEG".
        max_size: Optional max dimension for resizing before encoding.
        quality: JPEG quality (1-95). Ignored for PNG.

    Returns:
        Raw image bytes (e.g. a JPEG byte string starting with ``\\xff\\xd8``).
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
    return buf.getvalue()


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


def _extract_text(text: str | None, empty_message: str) -> str:
    """Strip a vision-model response, raising RuntimeError on empty/None.

    The project supports small models that can emit empty or content-filtered
    responses. Each backend used to index/strip the raw response with no guard,
    so a None/empty content crashed with the SDK's native exception
    (AttributeError / IndexError / ValueError) instead of the docstring-promised
    ``RuntimeError``. On the daemon side that also mis-mapped the error code (a
    Gemini ValueError / Anthropic IndexError landed in the INVALID_PARAMS clause
    rather than the generic internal error). Centralizing the guard keeps the
    three backends consistent and guarantees ``call_vision_model`` honors its
    "Raises: RuntimeError" contract. (G18)
    """
    content = (text or "").strip()
    if not content:
        raise RuntimeError(empty_message)
    return content


def _resolve_timeout(config: dict[str, Any]) -> float:
    """Resolve the vision-model client timeout (seconds), floored at 1s.

    The config default is 30s. A 0/negative value would be a misconfiguration
    that, on Gemini, becomes ``HttpOptions(timeout=0)`` (0 ms → every request
    times out instantly), so floor it. (G19b)
    """
    return max(float(config.get("timeout", 30)), 1.0)


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
    timeout = _resolve_timeout(config)
    # timeout is baked into the cached OpenAI client, so it must be part of the
    # cache key — otherwise `config set vlm.timeout` is swallowed by a stale
    # cached client on a long-running daemon. Use int(seconds) so the key is a
    # stable identifier (30, not 30.0) regardless of how the user wrote the value.
    # (G19b)
    cache_key = f"openai:{api_base}:{api_key}:{int(timeout)}"
    with _client_lock:
        if cache_key not in _client_cache:
            from openai import OpenAI
            _client_cache[cache_key] = OpenAI(
                api_key=api_key,
                base_url=api_base,
                timeout=timeout,
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

    # choices can be [] when an OpenAI-compatible provider filters the response
    # or returns only a usage record — guard the index so a missing choice is the
    # same RuntimeError class as a missing message, not an IndexError. (G18)
    choices = response.choices or []
    content = choices[0].message.content if choices else None
    return _extract_text(
        content,
        "OpenAI-compatible model returned an empty response"
        " (content may have been filtered, the model emitted only a tool call,"
        " or the provider returned no choices)",
    )


def _call_anthropic(image: Image.Image, prompt: str, config: dict[str, Any], api_key: str, max_tokens: int = 100) -> str:
    """Call Anthropic Claude vision API."""
    timeout = _resolve_timeout(config)
    # timeout is baked into the cached Anthropic client — include it in the key
    # so a config change isn't swallowed by the stale cached client. int() keeps
    # the key stable (30, not 30.0). (G19b)
    cache_key = f"anthropic:{api_key}:{int(timeout)}"
    with _client_lock:
        if cache_key not in _client_cache:
            import anthropic
            _client_cache[cache_key] = anthropic.Anthropic(api_key=api_key, timeout=timeout)
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

    # content can be [] when the response is filtered (finish_reason "content_
    # filter"); guard the index + extract. (G18)
    parts = response.content or []
    text = parts[0].text if parts else None
    return _extract_text(
        text,
        "Anthropic model returned an empty response"
        " (content may have been filtered or is non-text)",
    )


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

    # Cache key is api_key + timeout: the Client is not bound to a model (unlike
    # the old GenerativeModel), so changing vlm.model reuses the same client and
    # just passes the new name to generate_content. timeout IS bound into the
    # client (via http_options below), so it must be in the key too — otherwise a
    # `config set vlm.timeout` is swallowed by the stale cached client. int()
    # keeps the key stable (30, not 30.0). (G19b)
    timeout = _resolve_timeout(config)
    cache_key = f"gemini:{api_key}:{int(timeout)}"
    with _client_lock:
        if cache_key not in _client_cache:
            # genai.Client has no top-level timeout kwarg; the client-side request
            # timeout is set via HttpOptions. Unlike OpenAI/Anthropic (seconds),
            # HttpOptions.timeout is MILLISECONDS, so convert the project's
            # seconds-based vlm.timeout. Without this a hung Gemini call has no
            # client timeout and can only be saved by the IPC read_timeout. (G19b)
            _client_cache[cache_key] = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=int(timeout * 1000)),
            )
        client = _client_cache[cache_key]

    # Encode the image the same way the other backends do (JPEG compression +
    # user image_format/quality) and send it as inline bytes via Part.from_bytes.
    # Previously the raw PIL image was passed straight to generate_content, so
    # H-05/H-08 (resize/JPEG) were lost and the user's quality setting ignored.
    # (G19a)
    image_format, jpeg_quality = _resolve_image_format(config)
    image_bytes = encode_image_bytes(image, format=image_format, quality=jpeg_quality)
    image_part = types.Part.from_bytes(
        data=image_bytes, mime_type=f"image/{image_format.lower()}")

    model_name = config.get("model", "gemini-2.0-flash")
    response = client.models.generate_content(
        model=model_name,
        contents=[prompt, image_part],
        config=types.GenerateContentConfig(
            temperature=0,
            max_output_tokens=max_tokens,
        ),
    )
    # response.text raises ValueError when there are no text parts (e.g. all
    # candidates blocked by safety filters); translate that and an empty result
    # into the docstring-promised RuntimeError. (G18)
    try:
        text = response.text
    except ValueError:
        text = None
    return _extract_text(
        text,
        "Gemini model returned an empty response"
        " (the response may have been blocked by safety filters)",
    )
