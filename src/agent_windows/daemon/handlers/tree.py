"""Tree and element query handlers: snapshot, get_text, element_state, wait."""

import time
from typing import Any

from agent_windows.daemon.router import handler

# DoS bound: ``wait`` sleeps/polls on the single STA worker thread, so an
# unbounded ``seconds``/``timeout`` (e.g. 999999) blocks all other clients for
# the duration — the 120s ``submit`` timeout only reports to the caller, it
# doesn't interrupt the running sleep. Cap both at 5 minutes. (SEC3-01)
MAX_WAIT_SECONDS = 300


@handler("snapshot")
def _snapshot(params: dict) -> dict[str, Any]:
    """UIA tree traversal and semantic snapshot."""
    from agent_windows.core.tree import traverse_tree, render_snapshot
    from agent_windows.core.display import (
        parse_display_selection, filter_snapshot_to_displays,
    )
    from agent_windows.utils.config import load_config

    cfg = load_config().get("tree", {})
    data = traverse_tree(
        max_depth=cfg.get("max_depth", 15),
        include_invisible=cfg.get("include_invisible", False),
        include_offscreen=cfg.get("include_offscreen", False),
        max_elements=cfg.get("max_elements", 200),
        timeout=cfg.get("timeout", 10),
    )

    # Optional multi-display filter (TODO #9-B): clip windows/elements/cursor
    # to the selected display(s). parse_display_selection raises ValueError on
    # a bad selection (→ INVALID_PARAMS at the router). No display param → no
    # filtering (backward compatible).
    display = parse_display_selection(params.get("display"))
    if display is not None:
        data = filter_snapshot_to_displays(data, display)

    output = render_snapshot(data)
    return {"output": output}


@handler("get_text")
def _get_text(params: dict) -> dict[str, Any]:
    """Get text content of a UIA element."""
    from agent_windows.core.tree import resolve_element

    selector = params.get("selector", "")
    if not selector:
        raise ValueError("selector is required")

    el = resolve_element(selector)
    text = el.name or ""

    return {
        "output": f"## Get Text\nSelector: {selector}\nText: {text}",
        "selector": selector,
        "text": text,
        "control_type": el.control_type,
    }


@handler("element_state")
def _element_state(params: dict) -> dict[str, Any]:
    """Check UIA element state: visible or enabled."""
    from agent_windows.core.tree import resolve_element

    selector = params.get("selector", "")
    if not selector:
        raise ValueError("selector is required")

    check = params.get("check", "")
    if check not in ("visible", "enabled"):
        raise ValueError("check must be 'visible' or 'enabled'")

    el = resolve_element(selector)

    if check == "visible":
        result = not el.is_offscreen
    else:
        result = el.is_enabled

    return {
        "output": f"## Element State\nSelector: {selector}\n{check}: {result}",
        "selector": selector,
        "check": check,
        "result": result,
    }


def _wait_check_once(selector: str, text: str, cfg: dict) -> tuple[bool, dict[str, Any]]:
    """One non-blocking wait probe — does the element/text exist right now?

    No sleep, no loop. Shared by the blocking ``wait`` handler (which loops it
    server-side for ``batch``) and the non-blocking ``wait_check`` handler
    (polled client-side by the CLI so the STA worker is never blocked for the
    wait duration — M-28). Returns ``(found, payload)``.
    """
    from agent_windows.core.tree import resolve_element, get_tree_snapshot

    if selector:
        try:
            el = resolve_element(selector)
        except (ValueError, IndexError):
            return False, {}
        return True, {"element": el.name}

    for el in get_tree_snapshot(
        max_depth=cfg.get("max_depth", 15),
        include_invisible=cfg.get("include_invisible", False),
        include_offscreen=cfg.get("include_offscreen", False),
        max_elements=cfg.get("max_elements", 200),
        timeout=cfg.get("timeout", 10),
    ).get("interactive_elements", []):
        if text in (el.name or ""):
            return True, {"matched_element": el.name}
    return False, {}


@handler("wait")
def _wait(params: dict) -> dict[str, Any]:
    """Wait for a duration, element appearance, or text appearance.

    Modes:
        seconds > 0 and no selector/text → sleep
        selector → poll until resolve_element succeeds or timeout
        text → poll until an interactive element's name contains text

    Note: this handler loops and sleeps ON the STA worker, blocking the daemon
    for up to ``timeout``. It is retained for ``batch`` (whose single IPC
    round-trip is inherently sequential); interactive CLI use polls
    ``wait_check`` client-side instead (M-28).
    """
    from agent_windows.utils.config import load_config

    timeout = float(params.get("timeout", 30))
    selector = params.get("selector", "")
    text = params.get("text", "")
    seconds = float(params.get("seconds", 0))

    # SEC3-01: clamp both bounds so a single wait can't monopolize the STA
    # worker. Values above the cap are rejected (not silently truncated) so the
    # caller knows the limit applied.
    if timeout > MAX_WAIT_SECONDS:
        raise ValueError(
            f"timeout {timeout}s exceeds the {MAX_WAIT_SECONDS}s maximum")
    if seconds > MAX_WAIT_SECONDS:
        raise ValueError(
            f"seconds {seconds}s exceeds the {MAX_WAIT_SECONDS}s maximum")

    poll_interval = float(params.get("interval", 0)) or (
        load_config().get("wait", {}).get("poll_interval", 0.5)
    )

    # Mode 1: plain sleep
    if not selector and not text:
        if seconds <= 0:
            raise ValueError("provide seconds, selector, or text")
        time.sleep(seconds)
        return {"output": f"## Wait\nSlept {seconds}s"}

    # G14: bound the poll interval the same way ``wait_for`` (this module) does.
    # The polling modes below hammer ``get_tree_snapshot`` (a full UIA tree
    # traversal) on each iteration, so a bad config poll_interval (or an explicit
    # ``interval=0``) busy-loops it ~tens of thousands of times for a 300s wait —
    # a single-call DoS on the STA worker. wait_for already guards this; wait
    # didn't. Placed AFTER the sleep-mode return so plain-sleep calls (which
    # never poll) are unaffected. Mirrors the sibling's message verbatim.
    if poll_interval <= 0 or poll_interval > 5:
        raise ValueError("interval must be greater than 0 and at most 5 seconds")

    start = time.time()
    cfg = load_config().get("tree", {})

    # Mode 2/3: poll until found or timeout
    while True:
        found, payload = _wait_check_once(selector, text, cfg)
        if found:
            elapsed = time.time() - start
            if selector:
                return {
                    "output": f"## Wait\nElement {selector} appeared after {elapsed:.1f}s",
                    "selector": selector,
                    "elapsed": round(elapsed, 1),
                    **payload,
                }
            return {
                "output": f"## Wait\nText '{text}' appeared after {elapsed:.1f}s",
                "text": text,
                "elapsed": round(elapsed, 1),
                **payload,
            }
        if time.time() - start >= timeout:
            if selector:
                raise TimeoutError(f"Element {selector} not found within {timeout}s")
            raise TimeoutError(f"Text '{text}' not found within {timeout}s")
        time.sleep(poll_interval)


@handler("wait_check")
def _wait_check(params: dict) -> dict[str, Any]:
    """Single non-blocking wait probe: does the element/text exist right now?

    Returns ``{"found": bool, ...}`` with no looping or sleeping. The CLI
    ``wait`` command polls this client-side (sleeping between calls) so the
    daemon's single STA worker is never tied up for the wait duration (M-28).
    """
    selector = params.get("selector", "")
    text = params.get("text", "")
    if not selector and not text:
        raise ValueError("selector or text is required")

    from agent_windows.utils.config import load_config

    cfg = load_config().get("tree", {})
    found, payload = _wait_check_once(selector, text, cfg)
    return {"found": found, **payload}


# ── WaitFor: multi-condition polling (TODO #7) ──────────────────────


def _wait_for_once(
    condition: str, text: str, window_name: str, cfg: dict
) -> tuple[bool, dict[str, Any]]:
    """One non-blocking WaitFor probe — is the condition satisfied right now?

    No sleep, no loop. Shared by the blocking ``wait_for`` handler (loops it
    server-side for ``batch``) and the non-blocking ``wait_for_check`` handler
    (polled client-side by the CLI, same M-28 rationale as ``wait_check``).
    Returns ``(matched, payload)``.
    """
    from agent_windows.core.tree import get_tree_snapshot
    from agent_windows.core.wait_for import matches_condition

    snapshot = get_tree_snapshot(
        max_depth=cfg.get("max_depth", 15),
        include_invisible=cfg.get("include_invisible", False),
        include_offscreen=cfg.get("include_offscreen", False),
        max_elements=cfg.get("max_elements", 200),
        timeout=cfg.get("timeout", 10),
    )
    matched, detail = matches_condition(snapshot, condition, text, window_name)
    return matched, {"condition": condition, "detail": detail}


@handler("wait_for")
def _wait_for(params: dict) -> dict[str, Any]:
    """Wait until a multi-condition UI predicate is satisfied (TODO #7).

    Conditions: text_exists / active_window / element_exists /
    element_enabled / focused_element. Polls the UIA tree server-side until the
    condition matches or ``timeout`` elapses. Retained for ``batch`` (whose IPC
    round-trip is inherently sequential); interactive CLI use polls
    ``wait_for_check`` client-side (M-28).

    ``text`` and ``window_name`` are case-sensitive substring filters. The
    match semantics are documented in ``skills/.../commands.md``.
    """
    from agent_windows.core.wait_for import validate_wait_for
    from agent_windows.utils.config import load_config

    condition = params.get("condition", "")
    text = params.get("text", "")
    window_name = params.get("window_name", "")
    timeout = float(params.get("timeout", 30))
    interval = float(params.get("interval", 0))

    # Normalize + validate. interval=0 means "use config fallback", which is
    # resolved below — validate with None here so the bounds check is skipped,
    # then re-check after the fallback resolves (a bad config value must still
    # surface as INVALID_PARAMS rather than a tight busy-loop).
    condition = validate_wait_for(
        condition, text, window_name, timeout=timeout, interval=None,
        max_timeout=MAX_WAIT_SECONDS,
    )

    poll_interval = interval or (
        load_config().get("wait", {}).get("poll_interval", 0.5)
    )
    if poll_interval <= 0 or poll_interval > 5:
        raise ValueError("interval must be greater than 0 and at most 5 seconds")

    cfg = load_config().get("tree", {})
    start = time.time()

    while True:
        matched, payload = _wait_for_once(condition, text, window_name, cfg)
        if matched:
            elapsed = time.time() - start
            return {
                "output": (
                    f"## WaitFor\nCondition {condition} satisfied after "
                    f"{elapsed:.1f}s: {payload['detail']}"
                ),
                "condition": condition,
                "elapsed": round(elapsed, 1),
                **payload,
            }
        if time.time() - start >= timeout:
            raise TimeoutError(
                f"WaitFor condition {condition} not satisfied within "
                f"{timeout}s: {payload['detail']}"
            )
        time.sleep(poll_interval)


@handler("wait_for_check")
def _wait_for_check(params: dict) -> dict[str, Any]:
    """Single non-blocking WaitFor probe: is the condition satisfied right now?

    Returns ``{"found": bool, condition, detail}`` with no looping or sleeping.
    The CLI ``wait-for`` command polls this client-side so the daemon's single
    STA worker is never tied up for the wait duration (M-28).
    """
    from agent_windows.core.wait_for import validate_wait_for
    from agent_windows.utils.config import load_config

    condition = params.get("condition", "")
    text = params.get("text", "")
    window_name = params.get("window_name", "")

    # Normalize + validate the condition (bad condition → INVALID_PARAMS). Skip
    # the interval check (None) — a single probe has no poll interval — and use
    # a nominal in-range timeout so the timeout bounds check passes.
    condition = validate_wait_for(
        condition, text, window_name, timeout=1.0, interval=None,
        max_timeout=MAX_WAIT_SECONDS,
    )

    cfg = load_config().get("tree", {})
    found, payload = _wait_for_once(condition, text, window_name, cfg)
    return {"found": found, **payload}
