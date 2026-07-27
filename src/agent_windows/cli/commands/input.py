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


def _target_token(raw: str) -> str:
    """Positional parser for mouse-command targets: strip the negative-coord
    marker (added by ``_preprocess_argv``) and return the raw token — either an
    ``@eN`` element ref or a numeric coordinate string. ``_xy_or_selector``
    classifies and int-parses; ``parse_coord`` stays for ``drag``'s 4 int args.
    """
    return raw.removeprefix(_NEG_COORD_MARKER)


def _xy_or_selector(tokens: list[str] | None) -> dict:
    """Assemble the target for a mouse command: a single ``@eN`` element ref OR
    two ``X Y`` coordinates. ``@eN`` is a first-class positional — detected by
    its ``@e`` prefix, no ``--selector`` flag needed (matches expand/collapse/
    get-text/element-state/wait, which already take ``@eN`` bare). Exactly one
    form may be given; mixing is rejected. Returns ``{"selector": s}`` or
    ``{"x": .., "y": ..}`` — ready to splat into ``_cmd``.
    """
    if not tokens:
        raise typer.BadParameter("provide an @eN selector or X Y coordinates")
    sels = [t for t in tokens if t.startswith("@e")]
    nums = [t for t in tokens if not t.startswith("@e")]
    if sels and nums:
        raise typer.BadParameter("an @eN selector cannot be combined with X Y coordinates")
    if sels:
        if len(sels) != 1:
            raise typer.BadParameter("expected a single @eN selector")
        return {"selector": sels[0]}
    if len(nums) == 1:
        # The likely user mistake: typed one number, forgot the second.
        raise typer.BadParameter("expected X Y (two coordinates); got one")
    if len(nums) != 2:
        raise typer.BadParameter("expected X Y (two coordinates)")
    try:
        return {"x": int(nums[0]), "y": int(nums[1])}
    except ValueError:
        raise typer.BadParameter("coordinates must be integers")


@app.command()
def click(
    ctx: typer.Context,
    target: list[str] = typer.Argument(None, parser=_target_token,
                                       help="@eN element ref OR X Y coordinates"),
    right: bool = typer.Option(False, "--right", help="Right-click"),
    double: bool = typer.Option(False, "--double", help="Double-click"),
    background: bool = typer.Option(False, "--background",
                                    help="No-focus-steal click via UIA pattern (Invoke/Toggle/Select); "
                                         "left single-click only, errors if unsupported"),
) -> None:
    """Mouse click at (X, Y) or at an element (@eN).

    --background delivers the click through the element's UIA pattern without
    stealing foreground (no flash) — works on UWP/WinUI3/packaged apps. Errors
    with BACKGROUND_UNAVAILABLE (-1007) if the element has no invokable pattern.
    """
    _cmd(ctx, "click", **_xy_or_selector(target),
         right=right, double=double, background=background)


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
    target: list[str] = typer.Argument(None, parser=_target_token,
                                       help="@eN element ref OR X Y coordinates"),
    direction: str = typer.Option("down", "--direction", help="Scroll direction: up/down"),
    amount: int = typer.Option(3, "--amount", help="Scroll amount"),
) -> None:
    """Scroll at (X, Y) or at an element (@eN)."""
    _cmd(ctx, "scroll", **_xy_or_selector(target),
         direction=direction, amount=amount)


@app.command()
def move(
    ctx: typer.Context,
    target: list[str] = typer.Argument(None, parser=_target_token,
                                       help="@eN element ref OR X Y coordinates"),
) -> None:
    """Move mouse to (X, Y) or to an element (@eN)."""
    _cmd(ctx, "move", **_xy_or_selector(target))


@app.command()
def dblclick(
    ctx: typer.Context,
    target: list[str] = typer.Argument(None, parser=_target_token,
                                       help="@eN element ref OR X Y coordinates"),
    right: bool = typer.Option(False, "--right", help="Right-button double-click"),
) -> None:
    """Double-click at (X, Y) or at an element (@eN)."""
    _cmd(ctx, "dblclick", **_xy_or_selector(target), right=right)


@app.command()
def hover(
    ctx: typer.Context,
    target: list[str] = typer.Argument(None, parser=_target_token,
                                       help="@eN element ref OR X Y coordinates"),
) -> None:
    """Hover at (X, Y) or at an element (@eN)."""
    _cmd(ctx, "hover", **_xy_or_selector(target))


@app.command(name="mouse-down")
def mouse_down(
    ctx: typer.Context,
    target: list[str] = typer.Argument(None, parser=_target_token,
                                       help="@eN element ref OR X Y coordinates"),
    button: str = typer.Option("left", "--button", help="Mouse button: left, right, middle"),
) -> None:
    """Press and hold mouse button at (X, Y) or at an element (@eN)."""
    _cmd(ctx, "mouse_down", **_xy_or_selector(target), button=button)


@app.command(name="mouse-up")
def mouse_up(
    ctx: typer.Context,
    target: list[str] = typer.Argument(None, parser=_target_token,
                                       help="@eN element ref OR X Y coordinates"),
    button: str = typer.Option("left", "--button", help="Mouse button: left, right, middle"),
) -> None:
    """Release mouse button at (X, Y) or at an element (@eN)."""
    _cmd(ctx, "mouse_up", **_xy_or_selector(target), button=button)


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
