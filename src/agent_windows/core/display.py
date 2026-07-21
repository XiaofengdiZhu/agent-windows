"""Multi-display filtering for snapshot/observe (TODO #9-B).

Pure functions — no COM calls except ``GetMonitorsRect`` (lazily imported inside :func:`get_display_union_bbox`) and the ``EnumDisplayMonitors`` + ``GetMonitorInfoW`` calls inside :func:`enumerate_displays` (both lazy ctypes inside that function). The snapshot handler calls
:func:`filter_snapshot_to_displays` with the dict from :func:`traverse_tree`;
everything here is unit-testable with a hand-built snapshot dict and a patched
``GetMonitorsRect``.

Filtering uses **intersect-clip** semantics (not "window belongs to one
display"): each window/element bbox is CLIPPED to the union of the selected
displays; a bbox that does not intersect at all is dropped. A window spanning
two displays is retained but its bbox is clipped to the selected display(s).
This matches the Windows-MCP reference (``_clip_bounding_box_to_region`` /
``_filter_window_to_region`` / ``_filter_tree_state_to_region``).

The semantic-tree text (``## Accessibility Tree``) is NOT filtered — it is a
pre-rendered free-text string with no reliable bbox per line. Instead a
``display_filter`` note is attached for render_snapshot to surface.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from agent_windows.core.tree import BoundingBox, ElementInfo, WindowInfo


def parse_display_selection(
    display: int | list[int] | tuple[int, ...] | None,
) -> list[int] | None:
    """Parse a ``display`` argument into a de-duplicated list of indices.

    Accepts ``None`` (→ None, no filter), a single ``int``, or a list/tuple of
    ints. Rejects ``bool`` (Python ``bool`` is an ``int`` subclass — a stray
    ``True`` must not silently become display 1), negative values, non-ints,
    and duplicates. Returns ``None`` for an empty selection so the caller can
    treat "no filter" uniformly. Mirrors Windows-MCP's
    ``Desktop.parse_display_selection``, but is intentionally stricter: it also
    rejects ``bool`` *elements* (e.g. ``[True]``), which the reference's
    ``not isinstance(value, int)`` check would accept as display 1.
    """
    if display is None or display == "":
        return None

    if isinstance(display, bool):
        raise ValueError(
            "display must be a JSON array of non-negative integers, "
            "for example [0] or [0,1]"
        )

    if isinstance(display, int):
        values = [display]
    elif isinstance(display, (list, tuple)):
        values = list(display)
    else:
        raise ValueError(
            "display must be a JSON array of non-negative integers, "
            "for example [0] or [0,1]"
        )

    unique: list[int] = []
    for value in values:
        # Reject bool elements too (True/False are ints).
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("display must contain only non-negative integers")
        if value not in unique:
            unique.append(value)
    return unique or None


def get_display_union_bbox(indices: list[int]) -> BoundingBox:
    """Return the union bbox of the selected displays.

    Lazily imports :func:`GetMonitorsRect` (``EnumDisplayMonitors`` order —
    same assumption as the dxcam per-output fix, TODO #9-A) and takes the
    bounding box spanning the selected indices (min left/top, max right/bottom).
    Raises ``ValueError`` (→ INVALID_PARAMS) if a monitor list is empty or an
    index is out of range.
    """
    from agent_windows.uia.core import GetMonitorsRect

    monitors = GetMonitorsRect()
    if not monitors:
        raise ValueError("No displays detected")

    invalid = [i for i in indices if i >= len(monitors)]
    if invalid:
        raise ValueError(
            f"Invalid display index {invalid[0]}. "
            f"Available displays: 0-{len(monitors) - 1}"
        )

    selected = [monitors[i] for i in indices]
    return BoundingBox(
        left=min(m.left for m in selected),
        top=min(m.top for m in selected),
        right=max(m.right for m in selected),
        bottom=max(m.bottom for m in selected),
    )


def clip_bbox_to_region(
    box: BoundingBox | None, region: BoundingBox
) -> BoundingBox | None:
    """Clip ``box`` to ``region``; return None if they do not overlap.

    Adapted from Windows-MCP's ``_clip_bounding_box_to_region``. A box fully
    inside the region is returned unchanged; a box partially outside is clipped
    to the region edges; a box with no overlap (right<=left or bottom<=top
    after clipping) returns None.
    """
    if box is None:
        return None
    left = max(box.left, region.left)
    top = max(box.top, region.top)
    right = min(box.right, region.right)
    bottom = min(box.bottom, region.bottom)
    if right <= left or bottom <= top:
        return None
    return BoundingBox(left, top, right, bottom)


def point_in_region(point: tuple[int, int] | None, region: BoundingBox) -> bool:
    """True if ``point`` lies inside ``region`` (half-open, like the reference)."""
    if point is None:
        return False
    x, y = point
    return region.left <= x < region.right and region.top <= y < region.bottom


def filter_snapshot_to_displays(
    data: dict[str, Any], indices: list[int]
) -> dict[str, Any]:
    """Post-filter a ``traverse_tree`` snapshot dict to the selected displays.

    Uses intersect-clip semantics: each window/element bbox is clipped to the
    selected displays' union; non-overlapping items are dropped. The active
    window is clipped (→ None if off-display) and the cursor is nulled when it
    falls outside the region. The semantic-tree text is NOT filtered (free
    text); instead a ``display_filter`` note is attached for render_snapshot.

    Returns a NEW dict (the original is untouched). Desktop metadata
    (resolution/virtual_screen/dpi_scale/displays) is preserved unchanged.
    """
    region = get_display_union_bbox(indices)

    # Windows: clip each bbox; drop non-overlapping.
    filtered_windows: list[WindowInfo] = []
    for win in data.get("windows", []):
        clipped = clip_bbox_to_region(win.bbox, region)
        if clipped is None:
            continue
        filtered_windows.append(
            dataclasses.replace(win, bbox=clipped) if clipped != win.bbox else win
        )

    # Active window: clip; None if off-display.
    active = data.get("active_window")
    filtered_active: WindowInfo | None = None
    if active is not None:
        clipped = clip_bbox_to_region(active.bbox, region)
        if clipped is not None:
            filtered_active = (
                dataclasses.replace(active, bbox=clipped)
                if clipped != active.bbox else active
            )

    # Interactive elements: clip; drop non-overlapping. Modal-first order is
    # preserved (traverse_tree already promoted modals; filtering only drops,
    # never reorders, so the promotion survives). CRITICAL: each survivor keeps
    # its ORIGINAL 1-based index (stashed in metadata["_orig_idx"]) so that
    # render_snapshot emits non-contiguous @eN refs (@e1, @e3, @e7, …) that map
    # 1:1 back to resolve_element's UNFILTERED list. Renumbering from 1 after
    # filtering would make every @eN silently point at the wrong element,
    # because resolve_element resolves against the unfiltered snapshot. (B-1)
    filtered_elements: list[ElementInfo] = []
    for orig_idx, el in enumerate(data.get("interactive_elements", []), start=1):
        clipped = clip_bbox_to_region(el.bbox, region)
        if clipped is None:
            continue
        new_meta = {**el.metadata, "_orig_idx": orig_idx}
        new_bbox = clipped if clipped != el.bbox else el.bbox
        filtered_elements.append(
            dataclasses.replace(el, bbox=new_bbox, metadata=new_meta)
        )

    # Cursor: null when off the selected region. Render surfaces this as N/A.
    cursor = data.get("cursor")
    filtered_cursor = cursor if point_in_region(cursor, region) else None

    return {
        **data,
        "windows": filtered_windows,
        "active_window": filtered_active,
        "interactive_elements": filtered_elements,
        "cursor": filtered_cursor,
        "display_filter": indices,
    }


# ── Display enumeration (list-displays) ─────────────────────────────


@dataclasses.dataclass
class DisplayInfo:
    """One physical display, indexed in EnumDisplayMonitors order.

    ``index`` matches the ``-d N`` / ``--display N`` filter (same Win32
    enumeration order), so the N shown by ``list-displays`` is the N you pass
    to ``-d``. ``width``/``height`` are derived (not stored).
    """

    index: int
    is_primary: bool
    left: int
    top: int
    right: int
    bottom: int
    work_left: int
    work_top: int
    work_right: int
    work_bottom: int
    device_name: str

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top


def _enum_monitor_infoex() -> list[dict]:
    """Return raw per-monitor MONITORINFOEXW data in EnumDisplayMonitors order.

    Each dict carries the full rect (``left/top/right/bottom``), the work-area
    rect (``work_*`` — desktop minus taskbar), the primary flag
    (``is_primary``), and ``device_name`` (the raw ``\\\\.\\DISPLAYN`` string).

    Factored out of :func:`enumerate_displays` so the index/field-mapping
    logic is unit-testable with a patched return value (the ctypes call itself
    needs a real desktop). Mirrors the EnumDisplayMonitors callback shape used
    by :func:`agent_windows.uia.core.GetMonitorsRect`.
    """
    import ctypes
    import ctypes.wintypes

    MONITORINFOF_PRIMARY = 1

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("rcMonitor", ctypes.wintypes.RECT),
            ("rcWork", ctypes.wintypes.RECT),
            ("dwFlags", ctypes.wintypes.DWORD),
            ("szDevice", ctypes.wintypes.WCHAR * 32),
        ]

    raw: list[dict] = []

    def _cb(hmonitor, _hdc, _lprc_monitor, _lparam):
        mi = MONITORINFOEXW()
        mi.cbSize = ctypes.sizeof(MONITORINFOEXW)
        # GetMonitorInfoW returning 0 means it failed for this handle — skip
        # it but keep enumerating (return 1) so one bad monitor doesn't blank
        # the list.
        if ctypes.windll.user32.GetMonitorInfoW(hmonitor, ctypes.byref(mi)):
            rm, rw = mi.rcMonitor, mi.rcWork
            raw.append({
                "left": rm.left, "top": rm.top, "right": rm.right, "bottom": rm.bottom,
                "work_left": rw.left, "work_top": rw.top,
                "work_right": rw.right, "work_bottom": rw.bottom,
                "is_primary": bool(mi.dwFlags & MONITORINFOF_PRIMARY),
                "device_name": mi.szDevice or "",
            })
        return 1

    MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int, ctypes.wintypes.HMONITOR, ctypes.wintypes.HDC,
        ctypes.POINTER(ctypes.wintypes.RECT), ctypes.wintypes.LPARAM,
    )
    ctypes.windll.user32.EnumDisplayMonitors(
        ctypes.c_void_p(0), ctypes.c_void_p(0),
        MONITORENUMPROC(_cb), 0,
    )
    return raw


def enumerate_displays() -> list[DisplayInfo]:
    """Enumerate all displays as :class:`DisplayInfo`, 0-based, in
    EnumDisplayMonitors order.

    Index order is identical to :func:`agent_windows.uia.core.GetMonitorsRect`
    and to the ``-d N`` / ``--display N`` filter — the same Win32 enumeration.
    This is load-bearing: ``list-displays`` exists to show which N to pass to
    ``-d``, so the indices must line up exactly.

    Returns an empty list on a headless/no-monitor box (not an error — listing
    displays is legitimate even when there are none).
    """
    return [
        DisplayInfo(
            index=i,
            is_primary=m["is_primary"],
            left=m["left"], top=m["top"], right=m["right"], bottom=m["bottom"],
            work_left=m["work_left"], work_top=m["work_top"],
            work_right=m["work_right"], work_bottom=m["work_bottom"],
            device_name=m["device_name"],
        )
        for i, m in enumerate(_enum_monitor_infoex())
    ]
