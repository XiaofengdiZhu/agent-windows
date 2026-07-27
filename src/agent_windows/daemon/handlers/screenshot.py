"""Screenshot command handlers: capture, highlight, diff, cleanup."""

import logging
import time
from typing import Any

from agent_windows.daemon.router import handler, require
from agent_windows.utils.debug import is_debug

logger = logging.getLogger(__name__)

# Upper bound on the number of highlight regions in one call. Each region is
# drawn onto an image.copy() of the whole screen, so a multi-thousand-region
# call pins the single STA worker in a CPU sink. (G15)
MAX_HIGHLIGHT_REGIONS = 100


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
    # needs the capture to span a KNOWN region whose top-left (origin) lets us
    # translate each box into image pixels: a bbox/selector/monitor scope →
    # capture that region with origin = its top-left; NO scope → capture the
    # FULL virtual screen with origin=(0,0). (dxcam's default no-bbox grab is
    # primary-only, which would misalign boxes on a multi-monitor desktop —
    # hence the explicit full-screen capture when no scope is given.) Annotation
    # is checked alongside the normal capture so only one capture happens.
    #
    # use_annotation may arrive as a string ("true"/"false") — some MCP clients
    # serialize bool params as strings. Raw bool("false") is True, so coerce via
    # the same truthiness rule the MCP layer uses (_call._as_bool) rather than
    # bool(), else a stringified "false" would wrongly enable annotation.
    annotate_warn = ""
    annotation_fullscreen = False
    vscreen: tuple | None = None
    # Screenshot profiling (TODO #3): gate on is_debug() so the hot path pays
    # nothing in production. capture covers the screen-grab (the dominant cost —
    # dxcam/mss/pillow vary widely); resize covers scale_screenshot. Emitted via
    # logger.info, never in the response body (mirrors the reference's approach).
    _profile = is_debug()
    _t_capture = time.perf_counter() if _profile else 0.0
    raw_annotate = params.get("use_annotation")
    do_annotate = raw_annotate is True or (
        isinstance(raw_annotate, str) and raw_annotate.lower() == "true"
    )
    if do_annotate and not (bbox or selector or monitor >= 0):
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
        # origin = top-left of the captured region in virtual-desktop coords, so
        # annotate_screenshot can translate element boxes (vdesktop coords) into
        # image pixels.
        if annotation_fullscreen:
            origin = (0, 0)
        elif bbox:
            origin = (int(bbox[0]), int(bbox[1]))
        else:
            # monitor scope — derive its top-left (capture used monitor=).
            try:
                mb = _monitor_bbox(monitor)
                origin = (mb[0], mb[1])
            except Exception:
                origin = (0, 0)
        annotated_path = annotate_screenshot(
            image, elements, grid_lines=grid, origin=origin,
        )
        # Reload the saved annotated image so downstream scale/region math uses it.
        from PIL import Image
        with Image.open(annotated_path) as ai:
            ai.load()
            image = ai.copy()
        pre_scale_size = image.size  # annotation may not change size, but be safe

    # Scale if configured. Per-call params (TODO #15b) override the config
    # values when present, so an agent can request a quick low-res capture
    # vs. the configured default in a single request. None → config wins.
    _t_resize = time.perf_counter() if _profile else 0.0
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
    if do_annotate:
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
    # Backend intentionally omitted from the markdown output — it carries no
    # decision value for the agent and stays available as a structured field
    # below for diagnostics. (P2.3)

    # Screenshot profiling (TODO #3): capture dominates (backend-dependent);
    # resize is the scale_screenshot cost. Logged only under is_debug().
    if _profile:
        _t_now = time.perf_counter()
        capture_ms = (_t_resize - _t_capture) * 1000.0
        resize_ms = (_t_now - _t_resize) * 1000.0
        logger.info(
            "screenshot profile: capture_ms=%.1f resize_ms=%.1f backend=%s "
            "annotated=%s %dx%d",
            capture_ms, resize_ms, backend, do_annotate,
            image.width, image.height,
        )

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

    from agent_windows.core.input_controller import validate_coordinates

    regions = params.get("regions", [])
    # Single-region shortcut: x1,y1,x2,y2 + label
    if not regions and "x1" in params:
        x1, y1 = validate_coordinates(require(params, "x1"), require(params, "y1"))
        x2, y2 = validate_coordinates(require(params, "x2"), require(params, "y2"))
        regions = [{
            "x1": x1, "y1": y1, "x2": x2, "y2": y2,
            "label": params.get("label", ""),
            "color": params.get("color", "red"),
        }]

    # G15: cap the region count (each region draws onto an image.copy() of the
    # whole screen — thousands pin the single STA worker in a CPU sink) and
    # validate every region's coordinates before any image work. The list path
    # used to pass regions straight through unchecked (only the single-region
    # shortcut above ran validate_coordinates).
    if len(regions) > MAX_HIGHLIGHT_REGIONS:
        raise ValueError(
            f"Too many highlight regions: {len(regions)} (max {MAX_HIGHLIGHT_REGIONS})"
        )
    for r in regions:
        validate_coordinates(r.get("x1"), r.get("y1"))
        validate_coordinates(r.get("x2"), r.get("y2"))

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

    region_lines = []
    for i, r in enumerate(regions, 1):
        label = r.get("label", "")
        tag = f' "{label}"' if label else ""
        region_lines.append(f"{i}: ({r['x1']},{r['y1']})-({r['x2']},{r['y2']}){tag}")
    return {
        "output": (f"## Highlight\nRegions: {len(regions)}\n"
                   + "\n".join(region_lines) + f"\nPath: {path}"),
        "path": str(path),
        "regions": regions,
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

    cb = result.get("changed_bbox")
    region_line = (f"Changed region: ({cb[0]},{cb[1]})-({cb[2]},{cb[3]})"
                   if cb else "Changed region: none")
    return {
        "output": (
            f"## Diff\n"
            f"Diff pixels: {result['diff_pixels']} / {result['total_pixels']} "
            f"({result['diff_percent']}%)\n"
            f"Match: {result['match']}\n"
            f"{region_line}\n"
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


@handler("find_image")
def _find_image(params: dict) -> dict[str, Any]:
    """Locate a template image on screen (or in a source screenshot).

    Deterministic, zero-API-cost alternative to ground/detect when a template
    image file is available. ``image`` is the template path; ``source``
    optionally names a haystack screenshot (default: fresh capture). ``bbox``
    is a (x1,y1,x2,y2) search ROI. ``tolerance`` 0-255 per-channel;
    ``grayscale`` fast-path; ``all`` returns every NMS-deduped match instead of
    the best. DoS caps (template <=128x128, <=100 matches) are enforced in
    core.image_match; bad params here raise ValueError -> INVALID_PARAMS.
    """
    from agent_windows.core.image_match import find_image
    from agent_windows.core.filesystem import validate_read_path

    template = require(params, "image", str)
    template = str(validate_read_path(template))

    source = params.get("source") or None
    if source:
        source = str(validate_read_path(source))

    region: tuple[int, int, int, int] | None = None
    bbox = params.get("bbox")
    if bbox:
        # bbox is a search ROI in SOURCE coordinates. The source may be a file
        # far larger than the live display, so do NOT bounds-check it against
        # the virtual screen: validate_coordinates would reject a valid ROI on
        # a 4k screenshot when the live display is 1080p, and would raise the
        # wrong code (COORDINATE_OUT_OF_RANGE -1002 instead of INVALID_PARAMS
        # -32602). The core bounds-checks region against the actual source
        # dimensions and raises a plain ValueError -> INVALID_PARAMS, which is
        # the authoritative, correctly-framed check.
        if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
            raise ValueError("bbox must be a list of 4 ints [x1,y1,x2,y2]")
        ints: list[int] = []
        for v in bbox:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError("bbox values must be integers")
            # Reject non-integral floats (e.g. 1.5) instead of silently flooring
            # them (int(1.5) == 1). Only reachable via raw JSON-RPC: the
            # launcher parses i64 and the MCP schema is list[int].
            if isinstance(v, float) and not v.is_integer():
                raise ValueError("bbox values must be integers")
            ints.append(int(v))
        x1, y1, x2, y2 = ints
        if x2 <= x1 or y2 <= y1:
            raise ValueError("bbox must have x2 > x1 and y2 > y1")
        region = (x1, y1, x2, y2)

    tolerance = params.get("tolerance", 0)
    try:
        tolerance = int(tolerance)
    except (ValueError, TypeError):
        raise ValueError("tolerance must be an integer")
    if not (0 <= tolerance <= 255):
        raise ValueError("tolerance must be in [0, 255]")

    result = find_image(
        template=template,
        source=source,
        region=region,
        tolerance=tolerance,
        grayscale=bool(params.get("grayscale", False)),
        all_matches=bool(params.get("all", False)),
    )

    matches = result["matches"]
    if matches:
        lines = ["## Find Image", f"Method: {result['method']}",
                 f"Count: {result['count']}", "Matches:"]
        for m in matches:
            lines.append(
                f"  #{m['index']}: ({m['x']}, {m['y']}) "
                f"{m['w']}x{m['h']} center=({m['center_x']}, {m['center_y']}) "
                f"score={m['score']}"
            )
        if result.get("truncated"):
            lines.append("Note: results truncated at the 100-match cap.")
        if result.get("screenshot_path"):
            lines.append(f"Screenshot: {result['screenshot_path']} (marked)")
        output = "\n".join(lines)
    else:
        output = "## Find Image\nCount: 0\nNo match found."

    return {"output": output, **result}


@handler("pixel_at")
def _pixel_at(params: dict) -> dict[str, Any]:
    """Read the color at a screen point; optionally match a target color.

    Deterministic, zero-API-cost state sensor (LED on/off, progress-bar fill).
    ``x``/``y`` are ints (virtual-screen px for a live capture, image px with
    ``source``). ``source`` optionally names a screenshot file (default: fresh
    capture). ``sample`` N takes an N×N box average (default 1).
    ``match``/``tolerance`` compare a target color (per-channel max abs diff ≤
    tolerance). ``grayscale`` compares luminance. ``output`` saves an annotated
    capture. All bad params — wrong types, out-of-bounds coords, unparseable
    match color, sample/tolerance out of range, a blacklisted path, or a missing
    source file — raise ValueError → INVALID_PARAMS (-32602). (Matches find_image:
    the path blacklist and a missing source file surface as INVALID_PARAMS here,
    not PERMISSION_DENIED/ELEMENT_NOT_FOUND.)
    """
    from agent_windows.core.pixel import pixel_at
    from agent_windows.core.filesystem import validate_read_path, validate_write_path

    x = require(params, "x", int)
    y = require(params, "y", int)

    source = params.get("source") or None
    if source:
        source = str(validate_read_path(source))

    sample = params.get("sample", 1)
    if isinstance(sample, bool) or not isinstance(sample, int):
        raise ValueError("sample must be an integer")

    match = params.get("match") or None

    tolerance = params.get("tolerance", 0)
    if isinstance(tolerance, bool) or not isinstance(tolerance, int):
        raise ValueError("tolerance must be an integer")
    if not (0 <= tolerance <= 255):
        raise ValueError("tolerance must be in [0, 255]")

    output = params.get("output") or None
    if output:
        output = str(validate_write_path(output))

    result = pixel_at(
        x=x, y=y, source=source, sample=sample, match=match,
        tolerance=tolerance, grayscale=bool(params.get("grayscale", False)),
        output=output,
    )

    lines = ["## Pixel At", f"Method: {result['method']}",
             f"Point: ({result['x']}, {result['y']})  sample={result['sample']}",
             f"Color: {result['hex']}  rgb={result['rgb']}"]
    if "match" in result:
        m = result["match"]
        lines.append(
            f"Match: target={m['target']} tolerance={m['tolerance']} "
            f"is_match={m['is_match']} max_diff={m['max_diff']} score={m['score']}"
        )
    if result.get("screenshot_path"):
        lines.append(f"Screenshot: {result['screenshot_path']} (marked)")
    return {"output": "\n".join(lines), **result}
