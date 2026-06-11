"""Visual coordinate grounding: two-pass crop strategy.

Implements the coordinate pipeline from the design doc:
1. Full screenshot → resize to model space → coarse locate
2. Crop around coarse point → resize to model space → fine locate
3. Drift check → final coordinate
"""

import logging
import re
import time
from pathlib import Path
from typing import Any

from PIL import Image

from agent_windows.utils.api import call_vision_model, encode_image
from agent_windows.utils.config import load_config, get_screenshot_save_dir, get_api_key
from agent_windows.utils.coordinates import (
    prepare_model_input,
    model_to_physical,
    crop_around_point,
    parse_model_coordinates,
)

logger = logging.getLogger(__name__)


def ground_element(
    description: str,
    screenshot: Image.Image | None = None,
) -> dict[str, Any]:
    """Locate an element by visual description using two-pass grounding.

    Args:
        description: Natural language description of the target element.
        screenshot: Optional PIL Image. If None, captures fresh screenshot.

    Returns:
        Dict with: coordinate (x,y), confidence, method, screenshot_path.
    """
    config = load_config()
    ground_config = config.get("grounding", {})

    if not ground_config.get("enabled", True):
        raise RuntimeError("Visual grounding is disabled in config")

    # Capture screenshot if not provided
    if screenshot is None:
        from agent_windows.core.screenshot import capture_screenshot
        screenshot = capture_screenshot()

    src_width, src_height = screenshot.size
    model_w = ground_config.get("width", 1000)
    model_h = ground_config.get("height", 1000)
    crop_w = ground_config.get("crop_size", 400)
    crop_h = ground_config.get("crop_height", 300)
    max_drift = ground_config.get("max_drift", 40)

    # Load prompts
    prompts = ground_config.get("prompts", {})
    coarse_prompt = prompts.get("coarse", _DEFAULT_COARSE_PROMPT)
    fine_prompt = prompts.get("fine", _DEFAULT_FINE_PROMPT)

    # ── Pass 1: Coarse locate ──────────────────────────────────
    logger.info("Grounding pass 1: coarse locate for '%s'", description[:50])

    model_img, *_ = prepare_model_input(screenshot, model_w, model_h)
    prompt = coarse_prompt.format(description=description)

    response1 = call_vision_model(model_img, prompt, ground_config)
    logger.debug("Coarse response: %s", response1)

    coords1 = parse_model_coordinates(response1)
    if coords1 is None:
        raise RuntimeError(f"Failed to parse coordinates from model response: {response1}")

    mx1, my1 = coords1

    # Convert to physical pixels
    px, py = model_to_physical(mx1, my1, model_w, model_h, src_width, src_height)
    logger.info("Coarse coordinate: model(%d,%d) → physical(%d,%d)", mx1, my1, px, py)

    # ── Pass 2: Fine locate ────────────────────────────────────
    logger.info("Grounding pass 2: fine locate")

    cropped, crop_x, crop_y = crop_around_point(screenshot, px, py, crop_w, crop_h)
    fine_model_img, *_ = prepare_model_input(cropped, model_w, model_h)

    fine_prompt_text = fine_prompt.format(
        description=description,
        x=px, y=py,
        cx=crop_x, cy=crop_y,
    )

    response2 = call_vision_model(fine_model_img, fine_prompt_text, ground_config)
    logger.debug("Fine response: %s", response2)

    coords2 = parse_model_coordinates(response2)
    if coords2 is None:
        logger.warning("Failed to parse fine coordinates, using coarse result")
        final_x, final_y = px, py
        confidence = "low"
        method = "visual-coarse-only"
    else:
        mx2, my2 = coords2
        # Convert fine model coords to physical (relative to crop region)
        crop_src_w, crop_src_h = cropped.size
        fine_x, fine_y = model_to_physical(mx2, my2, model_w, model_h, crop_src_w, crop_src_h)

        # Offset by crop region position
        fine_x += crop_x
        fine_y += crop_y

        # Drift check
        distance = ((fine_x - px) ** 2 + (fine_y - py) ** 2) ** 0.5
        if distance > max_drift:
            logger.warning("Drift too large (%.1f > %d), using coarse result", distance, max_drift)
            final_x, final_y = px, py
            confidence = "medium"
            method = "visual-two-pass-drift"
        else:
            final_x, final_y = fine_x, fine_y
            confidence = "high"
            method = "visual-two-pass"

    logger.info("Final coordinate: (%d, %d) [%s, %s]", final_x, final_y, confidence, method)

    # Save marked screenshot
    marked_path = _save_marked_screenshot(screenshot, final_x, final_y, description)

    return {
        "x": final_x,
        "y": final_y,
        "confidence": confidence,
        "method": method,
        "screenshot_path": str(marked_path),
    }


def _save_marked_screenshot(
    image: Image.Image,
    x: int,
    y: int,
    description: str,
) -> Path:
    """Save a copy of the screenshot with a crosshair marker at the coordinate."""
    from PIL import ImageDraw

    marked = image.copy()
    draw = ImageDraw.Draw(marked)

    # Draw crosshair
    size = 15
    draw.line([(x - size, y), (x + size, y)], fill="red", width=2)
    draw.line([(x, y - size), (x, y + size)], fill="red", width=2)
    draw.ellipse([(x - 4, y - 4), (x + 4, y + 4)], outline="red", width=2)

    save_dir = get_screenshot_save_dir()
    timestamp = int(time.time() * 1000)
    path = save_dir / f"grounding-{timestamp}.png"
    marked.save(path, format="PNG")
    return path


# ── Default prompts ───────────────────────────────────────────────

_DEFAULT_COARSE_PROMPT = (
    "Query:{description}\n"
    "Output only the coordinate of one point in your response."
)

_DEFAULT_FINE_PROMPT = (
    "Query:{description}\n"
    "The previous coarse location is ({x},{y}). Crop from ({cx},{cy}).\n"
    "Output only the coordinate of one point in your response."
)


# ── ask-vision: free-form visual Q&A ─────────────────────────────────


def ask_vision(
    prompt: str,
    image_path: str | None = None,
    model: str | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Send a screenshot (or image) with a custom prompt to a vision model.

    Unlike ground_element(), this function does no coordinate parsing — it
    returns the model's raw text response, enabling free-form visual Q&A.

    Args:
        prompt: Custom prompt sent to the vision model.
        image_path: Path to an image file. If None, captures a fresh screenshot.
        model: Override the vision model from config (optional).
        output_path: Save the screenshot/image used for the call (optional).

    Returns:
        Dict with: response (raw text), screenshot_path, model_used.
    """
    config = load_config()
    ground_config = config.get("grounding", {})

    if not ground_config.get("enabled", True):
        raise RuntimeError("Visual grounding is disabled in config")

    # Build effective config with optional model override
    effective_config = dict(ground_config)
    if model:
        effective_config["model"] = model

    # Load image from file or capture screenshot
    if image_path:
        p = Path(image_path)
        if not p.exists():
            raise ValueError(f"Image file not found: {image_path}")
        image = Image.open(p)
        # Convert to RGB if necessary (e.g. RGBA PNGs)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
    else:
        from agent_windows.core.screenshot import capture_screenshot
        image = capture_screenshot()

    # Save screenshot if requested
    saved_path = None
    if output_path:
        save_target = Path(output_path)
        save_target.parent.mkdir(parents=True, exist_ok=True)
        image.save(save_target, format="PNG")
        saved_path = str(save_target)
    elif not image_path:
        # Auto-save captured screenshot for reference
        save_dir = get_screenshot_save_dir()
        timestamp = int(time.time() * 1000)
        auto_path = save_dir / f"ask-vision-{timestamp}.png"
        image.save(auto_path, format="PNG")
        saved_path = str(auto_path)

    # Call vision model with custom prompt (higher token limit for Q&A)
    logger.info("ask-vision: sending prompt (%d chars)", len(prompt))
    response_text = call_vision_model(image, prompt, effective_config, max_tokens=1024)
    logger.debug("ask-vision response: %s", response_text[:200])

    return {
        "response": response_text,
        "screenshot_path": saved_path or "",
        "model_used": effective_config.get("model", ""),
    }
