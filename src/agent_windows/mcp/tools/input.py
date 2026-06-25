"""Input MCP tools — mouse and keyboard automation.

All mouse tools accept either loc=[x, y] OR selector=@eN (selector wins),
mirroring the CLI's --selector override. The daemon's single STA worker
serializes these, so they are safe to call sequentially.
"""
from typing import Literal

from mcp.types import ToolAnnotations

from agent_windows.mcp._call import _call, xy_or_selector


def click_fn(loc: list[int] | None = None, selector: str | None = None,
             right: bool = False, double: bool = False) -> str:
    return str(_call("click", **xy_or_selector(loc, selector),
                     right=right, double=double).get("output", ""))


def dblclick_fn(loc: list[int] | None = None, selector: str | None = None,
                right: bool = False) -> str:
    return str(_call("dblclick", **xy_or_selector(loc, selector),
                     right=right).get("output", ""))


def hover_fn(loc: list[int] | None = None, selector: str | None = None) -> str:
    return str(_call("hover", **xy_or_selector(loc, selector)).get("output", ""))


def move_fn(loc: list[int] | None = None, selector: str | None = None) -> str:
    return str(_call("move", **xy_or_selector(loc, selector)).get("output", ""))


def mouse_down_fn(loc: list[int] | None = None, selector: str | None = None,
                  button: str = "left") -> str:
    return str(_call("mouse_down", **xy_or_selector(loc, selector),
                     button=button).get("output", ""))


def mouse_up_fn(loc: list[int] | None = None, selector: str | None = None,
                button: str = "left") -> str:
    return str(_call("mouse_up", **xy_or_selector(loc, selector),
                     button=button).get("output", ""))


def drag_fn(x1: int, y1: int, x2: int, y2: int, right: bool = False) -> str:
    # drag takes four explicit ints (mirrors the CLI: no selector form). All
    # four are required positionals with no default, so a missing one is a
    # TypeError at the call site — no runtime guard needed.
    return str(_call("drag", x1=x1, y1=y1, x2=x2, y2=y2, right=right).get("output", ""))


def scroll_fn(loc: list[int] | None = None, selector: str | None = None,
              direction: str = "down", amount: int = 3) -> str:
    return str(_call("scroll", **xy_or_selector(loc, selector),
                     direction=direction, amount=amount).get("output", ""))


def type_fn(
    text: str,
    loc: list[int] | None = None,
    at_selector: str | None = None,
    enter: bool = False,
    clear: bool = False,
    caret_position: Literal["start", "idle", "end"] = "idle",
) -> str:
    """Type text, optionally clicking a target first.

    Exactly one of loc (click [x,y]) or at_selector (@eN) may be given to focus
    a field first; omit both to type into the already-focused element.
    """
    params: dict = {"text": text, "enter": enter, "clear": clear,
                    "caret_position": caret_position}
    if at_selector:
        params["at_selector"] = at_selector
    elif loc is not None:
        if len(loc) != 2:
            raise ValueError("loc must be a list of exactly two integers [x, y]")
        params["at_x"], params["at_y"] = loc[0], loc[1]
    return str(_call("type", **params).get("output", ""))


def shortcut_fn(keys: str) -> str:
    return str(_call("shortcut", keys=keys).get("output", ""))


def register(mcp) -> None:
    @mcp.tool(name="click", description=(
        "Mouse click at [x, y] or at a UI element (@eN selector from a prior "
        "snapshot). Keywords: click, tap, press, mouse click, left click, "
        "select, left-click. right=True for a context-menu click, double=True "
        "for a double-click. Provide either loc OR selector."
    ), annotations=ToolAnnotations(title="Click", readOnlyHint=False,
                                   destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _click(loc: list[int] | None = None, selector: str | None = None,
               right: bool = False, double: bool = False) -> str:
        return click_fn(loc=loc, selector=selector, right=right, double=double)

    @mcp.tool(name="dblclick", description=(
        "Double-click at [x, y] or at a UI element (@eN). "
        "Keywords: double click, double-click, two clicks, open file, expand."
    ), annotations=ToolAnnotations(title="Double Click", readOnlyHint=False,
                                   destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _dblclick(loc: list[int] | None = None, selector: str | None = None,
                  right: bool = False) -> str:
        return dblclick_fn(loc=loc, selector=selector, right=right)

    @mcp.tool(name="hover", description=(
        "Hover at [x, y] or @eN to trigger tooltips/highlights. "
        "Keywords: hover, mouse over, tooltip, mouse hover."
    ), annotations=ToolAnnotations(title="Hover", readOnlyHint=False,
                                   destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def _hover(loc: list[int] | None = None, selector: str | None = None) -> str:
        return hover_fn(loc=loc, selector=selector)

    @mcp.tool(name="move", description=(
        "Move the mouse cursor to [x, y] or @eN (without clicking). "
        "Keywords: move mouse, cursor, mouse position, point."
    ), annotations=ToolAnnotations(title="Move", readOnlyHint=False,
                                   destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def _move(loc: list[int] | None = None, selector: str | None = None) -> str:
        return move_fn(loc=loc, selector=selector)

    @mcp.tool(name="mouse_down", description=(
        "Press and hold a mouse button at [x, y] or @eN. "
        "Keywords: mouse down, press button, hold, drag start. "
        "button: left/right/middle. Pair with mouse_up."
    ), annotations=ToolAnnotations(title="Mouse Down", readOnlyHint=False,
                                   destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _mouse_down(loc: list[int] | None = None, selector: str | None = None,
                    button: str = "left") -> str:
        return mouse_down_fn(loc=loc, selector=selector, button=button)

    @mcp.tool(name="mouse_up", description=(
        "Release a mouse button at [x, y] or @eN. "
        "Keywords: mouse up, release button, drop, drag end. "
        "button: left/right/middle."
    ), annotations=ToolAnnotations(title="Mouse Up", readOnlyHint=False,
                                   destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def _mouse_up(loc: list[int] | None = None, selector: str | None = None,
                  button: str = "left") -> str:
        return mouse_up_fn(loc=loc, selector=selector, button=button)

    @mcp.tool(name="drag", description=(
        "Drag from (x1,y1) to (x2,y2). Keywords: drag, move item, drop, "
        "drag and drop, slide. right=True for a right-button drag."
    ), annotations=ToolAnnotations(title="Drag", readOnlyHint=False,
                                   destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _drag(x1: int, y1: int, x2: int, y2: int, right: bool = False) -> str:
        return drag_fn(x1=x1, y1=y1, x2=x2, y2=y2, right=right)

    @mcp.tool(name="scroll", description=(
        "Scroll at [x, y] or @eN. Keywords: scroll, mouse wheel, page down, "
        "page up, swipe. direction: up/down. amount is wheel ticks "
        "(~3-5 lines each)."
    ), annotations=ToolAnnotations(title="Scroll", readOnlyHint=False,
                                   destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def _scroll(loc: list[int] | None = None, selector: str | None = None,
                direction: str = "down", amount: int = 3) -> str:
        return scroll_fn(loc=loc, selector=selector, direction=direction, amount=amount)

    @mcp.tool(name="type", description=(
        "Type text using Unicode direct input (bypasses IME). Keywords: type, "
        "enter text, input text, write text, keyboard, fill field. Optionally "
        "click a target first via loc=[x,y] or at_selector=@eN. enter=True "
        "presses Enter after; clear=True select-all+deletes first; "
        "caret_position: start|idle|end."
    ), annotations=ToolAnnotations(title="Type", readOnlyHint=False,
                                   destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _type(text: str, loc: list[int] | None = None,
              at_selector: str | None = None, enter: bool = False,
              clear: bool = False,
              caret_position: Literal["start", "idle", "end"] = "idle") -> str:
        return type_fn(text=text, loc=loc, at_selector=at_selector, enter=enter,
                       clear=clear, caret_position=caret_position)

    @mcp.tool(name="shortcut", description=(
        'Send a keyboard shortcut, e.g. "ctrl+c", "alt+tab", "win+r". '
        'Keywords: shortcut, hotkey, key combination, keyboard shortcut, '
        'ctrl, alt, ctrl+c, paste.'
    ), annotations=ToolAnnotations(title="Shortcut", readOnlyHint=False,
                                   destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def _shortcut(keys: str) -> str:
        return shortcut_fn(keys=keys)
