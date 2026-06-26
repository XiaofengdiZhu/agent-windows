"""IPC client: CLI-side connection to Daemon via Named Pipe.

Handles:
- Auto-starting daemon if not running
- Retrying connection during daemon startup
- Sending JSON-RPC requests and reading responses
- Version mismatch detection (auto-restart daemon)
"""

import logging
import os
import subprocess
import sys
import time

from agent_windows.ipc.protocol import JSONRPCRequest, JSONRPCResponse
from agent_windows.ipc.pipe import (
    PIPE_NAME,
    MAX_CONNECT_RETRIES,
    RETRY_INTERVAL_S,
    send_to_daemon,
    pipe_exists,
)

logger = logging.getLogger(__name__)


def unwrap_response(response: JSONRPCResponse) -> dict:
    """Return a JSON-RPC response's result, or raise if it carries an error.

    Centralizes the error→exception mapping so the daemon path
    (:func:`call_daemon`) and the in-process ``--no-daemon`` path
    (:func:`agent_windows.cli.dispatcher.dispatch`) surface identical errors.
    A response with ``error`` becomes ``RuntimeError("Daemon error [<code>]:
    <message>")`` — matching the codes :func:`route_request` produces
    (INVALID_PARAMS / WAIT_TIMEOUT / METHOD_NOT_FOUND / DAEMON_INTERNAL_ERROR).
    """
    if response.error is not None:
        raise RuntimeError(
            f"Daemon error [{response.error.get('code')}]: {response.error.get('message')}"
        )
    return response.result


def call_daemon(
    method: str,
    params: dict | None = None,
    debug: bool = False,
) -> dict:
    """Send a JSON-RPC command to the daemon and return the result.

    Args:
        method: JSON-RPC method name (e.g. "ping", "screenshot").
        params: Optional parameters dict.
        debug: Enable debug logging.

    Returns:
        The "result" field from the JSON-RPC response.

    Raises:
        ConnectionError: If daemon is unreachable.
        RuntimeError: If daemon returns an error.

    Note:
        This is the daemon-backed path only. The ``--no-daemon`` direct-execution
        path is handled entirely in :func:`agent_windows.cli.dispatcher.dispatch`
        (which never reaches this function when ``no_daemon`` is set).
    """
    request = JSONRPCRequest(id=1, method=method, params=params)
    message = request.to_json()

    # Ensure daemon is running
    ensure_daemon_running(debug=debug)

    # Send request
    response_str = send_to_daemon(message, PIPE_NAME)
    response = JSONRPCResponse.from_json(response_str)
    return unwrap_response(response)


def _daemon_process_alive() -> bool:
    """Check if a daemon process is alive via its PID file.

    Used as a second opinion when ``pipe_exists()`` reports the pipe missing:
    the daemon briefly tears down and recreates the named pipe between
    connections (disconnect → create → ConnectNamedPipe), and during that gap
    ``WaitNamedPipeW`` returns False even though the daemon is perfectly
    healthy. Spawning a new daemon in that gap leaks processes (each new
    daemon fails to bind the single-instance pipe but, launched detached, is
    never reaped) — observed as dozens of orphaned ``python.exe --daemon``
    processes under rapid-fire IPC. Checking the PID file avoids that: if a
    daemon process is still alive, we wait for it rather than spawning a rival.
    """
    from agent_windows.daemon.lifecycle import is_process_alive, read_pid_file
    from agent_windows.utils.config import get_pid_path

    info = read_pid_file(get_pid_path())
    if info is None:
        return False
    pid, _ = info
    return is_process_alive(pid)


def ensure_daemon_running(debug: bool = False) -> None:
    """Check if daemon is running, start it if not.

    Retries connection with backoff until daemon is ready. If the spawned
    daemon exits during startup — it crashed (missing dependency, COM init
    failure, secure-pipe refusal, version abort) instead of reaching its main
    loop — fail fast with its exit code rather than waiting out the full retry
    window: a broken install would otherwise hang every CLI command ~5s on an
    opaque "did not become ready". (M-31)

    The spawn decision is serialized across processes by a ``LockFileEx``-based
    lock (``DaemonSpawnLock``). Without it, two concurrent CLI calls could each
    observe "daemon down" during the pipe-recreate gap and both spawn a daemon;
    the loser exits cleanly now (FIRST_PIPE_INSTANCE), but the waste + spurious
    "exited during startup" error still degrade the experience. The lock is
    acquired only around the check-then-spawn critical section, not the whole
    command, so a stuck spawn can't wedge unrelated commands for long.
    """
    if pipe_exists():
        # Fast path: daemon already running and pipe is connectable
        return

    # The pipe may be momentarily absent while a live daemon tears down and
    # recreates it between connections. If the daemon process is still alive,
    # don't spawn a rival — send_to_daemon retries the pipe connect through
    # that transient gap. Only spawn when the daemon is genuinely gone.
    if _daemon_process_alive():
        return

    logger.info("Daemon not running, starting...")
    from agent_windows.ipc.lock import DaemonSpawnLock

    with DaemonSpawnLock():
        # Double-check inside the lock: another spawner may have just brought
        # the daemon up while we waited for the lock. Skip spawning if so.
        if pipe_exists() or _daemon_process_alive():
            logger.debug("Daemon came up while waiting for spawn lock")
            return

        proc = _start_daemon_process(debug=debug)

        # Retry connection
        for i in range(MAX_CONNECT_RETRIES):
            time.sleep(RETRY_INTERVAL_S)
            if pipe_exists():
                logger.info("Daemon ready (attempt %d)", i + 1)
                return
            # Spawned, but the pipe still isn't there — if the process has already
            # exited it failed during startup (a healthy daemon blocks in its main
            # loop after creating the pipe and never exits). Abort now with the
            # exit code instead of burning the rest of the window. (M-31)
            rc = proc.poll()
            if rc is not None:
                raise ConnectionError(
                    f"Daemon process exited during startup (code {rc}) — likely a "
                    "configuration or environment error. Check the daemon log at "
                    "~/.agent-windows/logs/daemon.log, or run "
                    f"`{sys.executable} -m agent_windows --daemon --debug` for "
                    "console output."
                )

        raise ConnectionError(
            f"Daemon did not become ready after {MAX_CONNECT_RETRIES} retries "
            f"({MAX_CONNECT_RETRIES * RETRY_INTERVAL_S:.1f}s)"
        )


def _start_daemon_process(debug: bool = False) -> subprocess.Popen:
    """Start the daemon as a subprocess.

    If a ``--config`` override is active in *this* process, append
    ``--config <path>`` so the spawned daemon reads the same file (TODO #21-1).
    Without this, the daemon would read the default config while the
    CLI/MCP process reads the override — silent divergence.
    """
    cmd = [sys.executable, "-m", "agent_windows", "--daemon"]
    if debug:
        cmd.append("--debug")
    from agent_windows.utils.config import get_config_override
    override = get_config_override()
    if override is not None:
        cmd += ["--config", str(override)]

    # Detach from parent: daemon runs independently
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.DETACHED_PROCESS
        ),
    )
    logger.info("Daemon process started (PID=%d)", proc.pid)
    return proc
