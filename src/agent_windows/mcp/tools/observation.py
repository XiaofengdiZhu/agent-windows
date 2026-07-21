"""Observation MCP tools — screenshot, snapshot, observe, get_text,
element_state, highlight, diff, cleanup.

Each ``*_fn`` is a plain function (unit-testable) that forwards to the daemon
via ``_call``. ``register(mcp)`` decorates each as a FastMCP tool with a
description and ToolAnnotations.

The high-frequency read-only tools (screenshot/snapshot/observe) catch
exceptions and return a retryable error string instead of letting the tool call
crash — an agent can then retry or adjust. The error text carries the daemon's
underlying message (which holds the JSON-RPC code), so it's both retryable and
informative. The daemon/router and CLI ``--format json`` paths are unchanged,
so programmatic consumers still get structured error codes. (TODO #12)
"""
import logging
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp.utilities.types import Image
from mcp.types import ToolAnnotations
from pydantic import Field

from agent_windows.mcp._call import _call

logger = logging.getLogger(__name__)


def _image_from_result(result: dict[str, Any], path_key: str = "path") -> list:
    """Build a [text, Image] response from a daemon result that has a saved image.

    The daemon writes every screenshot/grounding/detection image to disk and
    returns its path; MCP clients render an attached Image, so we read those
    bytes back and wrap them. Returns just the text if no path/bytes are present.

    The path key varies by handler: ``path`` (screenshot/highlight),
    ``screenshot_path`` (ground/detect), ``diff_path`` (diff). The explicit
    path_key is checked first, then these fallbacks.
    """
    text = str(result.get("output", ""))
    path = result.get(path_key) or result.get("screenshot_path") or result.get("diff_path")
    if not path:
        return [text]
    try:
        data = Path(path).read_bytes()
    except OSError:
        return [text]
    fmt = "png" if str(path).lower().endswith(".png") else "jpeg"
    return [text, Image(data=data, format=fmt)]


# ── plain functions ────────────────────────────────────────────────────


def screenshot_fn(
    output: str | None = None,
    monitor: int = -1,
    bbox: list[int] | None = None,
    selector: str | None = None,
    display: int | list[int] | None = None,
    scale: float | None = None,
    max_width: int | None = None,
    max_height: int | None = None,
    use_annotation: bool | str | None = None,
    width_reference_line: int | None = None,
    height_reference_line: int | None = None,
) -> list:
    """Capture a screenshot. Returns the metadata text plus the image.

    On failure returns ``[error_text]`` (retryable) instead of raising, so the
    agent can recover — e.g. when the capture backend isn't ready yet. (TODO #12)

    ``scale``/``max_width``/``max_height`` (TODO #15b) override the config
    defaults for this capture only — pass ``scale=0.5`` for a quick low-res
    shot. Omit to use the configured values.

    ``display`` (TODO #4) crops to the union of the given display index(es)
    (e.g. ``0``, ``[0, 1]``), mirroring the snapshot/observe tool. It resolves
    to a bbox and overrides ``bbox`` when both are given (a bad index raises
    → INVALID_PARAMS). The conversion happens here so the daemon's screenshot
    interface is unchanged.

    ``use_annotation`` (TODO #14) overlays a numbered ``@eN`` box per
    interactive element (matching the snapshot's refs) on the capture, plus an
    optional coordinate grid. Annotation boxes use virtual-desktop screen
    coordinates, so it requires a FULL virtual-screen capture — any
    bbox/selector/monitor/display is ignored (the daemon warns inline). Omit/False for
    a clean capture. ``width_reference_line``/``height_reference_line`` draw
    that many vertical/horizontal grid lines (0 = none); only honored when
    annotating.
    """
    # display → bbox conversion (mirrors observe.py). Done before _call so the
    # daemon's screenshot handler sees only a bbox — its interface stays put.
    # display wins over an explicit bbox to match observe's "display scopes the
    # capture" semantics. A bad index raises ValueError here, IN the MCP layer
    # (not the daemon) — surfaced as a tool error. Deliberately NOT caught into
    # a retryable string: a bad index is a caller programming error, not a
    # transient failure, so "please try again" framing would mislead. (This
    # differs from snapshot_fn/observe_fn, which forward display to the daemon
    # and let it raise INVALID_PARAMS — but those tools have no in-MCP
    # conversion step.)
    if display is not None:
        from agent_windows.core.display import (
            parse_display_selection, get_display_union_bbox,
        )
        indices = parse_display_selection(display)
        if indices is not None:
            u = get_display_union_bbox(indices)
            bbox = [u.left, u.top, u.right, u.bottom]

    try:
        result = _call("screenshot", output=output, monitor=monitor,
                       bbox=bbox, selector=selector,
                       scale=scale, max_width=max_width, max_height=max_height,
                       use_annotation=use_annotation,
                       width_reference_line=width_reference_line,
                       height_reference_line=height_reference_line)
        return _image_from_result(result, "path")
    except (RuntimeError, OSError) as e:
        # RuntimeError carries the daemon's JSON-RPC error code; OSError covers
        # the Path.read_bytes failure in _image_from_result. Narrowed from a
        # bare `except Exception` so a programming error (TypeError/AttributeError
        # after a refactor) still surfaces instead of becoming a retry loop.
        logger.warning("screenshot tool failed", exc_info=True)
        return [f"Error capturing screenshot: {e}. Please try again."]


def snapshot_fn(
    display: list[int] | None = None,
    window: str | None = None,
    active: bool = False,
) -> str:
    """Traverse the UIA tree and return a semantic snapshot.

    On failure returns a retryable error string instead of raising. (TODO #12)

    ``window``/``active`` scope the snapshot to a single top-level window.
    ``window`` accepts an HWND (decimal or ``0x`` hex) or a title/process
    substring — the daemon resolves it (mirroring ``app switch``: HWND first,
    then title substring, then process name fallback).
    ``active=True`` targets the foreground window. The two are mutually
    exclusive (the daemon raises INVALID_PARAMS); omit both for the full
    desktop snapshot. ``display`` further crops by display geometry and
    composes with either scoping mode.
    """
    params: dict[str, Any] = {}
    if display is not None:
        params["display"] = display
    if window is not None:
        params["window"] = window
    if active:
        params["active"] = True
    try:
        return str(_call("snapshot", **params).get("output", ""))
    except (RuntimeError, OSError) as e:
        logger.warning("snapshot tool failed", exc_info=True)
        return f"Error capturing desktop state: {e}. Please try again."


def observe_fn(
    snapshot: bool = False,
    screenshot: bool = False,
    ground: str = "",
    display: list[int] | None = None,
) -> str:
    """Combined snapshot + screenshot (+ optional grounding). Defaults to both
    snapshot and screenshot when none are requested.

    Returns the combined text (the daemon's observe handler concatenates the
    sub-results into ``output``). The screenshot is saved to disk by the
    daemon and its path is embedded in the text; to get the image *rendered*
    by the client, call ``screenshot`` directly (its result carries the path
    as a structured field we can attach as an Image)."""
    if not snapshot and not screenshot and not ground:
        snapshot = True
        screenshot = True
    try:
        result = _call("observe", snapshot=snapshot, screenshot=screenshot,
                       ground=ground, display=display)
        return str(result.get("output", ""))
    except (RuntimeError, OSError) as e:
        logger.warning("observe tool failed", exc_info=True)
        return f"Error during observation: {e}. Please try again."


def get_text_fn(selector: str) -> str:
    """Get the text/name of a UIA element (@eN)."""
    if not selector:
        raise ValueError("selector is required")
    return str(_call("get_text", selector=selector).get("output", ""))


def element_state_fn(selector: str, check: Literal["visible", "enabled"]) -> str:
    """Check whether a UIA element (@eN) is visible or enabled."""
    if not selector:
        raise ValueError("selector is required")
    if check not in ("visible", "enabled"):
        raise ValueError("check must be 'visible' or 'enabled'")
    return str(_call("element_state", selector=selector, check=check).get("output", ""))


def highlight_fn(
    x1: int, y1: int, x2: int, y2: int,
    label: str = "", color: str = "red",
    image: str | None = None, output: str | None = None,
) -> list:
    """Highlight a region on a screenshot; returns the marked image."""
    result = _call("highlight", x1=x1, y1=y1, x2=x2, y2=y2,
                   label=label, color=color, image=image, output=output)
    return _image_from_result(result, "path")


def diff_fn(
    baseline: str, compare: str | None = None,
    threshold: int = 30, output: str | None = None,
) -> list:
    """Pixel-level diff between two screenshots; returns stats + diff image."""
    result = _call("diff", baseline=baseline, compare=compare,
                   threshold=threshold, output=output)
    return _image_from_result(result, "diff_path")


def find_image_fn(
    image: str,
    source: str | None = None,
    bbox: list[int] | None = None,
    tolerance: int = 0,
    grayscale: bool = False,
    all_matches: bool = False,
) -> list:
    """Locate a template image on screen; returns coords + a marked screenshot.

    Deterministic, no vision-model call — use when a saved image of the target
    exists; use ``ground``/``detect`` for natural-language description. Mirrors
    the daemon's find_image (image=template, source=haystack screenshot).
    """
    result = _call("find_image", image=image, source=source, bbox=bbox,
                   tolerance=tolerance, grayscale=grayscale, all=all_matches)
    return _image_from_result(result, "screenshot_path")


def pixel_at_fn(
    x: int, y: int,
    source: str | None = None,
    sample: int = 1,
    match: str | None = None,
    tolerance: int = 0,
    grayscale: bool = False,
    output: str | None = None,
) -> list:
    """Read the color at a screen point; optionally match a target color.

    Deterministic, no vision-model call. Use to judge state by color (LED on/off,
    progress-bar fill, selected swatch). Returns rgb/hex; with ``match`` also an
    is_match boolean plus per-channel max_diff and score (0-1). Keywords: pixel
    color, sample pixel, LED state, color at point, is color, progress bar fill.
    """
    result = _call("pixel_at", x=x, y=y, source=source, sample=sample,
                   match=match, tolerance=tolerance, grayscale=grayscale,
                   output=output)
    return _image_from_result(result, "screenshot_path")


def cleanup_screenshots_fn() -> str:
    """Delete old/oversized screenshots from the data dir."""
    return str(_call("cleanup_screenshots").get("output", ""))


# ── registration ───────────────────────────────────────────────────────


def register(mcp) -> None:
    @mcp.tool(name="screenshot", description=(
        "Capture a screenshot of the screen (or a region/monitor/element). "
        "Keywords: screenshot, screen capture, see screen, image, take picture. "
        "Returns the image plus metadata (path, size, region, backend, "
        "coordinate_scale when downscaled). Use monitor=-1 for full virtual "
        "desktop, or selector=@eN to crop to a UI element's bounding box. "
        "display=[0] (or [0,1] for a multi-display union) crops to the selected "
        "display(s) — same semantics as snapshot/observe. "
        "Set use_annotation=True to overlay a numbered @eN box on each "
        "interactive element (requires full screen; bbox/selector/monitor "
        "ignored) — useful for confirming a target before clicking."
    ), annotations=ToolAnnotations(title="Screenshot", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _screenshot(output: str | None = None, monitor: int = -1,
                    bbox: list[int] | None = None, selector: str | None = None,
                    display: int | list[int] | None = None,
                    scale: Annotated[float | None, Field(
                        description="Optional downscale factor for this capture only (e.g. 0.5 for half-resolution). Omit to use the config default (1.0).",
                        ge=0.0, le=1.0,
                    )] = None,
                    max_width: Annotated[int | None, Field(
                        description="Optional max width in px for this capture only; downscales (aspect-preserving) if the image is wider. Omit to use the config default.",
                        ge=0,
                    )] = None,
                    max_height: Annotated[int | None, Field(
                        description="Optional max height in px for this capture only; downscales (aspect-preserving) if the image is taller. Omit to use the config default.",
                        ge=0,
                    )] = None,
                    use_annotation: Annotated[bool | str | None, Field(
                        description="Overlay a numbered @eN box on each interactive element (matching snapshot refs). Requires full screen — bbox/selector/monitor are ignored. Pair with width/height_reference_line for a coordinate grid.",
                    )] = None,
                    width_reference_line: Annotated[int | None, Field(
                        description="With use_annotation: draw this many vertical reference lines (X% labels). 0 = none.",
                        ge=0,
                    )] = None,
                    height_reference_line: Annotated[int | None, Field(
                        description="With use_annotation: draw this many horizontal reference lines (Y% labels). 0 = none.",
                        ge=0,
                    )] = None) -> list:
        return screenshot_fn(output=output, monitor=monitor, bbox=bbox, selector=selector,
                             display=display,
                             scale=scale, max_width=max_width, max_height=max_height,
                             use_annotation=use_annotation,
                             width_reference_line=width_reference_line,
                             height_reference_line=height_reference_line)

    @mcp.tool(name="snapshot", description=(
        "Return a semantic snapshot of the UIA accessibility tree: focused "
        "window, open windows, and interactive/scrollable elements with their "
        "bounding boxes and @eN labels. Pixel-precise; use before clicking. "
        "Keywords: accessibility tree, UI elements, inspect, what's on screen, "
        "read UI, element ids. Optionally filter to specific displays, e.g. "
        "display=[0]. Use window/active to scope to one top-level window."
    ), annotations=ToolAnnotations(title="Snapshot", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _snapshot(display: list[int] | None = None,
                  window: str | None = None, active: bool = False) -> str:
        return snapshot_fn(display=display, window=window, active=active)

    @mcp.tool(name="observe", description=(
        "Combined observation: snapshot + screenshot (+ optional visual "
        "grounding), returned as text. With no flags, returns both the UIA "
        "tree and a screenshot description. "
        "Keywords: observe, look at screen, inspect desktop, snapshot, screenshot, "
        "understand UI. Pass ground='the taskbar' to also locate an element by "
        "description. For a client-rendered image, call `screenshot` directly "
        "(this tool's screenshot path is embedded in text)."
    ), annotations=ToolAnnotations(title="Observe", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _observe(snapshot: bool = False, screenshot: bool = False,
                 ground: str = "", display: list[int] | None = None) -> str:
        return observe_fn(snapshot=snapshot, screenshot=screenshot,
                          ground=ground, display=display)

    @mcp.tool(name="get_text", description=(
        "Return the text/name of a UIA element identified by its @eN selector "
        "from a prior snapshot. Keywords: read text, element text, get name, "
        "label, caption."
    ), annotations=ToolAnnotations(title="Get Text", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _get_text(selector: str) -> str:
        return get_text_fn(selector=selector)

    @mcp.tool(name="element_state", description=(
        "Check whether a UIA element (@eN) is visible or enabled. "
        "check='visible' or check='enabled'. Keywords: element state, is visible, "
        "is enabled, is disabled, control state."
    ), annotations=ToolAnnotations(title="Element State", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _element_state(selector: str, check: Literal["visible", "enabled"]) -> str:
        return element_state_fn(selector=selector, check=check)

    @mcp.tool(name="highlight", description=(
        "Draw a labeled rectangle over a region [x1,y1,x2,y2] on a screenshot "
        "and return the marked image. Keywords: highlight, mark region, draw box, "
        "annotate, confirm target. Useful for confirming a target before "
        "clicking. Optionally pass image=<path> to annotate an existing file."
    ), annotations=ToolAnnotations(title="Highlight", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _highlight(x1: int, y1: int, x2: int, y2: int,
                   label: str = "", color: str = "red",
                   image: str | None = None, output: str | None = None) -> list:
        return highlight_fn(x1=x1, y1=y1, x2=x2, y2=y2,
                            label=label, color=color, image=image, output=output)

    @mcp.tool(name="diff", description=(
        "Pixel-level diff between a baseline screenshot and another image "
        "(or a fresh capture when compare is omitted). Keywords: diff, compare "
        "images, pixel diff, what changed, visual difference. Returns diff stats "
        "and the diff image."
    ), annotations=ToolAnnotations(title="Diff", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _diff(baseline: str, compare: str | None = None,
              threshold: int = 30, output: str | None = None):
        return diff_fn(baseline=baseline, compare=compare,
                       threshold=threshold, output=output)

    @mcp.tool(name="find_image", description=(
        "Locate a template image file on screen and return click-ready "
        "coordinates (deterministic, no vision-model call). Keywords: find "
        "image, template match, locate icon, locate-on-screen, where is image. "
        "Pass image=<template path>; optional source=<haystack screenshot> "
        "(default: fresh capture), bbox=[x1,y1,x2,y2] search region, tolerance "
        "0-255 per-channel slack (0=exact), grayscale=True for a faster "
        "less-color-sensitive match, all_matches=True to return every match "
        "(NMS-deduped) instead of just the best. Use ground when you only have "
        "a description."
    ), annotations=ToolAnnotations(title="Find Image", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _find_image(image: str, source: str | None = None,
                    bbox: list[int] | None = None, tolerance: int = 0,
                    grayscale: bool = False, all_matches: bool = False) -> list:
        return find_image_fn(image=image, source=source, bbox=bbox,
                             tolerance=tolerance, grayscale=grayscale,
                             all_matches=all_matches)

    @mcp.tool(name="pixel_at", description=(
        "Read the color at a screen point [x, y] and return rgb/hex "
        "(deterministic, no vision-model call). Keywords: pixel color, sample "
        "pixel, LED state, color at point, is color, progress bar fill. Optional "
        "match=<#hex or rgb(r,g,b)> tests against a target color and returns "
        "is_match plus per-channel max_diff and score (0-1); tolerance=N gives "
        "per-channel slack (0=exact). sample=N averages an NxN box (default 1). "
        "grayscale=True reads luminance. source=<path> samples a saved file "
        "instead of the live screen. output=<path> also saves the screenshot."
    ), annotations=ToolAnnotations(title="Pixel At", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _pixel_at(x: int, y: int, source: str | None = None, sample: int = 1,
                  match: str | None = None, tolerance: int = 0,
                  grayscale: bool = False, output: str | None = None) -> list:
        return pixel_at_fn(x=x, y=y, source=source, sample=sample, match=match,
                           tolerance=tolerance, grayscale=grayscale,
                           output=output)

    @mcp.tool(name="cleanup_screenshots", description=(
        "Delete old/oversized screenshots from the agent-windows data dir "
        "(~/.agent-windows/screenshots). Keywords: cleanup, delete screenshots, "
        "free disk space, clear images."
    ), annotations=ToolAnnotations(title="Cleanup Screenshots", readOnlyHint=False,
                                   idempotentHint=False, openWorldHint=False))
    def _cleanup_screenshots() -> str:
        return cleanup_screenshots_fn()
