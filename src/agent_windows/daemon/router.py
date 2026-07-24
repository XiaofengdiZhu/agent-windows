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
    BACKGROUND_UNAVAILABLE,
)
from agent_windows.uia.exceptions import (
    UIAException,
    UIADeadElementError,
    UIARetryableError,
    UIAInvalidOperationError,
    UIANotEnabledError,
    UIANotSupportedError,
    UIATimeoutError,
    UIAAccessDeniedError,
)
from agent_windows.core.uia_bypass import BackgroundUnavailableError

logger = logging.getLogger(__name__)

# Global handler registry
HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {}

# Process-global tool allowlist, resolved once at daemon startup from the
# ``[tools] include``/``exclude`` config (see :func:`configure_tool_policy`).
# ``None`` = no policy (the default — every registered handler is reachable,
# preserving the original behavior). When set, :func:`route_request` rejects any
# method NOT in this set with PERMISSION_DENIED *before* dispatch, so a client
# connecting directly to the named pipe (bypassing the MCP server, whose own
# tools/exclude_tools filter only trims discovery + the batch gate) is still
# bound by the same operator policy. (defense-in-depth; review #7 follow-up)
#
# The control-plane methods ``ping`` and ``shutdown`` are always allowed even
# under a restrictive policy: an over-restrictive ``[tools]`` typo must not turn
# the daemon into an unkillable, unprobeable process. ``configure_tool_policy``
# forces them into the allowed set.
_TOOL_POLICY: set[str] | None = None

# Methods exempt from the tool policy regardless of [tools] config. These are
# daemon control-plane (health/shutdown), not automation tools.
_POLICY_EXEMPT: frozenset[str] = frozenset({"ping", "shutdown"})


def configure_tool_policy(
    include: list[str] | str | None = None,
    exclude: list[str] | str | None = None,
) -> None:
    """Resolve and install the process-global tool allowlist from ``[tools]``.

    Called once from the daemon's ``_init_daemon_state`` after all handlers are
    registered (so the policy can intersect the live ``HANDLERS`` registry).
    Mirrors the MCP-layer semantics (``utils/config.py`` ToolsConfig +
    ``mcp/server.py`` apply_tool_filter): ``include`` (a whitelist) wins over
    ``exclude``; both default empty = no policy = all tools reachable.

    - ``include`` non-empty → allowed = (include ∩ HANDLERS) ∪ exempt.
    - else ``exclude`` non-empty → allowed = (HANDLERS − exclude) ∪ exempt.
    - both empty → ``None`` (no enforcement; original behavior).

    Accepts a bare string as well as a list: ``config set`` only writes
    scalars, so a TOML-array key set via the setter lands as a comma-joined
    string (e.g. ``"shell,registry"``) rather than a list. ``_normalize``
    tolerates both (and None) — the same defensive handling the MCP serve path
    uses in ``_as_list``. Unknown names are silently ignored, matching
    apply_tool_filter — a typo shouldn't crash startup (the tool simply isn't
    there to allow/deny). Idempotent: each call replaces the previous policy.
    Tests call ``configure_tool_policy(None, None)`` to reset between cases.
    """

    def _normalize(v: list[str] | str | None) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return [t for t in v if t]

    global _TOOL_POLICY
    include = _normalize(include)
    exclude = _normalize(exclude)
    if not include and not exclude:
        _TOOL_POLICY = None
        return
    registered = set(HANDLERS)
    if include:
        allowed = {t for t in include if t in registered}
        mode = "include"
    else:
        allowed = registered - set(exclude)
        mode = "exclude"
    # Control-plane methods are always reachable (don't brick the daemon).
    allowed |= _POLICY_EXEMPT
    _TOOL_POLICY = allowed
    logger.info(
        "Tool policy active (%s): %d of %d methods reachable",
        mode, len(allowed), len(registered),
    )


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

    # Defense-in-depth tool policy (review #7 follow-up): when [tools]
    # include/exclude is configured, a method outside the resolved allowlist is
    # rejected with PERMISSION_DENIED before dispatch. This closes the gap where
    # a client connecting directly to the named pipe (bypassing the MCP server's
    # own tools/exclude_tools filter) could reach any handler. ping/shutdown are
    # always exempt (see configure_tool_policy). _TOOL_POLICY is None by default
    # (no config) → no enforcement, original behavior.
    if _TOOL_POLICY is not None and request.method not in _TOOL_POLICY:
        logger.debug("Tool policy denied %r", request.method)
        return make_error(
            PERMISSION_DENIED,
            f"Method '{request.method}' is excluded by the [tools] policy",
            request.id,
        )

    # Thread caller identity for the audit log: pop the daemon-internal
    # ``__caller`` envelope param BEFORE dispatch (no handler ever sees it),
    # and stash it on the ``audit_caller`` contextvar for this request. The
    # daemon is single-threaded (one STA worker), so a contextvar set/reset
    # around dispatch is safe. Default "pipe" = a direct named-pipe client that
    # injected nothing (best-effort attribution, not an authz boundary). (audit)
    from agent_windows.core.audit import audit_caller
    params = dict(request.params or {})
    # An explicit __caller (CLI="local", MCP=resolved "<tag>:<ip>" scope) wins;
    # otherwise INHERIT the outer contextvar when one is set (a batch
    # sub-dispatch re-entering route_request — batch.py builds sub-params from
    # commands[] without __caller, so without this inherit the outer caller
    # would be clobbered to "pipe" and destructive ops inside batch
    # mis-attributed); otherwise "pipe" (a top-level direct named-pipe client
    # that injects nothing — the original default, unchanged).
    explicit_caller = params.pop("__caller", None)
    caller = explicit_caller or audit_caller.get() or "pipe"
    caller_token = audit_caller.set(caller)
    try:
        try:
            result = handler_func(params)
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
        except UIADeadElementError as e:
            # Element/window gone or its process disconnected (UIA_E_ELEMENTNOT-
            # AVAILABLE / RPC_E_DISCONNECTED / …). The desired action can no longer
            # succeed, so ELEMENT_NOT_FOUND (-1001) is the actionable signal for a
            # client to re-snapshot and retry. (TODO #6 decision 2)
            return make_error(ELEMENT_NOT_FOUND, str(e), request.id)
        except UIAInvalidOperationError as e:
            # UIA_E_INVALIDOPERATION — the element is live but the call is invalid
            # in its current state (e.g. expand on a control that can't now). Maps
            # to INVALID_PARAMS rather than a generic internal error.
            return make_error(INVALID_PARAMS, str(e), request.id)
        except (UIANotEnabledError, UIANotSupportedError) as e:
            # Both = "request is valid JSON-RPC but the element can't satisfy it"
            # (disabled element, or unsupported property/pattern) → INVALID_PARAMS.
            return make_error(INVALID_PARAMS, str(e), request.id)
        except BackgroundUnavailableError as e:
            # Background delivery can't land (no Invoke/Toggle/Select pattern,
            # no live control, no host HWND, or pattern COM failure). The caller
            # decides whether to retry the default (foreground) path.
            return make_error(BACKGROUND_UNAVAILABLE, str(e), request.id)
        except UIATimeoutError as e:
            # UIA/RPC timeout — the typed mirror of the stdlib TimeoutError clause
            # above: WAIT_TIMEOUT (-1003). (TODO #6 decision 2)
            return make_error(WAIT_TIMEOUT, str(e), request.id)
        except UIAAccessDeniedError as e:
            # E_ACCESSDENIED / RPC_E_ACCESS_DENIED (e.g. elevated target from a
            # non-elevated daemon) — the typed mirror of the stdlib PermissionError
            # clause above: PERMISSION_DENIED (-1004). (TODO #6 decision 2)
            return make_error(PERMISSION_DENIED, str(e), request.id)
        except UIARetryableError as e:
            # Target process is busy (RPC_E_CALL_REJECTED / …SERVERCALL_RETRY…).
            # This is transient, not a dead element — the client may retry the very
            # same request. The protocol has no SERVICE_BUSY code, so this surfaces
            # as DAEMON_INTERNAL_ERROR with the typed reason (RPC_E_CALL_REJECTED
            # reaches the client); logged at info (expected under load) rather than
            # .exception (which implies a crash). (TODO #6 decision 2) — a future
            # -1006 SERVICE_BUSY code would let clients retry programmatically.
            logger.info("Retryable UIA error for %s: %s", request.method, e)
            return make_error(DAEMON_INTERNAL_ERROR, str(e), request.id)
        except UIAException as e:
            # Any other typed UIA error (timeout / access-denied / thread / unknown)
            # — log at debug (mostly transient or environmental) and let the typed
            # message surface instead of the bare COMError. Must come AFTER the
            # specific subclasses above. (TODO #6 decision 2)
            logger.debug("UIA error for %s: %s", request.method, e)
            return make_error(DAEMON_INTERNAL_ERROR, str(e), request.id)
        except Exception as e:
            logger.exception("Handler error for %s", request.method)
            return make_error(DAEMON_INTERNAL_ERROR, str(e), request.id)
    finally:
        audit_caller.reset(caller_token)


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
