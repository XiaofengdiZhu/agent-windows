"""Visual coordinate grounding: two-pass crop strategy.

Implements the coordinate pipeline from the design doc:
1. Full screenshot → resize to model space → coarse locate
2. Crop around coarse point → resize to model space → fine locate
3. Drift check → final coordinate

Also provides detect_elements() for multi-target bounding box detection
and ask_vision() for free-form visual Q&A.
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from PIL import Image

from agent_windows.utils.api import call_vision_model, encode_image
from agent_windows.utils.config import load_config, get_screenshot_save_dir
from agent_windows.utils.coordinates import (
    prepare_model_input,
    model_to_physical,
    crop_around_point,
    parse_model_coordinates,
)
from agent_windows.core.screenshot import save_and_cleanup

logger = logging.getLogger(__name__)


def _load_image_for_vision(image_path: str) -> Image.Image:
    """Load an image file for vision-model input, ensuring RGB/L mode."""
    from agent_windows.core.filesystem import validate_read_path

    # L-06: blacklist-check the user-supplied image path before opening or even
    # probing existence, so ask-vision/detect can't read past the filesystem
    # blacklist (and don't leak whether a sensitive path exists).
    p = validate_read_path(image_path)
    if not p.exists():
        raise ValueError(f"Image file not found: {image_path}")
    with Image.open(p) as img:
        img.load()
        if img.mode not in ("RGB", "L"):
            return img.convert("RGB")
        return img.copy()


def ground_element(
    description: str,
    screenshot: Image.Image | None = None,
    single_pass: bool | None = None,
) -> dict[str, Any]:
    """Locate an element by visual description using two-pass grounding.

    Args:
        description: Natural language description of the target element.
        screenshot: Optional PIL Image. If None, captures fresh screenshot.
        single_pass: Skip the fine-pass API call — one network round-trip instead
            of two (~2x faster, ~half the cost) at the cost of click precision.
            When ``None`` (default), reads ``grounding.single_pass`` from config;
            an explicit ``True``/``False`` overrides the config for this call. (H-07)

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

    # H-07: single_pass skips the fine API call. Default from config; a per-call
    # value (CLI --single-pass / RPC param) wins. String values are coerced so a
    # flag passed as text can't trip the bool("false") == True footgun.
    if single_pass is None:
        single_pass = bool(ground_config.get("single_pass", False))
    elif isinstance(single_pass, str):
        single_pass = single_pass.strip().lower() in ("1", "true", "yes", "on")
    else:
        single_pass = bool(single_pass)

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

    if single_pass:
        # Single-pass (H-07): skip the fine API call entirely — one network
        # round-trip instead of two. ~2x faster and ~half the cost, at the cost
        # of click precision (no sub-element refinement).
        logger.info("Single-pass mode: using coarse coordinate (fine pass skipped)")
        final_x, final_y = px, py
        confidence = "medium"
        method = "visual-single-pass"
    else:
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

    return save_and_cleanup(marked, output_path=str(path))


# ── Default prompts ───────────────────────────────────────────────

_DEFAULT_COARSE_PROMPT = (
    "Query:{description}\n"
    "Output only the coordinate of one point in your response. "
    "Coordinates are in image pixel space, (0,0) is top-left. Output as x,y integers."
)

_DEFAULT_FINE_PROMPT = (
    "Query:{description}\n"
    "The previous coarse location is ({x},{y}). Crop from ({cx},{cy}).\n"
    "Output only the coordinate of one point in your response. "
    "Coordinates are in image pixel space, (0,0) is top-left. Output as x,y integers."
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

    # L-06: validate a user-supplied output path before any image work or the
    # vision API call, so a marked screenshot can't be dropped into the install
    # tree or data dir — and a bad path fails fast instead of after a paid call.
    if output_path:
        from agent_windows.core.filesystem import validate_write_path
        output_path = str(validate_write_path(output_path))

    # Load image from file or capture screenshot
    if image_path:
        image = _load_image_for_vision(image_path)
    else:
        from agent_windows.core.screenshot import capture_screenshot
        image = capture_screenshot()

    # Save screenshot if requested
    saved_path = None
    if output_path:
        path = save_and_cleanup(image, output_path=output_path)
        saved_path = str(path)
    elif not image_path:
        # Auto-save captured screenshot for reference
        save_dir = get_screenshot_save_dir()
        timestamp = int(time.time() * 1000)
        auto_path = str(save_dir / f"ask-vision-{timestamp}.png")
        path = save_and_cleanup(image, output_path=auto_path)
        saved_path = str(path)

    # Call vision model with custom prompt (higher token limit for Q&A)
    logger.info("ask-vision: sending prompt (%d chars)", len(prompt))
    response_text = call_vision_model(image, prompt, effective_config, max_tokens=1024)
    logger.debug("ask-vision response: %s", response_text[:200])

    return {
        "response": response_text,
        "screenshot_path": saved_path or "",
        "model_used": effective_config.get("model", ""),
    }


# ── detect: multi-target bounding box detection ──────────────────────


# 8-color palette for annotation
_PALETTE = [
    "#FF0000", "#00AA00", "#0066FF", "#FF8800",
    "#AA00FF", "#00AAAA", "#FF00AA", "#888800",
]

_DEFAULT_DETECT_PROMPT = (
    "Detect all elements matching: {description}\n"
    "Return a JSON array. Each element must have:\n"
    '- "label": a short text label for the element\n'
    '- "bbox": [x1, y1, x2, y2] where (x1,y1) is top-left, (x2,y2) is bottom-right\n'
    "  Coordinates are in the image coordinate space (0,0) is top-left, "
    "width and height match the image dimensions.\n"
    "  Values are integers in pixels.\n"
    "\n"
    "Return at most {max_targets} elements.\n"
    "Return ONLY the JSON array. If no elements match, return [].\n"
    "\n"
    "Example response:\n"
    '[{{"label": "icon", "bbox": [100, 200, 150, 250]}}]'
)


def detect_elements(
    description: str,
    image_path: str | None = None,
    model: str | None = None,
    max_targets: int = 20,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Detect all visual targets matching a description, return bounding boxes.

    Args:
        description: Natural language description of targets to detect.
        image_path: Path to an image file. If None, captures a fresh screenshot.
        model: Override the vision model from config (optional).
        max_targets: Maximum number of targets to return (default 20).
        output_path: Save annotated screenshot to this path (optional).

    Returns:
        Dict with: description, count, targets (list of bounding boxes),
        screenshot_path.
    """
    config = load_config()
    ground_config = config.get("grounding", {})

    if not ground_config.get("enabled", True):
        raise RuntimeError("Visual grounding is disabled in config")

    # Validate and clamp max_targets
    max_targets = max(1, int(max_targets))

    # Build effective config with optional model override
    effective_config = dict(ground_config)
    if model:
        effective_config["model"] = model

    # L-06: validate output path before the (paid) vision API call — see
    # ask_vision.
    if output_path:
        from agent_windows.core.filesystem import validate_write_path
        output_path = str(validate_write_path(output_path))

    # Load image from file or capture screenshot
    if image_path:
        image = _load_image_for_vision(image_path)
    else:
        from agent_windows.core.screenshot import capture_screenshot
        image = capture_screenshot()

    src_width, src_height = image.size
    model_w = ground_config.get("width", 1000)
    model_h = ground_config.get("height", 1000)

    # Prepare model input
    model_img, *_ = prepare_model_input(image, model_w, model_h)

    # Build prompt
    prompts = ground_config.get("prompts", {})
    detect_prompt = prompts.get("detect", _DEFAULT_DETECT_PROMPT)
    prompt = detect_prompt.format(description=description, max_targets=max_targets)

    # Call vision model
    logger.info("detect: detecting '%s' (max %d targets)", description[:50], max_targets)
    response = call_vision_model(model_img, prompt, effective_config, max_tokens=1024)
    logger.debug("detect response: %s", response[:300])

    # Parse JSON response
    raw_targets = _parse_detection_json(response)
    logger.info("detect: parsed %d raw targets", len(raw_targets))

    # Convert coordinates for each target
    # _validate_targets already guarantees valid bbox [x1,y1,x2,y2] lists
    targets = []
    for i, raw in enumerate(raw_targets[:max_targets]):
        bbox = raw["bbox"]
        mx1, my1, mx2, my2 = (int(v) for v in bbox)

        # Auto-swap inverted boxes
        if mx2 < mx1:
            mx1, mx2 = mx2, mx1
        if my2 < my1:
            my1, my2 = my2, my1

        # Convert to physical pixels
        px1, py1 = model_to_physical(mx1, my1, model_w, model_h, src_width, src_height)
        px2, py2 = model_to_physical(mx2, my2, model_w, model_h, src_width, src_height)

        targets.append({
            "index": i + 1,
            "label": str(raw.get("label", "")),
            "box": {"x1": px1, "y1": py1, "x2": px2, "y2": py2},
        })

    # Save annotated screenshot
    marked_path = _save_detection_screenshot(image, targets, output_path)

    return {
        "description": description,
        "count": len(targets),
        "targets": targets,
        "screenshot_path": str(marked_path),
    }


def _parse_detection_json(response: str) -> list[dict]:
    """Parse detection JSON from model response with 4-level fallback.

    Levels:
    1. Direct json.loads()
    2. Extract ```json ... ``` code block
    3. Balanced bracket matching for first [...]
    4. Return empty list

    Returns:
        List of dicts, each with "label" and "bbox" keys.
        Malformed entries are silently skipped.
    """
    # Level 1: Direct parse
    try:
        result = json.loads(response)
        if isinstance(result, list):
            return _validate_targets(result)
    except (json.JSONDecodeError, ValueError):
        pass

    # Level 2: Extract code block
    code_block = re.search(r"```(?:json)?\s*\n?(.*?)\n?\s*```", response, re.DOTALL)
    if code_block:
        try:
            result = json.loads(code_block.group(1).strip())
            if isinstance(result, list):
                return _validate_targets(result)
        except (json.JSONDecodeError, ValueError):
            pass

    # Level 3: Try json.loads on substrings from [ to each successive ]
    start = response.find("[")
    if start != -1:
        pos = start
        while True:
            end = response.find("]", pos + 1)
            if end == -1:
                break
            try:
                result = json.loads(response[start : end + 1])
                if isinstance(result, list):
                    return _validate_targets(result)
            except (json.JSONDecodeError, ValueError):
                pass
            pos = end

    # Level 4: Parsing failed
    logger.warning("detect: failed to parse JSON from model response")
    return []


def _validate_targets(raw_list: list) -> list[dict]:
    """Validate and normalize parsed target list.

    Skips entries without valid "label" and "bbox" keys.
    Converts bbox values to int.
    """
    valid = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        label = item.get("label")
        bbox = item.get("bbox")
        if not isinstance(label, str):
            continue
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            continue
        try:
            bbox_int = [int(v) for v in bbox]
        except (ValueError, TypeError):
            continue
        valid.append({"label": label, "bbox": bbox_int})
    return valid


def _save_detection_screenshot(
    image: Image.Image,
    targets: list[dict],
    output_path: str | None = None,
) -> Path:
    """Save annotated screenshot with numbered bounding boxes."""
    from PIL import ImageDraw, ImageFont

    marked = image.copy()
    draw = ImageDraw.Draw(marked)

    try:
        font = ImageFont.load_default(size=14)
    except TypeError:
        font = ImageFont.load_default()

    for t in targets:
        b = t["box"]
        x1, y1, x2, y2 = b["x1"], b["y1"], b["x2"], b["y2"]
        color = _PALETTE[(t["index"] - 1) % len(_PALETTE)]

        # Draw bounding box
        draw.rectangle([(x1, y1), (x2, y2)], outline=color, width=2)

        # Draw label
        label_text = f"#{t['index']} {t['label']}".strip()
        text_pos = (x1 + 2, y1 + 2)
        text_bbox = draw.textbbox(text_pos, label_text, font=font)
        text_bg = [
            text_bbox[0] - 1,
            text_bbox[1] - 1,
            text_bbox[2] + 1,
            text_bbox[3] + 1,
        ]
        draw.rectangle(text_bg, fill="white")
        draw.text(text_pos, label_text, fill="black", font=font)

    # Save
    if output_path:
        path = str(Path(output_path).with_suffix(".png"))
        return save_and_cleanup(marked, output_path=path)

    save_dir = get_screenshot_save_dir()
    timestamp = int(time.time() * 1000)
    path = str(save_dir / f"detection-{timestamp}.png")
    return save_and_cleanup(marked, output_path=path)
