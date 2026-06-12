"""JSON-RPC command router: method name → handler function.

All command handlers receive a params dict and return a result dict (or raise an error).
Handlers are registered via the @handler decorator or directly in HANDLERS.

Actual handler implementations live in the daemon.handlers subpackage.
"""

import logging
from typing import Any, Callable

from agent_windows.ipc.protocol import (
    JSONRPCRequest,
    JSONRPCResponse,
    make_error,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    DAEMON_INTERNAL_ERROR,
    WAIT_TIMEOUT,
)

logger = logging.getLogger(__name__)

# Global handler registry
HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {}


def handler(method: str):
    """Decorator to register a function as a JSON-RPC handler."""
    def decorator(func: Callable[..., dict[str, Any]]):
        if method in HANDLERS:
            logger.warning("Overwriting existing handler for %r", method)
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
    except TimeoutError as e:
        return make_error(WAIT_TIMEOUT, str(e), request.id)
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


# Import handler subpackage to trigger @handler registration for all commands.
# This must be at the bottom to avoid circular imports (handlers import
# `handler` / `HANDLERS` from this module, and this imports the handlers package).
import agent_windows.daemon.handlers  # noqa: E402, F401
