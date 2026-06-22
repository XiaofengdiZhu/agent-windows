"""Input handlers: mouse and keyboard automation."""

from typing import Any

from agent_windows.daemon.router import handler, require


# DoS bound: type_text sleeps per character (default 0.02s), so a 10MB string
# would block the single STA worker for ~56 hours. Cap the text length. (SEC3-05)
MAX_TYPE_TEXT_LEN = 10000


def _coords(params: dict) -> tuple[int, int]:
    """Extract the target (x, y) coordinates from params. (TODO #13)

    A ``selector`` (``@eN`` element ref) takes priority and is resolved to the
    element's bbox center via ``resolve_element_center`` — matching the
    screenshot handler's "selector takes priority over bbox" rule. Without a
    selector, falls back to the explicit ``x``/``y`` and validates them.

    Raises CoordinateRangeError (→ COORDINATE_OUT_OF_RANGE) for non-numeric or
    out-of-screen values; ValueError (bad selector format / no bbox) and
    IndexError (out-of-range @eN) both surface as INVALID_PARAMS. (SEC3-02)

    Note: unlike the screenshot handler (which warns-and-continues on a
    non-``@e`` selector), input fails loudly here — a silent fallback to x/y
    would risk clicking the wrong target. (TODO #13)
    """
    from agent_windows.core.input_controller import validate_coordinates

    selector = params.get("selector")
    if selector:
        from agent_windows.core.tree import resolve_element_center
        # resolve_element_center validates the center itself; return directly.
        return resolve_element_center(selector)
    return validate_coordinates(require(params, "x"), require(params, "y"))


@handler("click")
def _click(params: dict) -> dict[str, Any]:
    """Mouse click at coordinates."""
    from agent_windows.core.input_controller import click

    x, y = _coords(params)
    right = params.get("right", False)
    double = params.get("double", False)
    click(x, y, right=right, double=double)
    return {"output": f"## Click\nPosition: ({x}, {y})\nButton: {'right' if right else 'left'}\nDouble: {double}"}


@handler("dblclick")
def _dblclick(params: dict) -> dict[str, Any]:
    """Double-click at coordinates."""
    from agent_windows.core.input_controller import click

    x, y = _coords(params)
    right = params.get("right", False)
    click(x, y, right=right, double=True)
    return {"output": f"## Double Click\nPosition: ({x}, {y})\nButton: {'right' if right else 'left'}"}


@handler("hover")
def _hover(params: dict) -> dict[str, Any]:
    """Hover at position to trigger tooltip/highlight."""
    from agent_windows.core.input_controller import hover

    x, y = _coords(params)
    hover(x, y)
    return {"output": f"## Hover\nPosition: ({x}, {y})"}


@handler("mouse_down")
def _mouse_down(params: dict) -> dict[str, Any]:
    """Press and hold mouse button."""
    from agent_windows.core.input_controller import mouse_down

    x, y = _coords(params)
    button = params.get("button", "left")
    mouse_down(x, y, button=button)
    return {"output": f"## Mouse Down\nPosition: ({x}, {y})\nButton: {button}"}


@handler("mouse_up")
def _mouse_up(params: dict) -> dict[str, Any]:
    """Release mouse button."""
    from agent_windows.core.input_controller import mouse_up

    x, y = _coords(params)
    button = params.get("button", "left")
    mouse_up(x, y, button=button)
    return {"output": f"## Mouse Up\nPosition: ({x}, {y})\nButton: {button}"}


@handler("type")
def _type(params: dict) -> dict[str, Any]:
    """Type text."""
    from agent_windows.core.input_controller import (
        type_text, type_text_at, clear_field, move_caret,
        _send_vk_key, validate_coordinates,
    )

    text = require(params, "text", str)
    if len(text) > MAX_TYPE_TEXT_LEN:
        raise ValueError(
            f"text length {len(text)} exceeds the {MAX_TYPE_TEXT_LEN}-character maximum")
    at_x = params.get("at_x")
    at_y = params.get("at_y")
    enter = params.get("enter", False)
    clear = params.get("clear", False)
    # TODO #16: caret_position = start | idle | end. Validated here (not in
    # core) so a bad value surfaces as INVALID_PARAMS rather than a silent
    # no-op inside move_caret. A present-but-None value is treated as the
    # default ("idle"), matching `enter`/`clear` and require()'s None-as-missing
    # convention (router.py) — so `{"caret_position": null}` is a no-op, not
    # an error.
    caret_position = params.get("caret_position") or "idle"
    if caret_position not in ("start", "idle", "end"):
        raise ValueError(
            f"caret_position must be 'start', 'idle', or 'end'; got {caret_position!r}"
        )

    at_selector = params.get("at_selector")
    if at_selector:
        # TODO #13: @eN ref resolves to bbox center, overriding at_x/at_y.
        # Selector-first mirrors _coords' rule and the screenshot handler.
        from agent_windows.core.tree import resolve_element_center
        ax, ay = resolve_element_center(at_selector)
        type_text_at(text, ax, ay, clear=clear, enter=enter,
                     caret_position=caret_position)
    elif at_x is not None and at_y is not None:
        ax, ay = validate_coordinates(at_x, at_y)
        type_text_at(text, ax, ay, clear=clear, enter=enter,
                     caret_position=caret_position)
    else:
        # No click: operate on the already-focused field. caret_position still
        # applies (Home/End move the caret in the focused field), matching the
        # reference semantics. (TODO #16)
        move_caret(caret_position)
        if clear:
            clear_field()
        type_text(text)
        if enter:
            _send_vk_key(0x0D)  # VK_RETURN

    return {"output": f"## Type\nText: {text[:50]}{'...' if len(text) > 50 else ''}\nEnter: {enter}\nClear: {clear}\nCaret: {caret_position}"}


@handler("scroll")
def _scroll(params: dict) -> dict[str, Any]:
    """Scroll at position."""
    from agent_windows.core.input_controller import scroll_at

    x, y = _coords(params)
    direction = params.get("direction", "down")
    amount = params.get("amount", 3)
    scroll_at(x, y, direction=direction, amount=amount)
    return {"output": f"## Scroll\nPosition: ({x}, {y})\nDirection: {direction}\nAmount: {amount}"}


@handler("move")
def _move(params: dict) -> dict[str, Any]:
    """Move mouse to position."""
    from agent_windows.core.input_controller import move_to

    x, y = _coords(params)
    move_to(x, y)
    return {"output": f"## Move\nPosition: ({x}, {y})"}


@handler("drag")
def _drag(params: dict) -> dict[str, Any]:
    """Drag from one position to another."""
    from agent_windows.core.input_controller import drag, validate_coordinates

    # Validate both endpoints independently for precise error reporting.
    x1, y1 = validate_coordinates(require(params, "x1"), require(params, "y1"))
    x2, y2 = validate_coordinates(require(params, "x2"), require(params, "y2"))
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
