"""Windows Named Pipe wrappers for IPC.

Provides both server-side (daemon) and client-side (CLI) Named Pipe operations.
Uses win32pipe / win32file from pywin32.
"""

import logging

logger = logging.getLogger(__name__)

PIPE_NAME = r"\\.\pipe\agent-windows"
BUFFER_SIZE = 65536
CONNECT_TIMEOUT_MS = 5000  # 5 seconds for client connection attempts
MAX_CONNECT_RETRIES = 25
RETRY_INTERVAL_S = 0.2  # 200ms between retries


def create_pipe_server(pipe_name: str = PIPE_NAME, buffer_size: int = BUFFER_SIZE):
    """Create a Named Pipe server instance (not yet connected).

    Returns a pipe handle that can be used with connect_pipe_client().
    """
    import win32pipe
    import win32file

    pipe = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1,  # max instances (serial: one client at a time)
        buffer_size,
        buffer_size,
        0,  # default timeout
        None,  # default security descriptor
    )
    logger.debug("Named Pipe created: %s", pipe_name)
    return pipe


def wait_for_client(pipe) -> None:
    """Block until a client connects to the pipe."""
    import win32pipe

    win32pipe.ConnectNamedPipe(pipe, None)
    logger.debug("Client connected to pipe")


def read_pipe(pipe, buffer_size: int = BUFFER_SIZE) -> str:
    """Read a newline-delimited message from the pipe."""
    import win32file

    # Read in chunks until we find a newline (JSON-RPC is newline-delimited)
    data = b""
    while True:
        _, chunk = win32file.ReadFile(pipe, buffer_size)
        data += chunk
        if b"\n" in data or not chunk:
            break

    # Take everything up to the first newline
    message = data.split(b"\n")[0].decode("utf-8").strip()
    logger.debug("Pipe read: %s", message[:200])
    return message


def write_pipe(pipe, message: str) -> None:
    """Write a message to the pipe (appends newline)."""
    import win32file

    data = (message + "\n").encode("utf-8")
    win32file.WriteFile(pipe, data)
    logger.debug("Pipe write: %s", message[:200])


def disconnect_pipe(pipe) -> None:
    """Disconnect and close the pipe."""
    import win32pipe
    import win32file

    try:
        win32pipe.DisconnectNamedPipe(pipe)
    except Exception:
        pass
    try:
        win32file.CloseHandle(pipe)
    except Exception:
        pass


def send_to_daemon(message: str, pipe_name: str = PIPE_NAME) -> str:
    """Client-side: connect to daemon pipe, send message, read response.

    Raises ConnectionError if daemon is not reachable.
    """
    import win32file

    try:
        # Open existing pipe (daemon must be listening)
        pipe = win32file.CreateFile(
            pipe_name,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            0,
            None,
        )
    except Exception as e:
        raise ConnectionError(f"Cannot connect to daemon pipe: {e}") from e

    try:
        # Write request
        data = (message + "\n").encode("utf-8")
        win32file.WriteFile(pipe, data)
        logger.debug("Sent to daemon: %s", message[:200])

        # Read response — loop to handle large responses
        resp_data = b""
        while True:
            _, chunk = win32file.ReadFile(pipe, BUFFER_SIZE)
            resp_data += chunk
            if b"\n" in resp_data or not chunk:
                break
        response = resp_data.decode("utf-8").strip()
        logger.debug("Daemon response: %s", response[:200])
        return response
    finally:
        win32file.CloseHandle(pipe)


def pipe_exists(pipe_name: str = PIPE_NAME) -> bool:
    """Check if the Named Pipe currently exists (daemon is likely running).

    Uses WaitNamedPipe to check without actually connecting, avoiding
    the "pipe has ended" error on the daemon side.
    """
    import ctypes

    try:
        # WaitNamedPipe returns True if the pipe is available (even with timeout=0)
        return ctypes.windll.kernel32.WaitNamedPipeW(pipe_name, 0) != 0
    except Exception:
        return False
