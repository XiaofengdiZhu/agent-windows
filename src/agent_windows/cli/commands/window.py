"""Window management commands: minimize/maximize/restore/close/focus (P4.2).

Target resolution (HWND ``0x``-hex / title / process-name substring) happens in
the daemon core (core.window_manager → core.tree.resolve_window); no target
defaults to the foreground window.
"""
import typer

from agent_windows.cli.dispatcher import _cmd

_TARGET_HELP = ("Window HWND (e.g. 0x1234), title substring, or process name "
                "(default: the foreground window)")

window_cmd = typer.Typer(help="Window management (minimize/maximize/restore/close/focus/enable)")


@window_cmd.command("minimize")
def window_minimize(ctx: typer.Context,
                    target: str = typer.Argument("", help=_TARGET_HELP)) -> None:
    """Minimize a window."""
    _cmd(ctx, "window_minimize", target=target)


@window_cmd.command("maximize")
def window_maximize(ctx: typer.Context,
                    target: str = typer.Argument("", help=_TARGET_HELP)) -> None:
    """Maximize a window."""
    _cmd(ctx, "window_maximize", target=target)


@window_cmd.command("restore")
def window_restore(ctx: typer.Context,
                   target: str = typer.Argument("", help=_TARGET_HELP)) -> None:
    """Restore a window to its normal size."""
    _cmd(ctx, "window_restore", target=target)


@window_cmd.command("close")
def window_close(ctx: typer.Context,
                 target: str = typer.Argument("", help=_TARGET_HELP)) -> None:
    """Close a window (graceful — the app may prompt to save)."""
    _cmd(ctx, "window_close", target=target)


@window_cmd.command("focus")
def window_focus(ctx: typer.Context,
                 target: str = typer.Argument("", help=_TARGET_HELP)) -> None:
    """Focus (bring to foreground) a window."""
    _cmd(ctx, "window_focus", target=target)


@window_cmd.command("enable")
def window_enable(ctx: typer.Context,
                  target: str = typer.Argument("", help=_TARGET_HELP)) -> None:
    """Re-enable a window left disabled by an interrupted --background click."""
    _cmd(ctx, "window_enable", target=target)
