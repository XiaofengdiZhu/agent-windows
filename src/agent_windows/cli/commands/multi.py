"""Batch interaction commands: multi-select, multi-edit (TODO #12)."""

import json
import sys

import typer

from agent_windows.cli.dispatcher import _cmd


def _read_json(json_arg: str, file: str) -> list:
    """Read a JSON array from --file, stdin ('-'), or the argument.

    Mirrors the batch command's source precedence. Exits with a clear message
    on missing source or invalid JSON.
    """
    raw = ""
    if file:
        from pathlib import Path

        raw = Path(file).read_text(encoding="utf-8")
    elif json_arg == "-":
        raw = sys.stdin.read()
    elif json_arg:
        raw = json_arg
    else:
        typer.echo("Error: provide JSON as argument, --file, or stdin (-)", err=True)
        raise typer.Exit(1)

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        typer.echo(f"Error: invalid JSON: {e}", err=True)
        raise typer.Exit(1)

    if not isinstance(data, list):
        typer.echo("Error: JSON must be an array", err=True)
        raise typer.Exit(1)
    return data


def multi_select(
    ctx: typer.Context,
    json_arg: str = typer.Argument(
        "",
        help="JSON array of [x,y] pairs or @eN strings, or '-' for stdin",
    ),
    file: str = typer.Option("", "--file", "-f", help="Read JSON from file"),
    press_ctrl: bool = typer.Option(
        True,
        "--press-ctrl/--no-ctrl",
        help="Hold Ctrl across clicks (default) for multi-selection",
    ),
    selectors: bool = typer.Option(
        False,
        "--selectors",
        help="Treat JSON values as @eN element refs (resolved to centers)",
    ),
) -> None:
    """Click multiple targets, optionally holding Ctrl for multi-selection.

    \b
    Examples (coordinates):
      agent-windows multi-select '[[100,200],[300,400]]'
      agent-windows multi-select '[[100,200]]' --no-ctrl
    \b
    Examples (element refs):
      agent-windows multi-select '["@e1","@e3"]' --selectors
      agent-windows multi-select --file targets.json --selectors
    """
    data = _read_json(json_arg, file)
    params: dict = {"press_ctrl": press_ctrl}
    if selectors:
        params["selectors"] = data
    else:
        params["locs"] = data
    _cmd(ctx, "multi_select", **params)


def multi_edit(
    ctx: typer.Context,
    json_arg: str = typer.Argument(
        "",
        help="JSON array of [x,y,text] or [@eN,text], or '-' for stdin",
    ),
    file: str = typer.Option("", "--file", "-f", help="Read JSON from file"),
    selectors: bool = typer.Option(
        False,
        "--selectors",
        help="Treat JSON pairs as [@eN, text] (resolved to centers)",
    ),
) -> None:
    """Type text into multiple fields in one call.

    \b
    Examples (coordinates):
      agent-windows multi-edit '[[100,200,"Alice"],[300,400,"admin"]]'
    \b
    Examples (element refs):
      agent-windows multi-edit '[["@e1","Alice"],["@e3","admin"]]' --selectors
    """
    data = _read_json(json_arg, file)
    params: dict = {}
    if selectors:
        params["selectors"] = data
    else:
        params["locs"] = data
    _cmd(ctx, "multi_edit", **params)
