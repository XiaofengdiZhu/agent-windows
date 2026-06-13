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
ERROR_IO_PENDING = 997  # Overlapped I/O is in progress (normal async result)
ERROR_BROKEN_PIPE = 109  # The pipe end was closed (peer exited)
ERROR_NO_DATA = 232  # The pipe is being closed (peer going away)
# Win32 error codes indicating the peer is gone → map to ConnectionError
_PIPE_CLOSED_ERRORS = {ERROR_BROKEN_PIPE, ERROR_NO_DATA}


class DaemonStartupError(Exception):
    """Fatal daemon startup failure (e.g. the named pipe cannot be secured).

    Propagates past the main loop's per-connection ``except Exception`` retry
    handler so the daemon aborts instead of busy-spinning or serving insecure
    (M-02). Raise it for conditions where retrying cannot help and continuing
    would be unsafe.
    """


def _create_pipe_security_attributes():
    """Create a SECURITY_ATTRIBUTES that restricts pipe access to the current user.

    Returns None if the security descriptor cannot be created (logging the
    underlying error as a warning). Callers MUST treat None as fatal: serving
    on an unsecured pipe (Windows default grants read to Everyone + Anonymous)
    is a fail-open security hole (M-02).
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
            "Failed to create pipe security descriptor; daemon startup will be refused",
            exc_info=True,
        )
        return None


def create_pipe_server(pipe_name: str = PIPE_NAME, buffer_size: int = BUFFER_SIZE):
    """Create a Named Pipe server instance (not yet connected).

    The pipe is restricted to the current user via a security descriptor. If
    the descriptor cannot be built, this raises DaemonStartupError rather than
    falling back to the Windows default (which grants read access to Everyone
    and the anonymous account) — a security control must fail closed (M-02).

    Returns a pipe handle that can be used with connect_pipe_client().
    """
    import win32pipe
    import win32file

    sa = _create_pipe_security_attributes()
    if sa is None:
        # Fail closed: never serve on an unsecured pipe. The helper already
        # logged the underlying win32security error (with exc_info) above.
        raise DaemonStartupError(
            "Cannot create a secure named pipe: failed to build an owner-only "
            "security descriptor. Refusing to start the daemon with default "
            "(world-readable) pipe security. See the preceding warning."
        )

    pipe = win32pipe.CreateNamedPipe(
        pipe_name,
        win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1,  # max instances (serial: one client at a time)
        buffer_size,
        buffer_size,
        0,  # default timeout
        sa,  # security attributes (restricted to current user)
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


def _get_read_timeout() -> float:
    """Resolve the client response timeout (seconds) from config (default 150)."""
    from agent_windows.utils.config import load_config
    return float(load_config().get("ipc", {}).get("read_timeout_seconds", 150))


def _overlapped_write_all(pipe, data: bytes, timeout_ms: int) -> None:
    """Write all bytes to an overlapped handle, completing within timeout_ms.

    Raises ConnectionError if the write does not complete in time.
    """
    import win32api
    import win32event
    import win32file
    import pywintypes

    event = win32event.CreateEvent(None, True, False, None)
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = event
    try:
        hr = 0
        try:
            result = win32file.WriteFile(pipe, data, overlapped)
            hr = result[0] if isinstance(result, tuple) else 0
        except pywintypes.error as e:
            if e.winerror != ERROR_IO_PENDING:
                raise
            hr = ERROR_IO_PENDING

        if hr == ERROR_IO_PENDING:
            rc = win32event.WaitForSingleObject(event, timeout_ms)
            if rc == win32event.WAIT_TIMEOUT:
                win32file.CancelIo(pipe)
                raise ConnectionError(
                    f"Timed out sending request to daemon after {timeout_ms}ms"
                )
            win32file.GetOverlappedResult(pipe, overlapped, False)
    finally:
        win32api.CloseHandle(event)


def _overlapped_read_chunk(pipe, timeout_ms: int) -> bytes:
    """Read one BUFFER_SIZE chunk via overlapped I/O with a timeout.

    Raises ConnectionError on timeout. Other pipe errors (e.g. broken pipe)
    propagate for the caller to map.
    """
    import win32api
    import win32event
    import win32file
    import pywintypes

    event = win32event.CreateEvent(None, True, False, None)
    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = event
    try:
        try:
            hr, buf = win32file.ReadFile(pipe, BUFFER_SIZE, overlapped)
        except pywintypes.error as e:
            if e.winerror != ERROR_IO_PENDING:
                raise  # broken pipe etc. — propagate for mapping
            hr = ERROR_IO_PENDING

        if hr == ERROR_IO_PENDING:
            rc = win32event.WaitForSingleObject(event, timeout_ms)
            if rc == win32event.WAIT_TIMEOUT:
                win32file.CancelIo(pipe)
                raise ConnectionError(
                    f"Timed out waiting {timeout_ms}ms for daemon response"
                )

        # Recover the bytes actually read. ReadFile's buffer spans all of
        # BUFFER_SIZE; GetOverlappedResult returns the transfer count (int).
        n = win32file.GetOverlappedResult(pipe, overlapped, False)
        return bytes(buf)[:n]
    finally:
        win32api.CloseHandle(event)


def send_to_daemon(
    message: str,
    pipe_name: str = PIPE_NAME,
    timeout: float | None = None,
) -> str:
    """Client-side: connect to daemon pipe, send message, read response.

    Uses overlapped I/O so the read cannot block forever if the daemon hangs
    or exits mid-response (H-02). Raises ConnectionError on timeout or if the
    daemon drops the connection.

    Args:
        message: JSON-RPC request string.
        pipe_name: Named pipe path.
        timeout: Per-operation timeout (seconds). None → ``[ipc] read_timeout_seconds``
            (default 150). Applied to each overlapped read/write — not a hard ceiling
            on total response time; a daemon that makes no progress for this long
            triggers a timeout. Must exceed the slowest legitimate operation.
    """
    import win32file
    import pywintypes

    if timeout is None:
        timeout = _get_read_timeout()
    timeout_ms = int(timeout * 1000)

    try:
        # Open existing pipe (daemon must be listening) in overlapped mode
        # so reads can be bounded by a timeout.
        pipe = win32file.CreateFile(
            pipe_name,
            win32file.GENERIC_READ | win32file.GENERIC_WRITE,
            0,
            None,
            win32file.OPEN_EXISTING,
            win32file.FILE_FLAG_OVERLAPPED,
            None,
        )
    except Exception as e:
        raise ConnectionError(f"Cannot connect to daemon pipe: {e}") from e

    try:
        _overlapped_write_all(pipe, (message + "\n").encode("utf-8"), timeout_ms)
        logger.debug("Sent to daemon: %s", message[:200])

        # Read response — loop to handle large responses
        resp_data = b""
        while True:
            chunk = _overlapped_read_chunk(pipe, timeout_ms)
            resp_data += chunk
            if len(resp_data) > MAX_MESSAGE_SIZE:
                raise RuntimeError("Message exceeds maximum size")
            if b"\n" in resp_data or not chunk:
                break
        response = resp_data.decode("utf-8").strip()
        logger.debug("Daemon response: %s", response[:200])
        return response
    except pywintypes.error as e:
        # Daemon exited/closed the pipe mid-response → clear connection error
        if e.winerror in _PIPE_CLOSED_ERRORS:
            raise ConnectionError(
                "Daemon closed the connection without responding"
            ) from e
        raise
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
