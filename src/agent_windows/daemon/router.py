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
    except (ValueError, IndexError) as e:
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
    bbox = params.get("bbox")
    selector = params.get("selector", "")
    selector_warn = ""

    # UIA element selector → bbox
    if selector and selector.startswith("@e"):
        try:
            index = int(selector[2:]) - 1  # @e{N} is 1-based
            if index < 0:
                raise IndexError(f"Invalid selector: {selector}")
            from agent_windows.core.tree import traverse_tree
            from agent_windows.utils.config import load_config as _lc
            cfg = _lc().get("tree", {})
            data = traverse_tree(
                max_depth=cfg.get("max_depth", 30),
                include_invisible=cfg.get("include_invisible", False),
                include_offscreen=cfg.get("include_offscreen", False),
            )
            elements = data.get("interactive_elements", [])
            if index >= len(elements):
                raise IndexError(f"Element {selector} not found ({len(elements)} elements)")
            el = elements[index]
            if not el.bbox:
                raise ValueError(f"Element {selector} has no bounding box")
            bbox = [el.bbox.left, el.bbox.top, el.bbox.right, el.bbox.bottom]
        except (ValueError, IndexError) as e:
            selector_warn = f" [selector unresolved: {e}]"

    image = capture_screenshot(
        monitor=monitor,
        bbox=tuple(bbox) if bbox else None,
    )

    # Scale if configured
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
        "output": f"## Screenshot\nPath: {path}\nSize: {image.width}x{image.height}{selector_warn}",
        "path": str(path),
        "width": image.width,
        "height": image.height,
    }


@handler("highlight")
def _highlight(params: dict) -> dict[str, Any]:
    """Highlight regions on a screenshot."""
    from agent_windows.core.screenshot import capture_screenshot, highlight_region

    regions = params.get("regions", [])
    # Single-region shortcut: x1,y1,x2,y2 + label
    if not regions and "x1" in params:
        regions = [{
            "x1": params["x1"], "y1": params["y1"],
            "x2": params["x2"], "y2": params["y2"],
            "label": params.get("label", ""),
            "color": params.get("color", "red"),
        }]

    image_path = params.get("image", "")
    if image_path:
        from PIL import Image
        with Image.open(image_path) as img:
            img.load()
            image = img.copy()
    else:
        image = capture_screenshot()

    output = params.get("output", "")
    path = highlight_region(image, regions, output_path=output if output else None)

    return {
        "output": f"## Highlight\nRegions: {len(regions)}\nPath: {path}",
        "path": str(path),
        "regions": len(regions),
    }


@handler("diff")
def _diff(params: dict) -> dict[str, Any]:
    """Pixel-level diff between two screenshots."""
    from PIL import Image
    from agent_windows.core.screenshot import capture_screenshot, diff_images

    baseline_path = params["baseline"]
    compare_path = params.get("compare", "")
    threshold = params.get("threshold", 30)
    output = params.get("output", "")

    with Image.open(baseline_path) as img:
        img.load()
        image_a = img.copy()

    if compare_path:
        with Image.open(compare_path) as img:
            img.load()
            image_b = img.copy()
    else:
        image_b = capture_screenshot()

    result = diff_images(
        image_a, image_b,
        threshold=threshold,
        output_path=output if output else None,
    )

    return {
        "output": (
            f"## Diff\n"
            f"Diff pixels: {result['diff_pixels']} / {result['total_pixels']} "
            f"({result['diff_percent']}%)\n"
            f"Match: {result['match']}\n"
            f"Path: {result['diff_path']}"
        ),
        **result,
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


@handler("ask-vision")
def _ask_vision(params: dict) -> dict[str, Any]:
    """Free-form visual Q&A: send screenshot + custom prompt to vision model."""
    from agent_windows.core.grounding import ask_vision

    prompt = params.get("prompt", "")
    if not prompt:
        raise ValueError("prompt is required")

    result = ask_vision(
        prompt=prompt,
        image_path=params.get("image"),
        model=params.get("model"),
        output_path=params.get("output"),
    )
    return {
        "output": (
            f"## Ask Vision\n"
            f"Prompt: {prompt[:100]}{'...' if len(prompt) > 100 else ''}\n"
            f"Model: {result['model_used']}\n"
            f"Screenshot: {result['screenshot_path']}\n"
            "Note: Use `ground` for precise click coordinates. "
            "This tool is for visual understanding (ranges, states, descriptions).\n\n"
            f"{result['response']}"
        ),
        "response": result["response"],
        "screenshot_path": result["screenshot_path"],
        "model_used": result["model_used"],
    }


@handler("detect")
def _detect(params: dict) -> dict[str, Any]:
    """Visual detection: detect multiple targets with bounding boxes."""
    from agent_windows.core.grounding import detect_elements

    description = params.get("description", "")
    if not description:
        raise ValueError("description is required")

    max_targets = params.get("max_targets", 20)
    try:
        max_targets = int(max_targets)
    except (ValueError, TypeError):
        max_targets = 20

    result = detect_elements(
        description=description,
        image_path=params.get("image"),
        model=params.get("model"),
        max_targets=max_targets,
        output_path=params.get("output"),
    )

    targets = result["targets"]
    if targets:
        lines = [
            "## Detection Result",
            f'Description: "{result["description"]}"',
            f"Count: {result['count']}",
            f"Screenshot: {result['screenshot_path']} (marked)",
            "",
            "Targets:",
        ]
        for t in targets:
            b = t["box"]
            lines.append(
                f"  #{t['index']}: label=\"{t['label']}\", "
                f"box=({b['x1']}, {b['y1']}, {b['x2']}, {b['y2']})"
            )
        text_output = "\n".join(lines)
    else:
        text_output = (
            "## Detection Result\n"
            f'Description: "{result["description"]}"\n'
            f"Count: 0\n"
            "No targets found.\n"
            f"Screenshot: {result['screenshot_path']}"
        )

    return {
        "output": text_output,
        "description": result["description"],
        "count": result["count"],
        "targets": result["targets"],
        "screenshot_path": result["screenshot_path"],
    }


@handler("cleanup_screenshots")
def _cleanup_screenshots(params: dict) -> dict[str, Any]:
    """Force cleanup of old/oversized screenshots."""
    from agent_windows.core.screenshot import cleanup_screenshots

    stats = cleanup_screenshots(force=True)
    return {
        "output": (
            f"## Screenshot Cleanup\n"
            f"Deleted by age: {stats.get('deleted_by_age', 0)}\n"
            f"Deleted by size: {stats.get('deleted_by_size', 0)}\n"
            f"Remaining files: {stats.get('total_kept', 0)}\n"
            f"Directory size: {stats.get('dir_size_mb', 0)} MB"
        ),
        **stats,
    }


# ── Element query handlers ─────────────────────────────────────────


@handler("get_text")
def _get_text(params: dict) -> dict[str, Any]:
    """Get text content of a UIA element."""
    from agent_windows.core.tree import resolve_element

    selector = params.get("selector", "")
    if not selector:
        raise ValueError("selector is required")

    el = resolve_element(selector)
    text = el.name or ""

    return {
        "output": f"## Get Text\nSelector: {selector}\nText: {text}",
        "selector": selector,
        "text": text,
        "control_type": el.control_type,
    }


@handler("element_state")
def _element_state(params: dict) -> dict[str, Any]:
    """Check UIA element state: visible or enabled."""
    from agent_windows.core.tree import resolve_element

    selector = params.get("selector", "")
    if not selector:
        raise ValueError("selector is required")

    check = params.get("check", "")
    if check not in ("visible", "enabled"):
        raise ValueError("check must be 'visible' or 'enabled'")

    el = resolve_element(selector)

    if check == "visible":
        result = not el.is_offscreen
    else:
        result = el.is_enabled

    return {
        "output": f"## Element State\nSelector: {selector}\n{check}: {result}",
        "selector": selector,
        "check": check,
        "result": result,
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


@handler("dblclick")
def _dblclick(params: dict) -> dict[str, Any]:
    """Double-click at coordinates."""
    from agent_windows.core.input_controller import click

    x, y = params["x"], params["y"]
    right = params.get("right", False)
    click(x, y, right=right, double=True)
    return {"output": f"## Double Click\nPosition: ({x}, {y})\nButton: {'right' if right else 'left'}"}


@handler("hover")
def _hover(params: dict) -> dict[str, Any]:
    """Hover at position to trigger tooltip/highlight."""
    from agent_windows.core.input_controller import hover

    x, y = params["x"], params["y"]
    hover(x, y)
    return {"output": f"## Hover\nPosition: ({x}, {y})"}


@handler("mouse_down")
def _mouse_down(params: dict) -> dict[str, Any]:
    """Press and hold mouse button."""
    from agent_windows.core.input_controller import mouse_down

    x, y = params["x"], params["y"]
    button = params.get("button", "left")
    mouse_down(x, y, button=button)
    return {"output": f"## Mouse Down\nPosition: ({x}, {y})\nButton: {button}"}


@handler("mouse_up")
def _mouse_up(params: dict) -> dict[str, Any]:
    """Release mouse button."""
    from agent_windows.core.input_controller import mouse_up

    x, y = params["x"], params["y"]
    button = params.get("button", "left")
    mouse_up(x, y, button=button)
    return {"output": f"## Mouse Up\nPosition: ({x}, {y})\nButton: {button}"}


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


# ── System handlers ───────────────────────────────────────────────


@handler("app_launch")
def _app_launch(params: dict) -> dict[str, Any]:
    """Launch an application."""
    from agent_windows.core.app_manager import launch_app
    return launch_app(params["name_or_path"], params.get("args"))


@handler("app_switch")
def _app_switch(params: dict) -> dict[str, Any]:
    """Switch to an application window."""
    from agent_windows.core.app_manager import switch_window
    return switch_window(params["name_or_title"])


@handler("app_resize")
def _app_resize(params: dict) -> dict[str, Any]:
    """Resize an application window."""
    from agent_windows.core.app_manager import resize_window
    return resize_window(
        params["name_or_title"], params["width"], params["height"],
        params.get("x", -1), params.get("y", -1),
    )


@handler("app_list")
def _app_list(params: dict) -> dict[str, Any]:
    """List all visible windows."""
    from agent_windows.core.app_manager import list_windows
    return list_windows()


@handler("shell")
def _shell(params: dict) -> dict[str, Any]:
    """Execute a PowerShell command."""
    from agent_windows.core.shell_runner import run_shell
    return run_shell(params["command"])


@handler("fs_read")
def _fs_read(params: dict) -> dict[str, Any]:
    """Read a file."""
    from agent_windows.core.filesystem import read_file
    return read_file(params["path"])


@handler("fs_write")
def _fs_write(params: dict) -> dict[str, Any]:
    """Write to a file."""
    from agent_windows.core.filesystem import write_file
    return write_file(params["path"], params["content"])


@handler("fs_list")
def _fs_list(params: dict) -> dict[str, Any]:
    """List directory contents."""
    from agent_windows.core.filesystem import list_dir
    return list_dir(params["path"])


@handler("clipboard_get")
def _clipboard_get(params: dict) -> dict[str, Any]:
    """Get clipboard content."""
    from agent_windows.core.clipboard import get_clipboard
    from agent_windows.utils.config import load_config
    cfg_method = load_config().get("clipboard", {}).get("method", "system")
    keyboard = params.get("keyboard", cfg_method == "keyboard")
    return get_clipboard(keyboard=keyboard)


@handler("clipboard_set")
def _clipboard_set(params: dict) -> dict[str, Any]:
    """Set clipboard content."""
    from agent_windows.core.clipboard import set_clipboard
    from agent_windows.utils.config import load_config
    cfg_method = load_config().get("clipboard", {}).get("method", "system")
    keyboard = params.get("keyboard", cfg_method == "keyboard")
    return set_clipboard(params["text"], keyboard=keyboard)


@handler("process_list")
def _process_list(params: dict) -> dict[str, Any]:
    """List running processes."""
    from agent_windows.core.process_manager import list_processes
    return list_processes()


@handler("process_kill")
def _process_kill(params: dict) -> dict[str, Any]:
    """Kill a process."""
    from agent_windows.core.process_manager import kill_process
    return kill_process(params["pid_or_name"])


# ── Batch handler ────────────────────────────────────────────────────


@handler("batch")
def _batch(params: dict) -> dict[str, Any]:
    """Execute multiple commands sequentially in a single IPC request.

    Params:
        commands: list of {"method": str, "params": dict (optional)}
        stop_on_error: bool (default True) — stop on first error
        delay: float (default 0.0) — seconds between commands

    Returns:
        {"output": ..., "results": [...], "total": int, "success": int}
    """
    commands = params.get("commands", [])
    if not isinstance(commands, list) or not commands:
        raise ValueError("commands must be a non-empty list")

    stop_on_error = params.get("stop_on_error", True)
    delay = params.get("delay", 0.0)
    if not isinstance(delay, (int, float)) or delay < 0:
        raise ValueError("delay must be a non-negative number")

    results: list[dict[str, Any]] = []
    for cmd in commands:
        method = cmd.get("method", "")
        if not method:
            results.append({"method": "", "ok": False, "error": {"code": -32602, "message": "Missing method"}})
            if stop_on_error:
                break
            continue

        if method == "batch":
            results.append({"method": method, "ok": False, "error": {"code": -32602, "message": "Nested batch is not allowed"}})
            if stop_on_error:
                break
            continue

        req = JSONRPCRequest(id=0, method=method, params=cmd.get("params"))
        resp = route_request(req)

        entry: dict[str, Any] = {"method": method, "ok": resp.error is None}
        if resp.error is not None:
            entry["error"] = resp.error
        else:
            entry["result"] = resp.result
        results.append(entry)

        if resp.error is not None and stop_on_error:
            break

        if delay > 0:
            time.sleep(delay)

    ok_count = sum(1 for r in results if r["ok"])
    return {
        "output": f"## Batch\nExecuted: {len(results)}/{len(commands)}\nSuccess: {ok_count}\nFailed: {len(results) - ok_count}",
        "results": results,
        "total": len(results),
        "success": ok_count,
    }
