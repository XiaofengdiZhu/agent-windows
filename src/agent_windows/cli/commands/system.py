"""System commands: app, shell, fs, clipboard, batch, process."""

import json
import sys

import typer

from agent_windows.cli.dispatcher import _cmd

# ── app sub-commands ──────────────────────────────────────────────

app_cmd = typer.Typer(help="Application management")


@app_cmd.command("launch")
def app_launch(
    ctx: typer.Context,
    name_or_path: str = typer.Argument(help="App name or executable path"),
    args: list[str] = typer.Argument(None, help="Application arguments"),
) -> None:
    """Launch an application."""
    _cmd(ctx, "app_launch", name_or_path=name_or_path, args=args or [])


@app_cmd.command("switch")
def app_switch(
    ctx: typer.Context,
    name_or_title: str = typer.Argument(help="Window title or process name"),
) -> None:
    """Switch to an application window."""
    _cmd(ctx, "app_switch", name_or_title=name_or_title)


@app_cmd.command("resize")
def app_resize(
    ctx: typer.Context,
    name_or_title: str = typer.Argument(help="Window title or process name"),
    width: int = typer.Option(..., "--width", help="Window width"),
    height: int = typer.Option(..., "--height", help="Window height"),
    x: int = typer.Option(-1, "--x", help="Window X position (-1 to keep)"),
    y: int = typer.Option(-1, "--y", help="Window Y position (-1 to keep)"),
) -> None:
    """Resize and reposition an application window."""
    _cmd(ctx, "app_resize", name_or_title=name_or_title, width=width, height=height, x=x, y=y)


@app_cmd.command("list")
def app_list(ctx: typer.Context) -> None:
    """List all visible windows."""
    _cmd(ctx, "app_list")


# ── shell (top-level command) ────────────────────────────────────


def shell(
    ctx: typer.Context,
    command: str = typer.Argument(help="PowerShell command to execute"),
) -> None:
    """Execute a PowerShell command."""
    _cmd(ctx, "shell", command=command)


# ── fs ────────────────────────────────────────────────────────────

fs_cmd = typer.Typer(help="File system operations")


@fs_cmd.command("read")
def fs_read(
    ctx: typer.Context,
    path: str = typer.Argument(help="File path to read"),
) -> None:
    """Read a file."""
    _cmd(ctx, "fs_read", path=path)


@fs_cmd.command("write")
def fs_write(
    ctx: typer.Context,
    path: str = typer.Argument(help="File path to write"),
    content: str = typer.Argument(help="Content to write"),
) -> None:
    """Write to a file."""
    _cmd(ctx, "fs_write", path=path, content=content)


@fs_cmd.command("list")
def fs_list(
    ctx: typer.Context,
    path: str = typer.Argument(help="Directory path to list"),
) -> None:
    """List directory contents."""
    _cmd(ctx, "fs_list", path=path)


# ── clipboard ─────────────────────────────────────────────────────

clip_cmd = typer.Typer(help="Clipboard operations")


@clip_cmd.command("get")
def clipboard_get(
    ctx: typer.Context,
    keyboard: bool | None = typer.Option(None, "--keyboard", "-k", help="Simulate Ctrl+C before reading"),
) -> None:
    """Get clipboard content. Use --keyboard to simulate Ctrl+C first."""
    params: dict = {}
    if keyboard is not None:
        params["keyboard"] = keyboard
    _cmd(ctx, "clipboard_get", **params)


@clip_cmd.command("set")
def clipboard_set(
    ctx: typer.Context,
    text: str = typer.Argument(help="Text to copy to clipboard"),
    keyboard: bool | None = typer.Option(None, "--keyboard", "-k", help="Simulate Ctrl+V after setting"),
) -> None:
    """Set clipboard content. Use --keyboard to simulate Ctrl+V after setting."""
    params: dict = {"text": text}
    if keyboard is not None:
        params["keyboard"] = keyboard
    _cmd(ctx, "clipboard_set", **params)


# ── batch (top-level command) ──────────────────────────────────────


def batch(
    ctx: typer.Context,
    commands_json: str = typer.Argument(
        "",
        help="JSON array of commands, or '-' to read from stdin",
    ),
    file: str = typer.Option(
        "",
        "--file",
        "-f",
        help="Read commands from JSON file",
    ),
    stop_on_error: bool = typer.Option(
        True,
        "--stop-on-error/--continue-on-error",
        help="Stop on first error (default) or continue all",
    ),
    delay: float = typer.Option(
        0.0,
        "--delay",
        "-d",
        help="Delay in seconds between commands",
    ),
) -> None:
    """Execute multiple commands in a single IPC round-trip.

    \b
    Examples:
      agent-windows batch '[{"method":"click","params":{"x":500,"y":300}}]'
      agent-windows batch --file commands.json
      agent-windows batch --file commands.json --delay 0.5 --continue-on-error
      echo '[{"method":"screenshot"}]' | agent-windows batch -
    """
    # Determine JSON source: --file > argument > error
    raw = ""
    if file:
        from pathlib import Path

        raw = Path(file).read_text(encoding="utf-8")
    elif commands_json == "-":
        raw = sys.stdin.read()
    elif commands_json:
        raw = commands_json
    else:
        typer.echo("Error: provide commands as argument, --file, or stdin (-)", err=True)
        raise typer.Exit(1)

    try:
        commands = json.loads(raw)
    except json.JSONDecodeError as e:
        typer.echo(f"Error: invalid JSON: {e}", err=True)
        raise typer.Exit(1)

    if not isinstance(commands, list) or not commands:
        typer.echo("Error: commands must be a non-empty JSON array", err=True)
        raise typer.Exit(1)

    _cmd(ctx, "batch", commands=commands, stop_on_error=stop_on_error, delay=delay)


# ── process ───────────────────────────────────────────────────────

proc_cmd = typer.Typer(help="Process management")


@proc_cmd.command("list")
def process_list(ctx: typer.Context) -> None:
    """List running processes."""
    _cmd(ctx, "process_list")


@proc_cmd.command("kill")
def process_kill(
    ctx: typer.Context,
    pid_or_name: str = typer.Argument(help="PID or process name"),
) -> None:
    """Kill a process."""
    _cmd(ctx, "process_kill", pid_or_name=pid_or_name)
