"""Input handlers: mouse and keyboard automation."""

from typing import Any

from agent_windows.daemon.router import handler, require


# DoS bound: type_text sleeps per character (default 0.02s), so a 10MB string
# would block the single STA worker for ~56 hours. Cap the text length. (SEC3-05)
MAX_TYPE_TEXT_LEN = 10000

# DoS bound: key_down --hold sleeps for the duration on the single STA worker.
# 60s is well under STA_SUBMIT_TIMEOUT_S (300) and any realistic key hold. (F11)
MAX_KEY_HOLD_SECONDS = 60


def _coords(params: dict) -> tuple[int, int]:
    """Extract the target (x, y) coordinates from params. (TODO #13)

    A ``selector`` (``@eN`` element ref) takes priority and is resolved to the
    element's bbox center via ``resolve_element_center`` — matching the
    screenshot handler's "selector takes priority over bbox" rule. Without a
    selector, falls back to the explicit ``x``/``y`` and validates them.

    Raises CoordinateRangeError (→ COORDINATE_OUT_OF_RANGE) for non-numeric or
    out-of-screen values; ValueError (bad selector format / no bbox) and
    IndexError (out-of-range @eN) both surface as INVALID_PARAMS. (SEC3-02)

    Note: unlike the screenshot handler (which warns-and-continues on a
    non-``@e`` selector), input fails loudly here — a silent fallback to x/y
    would risk clicking the wrong target. (TODO #13)
    """
    from agent_windows.core.input_controller import validate_coordinates

    selector = params.get("selector")
    if selector:
        from agent_windows.core.tree import resolve_element_center
        # resolve_element_center validates the center itself; return directly.
        return resolve_element_center(selector)
    return validate_coordinates(require(params, "x"), require(params, "y"))


def _echo_int(v) -> int:
    """Coerce a degraded echo coord to int (str/None/list/bool/float → 0).

    Used only on the background best-effort degrade path so a non-numeric echo
    value never reaches the Position: line (coords are echo-only there).
    """
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _verify_enabled() -> bool:
    """Whether post-op verify lines are appended to input command output.

    Default-on ([input] verify = true in config); the daemon re-reads config per
    command, so `config set input.verify false` disables it live for raw speed.
    """
    from agent_windows.utils.config import load_config
    return bool(load_config().get("input", {}).get("verify", True))


def _resolve_verify_control(selector: str | None) -> Any:
    """Best-effort live UIA ``Control`` for selector-path verify (D3 Task 4).

    Returns None when ``selector`` is falsy (coordinate path — nothing to
    resolve) or when ``_resolve_live_control`` raises (element rebuilt/hidden
    post-op, owning window closed, provider disconnecting mid-FindFirst). verify
    must never block a successful op, so every failure degrades to None and the
    caller falls back to ``hit_element_at(x, y)``. Lazy import keeps this off the
    launcher cold path.
    """
    if not selector:
        return None
    try:
        from agent_windows.core.tree import _resolve_live_control
        return _resolve_live_control(selector)
    except Exception:
        return None


def _uipi_hint(result) -> str:
    """Actionable PermissionError message for a UIPI block."""
    from agent_windows.core.uipi import _RID_NAMES

    def _name(rid):
        return _RID_NAMES.get(rid, "unknown") if rid is not None else "unknown"
    return (
        f"UIPI: target integrity level ({_name(result.target_il)}) exceeds "
        f"daemon's ({_name(result.own_il)}); SendInput would be silently "
        f"dropped. Retry with --background (UIA Invoke bypasses UIPI) or run "
        f"an elevated daemon."
    )


def _require_uipi_clear_at(x, y) -> None:
    """Raise PermissionError if a foreground click/type-at (x,y) would be dropped."""
    from agent_windows.core.uipi import check_uipi_at
    r = check_uipi_at(x, y)
    if r.blocked:
        raise PermissionError(_uipi_hint(r))


def _require_uipi_clear_foreground() -> None:
    """Raise PermissionError if a focused-field type/key would be dropped."""
    from agent_windows.core.uipi import check_uipi_foreground
    r = check_uipi_foreground()
    if r.blocked:
        raise PermissionError(_uipi_hint(r))


def _append_hit_focus(lines: list[str], x: int, y: int,
                      control: Any = None) -> None:
    """Append best-effort Hit + Focus verify lines (P0.1).

    D3 Task 4: when ``control`` is given (selector path — the FindFirst-resolved
    live element), ``Hit:`` reads it via ``hit_element_of(control)`` instead of
    ``hit_element_at(x, y)`` (which is ``ControlFromPoint`` = visual z-order
    topmost, wrong for an occluded target). Falls back to ``hit_element_at`` if
    ``control`` is None, the read returns None, or raises. Never raises — the
    tree helpers are already fault-tolerant (degrade to None/empty) and this
    wraps them in a second guard, so a successful op can never become an error
    from verify. No-op when verify is disabled.
    """
    if not _verify_enabled():
        return
    from agent_windows.core.tree import (
        hit_element_at, hit_element_of, foreground_window)
    try:
        hit = None
        if control is not None:
            # Per-call guard: a raise from hit_element_of degrades to None so
            # the hit_element_at fallback below runs (the outer try/except
            # alone would skip the fallback entirely on a raise).
            try:
                hit = hit_element_of(control)
            except Exception:
                hit = None
        if hit is None:
            hit = hit_element_at(x, y)
        lines.append(
            f"Hit: {hit['control_type']} \"{hit['name']}\""
            if hit else "Hit: (none)")
        fg = foreground_window()
        if fg.get("title"):
            lines.append(f"Focus: \"{fg['title']}\"")
    except Exception:
        pass


def _append_drop_slider(lines: list[str], x2: int, y2: int,
                        control: Any = None) -> None:
    """Append best-effort Drop + Slider verify lines for drag (P0.3).

    D3 Task 4: when ``control`` is given (selector path), ``Drop:`` reads it via
    ``hit_element_of``; else ``hit_element_at(x2, y2)``. Same degrade-on-failure
    rule as ``_append_hit_focus``.

    Single hit-test at the destination: the drop target element, and — if that
    element exposes a RangeValue/Scroll pattern — the post-drag slider
    percentage. File drags surface Drop only; slider drags surface both. Never
    raises; no-op when verify is disabled.
    """
    if not _verify_enabled():
        return
    from agent_windows.core.tree import (
        hit_element_at, hit_element_of, range_percent_at)
    try:
        drop = None
        if control is not None:
            # Same per-call guard as _append_hit_focus: a raise here must
            # degrade to None so hit_element_at runs (not skip the fallback).
            try:
                drop = hit_element_of(control)
            except Exception:
                drop = None
        if drop is None:
            drop = hit_element_at(x2, y2)
        if drop:
            lines.append(f"Drop: {drop['control_type']} \"{drop['name']}\"")
        pct = range_percent_at(x2, y2)
        if pct is not None:
            lines.append(f"Slider: {pct}%")
    except Exception:
        pass


def _safe_field_read(getter) -> dict | None:
    """Run a field-value getter, degrading to None on any failure so verify
    never breaks a successful type (P0.2). The tree helpers already swallow
    internally; this is a second guard against a helper raising unexpectedly.
    """
    try:
        return getter()
    except Exception:
        return None


def _append_field_diff(lines: list[str], pre, post) -> dict | None:
    """Append the Field value diff block to *lines*; return a field_value
    sibling dict (or None). (P0.2)

    pre/post are focused_field_value/field_value_at dicts (or None). Mirrors
    _append_hit_focus/_append_drop_slider: never raises, no-op when verify off.
    A password field yields the hidden line + a sibling with null pre/post (no
    value leak); an unreadable field (no value pattern) yields no block/sibling.
    """
    if not _verify_enabled():
        return None
    from agent_windows.core.tree import format_value_diff, _truncate_with_count
    try:
        block = format_value_diff(pre, post)
        lines.extend(block)
        if not block:
            return None  # nothing readable (no value pattern) → no sibling either
        is_pw = bool((post or {}).get("is_password")) or \
            bool((pre or {}).get("is_password"))
        if is_pw:
            return {"pre": None, "post": None, "is_password": True}
        pv = (pre or {}).get("value")
        qv = (post or {}).get("value")
        return {
            "pre": None if pv is None else _truncate_with_count(pv, 500),
            "post": None if qv is None else _truncate_with_count(qv, 500),
            "is_password": False,
        }
    except Exception:
        return None


@handler("click")
def _click(params: dict) -> dict[str, Any]:
    """Mouse click at coordinates, or a no-focus-steal UIA pattern click."""
    from agent_windows.core.input_controller import click
    from agent_windows.core.uia_bypass import (
        invoke_background, BackgroundUnavailableError)

    background = bool(params.get("background", False))
    selector = params.get("selector")
    right = params.get("right", False)
    double = params.get("double", False)

    if background:
        if right or double:
            raise BackgroundUnavailableError(
                "background mode supports left single-click only "
                "(no Invoke semantics for right/double); retry without --background")
        # Two targeting modes, from invoke_background:
        # - selector present → Invoke fires on the resolved element; x/y are
        #   echo/verify-only and best-effort. A stale/off-screen echo center
        #   (CoordinateRangeError) or a snapshot/COM hiccup (RuntimeError) must
        #   not gate delivery, so those degrade (coerced to int so a bad echo
        #   value never reaches the Position: line). A bad selector
        #   (ValueError/IndexError) bubbles → INVALID_PARAMS.
        # - no selector → ControlFromPoint(x, y) IS the targeting mechanism, so
        #   coords are authoritative: validate strictly. A non-numeric or
        #   off-screen point surfaces (CoordinateRangeError → -1002) instead of
        #   silently misclicking at (0,0).
        from agent_windows.daemon.router import CoordinateRangeError
        if selector:
            try:
                x, y = _coords(params)
            except (CoordinateRangeError, RuntimeError):
                x, y = _echo_int(params.get("x", 0)), _echo_int(params.get("y", 0))
        else:
            x, y = _coords(params)
        res = invoke_background(x, y, selector)
        lines = ["## Click", f"Position: ({x}, {y})",
                 f"Delivery: background ({res['action']})"]
    else:
        x, y = _coords(params)
        _require_uipi_clear_at(x, y)
        click(x, y, right=right, double=double)
        # P2.2: echo only non-default flags — a plain left single click is the
        # common case, so Button/Double lines appear only when they deviate.
        lines = ["## Click", f"Position: ({x}, {y})"]
        if right:
            lines.append("Button: right")
        if double:
            lines.append("Double: true")
    # D3 Task 4: selector (non-background) path — verify reads the
    # FindFirst-resolved control, not ElementFromPoint at the click coord (which
    # would name the visual z-order topmost if the element is occluded).
    # Background delivery uses Invoke and is excluded: coords are echo-only on
    # that branch. Best-effort: a FindFirst miss (element rebuilt/hidden
    # post-click — common) degrades to None and verify falls back to coords.
    verify_control = None if background else _resolve_verify_control(selector)
    _append_hit_focus(lines, x, y, control=verify_control)
    return {"output": "\n".join(lines)}


@handler("dblclick")
def _dblclick(params: dict) -> dict[str, Any]:
    """Double-click at coordinates."""
    from agent_windows.core.input_controller import click

    x, y = _coords(params)
    right = params.get("right", False)
    selector = params.get("selector")
    _require_uipi_clear_at(x, y)
    click(x, y, right=right, double=True)
    lines = ["## Double Click", f"Position: ({x}, {y})"]
    if right:
        lines.append("Button: right")
    _append_hit_focus(lines, x, y,
                      control=_resolve_verify_control(selector))  # D3 Task 4
    return {"output": "\n".join(lines)}


@handler("hover")
def _hover(params: dict) -> dict[str, Any]:
    """Hover at position to trigger tooltip/highlight."""
    from agent_windows.core.input_controller import hover

    x, y = _coords(params)
    selector = params.get("selector")
    hover(x, y)
    lines = ["## Hover", f"Position: ({x}, {y})"]
    _append_hit_focus(lines, x, y,
                      control=_resolve_verify_control(selector))  # D3 Task 4
    return {"output": "\n".join(lines)}


@handler("mouse_down")
def _mouse_down(params: dict) -> dict[str, Any]:
    """Press and hold mouse button."""
    from agent_windows.core.input_controller import mouse_down

    x, y = _coords(params)
    button = params.get("button", "left")
    mouse_down(x, y, button=button)
    lines = ["## Mouse Down", f"Position: ({x}, {y})"]
    if button != "left":
        lines.append(f"Button: {button}")
    return {"output": "\n".join(lines)}


@handler("mouse_up")
def _mouse_up(params: dict) -> dict[str, Any]:
    """Release mouse button."""
    from agent_windows.core.input_controller import mouse_up

    x, y = _coords(params)
    button = params.get("button", "left")
    mouse_up(x, y, button=button)
    lines = ["## Mouse Up", f"Position: ({x}, {y})"]
    if button != "left":
        lines.append(f"Button: {button}")
    return {"output": "\n".join(lines)}


@handler("type")
def _type(params: dict) -> dict[str, Any]:
    """Type text."""
    from agent_windows.core.input_controller import (
        type_text, type_text_at, clear_field, move_caret,
        _send_vk_key, validate_coordinates,
    )

    text = require(params, "text", str)
    from agent_windows.utils.config import load_config
    max_text = load_config().get("input", {}).get("type_text_len", MAX_TYPE_TEXT_LEN)
    if len(text) > max_text:
        raise ValueError(
            f"text length {len(text)} exceeds the {max_text}-character maximum")
    at_x = params.get("at_x")
    at_y = params.get("at_y")
    enter = params.get("enter", False)
    clear = params.get("clear", False)
    # TODO #16: caret_position = start | idle | end. Validated here (not in
    # core) so a bad value surfaces as INVALID_PARAMS rather than a silent
    # no-op inside move_caret. A present-but-None value is treated as the
    # default ("idle"), matching `enter`/`clear` and require()'s None-as-missing
    # convention (router.py) — so `{"caret_position": null}` is a no-op, not
    # an error.
    caret_position = params.get("caret_position") or "idle"
    if caret_position not in ("start", "idle", "end"):
        raise ValueError(
            f"caret_position must be 'start', 'idle', or 'end'; got {caret_position!r}"
        )

    at_selector = params.get("at_selector")
    verify = _verify_enabled()
    pre = post = None
    if at_selector:
        # TODO #13: @eN ref resolves to bbox center, overriding at_x/at_y.
        # Selector-first mirrors _coords' rule and the screenshot handler.
        from agent_windows.core.tree import resolve_element_center, field_value_at
        ax, ay = resolve_element_center(at_selector)
        if verify:
            pre = _safe_field_read(lambda: field_value_at(ax, ay))  # P0.2: old value
        _require_uipi_clear_at(ax, ay)
        type_text_at(text, ax, ay, clear=clear, enter=enter,
                     caret_position=caret_position)
        if verify:
            post = _safe_field_read(lambda: field_value_at(ax, ay))  # P0.2: new value
    elif at_x is not None and at_y is not None:
        ax, ay = validate_coordinates(at_x, at_y)
        if verify:
            from agent_windows.core.tree import field_value_at
            pre = _safe_field_read(lambda: field_value_at(ax, ay))
        _require_uipi_clear_at(ax, ay)
        type_text_at(text, ax, ay, clear=clear, enter=enter,
                     caret_position=caret_position)
        if verify:
            post = _safe_field_read(lambda: field_value_at(ax, ay))
    else:
        # No click: operate on the already-focused field. caret_position still
        # applies (Home/End move the caret in the focused field), matching the
        # reference semantics. (TODO #16)
        from agent_windows.core.tree import focused_field_value
        if verify:
            pre = _safe_field_read(focused_field_value)  # P0.2: before keystroke
        _require_uipi_clear_foreground()
        move_caret(caret_position)
        if clear:
            clear_field()
        type_text(text)
        if enter:
            _send_vk_key(0x0D)  # VK_RETURN
        if verify:
            post = _safe_field_read(focused_field_value)

    # P3.2: no Text: echo. P0.2's Field value diff already reads the field's
    # actual value (untruncated), so echoing the param again would duplicate it
    # AND truncate at 50 chars — the original tail-loss bug. Click paths
    # (--at / --at-selector) echo the resolved target coord and verify what the
    # click hit (P0.1 consistency with click/dblclick/hover/move); the plain
    # focused-field path has no click point. ``clicked`` guards ``ax``/``ay``
    # scope (both click branches above define them; the plain branch does not).
    clicked = at_selector or (at_x is not None and at_y is not None)
    lines = ["## Type"]
    if clicked:
        if at_selector:
            lines.append(f"At: {at_selector} ({ax}, {ay})")
        else:
            lines.append(f"At: ({ax}, {ay})")
    # P2.2: echo only non-default flags. Enter/Clear default False, Caret
    # defaults "idle" — surfacing them on every call was noise.
    if enter:
        lines.append("Enter: true")
    if clear:
        lines.append("Clear: true")
    if caret_position != "idle":
        lines.append(f"Caret: {caret_position}")
    if clicked:
        # D3 Task 4: at_selector path verify reads the FindFirst-resolved
        # control; at_x/at_y path passes None (no selector) → hit_element_at.
        _append_hit_focus(lines, ax, ay,
                          control=_resolve_verify_control(at_selector))
    sibling = _append_field_diff(lines, pre, post)  # P0.2: field-value diff
    result: dict[str, Any] = {"output": "\n".join(lines)}
    if sibling is not None:
        result["field_value"] = sibling
    return result


@handler("scroll")
def _scroll(params: dict) -> dict[str, Any]:
    """Scroll at position."""
    from agent_windows.core.input_controller import scroll_at, MAX_SCROLL_STEPS

    x, y = _coords(params)
    direction = params.get("direction", "down")
    requested = int(params.get("amount", 3))
    # Mirror scroll_at's own clamp so the output reports what actually ran —
    # scroll_at would silently shrink a runaway amount, leaving the agent to
    # believe it scrolled far more than it did. (P3.1)
    amount = max(0, min(requested, MAX_SCROLL_STEPS))
    scroll_at(x, y, direction=direction, amount=amount)
    note = f" (clamped from {requested})" if amount != requested else ""
    return {"output": f"## Scroll\nPosition: ({x}, {y})\nDirection: {direction}\nAmount: {amount}{note}"}


@handler("expand")
def _expand(params: dict) -> dict[str, Any]:
    """Expand an expandable element (@eN) via its ExpandCollapsePattern. (TODO #12)

    Unlike the coordinate-based input handlers, this drives a live UIA Pattern,
    so it only accepts a ``selector`` (@eN) — the snapshot's bbox is reverse-
    looked-up to a live control. See expand_element for semantics (idempotent,
    skips if already expanded).
    """
    from agent_windows.core.tree import expand_element

    selector = require(params, "selector")
    result = expand_element(selector)
    return {"output": (
        f"## Expand\nElement: {selector}\nState: {result['state']}\n"
        f"Skipped: {result.get('skipped', False)}")}


@handler("collapse")
def _collapse(params: dict) -> dict[str, Any]:
    """Collapse an expandable element (@eN) via its ExpandCollapsePattern. (TODO #12)"""
    from agent_windows.core.tree import collapse_element

    selector = require(params, "selector")
    result = collapse_element(selector)
    return {"output": (
        f"## Collapse\nElement: {selector}\nState: {result['state']}\n"
        f"Skipped: {result.get('skipped', False)}")}


@handler("move")
def _move(params: dict) -> dict[str, Any]:
    """Move mouse to position."""
    from agent_windows.core.input_controller import move_to

    x, y = _coords(params)
    selector = params.get("selector")
    move_to(x, y)
    lines = ["## Move", f"Position: ({x}, {y})"]
    _append_hit_focus(lines, x, y,
                      control=_resolve_verify_control(selector))  # D3 Task 4
    return {"output": "\n".join(lines)}


@handler("drag")
def _drag(params: dict) -> dict[str, Any]:
    """Drag from one position to another."""
    from agent_windows.core.input_controller import drag, validate_coordinates

    # Validate both endpoints independently for precise error reporting.
    x1, y1 = validate_coordinates(require(params, "x1"), require(params, "y1"))
    x2, y2 = validate_coordinates(require(params, "x2"), require(params, "y2"))
    right = params.get("right", False)
    drag(x1, y1, x2, y2, right=right)
    lines = ["## Drag", f"From: ({x1}, {y1})", f"To: ({x2}, {y2})"]
    if right:
        lines.append("Button: right")
    _append_drop_slider(lines, x2, y2)  # P0.3: drop-target + slider verify
    return {"output": "\n".join(lines)}


@handler("shortcut")
def _shortcut(params: dict) -> dict[str, Any]:
    """Send keyboard shortcut."""
    from agent_windows.core.input_controller import shortcut

    keys = require(params, "keys", str)
    shortcut(keys)
    return {"output": f"## Shortcut\nKeys: {keys}"}


@handler("key_down")
def _key_down(params: dict) -> dict[str, Any]:
    """Press key(s) (raw key-down).

    ``hold`` (seconds, optional): when set, releases the keys automatically
    after the hold elapses; when omitted, keys stay pressed (pair with
    ``key_up``). Bounded by ``MAX_KEY_HOLD_SECONDS`` so a runaway hold can't
    tie up the single STA worker. (F11)
    """
    from agent_windows.core.input_controller import key_down

    keys = require(params, "keys", str)
    hold = params.get("hold")
    if hold is not None:
        try:
            hold = float(hold)
        except (TypeError, ValueError):
            raise ValueError(f"hold must be a number of seconds; got {hold!r}")
        from agent_windows.utils.config import load_config
        max_hold = load_config().get("input", {}).get(
            "key_hold_seconds", MAX_KEY_HOLD_SECONDS)
        if not (0 <= hold <= max_hold):
            raise ValueError(
                f"hold must be in [0, {max_hold}] seconds; got {hold}")
    key_down(keys, hold=hold)
    return {"output": f"## Key Down\nKeys: {keys}"
            + (f"\nHeld: {hold}s (auto-release)" if hold is not None else "\nHeld: until key-up")}


@handler("key_up")
def _key_up(params: dict) -> dict[str, Any]:
    """Release key(s) (raw key-up)."""
    from agent_windows.core.input_controller import key_up

    keys = require(params, "keys", str)
    key_up(keys)
    return {"output": f"## Key Up\nKeys: {keys}"}
