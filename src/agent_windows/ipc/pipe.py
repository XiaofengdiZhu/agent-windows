"""Windows Named Pipe wrappers for IPC.

Provides both server-side (daemon) and client-side (CLI) Named Pipe operations.
Uses win32pipe / win32file from pywin32.
"""

import logging

logger = logging.getLogger(__name__)

PIPE_NAME = r"\\.\pipe\agent-windows"
BUFFER_SIZE = 65536
MAX_MESSAGE_SIZE = 10 * 1024 * 1024  # 10 MB guard against runaway reads
CONNECT_TIMEOUT_MS = 5000  # 5 seconds for client connection attempts
MAX_CONNECT_RETRIES = 25
RETRY_INTERVAL_S = 0.2  # 200ms between retries


def _create_pipe_security_attributes():
    """Create a SECURITY_ATTRIBUTES that restricts pipe access to the current user.

    Returns None if the security descriptor cannot be created (falls back to
    default security), with a warning logged.
    """
    import win32security
    import ntsecuritycon

    try:
        user_sid = win32security.LookupAccountName(None, win32security.GetUserName())[0]
        dacl = win32security.ACL()
        dacl.AddAccessAllowedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_GENERIC_READ | ntsecuritycon.FILE_GENERIC_WRITE,
            user_sid,
        )
        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorDacl(1, dacl, 0)

        sa = win32security.SECURITY_ATTRIBUTES()
        sa.SECURITY_DESCRIPTOR = sd
        sa.bInheritHandle = False
        return sa
    except Exception:
        logger.warning(
            "Failed to create pipe security descriptor; falling back to default security",
            exc_info=True,
        )
        return None


def create_pipe_server(pipe_name: str = PIPE_NAME, buffer_size: int = BUFFER_SIZE):
    """Create a Named Pipe server instance (not yet connected).

    The pipe is restricted to the current user via a security descriptor.
    Returns a pipe handle that can be used with connect_pipe_client().
    """
    import win32pipe
    import win32file

    sa = _create_pipe_security_attributes()

    pipe = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1,  # max instances (serial: one client at a time)
        buffer_size,
        buffer_size,
        0,  # default timeout
        sa,  # security attributes (restricted to current user, or None on failure)
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
        if len(data) > MAX_MESSAGE_SIZE:
            raise RuntimeError("Message exceeds maximum size")
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
        logger.debug("Pipe disconnect failed", exc_info=True)
    try:
        win32file.CloseHandle(pipe)
    except Exception:
        logger.debug("Pipe disconnect failed", exc_info=True)


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
            if len(resp_data) > MAX_MESSAGE_SIZE:
                raise RuntimeError("Message exceeds maximum size")
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
        return ctypes.windll.kernel32.WaitNamedPipeW(pipe_name, 100) != 0
    except Exception:
        return False
