"""JSON-RPC command router: method name → handler function.

All command handlers receive a params dict and return a result dict (or raise an error).
Handlers are registered via the @handler decorator or directly in HANDLERS.
"""

import logging
import time
from typing import Any, Callable

from agent_windows.ipc.protocol import (
    JSONRPCRequest,
    JSONRPCResponse,
    make_error,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    DAEMON_INTERNAL_ERROR,
)

logger = logging.getLogger(__name__)

# Global handler registry
HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {}


def handler(method: str):
    """Decorator to register a function as a JSON-RPC handler."""
    def decorator(func: Callable[..., dict[str, Any]]):
        HANDLERS[method] = func
        return func
    return decorator


def route_request(request: JSONRPCRequest) -> JSONRPCResponse:
    """Route a JSON-RPC request to the appropriate handler."""
    if not request.method:
        return make_error(INVALID_REQUEST, "Missing method", request.id)

    handler_func = HANDLERS.get(request.method)
    if handler_func is None:
        return make_error(METHOD_NOT_FOUND, f"Unknown method: {request.method}", request.id)

    try:
        result = handler_func(request.params or {})
        return JSONRPCResponse(id=request.id, result=result)
    except ValueError as e:
        return make_error(INVALID_PARAMS, str(e), request.id)
    except Exception as e:
        logger.exception("Handler error for %s", request.method)
        return make_error(DAEMON_INTERNAL_ERROR, str(e), request.id)


# ── Built-in handlers ─────────────────────────────────────────────


@handler("ping")
def _ping(params: dict) -> dict[str, Any]:
    """Health check."""
    return {"status": "ok", "message": "pong"}


@handler("shutdown")
def _shutdown(params: dict) -> dict[str, Any]:
    """Request daemon shutdown."""
    return {"status": "ok", "action": "shutting_down"}


# ── Screenshot handlers ───────────────────────────────────────────


@handler("screenshot")
def _screenshot(params: dict) -> dict[str, Any]:
    """Capture and save a screenshot."""
    from agent_windows.core.screenshot import capture_screenshot, save_screenshot, scale_screenshot

    output = params.get("output", "")
    monitor = params.get("monitor", -1)

    image = capture_screenshot(monitor=monitor)

    # Scale if configured
    config_result = {}
    try:
        from agent_windows.utils.config import load_config
        cfg = load_config().get("screenshot", {})
        max_w = cfg.get("max_width", 0)
        max_h = cfg.get("max_height", 0)
        scale = cfg.get("scale", 1.0)
        image = scale_screenshot(image, max_width=max_w, max_height=max_h, scale=scale)
    except Exception:
        pass

    path = save_screenshot(image, output_path=output if output else None)
    return {
        "output": f"## Screenshot\nPath: {path}\nSize: {image.width}x{image.height}",
        "path": str(path),
        "width": image.width,
        "height": image.height,
    }


# ── Snapshot handlers ─────────────────────────────────────────────


@handler("snapshot")
def _snapshot(params: dict) -> dict[str, Any]:
    """UIA tree traversal and semantic snapshot."""
    from agent_windows.core.tree import traverse_tree, render_snapshot
    from agent_windows.utils.config import load_config

    cfg = load_config().get("tree", {})
    data = traverse_tree(
        max_depth=cfg.get("max_depth", 30),
        include_invisible=cfg.get("include_invisible", False),
        include_offscreen=cfg.get("include_offscreen", False),
    )
    output = render_snapshot(data)
    return {"output": output}


@handler("observe")
def _observe(params: dict) -> dict[str, Any]:
    """Combined observation: snapshot + screenshot + grounding."""
    import time as _time

    parts = []

    want_snapshot = params.get("snapshot", False)
    want_screenshot = params.get("screenshot", False)
    ground_desc = params.get("ground", "")

    if not want_snapshot and not want_screenshot and not ground_desc:
        want_snapshot = True
        want_screenshot = True

    if want_snapshot:
        snap_result = _snapshot(params)
        parts.append(snap_result.get("output", ""))

    if want_screenshot:
        if parts:
            parts.append("---\n")
        ss_result = _screenshot(params)
        parts.append(ss_result.get("output", ""))

    if ground_desc:
        if parts:
            parts.append("---\n")
        if "ground" in HANDLERS:
            ground_result = HANDLERS["ground"]({"description": ground_desc})
            parts.append(ground_result.get("output", ""))
        else:
            parts.append("## Grounding\nNot yet implemented (Phase 4)")

    return {"output": "\n".join(parts)}


# ── Grounding handler ─────────────────────────────────────────────


@handler("ground")
def _ground(params: dict) -> dict[str, Any]:
    """Visual grounding: locate element coordinates."""
    from agent_windows.core.grounding import ground_element

    description = params.get("description", "")
    if not description:
        raise ValueError("description is required")

    result = ground_element(description)
    return {
        "output": (
            f"## Grounding Result\n"
            f"Target: \"{description}\"\n"
            f"Coordinate: ({result['x']}, {result['y']})\n"
            f"Confidence: {result['confidence']}\n"
            f"Method: {result['method']}\n"
            f"Screenshot: {result['screenshot_path']} (marked)"
        ),
        "x": result["x"],
        "y": result["y"],
        "confidence": result["confidence"],
        "method": result["method"],
        "screenshot_path": result["screenshot_path"],
    }


# ── Input handlers ────────────────────────────────────────────────


@handler("click")
def _click(params: dict) -> dict[str, Any]:
    """Mouse click at coordinates."""
    from agent_windows.core.input_controller import click

    x = params["x"]
    y = params["y"]
    right = params.get("right", False)
    double = params.get("double", False)
    click(x, y, right=right, double=double)
    return {"output": f"## Click\nPosition: ({x}, {y})\nButton: {'right' if right else 'left'}\nDouble: {double}"}


@handler("type")
def _type(params: dict) -> dict[str, Any]:
    """Type text."""
    from agent_windows.core.input_controller import type_text, type_text_at, shortcut, _send_vk_key

    text = params["text"]
    at_x = params.get("at_x")
    at_y = params.get("at_y")
    enter = params.get("enter", False)
    clear = params.get("clear", False)

    if at_x is not None and at_y is not None:
        type_text_at(text, at_x, at_y, clear=clear, enter=enter)
    else:
        if clear:
            shortcut("ctrl+a")
            time.sleep(0.05)
            _send_vk_key(0x2E)  # VK_DELETE
            time.sleep(0.05)
        type_text(text)
        if enter:
            _send_vk_key(0x0D)  # VK_RETURN

    return {"output": f"## Type\nText: {text[:50]}{'...' if len(text) > 50 else ''}\nEnter: {enter}\nClear: {clear}"}


@handler("scroll")
def _scroll(params: dict) -> dict[str, Any]:
    """Scroll at position."""
    from agent_windows.core.input_controller import scroll_at

    x = params["x"]
    y = params["y"]
    direction = params.get("direction", "down")
    amount = params.get("amount", 3)
    scroll_at(x, y, direction=direction, amount=amount)
    return {"output": f"## Scroll\nPosition: ({x}, {y})\nDirection: {direction}\nAmount: {amount}"}


@handler("move")
def _move(params: dict) -> dict[str, Any]:
    """Move mouse to position."""
    from agent_windows.core.input_controller import move_to

    x = params["x"]
    y = params["y"]
    move_to(x, y)
    return {"output": f"## Move\nPosition: ({x}, {y})"}


@handler("drag")
def _drag(params: dict) -> dict[str, Any]:
    """Drag from one position to another."""
    from agent_windows.core.input_controller import drag

    x1, y1 = params["x1"], params["y1"]
    x2, y2 = params["x2"], params["y2"]
    right = params.get("right", False)
    drag(x1, y1, x2, y2, right=right)
    return {"output": f"## Drag\nFrom: ({x1}, {y1})\nTo: ({x2}, {y2})\nButton: {'right' if right else 'left'}"}


@handler("shortcut")
def _shortcut(params: dict) -> dict[str, Any]:
    """Send keyboard shortcut."""
    from agent_windows.core.input_controller import shortcut

    keys = params["keys"]
    shortcut(keys)
    return {"output": f"## Shortcut\nKeys: {keys}"}
