"""Observe commands: observe, snapshot, screenshot, ground."""

import typer

from agent_windows.cli.dispatcher import dispatch, print_result

app = typer.Typer(help="Observation commands")


@app.command()
def observe(
    ctx: typer.Context,
    snapshot: bool = typer.Option(False, "--snapshot", help="Include UIA tree snapshot"),
    screenshot: bool = typer.Option(False, "--screenshot", help="Include screenshot"),
    ground: str = typer.Option("", "--ground", help="Visual grounding with element description"),
) -> None:
    """Comprehensive observation: combines snapshot, screenshot, and/or grounding."""
    # Default: snapshot + screenshot when no flags specified
    if not snapshot and not screenshot and not ground:
        snapshot = True
        screenshot = True
    params = {"snapshot": snapshot, "screenshot": screenshot, "ground": ground}
    result = dispatch("observe", params, ctx)
    print_result(result, ctx)


@app.command()
def snapshot(ctx: typer.Context) -> None:
    """UIA accessibility tree traversal, returns semantic text."""
    result = dispatch("snapshot", {}, ctx)
    print_result(result, ctx)


@app.command()
def screenshot(
    ctx: typer.Context,
    output: str = typer.Option("", "--output", "-o", help="Save screenshot to path"),
    monitor: int = typer.Option(-1, "--monitor", help="Monitor index (-1 for all)"),
    selector: str = typer.Option("", "--selector", "-s", help="UIA element ref (e.g. @e3)"),
    bbox: str = typer.Option("", "--bbox", help="Crop region: x1,y1,x2,y2"),
) -> None:
    """Capture screen screenshot, optionally cropped to a region or UIA element."""
    params: dict = {"output": output, "monitor": monitor}
    if selector:
        params["selector"] = selector
    if bbox:
        params["bbox"] = [int(v) for v in bbox.split(",")]
    result = dispatch("screenshot", params, ctx)
    print_result(result, ctx)


@app.command()
def ground(
    ctx: typer.Context,
    description: str = typer.Argument(help="Element description to locate"),
) -> None:
    """Visual model grounding: locate element coordinates."""
    result = dispatch("ground", {"description": description}, ctx)
    print_result(result, ctx)


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
    result = dispatch("ask-vision", params, ctx)
    print_result(result, ctx)


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
    result = dispatch("detect", params, ctx)
    print_result(result, ctx)


@app.command()
def cleanup(ctx: typer.Context) -> None:
    """Clean up old/oversized screenshots based on age and size limits."""
    result = dispatch("cleanup_screenshots", {}, ctx)
    print_result(result, ctx)


@app.command()
def highlight(
    ctx: typer.Context,
    x1: int = typer.Argument(help="Left"),
    y1: int = typer.Argument(help="Top"),
    x2: int = typer.Argument(help="Right"),
    y2: int = typer.Argument(help="Bottom"),
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
    result = dispatch("highlight", params, ctx)
    print_result(result, ctx)


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
    result = dispatch("diff", params, ctx)
    print_result(result, ctx)


@app.command(name="get-text")
def get_text(
    ctx: typer.Context,
    selector: str = typer.Argument(help="UIA element ref (e.g. @e3)"),
) -> None:
    """Get text content of a UIA element."""
    result = dispatch("get_text", {"selector": selector}, ctx)
    print_result(result, ctx)
