"""System commands: app, shell, fs, clipboard, process."""

import typer

from agent_windows.cli.dispatcher import dispatch, print_result

# ── app sub-commands ──────────────────────────────────────────────

app_cmd = typer.Typer(help="Application management")


@app_cmd.command("launch")
def app_launch(
    ctx: typer.Context,
    name_or_path: str = typer.Argument(help="App name or executable path"),
    args: list[str] = typer.Argument(None, help="Application arguments"),
) -> None:
    """Launch an application."""
    params = {"name_or_path": name_or_path, "args": args or []}
    result = dispatch("app_launch", params, ctx)
    print_result(result, ctx)


@app_cmd.command("switch")
def app_switch(
    ctx: typer.Context,
    name_or_title: str = typer.Argument(help="Window title or process name"),
) -> None:
    """Switch to an application window."""
    result = dispatch("app_switch", {"name_or_title": name_or_title}, ctx)
    print_result(result, ctx)


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
    params = {"name_or_title": name_or_title, "width": width, "height": height, "x": x, "y": y}
    result = dispatch("app_resize", params, ctx)
    print_result(result, ctx)


@app_cmd.command("list")
def app_list(ctx: typer.Context) -> None:
    """List all visible windows."""
    result = dispatch("app_list", {}, ctx)
    print_result(result, ctx)


# ── shell (top-level command) ────────────────────────────────────


def shell(
    ctx: typer.Context,
    command: str = typer.Argument(help="PowerShell command to execute"),
) -> None:
    """Execute a PowerShell command."""
    result = dispatch("shell", {"command": command}, ctx)
    print_result(result, ctx)


# ── fs ────────────────────────────────────────────────────────────

fs_cmd = typer.Typer(help="File system operations")


@fs_cmd.command("read")
def fs_read(
    ctx: typer.Context,
    path: str = typer.Argument(help="File path to read"),
) -> None:
    """Read a file."""
    result = dispatch("fs_read", {"path": path}, ctx)
    print_result(result, ctx)


@fs_cmd.command("write")
def fs_write(
    ctx: typer.Context,
    path: str = typer.Argument(help="File path to write"),
    content: str = typer.Argument(help="Content to write"),
) -> None:
    """Write to a file."""
    result = dispatch("fs_write", {"path": path, "content": content}, ctx)
    print_result(result, ctx)


@fs_cmd.command("list")
def fs_list(
    ctx: typer.Context,
    path: str = typer.Argument(help="Directory path to list"),
) -> None:
    """List directory contents."""
    result = dispatch("fs_list", {"path": path}, ctx)
    print_result(result, ctx)


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
    result = dispatch("clipboard_get", params, ctx)
    print_result(result, ctx)


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
    result = dispatch("clipboard_set", params, ctx)
    print_result(result, ctx)


# ── process ───────────────────────────────────────────────────────

proc_cmd = typer.Typer(help="Process management")


@proc_cmd.command("list")
def process_list(ctx: typer.Context) -> None:
    """List running processes."""
    result = dispatch("process_list", {}, ctx)
    print_result(result, ctx)


@proc_cmd.command("kill")
def process_kill(
    ctx: typer.Context,
    pid_or_name: str = typer.Argument(help="PID or process name"),
) -> None:
    """Kill a process."""
    result = dispatch("process_kill", {"pid_or_name": pid_or_name}, ctx)
    print_result(result, ctx)
