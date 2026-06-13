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
    """Convert physical pixel coordinates to model-space coordinates."""
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

    Tries structured patterns first (parenthesized coordinates),
    then falls back to first-two-numbers heuristic.

    Returns:
        (x, y) tuple or None if parsing fails.
    """
    import re

    # Try structured patterns first: (x, y) or (x,y) or x,y
    structured = re.search(r"\(?\s*(\d+)\s*[,，]\s*(\d+)\s*\)?", response)
    if structured:
        return int(structured.group(1)), int(structured.group(2))

    # Fallback: first two numbers in the response
    numbers = re.findall(r"\d+", response)
    if len(numbers) >= 2:
        return int(numbers[0]), int(numbers[1])
    return None
