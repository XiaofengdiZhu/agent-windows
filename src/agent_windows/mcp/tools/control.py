"""Control MCP tools — ping, wait, wait_for.

wait/wait_for poll the daemon's non-blocking wait_check / wait_for_check probes
client-side (sleeping between polls), mirroring cli/commands/wait.py. This keeps
the daemon's single STA worker free and bounds each probe to a fast tree scan.
"""
import time

from mcp.types import ToolAnnotations

from agent_windows.mcp._call import _call


def ping_fn() -> str:
    return str(_call("ping").get("output", "pong"))


def wait_fn(
    seconds: float | None = None,
    selector: str | None = None,
    text: str | None = None,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> str:
    # Mode 1: plain sleep — no selector/text.
    if not selector and not text:
        if not seconds or seconds <= 0:
            raise ValueError("provide seconds, a selector, or text")
        time.sleep(seconds)
        return f"Waited {seconds}s"

    start = time.monotonic()
    while True:
        result = _call("wait_check", selector=selector, text=text)
        if result.get("found"):
            elapsed = time.monotonic() - start
            who = f"Element {selector}" if selector else f"Text '{text}'"
            return f"{who} appeared after {elapsed:.1f}s"
        if time.monotonic() - start >= timeout:
            who = f"Element {selector}" if selector else f"Text '{text}'"
            raise TimeoutError(f"{who} not found within {timeout}s")
        time.sleep(min(interval, max(0.0, timeout - (time.monotonic() - start))))


def wait_for_fn(
    condition: str,
    text: str | None = None,
    window_name: str | None = None,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> str:
    start = time.monotonic()
    while True:
        result = _call("wait_for_check", condition=condition, text=text,
                       window_name=window_name)
        if result.get("found"):
            elapsed = time.monotonic() - start
            return (f"Condition '{condition}' satisfied after {elapsed:.1f}s: "
                    f"{result.get('detail', '')}")
        if time.monotonic() - start >= timeout:
            raise TimeoutError(
                f"Condition '{condition}' not satisfied within {timeout}s: "
                f"{result.get('detail', '')}"
            )
        time.sleep(min(interval, max(0.0, timeout - (time.monotonic() - start))))


def register(mcp) -> None:
    _REG = ToolAnnotations

    @mcp.tool(name="ping", description=(
        "Daemon health check (single IPC round-trip). "
        "Keywords: ping, health, health check, alive, status."
    ), annotations=_REG(title="Ping", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _ping() -> str:
        return ping_fn()

    @mcp.tool(name="wait", description=(
        "Wait for a duration, an element (@eN selector) to appear, or text to "
        "appear in an interactive element's name. Keywords: wait, sleep, delay, "
        "wait for element, wait for text, pause. Provide seconds for a plain "
        "sleep, OR a selector, OR text. timeout bounds the poll (default 30s); "
        "interval is the poll period (default 0.5s)."
    ), annotations=_REG(title="Wait", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _wait(seconds: float | None = None, selector: str | None = None,
              text: str | None = None, timeout: float = 30.0,
              interval: float = 0.5) -> str:
        return wait_fn(seconds=seconds, selector=selector, text=text,
                       timeout=timeout, interval=interval)

    @mcp.tool(name="wait_for", description=(
        "Wait until a UI condition holds. Keywords: wait for condition, wait "
        "for window, wait until, expect, synchronize. condition: text_exists / "
        "active_window / element_exists / element_enabled / focused_element "
        "(aliases text/window/element/enabled/focused). Provide text and/or "
        "window_name per the condition. Polls the UIA tree until matched or "
        "timeout (default 30s)."
    ), annotations=_REG(title="Wait For", readOnlyHint=True, idempotentHint=True, openWorldHint=False))
    def _wait_for(condition: str, text: str | None = None,
                  window_name: str | None = None, timeout: float = 30.0,
                  interval: float = 0.5) -> str:
        return wait_for_fn(condition=condition, text=text, window_name=window_name,
                           timeout=timeout, interval=interval)
