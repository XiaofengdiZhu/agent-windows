"""Input commands: click, type, scroll, move, drag, shortcut."""

import typer

from agent_windows.cli.dispatcher import dispatch, print_result

app = typer.Typer(help="Input commands")


@app.command()
def click(
    ctx: typer.Context,
    x: int = typer.Argument(help="X coordinate"),
    y: int = typer.Argument(help="Y coordinate"),
    right: bool = typer.Option(False, "--right", help="Right-click"),
    double: bool = typer.Option(False, "--double", help="Double-click"),
) -> None:
    """Mouse click at (x, y)."""
    params = {"x": x, "y": y, "right": right, "double": double}
    result = dispatch("click", params, ctx)
    print_result(result, ctx)


@app.command()
def type_text(
    ctx: typer.Context,
    text: str = typer.Argument(help="Text to type"),
    at: str = typer.Option("", "--at", help="Click at x,y before typing"),
    enter: bool = typer.Option(False, "--enter", help="Press Enter after typing"),
    clear: bool = typer.Option(False, "--clear", help="Select-all and delete before typing"),
) -> None:
    """Type text using Unicode direct input (bypasses IME)."""
    params = {"text": text, "enter": enter, "clear": clear}
    if at:
        parts = at.split(",")
        if len(parts) == 2:
            params["at_x"] = int(parts[0])
            params["at_y"] = int(parts[1])
    result = dispatch("type", params, ctx)
    print_result(result, ctx)


@app.command()
def scroll(
    ctx: typer.Context,
    x: int = typer.Argument(help="X coordinate"),
    y: int = typer.Argument(help="Y coordinate"),
    direction: str = typer.Option("down", "--direction", help="Scroll direction: up/down"),
    amount: int = typer.Option(3, "--amount", help="Scroll amount"),
) -> None:
    """Scroll at position (x, y)."""
    params = {"x": x, "y": y, "direction": direction, "amount": amount}
    result = dispatch("scroll", params, ctx)
    print_result(result, ctx)


@app.command()
def move(
    ctx: typer.Context,
    x: int = typer.Argument(help="X coordinate"),
    y: int = typer.Argument(help="Y coordinate"),
) -> None:
    """Move mouse to (x, y)."""
    result = dispatch("move", {"x": x, "y": y}, ctx)
    print_result(result, ctx)


@app.command()
def drag(
    ctx: typer.Context,
    x1: int = typer.Argument(help="Start X"),
    y1: int = typer.Argument(help="Start Y"),
    x2: int = typer.Argument(help="End X"),
    y2: int = typer.Argument(help="End Y"),
    right: bool = typer.Option(False, "--right", help="Right-button drag"),
) -> None:
    """Drag from (x1,y1) to (x2,y2)."""
    params = {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "right": right}
    result = dispatch("drag", params, ctx)
    print_result(result, ctx)


@app.command()
def shortcut(
    ctx: typer.Context,
    keys: str = typer.Argument(help="Keyboard shortcut, e.g. 'ctrl+c'"),
) -> None:
    """Send keyboard shortcut."""
    result = dispatch("shortcut", {"keys": keys}, ctx)
    print_result(result, ctx)


# Register 'type' as the command name (Python built-in conflict)
type_text.__name__ = "type"
