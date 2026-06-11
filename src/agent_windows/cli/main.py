"""CLI entry point: Typer app with command registration."""

import typer

app = typer.Typer(
    name="agent-windows",
    help="Windows Computer-Use CLI for AI Agents.",
    add_completion=False,
    no_args_is_help=True,
)


# ── Global options ────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def global_options(
    ctx: typer.Context,
    no_daemon: bool = typer.Option(False, "--no-daemon", help="Skip daemon, execute directly (debug)"),
    format: str = typer.Option("text", "--format", help="Output format: text|json"),
    debug: bool = typer.Option(False, "--debug", help="Enable verbose logging"),
) -> None:
    """agent-windows — Windows Computer-Use CLI for AI Agents."""
    ctx.ensure_object(dict)
    ctx.obj["no_daemon"] = no_daemon
    ctx.obj["format"] = format
    ctx.obj["debug"] = debug


# ── Register top-level commands ───────────────────────────────────
# Commands defined in individual modules, added here to the main app.

from agent_windows.cli.commands.observe import observe, snapshot, screenshot, ground, ask_vision, detect, cleanup  # noqa: E402
from agent_windows.cli.commands.input import click, type_text, scroll, move, dblclick, hover, mouse_down, mouse_up, drag, shortcut  # noqa: E402
from agent_windows.cli.commands.system import (  # noqa: E402
    shell,
    app_cmd, fs_cmd, clip_cmd, proc_cmd,
)
from agent_windows.cli.commands.wait import wait  # noqa: E402

app.command()(observe)
app.command()(snapshot)
app.command()(screenshot)
app.command()(ground)
app.command()(ask_vision)
app.command()(detect)
app.command()(cleanup)
app.command()(click)
app.command(name="type")(type_text)
app.command()(scroll)
app.command()(move)
app.command()(dblclick)
app.command()(hover)
app.command()(mouse_down)
app.command()(mouse_up)
app.command()(drag)
app.command()(shortcut)
app.command()(shell)
app.command()(wait)

# ── Register sub-command groups ───────────────────────────────────

app.add_typer(app_cmd, name="app")
app.add_typer(fs_cmd, name="fs")
app.add_typer(clip_cmd, name="clipboard")
app.add_typer(proc_cmd, name="process")

# ── Daemon and Config groups ──────────────────────────────────────

from agent_windows.cli.commands.daemon_cmd import app as daemon_app  # noqa: E402
from agent_windows.cli.commands.config_cmd import app as config_app  # noqa: E402

app.add_typer(daemon_app, name="daemon")
app.add_typer(config_app, name="config")
