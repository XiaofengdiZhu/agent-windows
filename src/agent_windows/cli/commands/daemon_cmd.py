"""Daemon commands: start, stop, status."""

import sys

import typer

from agent_windows.utils.config import get_pid_path, get_version_path
from agent_windows.daemon.lifecycle import read_pid_file, is_process_alive, read_version_file

app = typer.Typer(help="Daemon management")


@app.command("start")
def daemon_start() -> None:
    """Start the daemon process."""
    import subprocess

    pid_path = get_pid_path()

    # Check if already running
    info = read_pid_file(pid_path)
    if info and is_process_alive(info[0]):
        typer.echo(f"Daemon already running (PID={info[0]})")
        return

    # Start daemon subprocess
    cmd = [sys.executable, "-m", "agent_windows", "--daemon"]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS,
    )
    typer.echo(f"Daemon started (PID={proc.pid})")


@app.command("stop")
def daemon_stop() -> None:
    """Stop the daemon process."""
    from agent_windows.ipc.client import call_daemon
    from agent_windows.ipc.pipe import pipe_exists

    if not pipe_exists():
        typer.echo("Daemon is not running")
        return

    try:
        call_daemon("shutdown")
        typer.echo("Daemon shutdown requested")
    except Exception as e:
        typer.echo(f"Error stopping daemon: {e}", err=True)


@app.command("status")
def daemon_status() -> None:
    """Check daemon status."""
    from agent_windows import __version__
    from agent_windows.ipc.pipe import pipe_exists

    pid_path = get_pid_path()
    version_path = get_version_path()
    info = read_pid_file(pid_path)
    running = info is not None and is_process_alive(info[0])
    pipe_ok = pipe_exists()
    version = read_version_file(version_path) or "unknown"

    status = "running" if running and pipe_ok else "stopped"
    typer.echo(f"Status: {status}")
    if running:
        typer.echo(f"PID: {info[0]}")
        typer.echo(f"Daemon version: {version}")
        typer.echo(f"Tool version: {__version__}")
        if version != __version__:
            typer.echo(f"⚠ Version mismatch (will auto-restart on next command)")
    else:
        typer.echo(f"Tool version: {__version__}")
