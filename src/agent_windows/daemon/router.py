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
    COORDINATE_OUT_OF_RANGE,
    ELEMENT_NOT_FOUND,
    DAEMON_INTERNAL_ERROR,
    WAIT_TIMEOUT,
    PERMISSION_DENIED,
)

logger = logging.getLogger(__name__)

# Global handler registry
HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {}


class CoordinateRangeError(ValueError):
    """Raised when an input coordinate falls outside the virtual screen.

    A ``ValueError`` subclass so handlers that don't care still surface it as
    INVALID_PARAMS; ``route_request`` specifically maps it to
    ``COORDINATE_OUT_OF_RANGE`` (-1002). (SEC3-02 / QUAL3-05)
    """


def handler(method: str):
    """Decorator to register a function as a JSON-RPC handler."""
    def decorator(func: Callable[..., dict[str, Any]]):
        if method in HANDLERS:
            logger.warning("Overwriting existing handler for %r", method)
        HANDLERS[method] = func
        return func
    return decorator


def require(
    params: dict,
    key: str,
    expected_type: type | tuple[type, ...] | None = None,
) -> Any:
    """Return a required request parameter, raising a clear ``ValueError`` if absent.

    Use this instead of bare ``params["key"]`` so a missing parameter surfaces
    to the client as JSON-RPC ``INVALID_PARAMS`` (-32602,
    "missing required parameter 'key'") via :func:`route_request`, rather than
    leaking a :class:`KeyError` as ``DAEMON_INTERNAL_ERROR`` (-32603) (H-10).

    A present-but-``None`` value is treated as missing. When *expected_type* is
    given, a value of the wrong type is rejected with a clear message (also a
    ``ValueError``, so it maps to ``INVALID_PARAMS`` as well).
    """
    if key not in params or params[key] is None:
        raise ValueError(f"missing required parameter '{key}'")
    value = params[key]
    if expected_type is not None and not isinstance(value, expected_type):
        if isinstance(expected_type, tuple):
            names = " or ".join(t.__name__ for t in expected_type)
        else:
            names = expected_type.__name__
        raise ValueError(f"parameter '{key}' must be {names}, got {type(value).__name__}")
    return value


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
    except CoordinateRangeError as e:
        # Out-of-screen coordinates → COORDINATE_OUT_OF_RANGE (-1002), a more
        # specific code than the generic INVALID_PARAMS. Must be caught before
        # the ValueError clause below since it's a subclass. (SEC3-02/QUAL3-05)
        return make_error(COORDINATE_OUT_OF_RANGE, str(e), request.id)
    except (ValueError, IndexError) as e:
        return make_error(INVALID_PARAMS, str(e), request.id)
    except FileNotFoundError as e:
        # Missing file/dir (and missing UIA element) → ELEMENT_NOT_FOUND (-1001),
        # a more specific code than the generic DAEMON_INTERNAL_ERROR and without
        # the logger.exception log spam a routine "file not found" would cause.
        return make_error(ELEMENT_NOT_FOUND, str(e), request.id)
    except TimeoutError as e:
        return make_error(WAIT_TIMEOUT, str(e), request.id)
    except PermissionError as e:
        # M-05: shell blacklist (and any other policy denial) → PERMISSION_DENIED,
        # not the generic DAEMON_INTERNAL_ERROR.
        return make_error(PERMISSION_DENIED, str(e), request.id)
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
