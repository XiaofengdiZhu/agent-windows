"""Input handlers: mouse and keyboard automation."""

from typing import Any

from agent_windows.daemon.router import handler, require


@handler("click")
def _click(params: dict) -> dict[str, Any]:
    """Mouse click at coordinates."""
    from agent_windows.core.input_controller import click

    x = require(params, "x")
    y = require(params, "y")
    right = params.get("right", False)
    double = params.get("double", False)
    click(x, y, right=right, double=double)
    return {"output": f"## Click\nPosition: ({x}, {y})\nButton: {'right' if right else 'left'}\nDouble: {double}"}


@handler("dblclick")
def _dblclick(params: dict) -> dict[str, Any]:
    """Double-click at coordinates."""
    from agent_windows.core.input_controller import click

    x, y = require(params, "x"), require(params, "y")
    right = params.get("right", False)
    click(x, y, right=right, double=True)
    return {"output": f"## Double Click\nPosition: ({x}, {y})\nButton: {'right' if right else 'left'}"}


@handler("hover")
def _hover(params: dict) -> dict[str, Any]:
    """Hover at position to trigger tooltip/highlight."""
    from agent_windows.core.input_controller import hover

    x, y = require(params, "x"), require(params, "y")
    hover(x, y)
    return {"output": f"## Hover\nPosition: ({x}, {y})"}


@handler("mouse_down")
def _mouse_down(params: dict) -> dict[str, Any]:
    """Press and hold mouse button."""
    from agent_windows.core.input_controller import mouse_down

    x, y = require(params, "x"), require(params, "y")
    button = params.get("button", "left")
    mouse_down(x, y, button=button)
    return {"output": f"## Mouse Down\nPosition: ({x}, {y})\nButton: {button}"}


@handler("mouse_up")
def _mouse_up(params: dict) -> dict[str, Any]:
    """Release mouse button."""
    from agent_windows.core.input_controller import mouse_up

    x, y = require(params, "x"), require(params, "y")
    button = params.get("button", "left")
    mouse_up(x, y, button=button)
    return {"output": f"## Mouse Up\nPosition: ({x}, {y})\nButton: {button}"}


@handler("type")
def _type(params: dict) -> dict[str, Any]:
    """Type text."""
    from agent_windows.core.input_controller import type_text, type_text_at, clear_field, _send_vk_key

    text = require(params, "text", str)
    at_x = params.get("at_x")
    at_y = params.get("at_y")
    enter = params.get("enter", False)
    clear = params.get("clear", False)

    if at_x is not None and at_y is not None:
        type_text_at(text, at_x, at_y, clear=clear, enter=enter)
    else:
        if clear:
            clear_field()
        type_text(text)
        if enter:
            _send_vk_key(0x0D)  # VK_RETURN

    return {"output": f"## Type\nText: {text[:50]}{'...' if len(text) > 50 else ''}\nEnter: {enter}\nClear: {clear}"}


@handler("scroll")
def _scroll(params: dict) -> dict[str, Any]:
    """Scroll at position."""
    from agent_windows.core.input_controller import scroll_at

    x = require(params, "x")
    y = require(params, "y")
    direction = params.get("direction", "down")
    amount = params.get("amount", 3)
    scroll_at(x, y, direction=direction, amount=amount)
    return {"output": f"## Scroll\nPosition: ({x}, {y})\nDirection: {direction}\nAmount: {amount}"}


@handler("move")
def _move(params: dict) -> dict[str, Any]:
    """Move mouse to position."""
    from agent_windows.core.input_controller import move_to

    x, y = require(params, "x"), require(params, "y")
    move_to(x, y)
    return {"output": f"## Move\nPosition: ({x}, {y})"}


@handler("drag")
def _drag(params: dict) -> dict[str, Any]:
    """Drag from one position to another."""
    from agent_windows.core.input_controller import drag

    x1, y1 = require(params, "x1"), require(params, "y1")
    x2, y2 = require(params, "x2"), require(params, "y2")
    right = params.get("right", False)
    drag(x1, y1, x2, y2, right=right)
    return {"output": f"## Drag\nFrom: ({x1}, {y1})\nTo: ({x2}, {y2})\nButton: {'right' if right else 'left'}"}


@handler("shortcut")
def _shortcut(params: dict) -> dict[str, Any]:
    """Send keyboard shortcut."""
    from agent_windows.core.input_controller import shortcut

    keys = require(params, "keys", str)
    shortcut(keys)
    return {"output": f"## Shortcut\nKeys: {keys}"}
