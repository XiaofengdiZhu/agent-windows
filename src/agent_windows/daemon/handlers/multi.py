"""Batch interaction handlers: MultiSelect and MultiEdit (TODO #12).

Both accept a list of targets as either explicit coordinates (``locs``) or
element refs resolved via the accessibility tree (``selectors``). Coordinates
are validated against the virtual screen; selectors resolve to bbox centers
(see resolve_element_center, TODO #13).
"""

from typing import Any

from agent_windows.daemon.router import handler, CoordinateRangeError
from agent_windows.daemon.handlers.batch import MAX_BATCH_SIZE
from agent_windows.daemon.handlers.input import MAX_TYPE_TEXT_LEN

# DoS bound: a batch interaction sleeps per target (multi_select waits 0.2s per
# click), so an unbounded list blocks the single STA worker. Reuse batch's cap
# so the two stay in sync if either is revisited.
MAX_MULTI_TARGETS = MAX_BATCH_SIZE


def _resolve_selectors(selectors: list[str]) -> list[tuple[int, int]]:
    """Resolve a list of ``@eN`` selectors to ``(x, y)`` centers. (TODO #12/#13)

    Each selector is resolved independently via resolve_element_center. A
    CoordinateRangeError (off-screen center) is re-raised AS-IS so the router
    maps it to COORDINATE_OUT_OF_RANGE; other ValueError / IndexError are
    rewrapped with the offending index so the caller knows which entry failed.
    """
    from agent_windows.core.tree import resolve_element_center

    if not isinstance(selectors, list) or not all(
            isinstance(s, str) for s in selectors):
        raise ValueError("selectors must be a list of strings (@eN refs)")
    points: list[tuple[int, int]] = []
    for i, sel in enumerate(selectors):
        try:
            points.append(resolve_element_center(sel))
        except CoordinateRangeError:
            # Preserve COORDINATE_OUT_OF_RANGE — do NOT rewrap as ValueError
            # (that would downgrade it to INVALID_PARAMS at the router).
            raise
        except (ValueError, IndexError) as e:
            raise ValueError(f"selectors[{i}] ({sel!r}): {e}") from e
    return points


def _validate_point(x: Any, y: Any) -> tuple[int, int]:
    """Validate one (x, y) pair from a locs entry → COORDINATE_OUT_OF_RANGE."""
    from agent_windows.core.input_controller import validate_coordinates
    return validate_coordinates(x, y)


def _parse_select_locs(locs: Any) -> list[tuple[int, int]]:
    """Parse and validate the ``locs`` arg for multi_select: ``[[x, y], ...]``."""
    if not isinstance(locs, list) or not locs:
        raise ValueError("locs must be a non-empty list of [x, y] pairs")
    points: list[tuple[int, int]] = []
    for i, entry in enumerate(locs):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError(
                f"locs[{i}] must be a [x, y] pair, got {entry!r}")
        points.append(_validate_point(entry[0], entry[1]))
    return points


def _parse_edit_locs(locs: Any) -> list[tuple[int, int, str]]:
    """Parse and validate the ``locs`` arg for multi_edit: ``[[x, y, text], ...]``."""
    if not isinstance(locs, list) or not locs:
        raise ValueError("locs must be a non-empty list of [x, y, text] triples")
    triples: list[tuple[int, int, str]] = []
    for i, entry in enumerate(locs):
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ValueError(
                f"locs[{i}] must be a [x, y, text] triple, got {entry!r}")
        x, y = _validate_point(entry[0], entry[1])
        text = entry[2]
        if not isinstance(text, str):
            raise ValueError(f"locs[{i}] text must be a string, got {text!r}")
        if len(text) > MAX_TYPE_TEXT_LEN:
            raise ValueError(
                f"locs[{i}] text exceeds the {MAX_TYPE_TEXT_LEN}-character maximum")
        triples.append((x, y, text))
    return triples


def _parse_edit_selectors(items: Any) -> list[tuple[int, int, str]]:
    """Parse ``selectors`` for multi_edit: ``[[@eN, text], ...]`` → resolved triples."""
    if not isinstance(items, list) or not items:
        raise ValueError("selectors must be a non-empty list of [@eN, text] pairs")
    from agent_windows.core.tree import resolve_element_center

    triples: list[tuple[int, int, str]] = []
    for i, entry in enumerate(items):
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError(
                f"selectors[{i}] must be a [@eN, text] pair, got {entry!r}")
        sel, text = entry
        if not isinstance(sel, str):
            raise ValueError(f"selectors[{i}] element ref must be a string, got {sel!r}")
        if not isinstance(text, str):
            raise ValueError(f"selectors[{i}] text must be a string, got {text!r}")
        if len(text) > MAX_TYPE_TEXT_LEN:
            raise ValueError(
                f"selectors[{i}] text exceeds the {MAX_TYPE_TEXT_LEN}-character maximum")
        try:
            x, y = resolve_element_center(sel)
        except CoordinateRangeError:
            # Preserve COORDINATE_OUT_OF_RANGE — see _resolve_selectors.
            raise
        except (ValueError, IndexError) as e:
            raise ValueError(f"selectors[{i}] ({sel!r}): {e}") from e
        triples.append((x, y, text))
    return triples


def _check_count(raw: object) -> None:
    """Reject an oversized target list BEFORE any COM work. (TODO #12 DoS bound)

    Each selector resolution can trigger a UIA traversal (up to 2s on the single
    STA worker), so the cap must be enforced against the raw input length — not
    after resolution, which would already have done the work we're bounding.
    Non-list values fall through (shape validation later raises a clearer error).
    """
    if isinstance(raw, list) and len(raw) > MAX_MULTI_TARGETS:
        raise ValueError(
            f"too many targets (max {MAX_MULTI_TARGETS}, got {len(raw)})")


@handler("multi_select")
def _multi_select(params: dict) -> dict[str, Any]:
    """Click multiple targets, optionally holding Ctrl for multi-selection.

    Provide exactly one of ``locs`` (``[[x, y], ...]``) or ``selectors``
    (``["@e1", "@e3", ...]``). ``press_ctrl`` (default True) holds Ctrl across
    all clicks so each toggles selection rather than activating.
    """
    from agent_windows.core.input_controller import multi_select

    locs_raw = params.get("locs")
    selectors_raw = params.get("selectors")
    if locs_raw is None and selectors_raw is None:
        raise ValueError("provide either locs or selectors")
    if locs_raw is not None and selectors_raw is not None:
        raise ValueError("provide locs OR selectors, not both")

    # Validate scalar params BEFORE any COM work — a bad press_ctrl shouldn't
    # waste up to 50 UIA traversals before surfacing.
    press_ctrl = params.get("press_ctrl", True)
    if not isinstance(press_ctrl, bool):
        raise ValueError("press_ctrl must be a boolean")

    # Cap BEFORE resolution: a selector list can each trigger a UIA traversal.
    _check_count(locs_raw)
    _check_count(selectors_raw)

    if selectors_raw is not None:
        points = _resolve_selectors(selectors_raw)
    else:
        points = _parse_select_locs(locs_raw)

    multi_select(press_ctrl, points)
    loc_strs = [f"({x},{y})" for x, y in points]
    return {
        "output": (
            f"## MultiSelect\n"
            f"Targets: {len(points)}\n"
            f"Ctrl: {press_ctrl}\n"
            + "\n".join(loc_strs)
        ),
        "count": len(points),
        "press_ctrl": press_ctrl,
        "locations": loc_strs,
    }


@handler("multi_edit")
def _multi_edit(params: dict) -> dict[str, Any]:
    """Type text into multiple fields in one call.

    Provide exactly one of ``locs`` (``[[x, y, text], ...]``) or ``selectors``
    (``[["@e1", "text"], ...]``). Each field is clicked, cleared, and typed into.

    Note: multi_edit commits fields sequentially and does not roll back on
    failure — if field N raises (window closed, COM error), fields 1..N-1 have
    already been typed. The error propagates with its message; callers filling
    critical forms should verify with `snapshot` afterward, or issue separate
    `type` calls per field when rollback matters. (TODO #12)
    """
    from agent_windows.core.input_controller import multi_edit

    locs_raw = params.get("locs")
    selectors_raw = params.get("selectors")
    if locs_raw is None and selectors_raw is None:
        raise ValueError("provide either locs or selectors")
    if locs_raw is not None and selectors_raw is not None:
        raise ValueError("provide locs OR selectors, not both")

    # Cap BEFORE resolution (see _check_count).
    _check_count(locs_raw)
    _check_count(selectors_raw)

    if selectors_raw is not None:
        triples = _parse_edit_selectors(selectors_raw)
    else:
        triples = _parse_edit_locs(locs_raw)

    multi_edit(triples)
    fields = [f"({x},{y})='{text[:40]}'" for x, y, text in triples]
    return {
        "output": (
            f"## MultiEdit\n"
            f"Fields: {len(triples)}\n"
            + ", ".join(fields)
        ),
        "count": len(triples),
        "fields": fields,
    }
