"""Observe handler: combined snapshot + screenshot + grounding."""

from typing import Any

from agent_windows.daemon.router import handler
from agent_windows.ipc.protocol import JSONRPCRequest


def _run_sub(method: str, params: dict) -> tuple[str, dict | None]:
    """Run a sub-command via route_request so its errors map to the right
    JSON-RPC code (INVALID_PARAMS / VISION_API_ERROR / …) instead of being
    collapsed into a generic -1999 by COMWorkerThread. (ARCH3-01)

    Returns ``(text, error)``: on success ``text`` is the sub-command's output
    and ``error`` is None; on failure ``text`` is an inline ``[<method> failed:
    ...]`` note (so the human-readable ``output`` still explains what went
    wrong) and ``error`` is the structured ``{"method", "code", "message"}``
    dict the caller collects for the structured ``errors`` field. Keeping the
    inline note preserves the old ``output`` shape for clients (MCP/CLI) that
    only read ``output``; the structured field is additive. (F3, round 2)
    """
    # Imported lazily to avoid the module-level circular import
    # (router.py imports the handlers package at its bottom).
    from agent_windows.daemon.router import route_request

    resp = route_request(JSONRPCRequest(id=0, method=method, params=params))
    if resp.error is not None:
        msg = resp.error.get("message", "error")
        # Surface the specific error inline rather than failing the whole
        # observe — a snapshot can still be useful even if grounding fails.
        return (
            f"[{method} failed: {msg}]",
            {"method": method, "code": resp.error.get("code"), "message": msg},
        )
    result = resp.result or {}
    return str(result.get("output", "")), None


@handler("observe")
def _observe(params: dict) -> dict[str, Any]:
    """Combined observation: snapshot + screenshot + grounding.

    Each sub-command runs through route_request so a failure in one (e.g. a
    grounding API error) surfaces its specific JSON-RPC code inline rather than
    collapsing the whole observe into DAEMON_INTERNAL_ERROR. (ARCH3-01)

    Failure reporting (F3, round 2): the result carries a structured ``errors``
    list of ``{"method", "code", "message"}`` for every sub-command that failed,
    so a programmatic caller can detect partial/total failure without grepping
    ``output``. A *partial* failure (some sub-commands succeeded) still returns
    200-OK with ``output`` + ``errors`` — the useful parts remain actionable.
    But if *every* requested sub-command failed, observe raises (→
    DAEMON_INTERNAL_ERROR) rather than returning 200-OK, since there is no
    useful output and a success response would mislead callers that branch on
    the JSON-RPC error code / a structured field.
    """
    parts: list[str] = []
    errors: list[dict] = []
    requested = 0  # how many sub-commands were asked for (for the all-failed check)

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

    def _append(method: str, sub_params: dict, *, sep: bool = True) -> None:
        nonlocal requested
        requested += 1
        text, error = _run_sub(method, sub_params)
        if sep and parts:
            parts.append("---\n")
        parts.append(text)
        if error is not None:
            errors.append(error)

    if want_snapshot:
        _append("snapshot", snapshot_params)

    if want_screenshot:
        _append("screenshot", screenshot_params)

    if ground_desc:
        # NOTE: ground operates on the FULL desktop (ground_element has no bbox
        # scope yet), so its coordinates may fall outside the selected display(s)
        # when `display` is set. Surface that inline so the agent doesn't trust
        # a grounding hit that's off the scoped screenshot/snapshot. (B-2)
        if display is not None:
            # Mirror the old layout exactly: a "---\n" between the preceding
            # parts and the note (when parts is non-empty), then the note (which
            # itself ends in "---\n"), then the ground text with NO extra sep.
            if parts:
                parts.append("---\n")
            parts.append(
                "[grounding performed against the full desktop; display filter "
                "applies only to snapshot/screenshot above]\n---\n"
            )
            _append("ground", {"description": ground_desc}, sep=False)
        else:
            _append("ground", {"description": ground_desc})

    # Every requested sub-command failed → no useful output. Raise rather than
    # return 200-OK so programmatic callers see a JSON-RPC error. The full
    # per-sub breakdown is embedded for diagnostics. (A single-sub observe
    # just reports that one failure.)
    if requested > 0 and len(errors) == requested:
        detail = "; ".join(f"{e['method']}: {e['message']}" for e in errors)
        raise RuntimeError(f"observe failed — every sub-command failed ({detail})")

    result: dict[str, Any] = {"output": "\n".join(parts)}
    if errors:
        result["errors"] = errors
    return result
