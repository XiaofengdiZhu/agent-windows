"""Screenshot command handlers: capture, highlight, diff, cleanup."""

import logging
from typing import Any

from agent_windows.daemon.router import handler, require

logger = logging.getLogger(__name__)


@handler("screenshot")
def _screenshot(params: dict) -> dict[str, Any]:
    """Capture and save a screenshot."""
    from agent_windows.core.screenshot import (
        capture_screenshot,
        save_and_cleanup,
        scale_screenshot,
        get_capture_backend,
        get_virtual_screen_rect,
        _monitor_bbox,
    )

    output = params.get("output", "")
    monitor = params.get("monitor", -1)
    bbox = params.get("bbox")
    selector = params.get("selector", "")
    selector_warn = ""

    # L-05-W1: blacklist-check a user-supplied output path (before the expensive
    # capture) so a screenshot can't be dropped into the install tree or data dir.
    if output:
        from agent_windows.core.filesystem import validate_write_path
        output = str(validate_write_path(output))

    # UIA element selector → bbox. resolve_element owns the selector parsing +
    # snapshot lookup; this handler only adds the bbox-existence check and the
    # (left, top, right, bottom) crop list. (M-20)
    if selector and selector.startswith("@e"):
        try:
            from agent_windows.core.tree import resolve_element
            el = resolve_element(selector)
            if not el.bbox:
                raise ValueError(f"Element {selector} has no bounding box")
            bbox = [el.bbox.left, el.bbox.top, el.bbox.right, el.bbox.bottom]
        except (ValueError, IndexError) as e:
            selector_warn = f" [selector unresolved: {e}]"

    image = capture_screenshot(
        monitor=monitor,
        bbox=tuple(bbox) if bbox else None,
    )
    pre_scale_size = image.size  # to derive coord_scale below

    # Scale if configured
    try:
        from agent_windows.utils.config import load_config
        cfg = load_config().get("screenshot", {})
        max_w = cfg.get("max_width", 0)
        max_h = cfg.get("max_height", 0)
        scale = cfg.get("scale", 1.0)
        image = scale_screenshot(image, max_width=max_w, max_height=max_h, scale=scale)
    except Exception:
        logger.debug("Screenshot scaling skipped", exc_info=True)

    # coord_scale: multiplier from image pixels to screen coords. Only != 1.0
    # when scale_screenshot actually downscaled (scale_screenshot aspect-
    # preserves, so width ratio == height ratio).
    if image.size != pre_scale_size and image.width > 0:
        coord_scale = round(pre_scale_size[0] / image.width, 6)
    else:
        coord_scale = 1.0

    backend = get_capture_backend()

    # Region the capture covered, as (left, top, right, bottom) screen edges.
    if bbox:
        region = tuple(int(v) for v in bbox)
    elif monitor >= 0:
        try:
            region = _monitor_bbox(monitor)
        except Exception:
            region = None
    elif backend == "dxcam":
        # dxcam grabs the primary monitor only (output_idx=0); mirror that,
        # not the full virtual screen, which would mislead the agent.
        try:
            region = _monitor_bbox(0)
        except Exception:
            region = None
    else:
        # mss / pillow / unknown: full virtual screen. get_virtual_screen_rect
        # returns (left, top, width, height) — convert to edges.
        l, t, w, h = get_virtual_screen_rect()
        region = (l, t, l + w, t + h)

    path = save_and_cleanup(image, output_path=output if output else None)

    lines = [
        "## Screenshot",
        f"Path: {path}",
        f"Size: {image.width}x{image.height}{selector_warn}",
    ]
    if coord_scale != 1.0:
        lines.append(
            f"Coordinate Scale: {coord_scale:g} — multiply image pixel coords "
            f"by {coord_scale:g} before Click/Move/Scroll"
        )
    if region is not None:
        lines.append(f"Region: {region[0]}, {region[1]}, {region[2]}, {region[3]}")
    if backend:
        lines.append(f"Backend: {backend}")

    return {
        "output": "\n".join(lines),
        "path": str(path),
        "width": image.width,
        "height": image.height,
        "coord_scale": coord_scale,
        "backend": backend,
        "region": list(region) if region is not None else None,
    }


@handler("highlight")
def _highlight(params: dict) -> dict[str, Any]:
    """Highlight regions on a screenshot."""
    from agent_windows.core.screenshot import capture_screenshot, highlight_region

    regions = params.get("regions", [])
    # Single-region shortcut: x1,y1,x2,y2 + label
    if not regions and "x1" in params:
        from agent_windows.core.input_controller import validate_coordinates
        x1, y1 = validate_coordinates(require(params, "x1"), require(params, "y1"))
        x2, y2 = validate_coordinates(require(params, "x2"), require(params, "y2"))
        regions = [{
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "label": params.get("label", ""),
            "color": params.get("color", "red"),
        }]

    output = params.get("output", "")
    # L-05-W1: blacklist-check a user-supplied output path before any image work.
    if output:
        from agent_windows.core.filesystem import validate_write_path
        output = str(validate_write_path(output))

    image_path = params.get("image", "")
    if image_path:
        from PIL import Image
        from agent_windows.core.filesystem import validate_read_path

        # L-05: blacklist-check the user-supplied image path before opening it,
        # so highlight can't be used to read past the filesystem blacklist.
        image_path = validate_read_path(image_path)
        with Image.open(image_path) as img:
            img.load()
            image = img.copy()
    else:
        image = capture_screenshot()

    path = highlight_region(image, regions, output_path=output if output else None)

    return {
        "output": f"## Highlight\nRegions: {len(regions)}\nPath: {path}",
        "path": str(path),
        "regions": len(regions),
    }


@handler("diff")
def _diff(params: dict) -> dict[str, Any]:
    """Pixel-level diff between two screenshots."""
    from PIL import Image
    from agent_windows.core.screenshot import capture_screenshot, diff_images

    baseline_path = require(params, "baseline", str)
    compare_path = params.get("compare", "")
    threshold = params.get("threshold", 30)
    output = params.get("output", "")

    # L-05 / L-05-W1: blacklist-check the user-supplied image READ paths and the
    # output WRITE path before touching the filesystem.
    from agent_windows.core.filesystem import validate_read_path, validate_write_path
    baseline_path = validate_read_path(baseline_path)
    if compare_path:
        compare_path = validate_read_path(compare_path)
    if output:
        output = str(validate_write_path(output))

    with Image.open(baseline_path) as img:
        img.load()
        image_a = img.copy()

    if compare_path:
        with Image.open(compare_path) as img:
            img.load()
            image_b = img.copy()
    else:
        image_b = capture_screenshot()

    result = diff_images(
        image_a, image_b,
        threshold=threshold,
        output_path=output if output else None,
    )

    return {
        "output": (
            f"## Diff\n"
            f"Diff pixels: {result['diff_pixels']} / {result['total_pixels']} "
            f"({result['diff_percent']}%)\n"
            f"Match: {result['match']}\n"
            f"Path: {result['diff_path']}"
        ),
        **result,
    }


@handler("cleanup_screenshots")
def _cleanup_screenshots(params: dict) -> dict[str, Any]:
    """Force cleanup of old/oversized screenshots."""
    from agent_windows.core.screenshot import cleanup_screenshots

    stats = cleanup_screenshots(force=True)
    return {
        "output": (
            f"## Screenshot Cleanup\n"
            f"Deleted by age: {stats.get('deleted_by_age', 0)}\n"
            f"Deleted by size: {stats.get('deleted_by_size', 0)}\n"
            f"Remaining files: {stats.get('total_kept', 0)}\n"
            f"Directory size: {stats.get('dir_size_mb', 0)} MB"
        ),
        **stats,
    }
