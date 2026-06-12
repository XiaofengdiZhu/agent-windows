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
        max_depth=cfg.get("max_depth", 30),
        include_invisible=cfg.get("include_invisible", False),
        include_offscreen=cfg.get("include_offscreen", False),
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


@handler("wait")
def _wait(params: dict) -> dict[str, Any]:
    """Wait for a duration, element appearance, or text appearance.

    Modes:
        seconds > 0 and no selector/text → sleep
        selector → poll until resolve_element succeeds or timeout
        text → poll until an interactive element's name contains text
    """
    from agent_windows.utils.config import load_config
    from agent_windows.core.tree import resolve_element, get_tree_snapshot

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

    # Mode 2: wait for element
    if selector:
        while True:
            try:
                el = resolve_element(selector)
                elapsed = time.time() - start
                return {
                    "output": f"## Wait\nElement {selector} appeared after {elapsed:.1f}s",
                    "selector": selector,
                    "elapsed": round(elapsed, 1),
                    "element": el.name,
                }
            except (ValueError, IndexError):
                pass
            if time.time() - start >= timeout:
                raise TimeoutError(f"Element {selector} not found within {timeout}s")
            time.sleep(poll_interval)

    # Mode 3: wait for text
    while True:
        data = get_tree_snapshot(
            max_depth=cfg.get("max_depth", 30),
            include_invisible=cfg.get("include_invisible", False),
            include_offscreen=cfg.get("include_offscreen", False),
        )
        for el in data.get("interactive_elements", []):
            if text in (el.name or ""):
                elapsed = time.time() - start
                return {
                    "output": f"## Wait\nText '{text}' appeared after {elapsed:.1f}s",
                    "text": text,
                    "elapsed": round(elapsed, 1),
                    "matched_element": el.name,
                }
        if time.time() - start >= timeout:
            raise TimeoutError(f"Text '{text}' not found within {timeout}s")
        time.sleep(poll_interval)
