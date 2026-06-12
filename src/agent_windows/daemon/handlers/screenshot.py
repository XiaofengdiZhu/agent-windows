"""Screenshot command handlers: capture, highlight, diff, cleanup."""

from typing import Any

from agent_windows.daemon.router import handler


@handler("screenshot")
def _screenshot(params: dict) -> dict[str, Any]:
    """Capture and save a screenshot."""
    from agent_windows.core.screenshot import capture_screenshot, save_and_cleanup, scale_screenshot

    output = params.get("output", "")
    monitor = params.get("monitor", -1)
    bbox = params.get("bbox")
    selector = params.get("selector", "")
    selector_warn = ""

    # UIA element selector → bbox
    if selector and selector.startswith("@e"):
        try:
            index = int(selector[2:]) - 1  # @e{N} is 1-based
            if index < 0:
                raise IndexError(f"Invalid selector: {selector}")
            from agent_windows.core.tree import get_tree_snapshot
            from agent_windows.utils.config import load_config as _lc
            cfg = _lc().get("tree", {})
            data = get_tree_snapshot(
                max_depth=cfg.get("max_depth", 30),
                include_invisible=cfg.get("include_invisible", False),
                include_offscreen=cfg.get("include_offscreen", False),
            )
            elements = data.get("interactive_elements", [])
            if index >= len(elements):
                raise IndexError(f"Element {selector} not found ({len(elements)} elements)")
            el = elements[index]
            if not el.bbox:
                raise ValueError(f"Element {selector} has no bounding box")
            bbox = [el.bbox.left, el.bbox.top, el.bbox.right, el.bbox.bottom]
        except (ValueError, IndexError) as e:
            selector_warn = f" [selector unresolved: {e}]"

    image = capture_screenshot(
        monitor=monitor,
        bbox=tuple(bbox) if bbox else None,
    )

    # Scale if configured
    try:
        from agent_windows.utils.config import load_config
        cfg = load_config().get("screenshot", {})
        max_w = cfg.get("max_width", 0)
        max_h = cfg.get("max_height", 0)
        scale = cfg.get("scale", 1.0)
        image = scale_screenshot(image, max_width=max_w, max_height=max_h, scale=scale)
    except Exception:
        pass

    path = save_and_cleanup(image, output_path=output if output else None)
    return {
        "output": f"## Screenshot\nPath: {path}\nSize: {image.width}x{image.height}{selector_warn}",
        "path": str(path),
        "width": image.width,
        "height": image.height,
    }


@handler("highlight")
def _highlight(params: dict) -> dict[str, Any]:
    """Highlight regions on a screenshot."""
    from agent_windows.core.screenshot import capture_screenshot, highlight_region

    regions = params.get("regions", [])
    # Single-region shortcut: x1,y1,x2,y2 + label
    if not regions and "x1" in params:
        regions = [{
            "x1": params["x1"], "y1": params["y1"],
            "x2": params["x2"], "y2": params["y2"],
            "label": params.get("label", ""),
            "color": params.get("color", "red"),
        }]

    image_path = params.get("image", "")
    if image_path:
        from PIL import Image
        with Image.open(image_path) as img:
            img.load()
            image = img.copy()
    else:
        image = capture_screenshot()

    output = params.get("output", "")
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

    baseline_path = params["baseline"]
    compare_path = params.get("compare", "")
    threshold = params.get("threshold", 30)
    output = params.get("output", "")

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
