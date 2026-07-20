"""Input commands: click, type, scroll, move, drag, shortcut."""

import typer

from agent_windows.cli.dispatcher import _cmd

app = typer.Typer(help="Input commands")

# ── Negative-coordinate support (C1) ──────────────────────────────────
# Click/Typer reject a bare token like ``-1`` as "No such option" before the
# command runs — so multi-monitor coordinates (a window at x=-1920) crash the
# Python fallback CLI. The native Rust launcher handles this
# (commands.rs:376-380: a leading-``-<digit>`` is a positional, not a flag).
#
# On the Python side there is no Click-native fix (group-level
# ignore_unknown_options breaks subcommand routing; int() accepts no Unicode
# minus, so sign re-encoding can't round-trip). The fix is argv preprocessing
# in cli/main.main(): a leading ``-<digits>`` token on a coordinate command is
# prefixed with this marker so Click sees a non-option, then parse_coord
# strips it. The marker is SOH (0x01): never an option prefix, never in real
# user input.
_NEG_COORD_MARKER = "\x01"


def parse_coord(raw: str) -> int:
    """Argument parser for coordinate positionals.

    Strips the negative-coordinate marker (added by _preprocess_argv) and
    converts to int. For positive coordinates the marker is absent and
    ``removeprefix`` is a no-op, so the positive path is unchanged.
    """
    return int(raw.removeprefix(_NEG_COORD_MARKER))


def _xy_or_selector(coords: list[int] | None, selector: str) -> dict:
    """Assemble the coordinate params for a mouse command. (TODO #13)

    ``--selector @eN`` overrides positional ``X Y``. Exactly one must be given.
    Returns ``{"selector": s}`` or ``{"x": .., "y": ..}`` — ready to splat into
    ``_cmd`` alongside the command's other params.
    """
    if selector:
        return {"selector": selector}
    if coords and len(coords) == 1:
        # The likely user mistake: typed one number, forgot the second.
        raise typer.BadParameter("expected X Y (two coordinates); got one")
    if not coords or len(coords) != 2:
        raise typer.BadParameter("provide X Y coordinates, or --selector")
    return {"x": coords[0], "y": coords[1]}


@app.command()
def click(
    ctx: typer.Context,
    coords: list[int] = typer.Argument(None, parser=parse_coord, help="X Y coordinates (omit with --selector)"),
    selector: str = typer.Option("", "--selector", "-s", help="Element ref (@eN); overrides X Y"),
    right: bool = typer.Option(False, "--right", help="Right-click"),
    double: bool = typer.Option(False, "--double", help="Double-click"),
) -> None:
    """Mouse click at (X, Y) or at an element (@eN via --selector)."""
    _cmd(ctx, "click", **_xy_or_selector(coords, selector),
         right=right, double=double)


@app.command()
def type_text(
    ctx: typer.Context,
    text: str = typer.Argument(help="Text to type"),
    at: str = typer.Option("", "--at", help="Click at x,y before typing"),
    at_selector: str = typer.Option(
        "", "--at-selector", "--ats",
        help="Click at element @eN before typing; overrides --at"),
    enter: bool = typer.Option(False, "--enter", help="Press Enter after typing"),
    clear: bool = typer.Option(False, "--clear", help="Select-all and delete before typing"),
    caret: str = typer.Option(
        "idle", "--caret",
        help="Move caret before typing: start | idle | end"),
) -> None:
    """Type text using Unicode direct input (bypasses IME)."""
    params = {"text": text, "enter": enter, "clear": clear, "caret_position": caret}
    if at_selector:
        params["at_selector"] = at_selector
    elif at:
        parts = at.split(",")
        if len(parts) == 2:
            params["at_x"] = int(parts[0])
            params["at_y"] = int(parts[1])
    _cmd(ctx, "type", **params)


@app.command()
def scroll(
    ctx: typer.Context,
    coords: list[int] = typer.Argument(None, parser=parse_coord, help="X Y coordinates (omit with --selector)"),
    selector: str = typer.Option("", "--selector", "-s", help="Element ref (@eN); overrides X Y"),
    direction: str = typer.Option("down", "--direction", help="Scroll direction: up/down"),
    amount: int = typer.Option(3, "--amount", help="Scroll amount"),
) -> None:
    """Scroll at (X, Y) or at an element (@eN via --selector)."""
    _cmd(ctx, "scroll", **_xy_or_selector(coords, selector),
         direction=direction, amount=amount)


@app.command()
def move(
    ctx: typer.Context,
    coords: list[int] = typer.Argument(None, parser=parse_coord, help="X Y coordinates (omit with --selector)"),
    selector: str = typer.Option("", "--selector", "-s", help="Element ref (@eN); overrides X Y"),
) -> None:
    """Move mouse to (X, Y) or to an element (@eN via --selector)."""
    _cmd(ctx, "move", **_xy_or_selector(coords, selector))


@app.command()
def dblclick(
    ctx: typer.Context,
    coords: list[int] = typer.Argument(None, parser=parse_coord, help="X Y coordinates (omit with --selector)"),
    selector: str = typer.Option("", "--selector", "-s", help="Element ref (@eN); overrides X Y"),
    right: bool = typer.Option(False, "--right", help="Right-button double-click"),
) -> None:
    """Double-click at (X, Y) or at an element (@eN via --selector)."""
    _cmd(ctx, "dblclick", **_xy_or_selector(coords, selector), right=right)


@app.command()
def hover(
    ctx: typer.Context,
    coords: list[int] = typer.Argument(None, parser=parse_coord, help="X Y coordinates (omit with --selector)"),
    selector: str = typer.Option("", "--selector", "-s", help="Element ref (@eN); overrides X Y"),
) -> None:
    """Hover at (X, Y) or at an element (@eN via --selector)."""
    _cmd(ctx, "hover", **_xy_or_selector(coords, selector))


@app.command(name="mouse-down")
def mouse_down(
    ctx: typer.Context,
    coords: list[int] = typer.Argument(None, parser=parse_coord, help="X Y coordinates (omit with --selector)"),
    selector: str = typer.Option("", "--selector", "-s", help="Element ref (@eN); overrides X Y"),
    button: str = typer.Option("left", "--button", help="Mouse button: left, right, middle"),
) -> None:
    """Press and hold mouse button at (X, Y) or at an element (@eN via --selector)."""
    _cmd(ctx, "mouse_down", **_xy_or_selector(coords, selector), button=button)


@app.command(name="mouse-up")
def mouse_up(
    ctx: typer.Context,
    coords: list[int] = typer.Argument(None, parser=parse_coord, help="X Y coordinates (omit with --selector)"),
    selector: str = typer.Option("", "--selector", "-s", help="Element ref (@eN); overrides X Y"),
    button: str = typer.Option("left", "--button", help="Mouse button: left, right, middle"),
) -> None:
    """Release mouse button at (X, Y) or at an element (@eN via --selector)."""
    _cmd(ctx, "mouse_up", **_xy_or_selector(coords, selector), button=button)


@app.command()
def drag(
    ctx: typer.Context,
    x1: int = typer.Argument(parser=parse_coord, help="Start X"),
    y1: int = typer.Argument(parser=parse_coord, help="Start Y"),
    x2: int = typer.Argument(parser=parse_coord, help="End X"),
    y2: int = typer.Argument(parser=parse_coord, help="End Y"),
    right: bool = typer.Option(False, "--right", help="Right-button drag"),
) -> None:
    """Drag from (x1,y1) to (x2,y2)."""
    _cmd(ctx, "drag", x1=x1, y1=y1, x2=x2, y2=y2, right=right)


@app.command()
def shortcut(
    ctx: typer.Context,
    keys: str = typer.Argument(help="Keyboard shortcut, e.g. 'ctrl+c'"),
) -> None:
    """Send keyboard shortcut."""
    _cmd(ctx, "shortcut", keys=keys)


@app.command(name="key-down")
def key_down(
    ctx: typer.Context,
    keys: str = typer.Argument(help="Key(s) to press, e.g. 'a', 'ctrl+shift+a', 'enter'"),
    hold: float = typer.Option(
        None, "--hold",
        help="Hold N seconds then auto-release; omit to keep pressed (pair with key-up)"),
) -> None:
    """Press key(s) (raw key-down). With --hold, releases after N seconds; without, stays pressed."""
    params = {"keys": keys}
    if hold is not None:
        params["hold"] = hold
    _cmd(ctx, "key_down", **params)


@app.command(name="key-up")
def key_up(
    ctx: typer.Context,
    keys: str = typer.Argument(help="Key(s) to release"),
) -> None:
    """Release key(s) (raw key-up)."""
    _cmd(ctx, "key_up", keys=keys)


@app.command()
def expand(
    ctx: typer.Context,
    selector: str = typer.Argument(help="Element ref (@eN)"),
) -> None:
    """Expand an expandable element (ComboBox/TreeItem/Menu) at @eN."""
    _cmd(ctx, "expand", selector=selector)


@app.command()
def collapse(
    ctx: typer.Context,
    selector: str = typer.Argument(help="Element ref (@eN)"),
) -> None:
    """Collapse an expandable element at @eN."""
    _cmd(ctx, "collapse", selector=selector)
