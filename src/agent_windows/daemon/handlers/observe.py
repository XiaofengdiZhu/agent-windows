"""Observe handler: combined snapshot + screenshot (always both)."""

import dataclasses
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
    """Combined observation: snapshot + screenshot (always both).

    The bundle is observe's reason to exist — for a single piece call
    ``snapshot``/``screenshot`` directly. Each sub-command runs through
    route_request so a failure surfaces its specific JSON-RPC code inline
    rather than collapsing into DAEMON_INTERNAL_ERROR. (ARCH3-01)

    Optional ``use_annotation`` (width/height_reference_line) is forwarded to
    the screenshot sub-call so observe can return a numbered-box image (@eN)
    alongside the snapshot's table — one call yields "image + text index".

    Failure reporting: the result carries a structured ``errors`` list of
    ``{"method", "code", "message"}`` for every sub-command that failed, so a
    programmatic caller can detect partial/total failure. A *partial* failure
    still returns 200-OK with ``output`` + ``errors``; if *both* sub-commands
    failed, observe raises (→ DAEMON_INTERNAL_ERROR) — no useful output.
    """
    parts: list[str] = []
    errors: list[dict] = []

    # Optional multi-display filter: forward to snapshot (clips windows/
    # elements/cursor) and crop the screenshot to the union bbox. (TODO #9-B)
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

    # Optional annotation → forward to the screenshot sub-call.
    raw_annotate = params.get("use_annotation")
    if raw_annotate is True or (
        isinstance(raw_annotate, str) and raw_annotate.lower() == "true"
    ):
        screenshot_params["use_annotation"] = True
        wgrid = int(params.get("width_reference_line", 0) or 0)
        hgrid = int(params.get("height_reference_line", 0) or 0)
        if wgrid > 0:
            screenshot_params["width_reference_line"] = wgrid
        if hgrid > 0:
            screenshot_params["height_reference_line"] = hgrid

    def _append(method: str, sub_params: dict) -> None:
        text, error = _run_sub(method, sub_params)
        if parts:
            parts.append("---\n")
        parts.append(text)
        if error is not None:
            errors.append(error)

    _append("snapshot", snapshot_params)
    _append("screenshot", screenshot_params)

    # Both sub-commands failed → no useful output → raise (don't return 200-OK).
    if len(errors) == 2:
        detail = "; ".join(f"{e['method']}: {e['message']}" for e in errors)
        raise RuntimeError(f"observe failed — every sub-command failed ({detail})")

    result: dict[str, Any] = {"output": "\n".join(parts)}
    if errors:
        result["errors"] = errors
    return result


def _render_displays(displays: list, dpi_scale: float) -> str:
    """Render a list of DisplayInfo as a markdown table (list-displays output)."""
    lines = [f"## Displays ({len(displays)})\n"]
    if not displays:
        lines.append("_No displays detected._\n")
    else:
        lines.append(
            "| # | Primary | Resolution | Rect (L,T,R,B) | "
            "Work Area (L,T,R,B) | Device |"
        )
        lines.append("|---|---------|------------|----------------|"
                     "---------------------|--------|")
        for d in displays:
            primary = "*" if d.is_primary else ""
            lines.append(
                f"| {d.index} | {primary} | {d.width}x{d.height} | "
                f"{d.left},{d.top},{d.right},{d.bottom} | "
                f"{d.work_left},{d.work_top},{d.work_right},{d.work_bottom} | "
                f"{d.device_name} |"
            )
    lines.append(f"\nSystem DPI scale: {dpi_scale}")
    return "\n".join(lines)


@handler("list_displays")
def _list_displays(params: dict) -> dict[str, Any]:
    """List all displays: index, primary, resolution, rect, work area, device.

    Index order matches ``-d N`` / ``--display N`` (EnumDisplayMonitors). Returns
    a markdown table under ``output`` plus a structured ``displays`` list,
    ``count``, and ``dpi_scale`` so ``--format json`` consumers get machine-
    readable data (same multi-field pattern as ``get_text``).
    """
    from agent_windows.core.display import enumerate_displays
    from agent_windows.core.screenshot import get_dpi_scale

    displays = enumerate_displays()
    dpi_scale = get_dpi_scale()
    return {
        "output": _render_displays(displays, dpi_scale),
        # width/height are @property on DisplayInfo, so dataclasses.asdict omits
        # them — fold them in explicitly so JSON consumers get the resolution
        # too (parity with the markdown Resolution column).
        "displays": [
            {**dataclasses.asdict(d), "width": d.width, "height": d.height}
            for d in displays
        ],
        "count": len(displays),
        "dpi_scale": dpi_scale,
    }
