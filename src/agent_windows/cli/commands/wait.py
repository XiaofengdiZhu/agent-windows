"""Wait command: sleep, wait for element, or wait for text."""

import typer

from agent_windows.cli.dispatcher import _cmd


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
    params: dict = {"timeout": timeout}
    if interval > 0:
        params["interval"] = interval

    # Determine mode
    if text:
        if target:
            typer.echo("Error: cannot specify both a target and --text", err=True)
            raise typer.Exit(1)
        params["text"] = text
    elif target.startswith("@e"):
        params["selector"] = target
    else:
        try:
            params["seconds"] = float(target)
        except ValueError:
            typer.echo(f"Error: invalid target {target!r} (expected seconds or @eN)", err=True)
            raise typer.Exit(1)

    _cmd(ctx, "wait", **params)
