"""Coordinate conversion utilities for visual grounding.

Handles conversion between physical pixel coordinates and model coordinate space,
including aspect-ratio-preserving scaling and padding offset compensation.
"""

from PIL import Image


def prepare_model_input(
    image: Image.Image,
    model_width: int = 1000,
    model_height: int = 1000,
) -> tuple[Image.Image, float, float, float, float]:
    """Resize image to model coordinate space with aspect ratio preservation.

    Pads with black bars to fill the model dimensions.

    Args:
        image: Source image (physical pixels).
        model_width: Model coordinate space width.
        model_height: Model coordinate space height.

    Returns:
        Tuple of (resized_image, scale_x, scale_y, pad_x, pad_y) where:
        - scale_x/scale_y: ratio of padded area to original dimensions
        - pad_x/pad_y: pixel offset of the padded area within the model space
    """
    src_w, src_h = image.size

    # Compute scale to fit within model dimensions
    scale = min(model_width / src_w, model_height / src_h)

    # Resized dimensions (preserving aspect ratio)
    new_w = int(src_w * scale)
    new_h = int(src_h * scale)

    # Padding offsets (center the image)
    pad_x = (model_width - new_w) // 2
    pad_y = (model_height - new_h) // 2

    # Create padded image
    # BILINEAR is sufficient for vision-model input and ~5-10x faster than LANCZOS;
    # the model's effective resolution is far below LANCZOS fidelity. (H-05)
    result = Image.new("RGB", (model_width, model_height), (0, 0, 0))
    resized = image.resize((new_w, new_h), Image.BILINEAR)
    result.paste(resized, (pad_x, pad_y))

    return result, scale, scale, pad_x, pad_y


def model_to_physical(
    mx: int,
    my: int,
    model_width: int,
    model_height: int,
    src_width: int,
    src_height: int,
) -> tuple[int, int]:
    """Convert model-space coordinates to physical pixel coordinates.

    Accounts for aspect-ratio-preserving padding.
    """
    scale = min(model_width / src_width, model_height / src_height)
    new_w = int(src_width * scale)
    new_h = int(src_height * scale)
    pad_x = (model_width - new_w) // 2
    pad_y = (model_height - new_h) // 2

    # Remove padding offset, then scale back to physical pixels
    px = int((mx - pad_x) / scale)
    py = int((my - pad_y) / scale)

    # Clamp to screen bounds
    px = max(0, min(px, src_width - 1))
    py = max(0, min(py, src_height - 1))

    return px, py


def physical_to_model(
    px: int,
    py: int,
    model_width: int,
    model_height: int,
    src_width: int,
    src_height: int,
) -> tuple[int, int]:
    """Convert physical pixel coordinates to model-space coordinates.

    This is the inverse of :func:`model_to_physical`. The grounding algorithm
    only ever converts model→physical (the model predicts coordinates, which are
    mapped back to physical pixels), so this function has **no production caller**
    today. It is kept as the symmetric partner of ``model_to_physical`` and is
    exercised by ``tests/test_grounding.py``'s round-trip tests (physical→model→
    physical) to verify the inverse is correct. If a future feature needs the
    forward direction (e.g. mapping a known physical point into model space to
    pass to a prompt), it is ready to use. (QUAL3-06)
    """
    scale = min(model_width / src_width, model_height / src_height)
    new_w = int(src_width * scale)
    new_h = int(src_height * scale)
    pad_x = (model_width - new_w) // 2
    pad_y = (model_height - new_h) // 2

    mx = int(px * scale + pad_x)
    my = int(py * scale + pad_y)

    return mx, my


def crop_around_point(
    image: Image.Image,
    cx: int,
    cy: int,
    crop_width: int,
    crop_height: int,
) -> tuple[Image.Image, int, int]:
    """Crop a region around a center point, clamping to image bounds.

    Args:
        image: Source image.
        cx, cy: Center point in image coordinates.
        crop_width, crop_height: Desired crop dimensions.

    Returns:
        Tuple of (cropped_image, crop_x, crop_y) where crop_x/y is the
        top-left corner of the crop in the original image coordinates.
    """
    src_w, src_h = image.size

    # Clamp crop size to image size
    cw = min(crop_width, src_w)
    ch = min(crop_height, src_h)

    # Compute top-left, clamped to image bounds
    crop_x = max(0, min(cx - cw // 2, src_w - cw))
    crop_y = max(0, min(cy - ch // 2, src_h - ch))

    cropped = image.crop((crop_x, crop_y, crop_x + cw, crop_y + ch))
    return cropped, crop_x, crop_y


def parse_model_coordinates(response: str) -> tuple[int, int] | None:
    """Extract (x, y) coordinates from a vision model response.

    Parsing is layered from most- to least-precise, so a model that follows the
    requested format is matched exactly, while a loose/small model that decorates
    its answer with prose still yields a sensible coordinate:

    1. **Explicit marker** — ``POINT:x,y`` / ``<coord>x,y</coord>`` /
       ``{"x":..,"y":..}`` (the formats the prompts ask for). Highest confidence:
       the marker is unambiguous, so prose numbers elsewhere are ignored.
    2. **Parenthesized** — ``(x, y)``. The parens signal "this is the coordinate".
    3. **Last comma-pair** — the final ``N, M`` in the response. Models tend to
       put the answer last ("...so the point is 500, 600"), so the *last* pair is
       a better guess than the *first* (which is often an incidental pair like
       "Section 3, Row 4").
    4. **First two integers** — final fallback for a model that emits bare
       numbers with no separators ("the point is 100 200").

    Returns:
        (x, y) tuple or None if parsing fails.
    """
    import re

    # Level 1: explicit marker. The prompts request POINT:x,y; a model honoring
    # it is unambiguous, so we trust the marker and ignore any other numbers in
    # the prose. Also accept the <coord>x,y</coord> and JSON {"x":..,"y":..}
    # forms a model might emit.
    for _pat in (
        r"POINT\s*[:=]\s*\(?\s*(\d+)\s*[,，]\s*(\d+)\s*\)?",            # POINT:500,600
        r"<coord>\s*\(?\s*(\d+)\s*[,，]\s*(\d+)\s*\)?\s*</coord>",     # <coord>..</coord>
        r'"x"\s*:\s*(\d+)\D+"y"\s*:\s*(\d+)',                          # {"x":500,"y":600}
    ):
        m = re.search(_pat, response, re.IGNORECASE)
        if m:
            return int(m.group(1)), int(m.group(2))

    # Level 2: parenthesized (x, y) — the parens mark the coordinate explicitly.
    m = re.search(r"\(\s*(\d+)\s*[,，]\s*(\d+)\s*\)", response)
    if m:
        return int(m.group(1)), int(m.group(2))

    # Level 3: the LAST "N, M" pair. Models usually emit the answer at the end
    # ("...the point is 500, 600"), and prose often contains an incidental
    # earlier pair ("Section 3, Row 4"). Taking the last pair avoids grabbing
    # that incidental one. (Chinese comma ， supported.)
    pairs = re.findall(r"(\d+)\s*[,，]\s*(\d+)", response)
    if pairs:
        return int(pairs[-1][0]), int(pairs[-1][1])

    # Level 4: first two bare integers (no separator between them).
    numbers = re.findall(r"\d+", response)
    if len(numbers) >= 2:
        return int(numbers[0]), int(numbers[1])
    return None
