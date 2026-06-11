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
) -> None:
    """Capture screen screenshot."""
    params = {"output": output, "monitor": monitor}
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
