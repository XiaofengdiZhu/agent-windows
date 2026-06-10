"""JSON-RPC command router: method name → handler function.

All command handlers receive a params dict and return a result dict (or raise an error).
Handlers are registered via the @handler decorator or directly in HANDLERS.
"""

import logging
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
