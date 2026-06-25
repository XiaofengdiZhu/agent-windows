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

    # TODO #14: optional annotation overlay. Detection boxes are drawn in
    # virtual-desktop SCREEN coordinates (from the UIA tree), so annotation
    # requires a capture spanning the WHOLE virtual screen — a bbox/selector/
    # monitor crop would misalign the boxes, and dxcam's default no-bbox grab
    # only covers the primary monitor. So when annotating we ignore any
    # bbox/selector/monitor (warn) and capture the full virtual-screen rect
    # explicitly. That bbox spans monitors → dxcam can't resolve it and the
    # backend chain falls through to mss/pillow, which DO use virtual-screen
    # coords. (Single-monitor setups still capture correctly.) Annotation is
    # checked BEFORE the normal capture so only one capture happens.
    annotate_warn = ""
    annotation_fullscreen = False
    vscreen: tuple | None = None
    do_annotate = bool(params.get("use_annotation"))
    if do_annotate:
        if bbox or selector or monitor >= 0:
            annotate_warn = " [annotation requires full screen; bbox/selector/monitor ignored]"
        bbox = None
        selector = ""
        monitor = -1
        vscreen = get_virtual_screen_rect()  # (left, top, width, height)
        image = capture_screenshot(
            bbox=(vscreen[0], vscreen[1],
                  vscreen[0] + vscreen[2], vscreen[1] + vscreen[3]),
        )
        annotation_fullscreen = True
    else:
        image = capture_screenshot(
            monitor=monitor,
            bbox=tuple(bbox) if bbox else None,
        )
    pre_scale_size = image.size  # to derive coord_scale below

    if do_annotate:
        from agent_windows.core.screenshot import annotate_screenshot
        from agent_windows.core.tree import get_tree_snapshot
        from agent_windows.utils.config import load_config as _load_cfg
        wgrid = int(params.get("width_reference_line", 0) or 0)
        hgrid = int(params.get("height_reference_line", 0) or 0)
        if wgrid < 0 or hgrid < 0:
            raise ValueError("width/height_reference_line must be non-negative")
        cfg = _load_cfg().get("tree", {})
        elements = get_tree_snapshot(
            max_depth=cfg.get("max_depth", 15),
            include_invisible=cfg.get("include_invisible", False),
            include_offscreen=cfg.get("include_offscreen", False),
            max_elements=cfg.get("max_elements", 200),
            timeout=cfg.get("timeout", 10),
        ).get("interactive_elements", [])
        grid = (wgrid, hgrid) if (wgrid > 0 or hgrid > 0) else None
        # annotate_screenshot saves its own copy; reassign image+path below.
        annotated_path = annotate_screenshot(image, elements, grid_lines=grid)
        # Reload the saved annotated image so downstream scale/region math uses it.
        from PIL import Image
        with Image.open(annotated_path) as ai:
            ai.load()
            image = ai.copy()
        pre_scale_size = image.size  # annotation may not change size, but be safe

    # Scale if configured. Per-call params (TODO #15b) override the config
    # values when present, so an agent can request a quick low-res capture
    # vs. the configured default in a single request. None → config wins.
    try:
        from agent_windows.utils.config import load_config
        cfg = load_config().get("screenshot", {})
        max_w = params.get("max_width", cfg.get("max_width", 0))
        max_h = params.get("max_height", cfg.get("max_height", 0))
        scale = params.get("scale", cfg.get("scale", 1.0))
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
    if annotation_fullscreen:
        # Annotation forced a full virtual-screen capture; report that truthfully
        # rather than letting the dxcam-primary branch below misreport it.
        l, t, w, h = vscreen
        region = (l, t, l + w, t + h)
    elif bbox:
        region = tuple(int(v) for v in bbox)
    elif monitor >= 0:
        try:
            region = _monitor_bbox(monitor)
        except Exception:
            region = None
    elif backend == "dxcam":
        # dxcam grabs the primary monitor only (output_idx=0) when no
        # bbox/monitor is specified; mirror that, not the full virtual screen,
        # which would mislead the agent. (A bbox/monitor is reported verbatim
        # above and now resolves to the correct per-output capture — TODO #9-A.)
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
        f"Size: {image.width}x{image.height}{selector_warn}{annotate_warn}",
    ]
    if params.get("use_annotation"):
        # Count boxes actually drawable (elements with a bbox); off-image ones
        # are skipped by annotate_screenshot, but the @eN numbering is unchanged.
        drawn = sum(1 for e in elements if getattr(e, "bbox", None))
        lines.append(f"Annotated: {drawn}/{len(elements)} element boxes"
                     + (f", grid {wgrid}x{hgrid}" if (wgrid or hgrid) else ""))
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
