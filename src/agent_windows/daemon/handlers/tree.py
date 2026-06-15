"""Tree and element query handlers: snapshot, get_text, element_state, wait."""

import time
from typing import Any

from agent_windows.daemon.router import handler


@handler("snapshot")
def _snapshot(params: dict) -> dict[str, Any]:
    """UIA tree traversal and semantic snapshot."""
    from agent_windows.core.tree import traverse_tree, render_snapshot
    from agent_windows.utils.config import load_config

    cfg = load_config().get("tree", {})
    data = traverse_tree(
        max_depth=cfg.get("max_depth", 15),
        include_invisible=cfg.get("include_invisible", False),
        include_offscreen=cfg.get("include_offscreen", False),
        max_elements=cfg.get("max_elements", 200),
        timeout=cfg.get("timeout", 10),
    )
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

    poll_interval = float(params.get("interval", 0)) or (
        load_config().get("wait", {}).get("poll_interval", 0.5)
    )

    # Mode 1: plain sleep
    if not selector and not text:
        if seconds <= 0:
            raise ValueError("provide seconds, selector, or text")
        time.sleep(seconds)
        return {"output": f"## Wait\nSlept {seconds}s"}

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
