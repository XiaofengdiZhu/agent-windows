"""Observe commands: observe, snapshot, screenshot, ground, ask-vision,
detect, find-image, highlight, diff, get-text, is, cleanup."""

import typer

from agent_windows.cli.dispatcher import _cmd
# parse_coord strips the negative-coordinate marker (C1) added by argv
# preprocessing — see cli/commands/input.py for why this exists.
from agent_windows.cli.commands.input import parse_coord

app = typer.Typer(help="Observation commands")


@app.command()
def observe(
    ctx: typer.Context,
    snapshot: bool = typer.Option(False, "--snapshot", help="Include UIA tree snapshot"),
    screenshot: bool = typer.Option(False, "--screenshot", help="Include screenshot"),
    ground: str = typer.Option("", "--ground", help="Visual grounding with element description"),
    display: list[int] = typer.Option(
        None, "--display", "-d",
        help="Limit to display index(es), e.g. -d 0 -d 1 (repeatable)"),
) -> None:
    """Comprehensive observation: combines snapshot, screenshot, and/or grounding."""
    # Default: snapshot + screenshot when no flags specified
    if not snapshot and not screenshot and not ground:
        snapshot = True
        screenshot = True
    params: dict = {"snapshot": snapshot, "screenshot": screenshot, "ground": ground}
    if display:
        params["display"] = display
    _cmd(ctx, "observe", **params)


@app.command()
def snapshot(
    ctx: typer.Context,
    display: list[int] = typer.Option(
        None, "--display", "-d",
        help="Limit to display index(es), e.g. -d 0 -d 1 (repeatable)"),
) -> None:
    """UIA accessibility tree traversal, returns semantic text."""
    params: dict = {}
    if display:
        params["display"] = display
    _cmd(ctx, "snapshot", **params)


@app.command()
def screenshot(
    ctx: typer.Context,
    output: str = typer.Option("", "--output", "-o", help="Save screenshot to path"),
    monitor: int = typer.Option(-1, "--monitor", help="Monitor index (-1 for all)"),
    selector: str = typer.Option("", "--selector", "-s", help="UIA element ref (e.g. @e3)"),
    bbox: str = typer.Option("", "--bbox", help="Crop region: x1,y1,x2,y2"),
    annotate: bool = typer.Option(
        False, "--annotate",
        help="Overlay numbered detection boxes (@eN) for each interactive element "
             "(requires full screen; ignores --bbox/--selector/--monitor)"),
    grid_w: int = typer.Option(
        0, "--grid-w",
        help="With --annotate: draw N vertical reference lines (X%% labels)"),
    grid_h: int = typer.Option(
        0, "--grid-h",
        help="With --annotate: draw N horizontal reference lines (Y%% labels)"),
) -> None:
    """Capture screen screenshot, optionally cropped to a region or UIA element."""
    params: dict = {"output": output, "monitor": monitor}
    if selector:
        params["selector"] = selector
    if bbox:
        params["bbox"] = [int(v) for v in bbox.split(",")]
    if annotate:
        params["use_annotation"] = True
        if grid_w:
            params["width_reference_line"] = grid_w
        if grid_h:
            params["height_reference_line"] = grid_h
    _cmd(ctx, "screenshot", **params)


@app.command()
def ground(
    ctx: typer.Context,
    description: str = typer.Argument(help="Element description to locate"),
    single_pass: bool = typer.Option(
        False,
        "--single-pass",
        help="Single-pass (coarse only): one API call, ~2x faster, lower precision. "
             "Overrides config grounding.single_pass for this call.",
    ),
) -> None:
    """Visual model grounding: locate element coordinates."""
    params = {"description": description}
    if single_pass:
        params["single_pass"] = True
    _cmd(ctx, "ground", **params)


@app.command(name="ask-vision")
def ask_vision(
    ctx: typer.Context,
    prompt: str = typer.Argument(help="Custom prompt for the vision model"),
    image: str = typer.Option("", "--image", "-i", help="Path to image file (default: screenshot)"),
    model: str = typer.Option("", "--model", "-m", help="Override vision model"),
    output: str = typer.Option("", "--output", "-o", help="Save screenshot to path"),
) -> None:
    """Free-form visual Q&A: screenshot + custom prompt → raw model response."""
    params = {"prompt": prompt}
    if image:
        params["image"] = image
    if model:
        params["model"] = model
    if output:
        params["output"] = output
    _cmd(ctx, "ask-vision", **params)


@app.command()
def detect(
    ctx: typer.Context,
    description: str = typer.Argument(help="Description of targets to detect"),
    image: str = typer.Option("", "--image", "-i", help="Path to image file (default: screenshot)"),
    model: str = typer.Option("", "--model", "-m", help="Override vision model"),
    max_targets: int = typer.Option(20, "--max-targets", help="Maximum number of targets to return"),
    output: str = typer.Option("", "--output", "-o", help="Save annotated screenshot to path"),
) -> None:
    """Detect multiple visual targets, returning bounding boxes."""
    params: dict = {"description": description, "max_targets": max_targets}
    if image:
        params["image"] = image
    if model:
        params["model"] = model
    if output:
        params["output"] = output
    _cmd(ctx, "detect", **params)


@app.command(name="find-image")
def find_image(
    ctx: typer.Context,
    template: str = typer.Argument(help="Path to the template image to locate"),
    in_: str = typer.Option("", "--in", help="Source screenshot path (default: fresh capture)"),
    bbox: str = typer.Option("", "--bbox", help="Restrict search to region x1,y1,x2,y2"),
    tolerance: int = typer.Option(0, "--tolerance", "-t", help="Per-channel max diff 0-255 (0=exact)"),
    grayscale: bool = typer.Option(False, "--grayscale", help="Match in grayscale (faster)"),
    all_: bool = typer.Option(False, "--all", help="Return all matches, not just the best"),
) -> None:
    """Find a template image on screen (deterministic, no vision-model call).

    Use when you have a saved image of the target (e.g. an icon); use ``ground``
    when you only have a description. Returns click-ready coordinates.
    """
    params: dict = {"image": template, "tolerance": tolerance}
    if in_:
        params["source"] = in_
    if bbox:
        params["bbox"] = [int(v) for v in bbox.split(",")]
    if grayscale:
        params["grayscale"] = True
    if all_:
        params["all"] = True
    _cmd(ctx, "find_image", **params)


@app.command(name="pixel-at")
def pixel_at(
    ctx: typer.Context,
    x: int = typer.Argument(parser=parse_coord, help="X (virtual-screen px; image px with --in)"),
    y: int = typer.Argument(parser=parse_coord, help="Y"),
    in_: str = typer.Option("", "--in", help="Source image path (default: fresh capture)"),
    sample: int = typer.Option(1, "--sample", help="Sample N×N box, averaged (default 1)"),
    match: str = typer.Option("", "--match", help="Target color, e.g. #RRGGBB / rgb(r,g,b)"),
    tolerance: int = typer.Option(0, "--tolerance", "-t", help="Per-channel max diff 0-255 (0=exact)"),
    grayscale: bool = typer.Option(False, "--grayscale", help="Compare luminance"),
    output: str = typer.Option("", "--output", "-o", help="Save annotated capture to path"),
) -> None:
    """Read the color at a screen point; optionally match a target color.

    Deterministic, no vision-model call. Use to judge state by color (LED on/off,
    progress-bar fill). With --match, returns is_match within --tolerance.
    """
    params: dict = {"x": x, "y": y, "sample": sample, "tolerance": tolerance}
    if in_:
        params["source"] = in_
    if match:
        params["match"] = match
    if grayscale:
        params["grayscale"] = True
    if output:
        params["output"] = output
    _cmd(ctx, "pixel_at", **params)


@app.command()
def cleanup(ctx: typer.Context) -> None:
    """Clean up old/oversized screenshots based on age and size limits."""
    _cmd(ctx, "cleanup_screenshots")


@app.command()
def highlight(
    ctx: typer.Context,
    x1: int = typer.Argument(parser=parse_coord, help="Left"),
    y1: int = typer.Argument(parser=parse_coord, help="Top"),
    x2: int = typer.Argument(parser=parse_coord, help="Right"),
    y2: int = typer.Argument(parser=parse_coord, help="Bottom"),
    label: str = typer.Option("", "--label", "-l", help="Label text"),
    color: str = typer.Option("red", "--color", "-c", help="Highlight color"),
    image: str = typer.Option("", "--image", "-i", help="Source image (default: fresh screenshot)"),
    output: str = typer.Option("", "--output", "-o", help="Save to path"),
) -> None:
    """Highlight a region on a screenshot with border and optional label."""
    params: dict = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    if label:
        params["label"] = label
    if color != "red":
        params["color"] = color
    if image:
        params["image"] = image
    if output:
        params["output"] = output
    _cmd(ctx, "highlight", **params)


@app.command(name="diff")
def diff_screenshot(
    ctx: typer.Context,
    baseline: str = typer.Argument(help="Baseline image path"),
    compare: str = typer.Option("", "--compare", "-c", help="Compare image (default: fresh screenshot)"),
    threshold: int = typer.Option(30, "--threshold", "-t", help="Pixel diff threshold (0-255)"),
    output: str = typer.Option("", "--output", "-o", help="Save diff image to path"),
) -> None:
    """Pixel-level diff between two screenshots."""
    params: dict = {"baseline": baseline, "threshold": threshold}
    if compare:
        params["compare"] = compare
    if output:
        params["output"] = output
    _cmd(ctx, "diff", **params)


@app.command(name="get-text")
def get_text(
    ctx: typer.Context,
    selector: str = typer.Argument(help="UIA element ref (e.g. @e3)"),
) -> None:
    """Get text content of a UIA element."""
    _cmd(ctx, "get_text", selector=selector)


# ── is sub-command group ────────────────────────────────────────────

is_cmd = typer.Typer(help="Check element state")


@is_cmd.command("visible")
def is_visible(
    ctx: typer.Context,
    selector: str = typer.Argument(help="UIA element ref (e.g. @e3)"),
) -> None:
    """Check if a UIA element is visible (not offscreen)."""
    _cmd(ctx, "element_state", selector=selector, check="visible")


@is_cmd.command("enabled")
def is_enabled(
    ctx: typer.Context,
    selector: str = typer.Argument(help="UIA element ref (e.g. @e3)"),
) -> None:
    """Check if a UIA element is enabled."""
    _cmd(ctx, "element_state", selector=selector, check="enabled")
