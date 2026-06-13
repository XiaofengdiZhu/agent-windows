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
    no_daemon: bool = False,
    debug: bool = False,
) -> dict:
    """Send a JSON-RPC command to the daemon and return the result.

    Args:
        method: JSON-RPC method name (e.g. "ping", "screenshot").
        params: Optional parameters dict.
        no_daemon: If True, raise (direct execution not yet supported at this layer).
        debug: Enable debug logging.

    Returns:
        The "result" field from the JSON-RPC response.

    Raises:
        ConnectionError: If daemon is unreachable.
        RuntimeError: If daemon returns an error.
    """
    request = JSONRPCRequest(id=1, method=method, params=params)
    message = request.to_json()

    # Ensure daemon is running
    ensure_daemon_running(debug=debug)

    # Send request
    response_str = send_to_daemon(message, PIPE_NAME)
    response = JSONRPCResponse.from_json(response_str)
    return unwrap_response(response)


def ensure_daemon_running(debug: bool = False) -> None:
    """Check if daemon is running, start it if not.

    Retries connection with backoff until daemon is ready.
    """
    if pipe_exists():
        # Fast path: daemon already running
        return

    logger.info("Daemon not running, starting...")
    _start_daemon_process(debug=debug)

    # Retry connection
    for i in range(MAX_CONNECT_RETRIES):
        time.sleep(RETRY_INTERVAL_S)
        if pipe_exists():
            logger.info("Daemon ready (attempt %d)", i + 1)
            return

    raise ConnectionError(
        f"Daemon did not become ready after {MAX_CONNECT_RETRIES} retries "
        f"({MAX_CONNECT_RETRIES * RETRY_INTERVAL_S:.1f}s)"
    )


def _start_daemon_process(debug: bool = False) -> subprocess.Popen:
    """Start the daemon as a subprocess."""
    cmd = [sys.executable, "-m", "agent_windows", "--daemon"]
    if debug:
        cmd.append("--debug")

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
