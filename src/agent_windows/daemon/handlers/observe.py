"""Observe handler: combined snapshot + screenshot + grounding."""

from typing import Any

from agent_windows.daemon.router import HANDLERS, handler


@handler("observe")
def _observe(params: dict) -> dict[str, Any]:
    """Combined observation: snapshot + screenshot + grounding."""
    parts = []

    want_snapshot = params.get("snapshot", False)
    want_screenshot = params.get("screenshot", False)
    ground_desc = params.get("ground", "")

    if not want_snapshot and not want_screenshot and not ground_desc:
        want_snapshot = True
        want_screenshot = True

    if want_snapshot:
        snap_result = HANDLERS["snapshot"]({})
        parts.append(snap_result.get("output", ""))

    if want_screenshot:
        if parts:
            parts.append("---\n")
        ss_result = HANDLERS["screenshot"]({})
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
