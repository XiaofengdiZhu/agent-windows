"""Daemon commands: start, stop, status, ping."""

import sys

import typer

from agent_windows.utils.config import get_pid_path, get_version_path
from agent_windows.daemon.lifecycle import read_pid_file, is_process_alive, read_version_file
from agent_windows.cli.dispatcher import _cmd

app = typer.Typer(help="Daemon management")


def ping(ctx: typer.Context) -> None:
    """Daemon health check — one IPC round-trip (C2).

    Registered as a *top-level* command (``agent-windows ping``), not
    ``daemon ping``: the README and the native launcher expose it at the top
    level (launcher commands.rs maps ``"ping"`` straight to a daemon call),
    so the Python fallback (``agent-windows-python`` / ``--no-daemon``) matches.
    The daemon's ping handler returns ``{"status": "ok", "message": "pong"}``
    with no ``output`` key, so ``print_result`` renders the fields directly
    (and ``--format json`` dumps the whole dict). ``ping`` is policy-exempt in
    the router, so it works even before any tool authorization is configured.
    """
    _cmd(ctx, "ping")


@app.command("start")
def daemon_start() -> None:
    """Start the daemon process."""
    import subprocess
    from agent_windows.ipc.lock import DaemonSpawnLock
    from agent_windows.ipc.pipe import pipe_exists

    # Serialize the check-then-spawn with the same cross-process lock the
    # launcher and ensure_daemon_running use, so two concurrent `daemon start`
    # invocations can't each pass the liveness check and spawn a rival.
    with DaemonSpawnLock():
        # Double-check inside the lock: another starter may have brought the
        # daemon up while we waited.
        if pipe_exists():
            typer.echo("Daemon already running")
            return
        info = read_pid_file(get_pid_path())
        if info and is_process_alive(info[0]):
            typer.echo(f"Daemon already running (PID={info[0]})")
            return

        # Start daemon subprocess. Propagate --config if an override is active
        # in this process so the daemon reads the same file (TODO #21-1).
        # Mirrors ipc.client._start_daemon_process.
        #
        # --debug is NOT passed on the argv here. If the user ran with --debug,
        # main()'s global_options callback already called enable_debug(), which
        # sets AGENT_WINDOWS_DEBUG in this process's environment; Popen inherits
        # it, so the spawned daemon picks debug mode up via that env var. This
        # matches the Python auto-start path (_start_daemon_process), where the
        # flag is "redundant-but-harmless" for the same reason. The argv --debug
        # is only load-bearing on the Rust-launcher spawn path (the launcher
        # never sets the env var, so the daemon reads "--debug" from argv and
        # calls enable_debug() itself). Keeping this path env-var-driven avoids
        # divergence; do not add `--debug` to `cmd` without also reconsidering
        # _start_daemon_process. (C3, TODO #21-3)
        cmd = [sys.executable, "-m", "agent_windows", "--daemon"]
        from agent_windows.utils.config import get_config_override
        if (override := get_config_override()) is not None:
            cmd += ["--config", str(override)]
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
        # A non-zero exit distinguishes "shutdown failed" from success for
        # scripts/agents; previously this swallowed the error and exited 0.
        typer.echo(f"Error stopping daemon: {e}", err=True)
        raise typer.Exit(1)


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
