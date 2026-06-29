"""Wait command: sleep, wait for element, or wait for text.

Polling is client-side: each probe is a short ``wait_check`` request to the
daemon, and the CLI sleeps between probes locally. This keeps the daemon's
single STA worker free between checks instead of blocking it for the whole
wait duration (M-28).
"""

import time

import typer

from agent_windows.cli.dispatcher import dispatch, print_result

# Floor for the poll interval: a caller-supplied 0 (or tiny) value, or a
# config value of 0, would spin without yielding and pin the daemon's single
# STA worker for the whole timeout. Clamp so the worker always gets a breather
# between probes. Mirrors mcp/tools/control.py::_MIN_POLL_INTERVAL.
_MIN_POLL_INTERVAL = 0.05


def wait(
    ctx: typer.Context,
    target: str = typer.Argument(
        "",
        help="Seconds to wait, or element selector (e.g. @e3)",
    ),
    timeout: float = typer.Option(
        30,
        "--timeout",
        "-t",
        help="Max seconds to wait for element/text",
    ),
    text: str = typer.Option(
        "",
        "--text",
        help="Wait until an element's name contains this text",
    ),
    interval: float = typer.Option(
        0,
        "--interval",
        help="Poll interval in seconds (default: from config, usually 0.5)",
    ),
) -> None:
    """Wait for a duration, element, or text to appear.

    \b
    Examples:
      agent-windows wait 3
      agent-windows wait @e3 --timeout 10
      agent-windows wait --text "完成" --timeout 10
      agent-windows wait @e5 --interval 0.2 --timeout 5
    """
    # Determine mode
    if text:
        if target:
            typer.echo("Error: cannot specify both a target and --text", err=True)
            raise typer.Exit(1)
        probe = {"text": text}
    elif target.startswith("@e"):
        probe = {"selector": target}
    else:
        try:
            seconds = float(target)
        except ValueError:
            typer.echo(f"Error: invalid target {target!r} (expected seconds or @eN)", err=True)
            raise typer.Exit(1)
        if seconds <= 0:
            typer.echo("Error: provide a positive number of seconds, a selector, or --text", err=True)
            raise typer.Exit(1)
        # Plain sleep is local — no reason to round-trip to the daemon (M-28)
        time.sleep(seconds)
        print_result({"output": f"## Wait\nSlept {seconds}s"}, ctx)
        return

    from agent_windows.utils.config import load_config

    poll_interval = interval or (
        load_config().get("wait", {}).get("poll_interval", 0.5)
    )
    poll_interval = max(poll_interval, _MIN_POLL_INTERVAL)

    # Poll client-side: each probe is one short wait_check request, the CLI
    # sleeps between probes so the daemon's STA worker is never blocked for
    # the wait duration (M-28).
    start = time.time()
    while True:
        result = dispatch("wait_check", probe, ctx)
        if result.get("found"):
            elapsed = time.time() - start
            if "selector" in probe:
                print_result({
                    "output": f"## Wait\nElement {probe['selector']} appeared after {elapsed:.1f}s",
                    "selector": probe["selector"],
                    "elapsed": round(elapsed, 1),
                    "element": result.get("element", ""),
                }, ctx)
            else:
                print_result({
                    "output": f"## Wait\nText '{probe['text']}' appeared after {elapsed:.1f}s",
                    "text": probe["text"],
                    "elapsed": round(elapsed, 1),
                    "matched_element": result.get("matched_element", ""),
                }, ctx)
            return
        if time.time() - start >= timeout:
            if "selector" in probe:
                typer.echo(
                    f"Error: element {probe['selector']} not found within {timeout}s",
                    err=True,
                )
            else:
                typer.echo(
                    f"Error: text {probe['text']!r} not found within {timeout}s",
                    err=True,
                )
            raise typer.Exit(1)
        time.sleep(poll_interval)


def wait_for(
    ctx: typer.Context,
    condition: str = typer.Argument(
        ...,
        help="Condition: text_exists|active_window|element_exists|"
             "element_enabled|focused_element (short aliases ok: "
             "text/window/element/enabled/focused)",
    ),
    text: str = typer.Option(
        "",
        "--text",
        help="Case-sensitive substring to match in element name/control_type/"
             "metadata, or window title",
    ),
    window: str = typer.Option(
        "",
        "--window",
        help="Case-sensitive substring to scope matching to a window title",
    ),
    timeout: float = typer.Option(
        30,
        "--timeout",
        "-t",
        help="Max seconds to wait (capped at 300s by the daemon)",
    ),
    interval: float = typer.Option(
        0,
        "--interval",
        help="Poll interval in seconds (default: from config, usually 0.5)",
    ),
) -> None:
    """Wait until a multi-condition UI predicate is satisfied (TODO #7).

    \b
    Conditions (text/window filters are case-sensitive substring):
      text_exists      -- any visible text contains --text
      active_window    -- the active window title contains --text or --window
      element_exists   -- an element matches --text (and --window if given)
      element_enabled  -- a matching element that is enabled
      focused_element  -- a matching element that has keyboard focus

    \b
    Examples:
      agent-windows wait-for text_exists --text "完成"
      agent-windows wait-for active_window --window "Notepad"
      agent-windows wait-for element_exists --text OK --timeout 10
      agent-windows wait-for enabled --text Submit --interval 0.2
    """
    from agent_windows.utils.config import load_config
    from agent_windows.core.wait_for import normalize_condition

    # Validate the condition client-side so a typo fails fast (one clear error)
    # instead of busy-polling the daemon with an INVALID_PARAMS request every
    # interval for the whole timeout window. normalize_condition raises
    # ValueError on an unknown condition.
    try:
        normalize_condition(condition)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    poll_interval = interval or (
        load_config().get("wait", {}).get("poll_interval", 0.5)
    )
    poll_interval = max(poll_interval, _MIN_POLL_INTERVAL)

    probe = {"condition": condition, "text": text, "window_name": window}

    # Poll client-side: each probe is one short wait_for_check request, the CLI
    # sleeps between probes so the daemon's STA worker is never blocked for the
    # wait duration (M-28, same pattern as `wait`).
    start = time.time()
    while True:
        result = dispatch("wait_for_check", probe, ctx)
        if result.get("found"):
            elapsed = time.time() - start
            detail = result.get("detail", "")
            cond = result.get("condition", condition)
            print_result({
                "output": (
                    f"## WaitFor\nCondition {cond} satisfied after "
                    f"{elapsed:.1f}s: {detail}"
                ),
                "condition": cond,
                "elapsed": round(elapsed, 1),
                "detail": detail,
            }, ctx)
            return
        if time.time() - start >= timeout:
            detail = result.get("detail", "")
            typer.echo(
                f"Error: WaitFor condition {condition!r} not satisfied within "
                f"{timeout}s: {detail}",
                err=True,
            )
            raise typer.Exit(1)
        time.sleep(poll_interval)
