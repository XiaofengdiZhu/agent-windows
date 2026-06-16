"""Observe handler: combined snapshot + screenshot + grounding."""

from typing import Any

from agent_windows.daemon.router import handler
from agent_windows.ipc.protocol import JSONRPCRequest


def _run_sub(method: str, params: dict) -> str:
    """Run a sub-command via route_request so its errors map to the right
    JSON-RPC code (INVALID_PARAMS / VISION_API_ERROR / …) instead of being
    collapsed into a generic -1999 by COMWorkerThread. Returns the formatted
    output text, or an inline error note if the sub-command failed. (ARCH3-01)
    """
    # Imported lazily to avoid the module-level circular import
    # (router.py imports the handlers package at its bottom).
    from agent_windows.daemon.router import route_request

    resp = route_request(JSONRPCRequest(id=0, method=method, params=params))
    if resp.error is not None:
        # Surface the specific error inline rather than failing the whole
        # observe — a snapshot can still be useful even if grounding fails.
        return f"[{method} failed: {resp.error.get('message', 'error')}]"
    result = resp.result or {}
    return str(result.get("output", ""))


@handler("observe")
def _observe(params: dict) -> dict[str, Any]:
    """Combined observation: snapshot + screenshot + grounding.

    Each sub-command runs through route_request so a failure in one (e.g. a
    grounding API error) surfaces its specific JSON-RPC code inline rather than
    collapsing the whole observe into DAEMON_INTERNAL_ERROR. (ARCH3-01)
    """
    parts = []

    want_snapshot = params.get("snapshot", False)
    want_screenshot = params.get("screenshot", False)
    ground_desc = params.get("ground", "")

    if not want_snapshot and not want_screenshot and not ground_desc:
        want_snapshot = True
        want_screenshot = True

    if want_snapshot:
        parts.append(_run_sub("snapshot", {}))

    if want_screenshot:
        if parts:
            parts.append("---\n")
        parts.append(_run_sub("screenshot", {}))

    if ground_desc:
        if parts:
            parts.append("---\n")
        parts.append(_run_sub("ground", {"description": ground_desc}))

    return {"output": "\n".join(parts)}
