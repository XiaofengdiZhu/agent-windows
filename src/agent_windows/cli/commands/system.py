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
    args: list[str] | None = typer.Argument(None, help="Application arguments"),
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
    offset: int = typer.Option(0, "--offset", help="1-based start line (0 = whole file)"),
    limit: int = typer.Option(0, "--limit", help="Max lines to read (0 = to EOF)"),
    encoding: str = typer.Option("", "--encoding", help="Text encoding (default utf-8; e.g. gbk, latin-1)"),
) -> None:
    """Read a file. Use --offset/--limit for a line range."""
    params: dict = {"path": path}
    if offset:
        params["offset"] = offset
    if limit:
        params["limit"] = limit
    if encoding:
        params["encoding"] = encoding
    _cmd(ctx, "fs_read", **params)


@fs_cmd.command("write")
def fs_write(
    ctx: typer.Context,
    path: str = typer.Argument(help="File path to write"),
    content: str = typer.Argument(help="Content to write"),
    append: bool = typer.Option(False, "--append", help="Append instead of overwriting"),
) -> None:
    """Write to a file. Use --append to add to an existing file."""
    params: dict = {"path": path, "content": content}
    if append:
        params["append"] = True
    _cmd(ctx, "fs_write", **params)


@fs_cmd.command("list")
def fs_list(
    ctx: typer.Context,
    path: str = typer.Argument(help="Directory path to list"),
    pattern: str = typer.Option("", "--pattern", help="Name filter (glob)"),
    recursive: bool = typer.Option(False, "--recursive", help="Walk subdirectories"),
    show_hidden: bool = typer.Option(False, "--show-hidden", help="Include dotfiles"),
) -> None:
    """List directory contents."""
    params: dict = {"path": path}
    if pattern:
        params["pattern"] = pattern
    if recursive:
        params["recursive"] = True
    if show_hidden:
        params["show_hidden"] = True
    _cmd(ctx, "fs_list", **params)


@fs_cmd.command("copy")
def fs_copy(
    ctx: typer.Context,
    path: str = typer.Argument(help="Source path"),
    destination: str = typer.Argument(help="Destination path"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace existing destination"),
) -> None:
    """Copy a file or directory."""
    params: dict = {"path": path, "destination": destination}
    if overwrite:
        params["overwrite"] = True
    _cmd(ctx, "fs_copy", **params)


@fs_cmd.command("move")
def fs_move(
    ctx: typer.Context,
    path: str = typer.Argument(help="Source path"),
    destination: str = typer.Argument(help="Destination path"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Replace existing destination"),
) -> None:
    """Move or rename a file or directory."""
    params: dict = {"path": path, "destination": destination}
    if overwrite:
        params["overwrite"] = True
    _cmd(ctx, "fs_move", **params)


@fs_cmd.command("delete")
def fs_delete(
    ctx: typer.Context,
    path: str = typer.Argument(help="Path to delete"),
    recursive: bool = typer.Option(False, "--recursive", help="Delete non-empty directories"),
) -> None:
    """Delete a file or directory."""
    params: dict = {"path": path}
    if recursive:
        params["recursive"] = True
    _cmd(ctx, "fs_delete", **params)


@fs_cmd.command("search")
def fs_search(
    ctx: typer.Context,
    path: str = typer.Argument(help="Directory to search in"),
    pattern: str = typer.Argument(help="Glob pattern (e.g. *.py)"),
    no_recursive: bool = typer.Option(False, "--no-recursive", help="Search top level only"),
) -> None:
    """Find files/dirs matching a glob pattern."""
    params: dict = {"path": path, "pattern": pattern, "recursive": not no_recursive}
    _cmd(ctx, "fs_search", **params)


@fs_cmd.command("info")
def fs_info(
    ctx: typer.Context,
    path: str = typer.Argument(help="File or directory path"),
) -> None:
    """Get metadata about a file or directory."""
    _cmd(ctx, "fs_info", path=path)


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
def process_list(
    ctx: typer.Context,
    sort_by: str = typer.Option(
        "memory", "--sort-by",
        help="Sort key: memory|cpu|name (default memory, descending)",
    ),
    limit: int = typer.Option(
        0, "--limit", help="Cap rows to top-N (0 = all)",
    ),
    name: str = typer.Option(
        "", "--name", help="Filter to processes whose name contains this (case-insensitive)",
    ),
) -> None:
    """List running processes (PID/name/memory/CPU)."""
    params: dict = {}
    if sort_by != "memory":
        params["sort_by"] = sort_by
    if limit:
        params["limit"] = limit
    if name:
        params["name"] = name
    _cmd(ctx, "process_list", **params)


@proc_cmd.command("kill")
def process_kill(
    ctx: typer.Context,
    pid_or_name: str = typer.Argument(help="PID or process name"),
    force: bool = typer.Option(
        False, "--force",
        help="强杀 immediately (default: graceful, lets the app save)",
    ),
) -> None:
    """Kill a process. Use --force for an immediate 强杀."""
    params: dict = {"pid_or_name": pid_or_name}
    if force:
        params["force"] = True
    _cmd(ctx, "process_kill", **params)


# ── notify (top-level command) ──────────────────────────────────────


def notify(
    ctx: typer.Context,
    title: str = typer.Argument(help="Toast title/heading"),
    message: str = typer.Argument(help="Toast body text"),
    app_id: str = typer.Option(
        "",
        "--app-id",
        help="AppUserModelID to attribute the toast to (default: PowerShell's AUMID)",
    ),
) -> None:
    """Send a Windows toast notification (title + message)."""
    params: dict = {"title": title, "message": message}
    if app_id:
        params["app_id"] = app_id
    _cmd(ctx, "notify", **params)


# ── registry ────────────────────────────────────────────────────────

registry_cmd = typer.Typer(help="Windows Registry operations")


@registry_cmd.command("get")
def registry_get(
    ctx: typer.Context,
    path: str = typer.Argument(help="Registry path (e.g. HKCU:\\Software\\MyApp)"),
    name: str = typer.Argument(help="Value name"),
) -> None:
    """Read a registry value."""
    _cmd(ctx, "registry", mode="get", path=path, name=name)


@registry_cmd.command("set")
def registry_set(
    ctx: typer.Context,
    path: str = typer.Argument(help="Registry path"),
    name: str = typer.Argument(help="Value name"),
    value: str = typer.Argument(help="Value (string/int, JSON array for MultiString, hex for Binary)"),
    type: str = typer.Option(
        "String", "--type", "-t",
        help="Value type: String|ExpandString|DWord|QWord|MultiString|Binary",
    ),
) -> None:
    """Set a registry value (creates the key if absent)."""
    # MultiString takes a list; the CLI receives a string, so JSON-parse it here
    # (the boundary where a CLI string becomes a typed value). The daemon's
    # JSON-RPC path already receives a native list. (BUG-A fix)
    set_value = value
    if type == "MultiString":
        import json
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as e:
            typer.echo(f"Error: --type MultiString requires a JSON array: {e}", err=True)
            raise typer.Exit(1)
        if not isinstance(parsed, list):
            typer.echo("Error: --type MultiString requires a JSON array", err=True)
            raise typer.Exit(1)
        set_value = parsed
    _cmd(ctx, "registry", mode="set", path=path, name=name, value=set_value, type=type)


@registry_cmd.command("delete")
def registry_delete(
    ctx: typer.Context,
    path: str = typer.Argument(help="Registry path"),
    name: str = typer.Argument(
        None, help="Value name (omit to delete the whole key)",
    ),
) -> None:
    """Delete a registry value, or the whole key when NAME is omitted.

    NAME is optional: pass an explicit empty string to delete the unnamed
    (default) value; omit the argument entirely to delete the whole key.
    """
    params: dict = {"mode": "delete", "path": path}
    # name is None when the argument is omitted (delete whole key). An explicit
    # empty string ("") targets the registry's unnamed default value, so we
    # forward it only when the argument was actually provided. (BUG-B fix)
    if name is not None:
        params["name"] = name
    _cmd(ctx, "registry", **params)


@registry_cmd.command("list")
def registry_list(
    ctx: typer.Context,
    path: str = typer.Argument(help="Registry path"),
) -> None:
    """List values and sub-keys under a path."""
    _cmd(ctx, "registry", mode="list", path=path)
