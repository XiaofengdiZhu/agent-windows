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


def snapshot_fn(display: list[int] | None = None) -> str:
    """Traverse the UIA tree and return a semantic snapshot.

    On failure returns a retryable error string instead of raising. (TODO #12)
    """
    try:
        return str(_call("snapshot", display=display).get("output", ""))
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
        "display=[0]."
    ), annotations=ToolAnnotations(title="Snapshot", readOnlyHint=True,
                                   idempotentHint=True, openWorldHint=False))
    def _snapshot(display: list[int] | None = None) -> str:
        return snapshot_fn(display=display)

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

    @mcp.tool(name="cleanup_screenshots", description=(
        "Delete old/oversized screenshots from the agent-windows data dir "
        "(~/.agent-windows/screenshots). Keywords: cleanup, delete screenshots, "
        "free disk space, clear images."
    ), annotations=ToolAnnotations(title="Cleanup Screenshots", readOnlyHint=False,
                                   idempotentHint=False, openWorldHint=False))
    def _cleanup_screenshots() -> str:
        return cleanup_screenshots_fn()
