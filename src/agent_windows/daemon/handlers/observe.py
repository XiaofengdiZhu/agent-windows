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

    # Optional multi-display filter (TODO #9-B): forward to the snapshot sub-
    # call (which clips windows/elements/cursor) and crop the screenshot to the
    # selected displays' union bbox. parse_display_selection raises ValueError
    # on a bad selection (→ INVALID_PARAMS). No display param → no filtering.
    from agent_windows.core.display import (
        parse_display_selection, get_display_union_bbox,
    )
    display = parse_display_selection(params.get("display"))
    screenshot_params: dict = {}
    snapshot_params: dict = {}
    if display is not None:
        snapshot_params["display"] = display
        union = get_display_union_bbox(display)
        screenshot_params["bbox"] = [
            union.left, union.top, union.right, union.bottom
        ]

    if want_snapshot:
        parts.append(_run_sub("snapshot", snapshot_params))

    if want_screenshot:
        if parts:
            parts.append("---\n")
        parts.append(_run_sub("screenshot", screenshot_params))

    if ground_desc:
        if parts:
            parts.append("---\n")
        # NOTE: ground operates on the FULL desktop (ground_element has no bbox
        # scope yet), so its coordinates may fall outside the selected display(s)
        # when `display` is set. Surface that inline so the agent doesn't trust
        # a grounding hit that's off the scoped screenshot/snapshot. (B-2)
        if display is not None:
            parts.append(
                "[grounding performed against the full desktop; display filter "
                "applies only to snapshot/screenshot above]\n---\n"
            )
        parts.append(_run_sub("ground", {"description": ground_desc}))

    return {"output": "\n".join(parts)}
