"""Windows Named Pipe wrappers for IPC.

Provides both server-side (daemon) and client-side (CLI) Named Pipe operations.
Uses win32pipe / win32file from pywin32.
"""

import logging
import time

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

# Server-side read deadline. The daemon serves one client at a time on a single
# I/O thread, so a client that connects but never sends (or sends only a partial
# request) would block the read forever — wedging the whole daemon. A bounded
# overlapped read lets the server drop such a client instead. 30s is far above
# the time any legitimate single JSON-RPC request takes to finish writing (a few
# KB), so it only ever trips on a stuck/malicious client. (SEC3-06)
SERVER_READ_TIMEOUT_S = 30.0
# Server-side write deadline. The response is written to an overlapped handle
# (FILE_FLAG_OVERLAPPED); a synchronous WriteFile would return ERROR_IO_PENDING
# for any response larger than the kernel pipe buffer (BUFFER_SIZE = 64 KB) and
# leave the transfer unfinished, so disconnect_pipe would truncate it. The
# overlapped write completes as the client drains its end; bound it so a client
# that stops reading can't wedge the write the way a stuck client wedged the
# read. Generous vs. legitimate clients (they read promptly). (SEC3-06)
SERVER_WRITE_TIMEOUT_S = 30.0


class DaemonStartupError(Exception):
    """Fatal daemon startup failure (e.g. the named pipe cannot be secured).

    Propagates past the main loop's per-connection ``except Exception`` retry
    handler so the daemon aborts instead of busy-spinning or serving insecure
    (M-02). Raise it for conditions where retrying cannot help and continuing
    would be unsafe.
    """


class PipeReadTimeout(Exception):
    """Server-side: a connected client did not send a complete request within
    the read deadline (``SERVER_READ_TIMEOUT_S``).

    Distinct from ``ConnectionError`` (the client-side "no response" timeout) so
    ``_main_pipe_loop`` can send a JSON-RPC error reply before disconnecting the
    stuck client, rather than treating it like an ordinary pipe break. The daemon
    stays alive and accepts the next connection. (SEC3-06)
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
    import win32api

    try:
        user_sid = win32security.LookupAccountName(None, win32api.GetUserName())[0]
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

    FILE_FLAG_FIRST_PIPE_INSTANCE makes CreateNamedPipe fail fast (winerror 5,
    ERROR_ACCESS_DENIED) if another process already created a pipe with this
    name. Combined with mapping that error to DaemonStartupError, this makes a
    losing daemon (one that raced another for the single-instance pipe) exit
    cleanly instead of busy-spinning forever on the failed bind — the root cause
    of orphaned ``python.exe --daemon`` processes accumulating under concurrent
    invocations. The winner recreates its own pipe between client connections
    without issue: after disconnect the instance is released, so re-creation is
    a legitimate "first instance" again.

    Returns a pipe handle that can be used with connect_pipe_client().
    """
    import win32pipe
    import win32file
    import pywintypes

    sa = _create_pipe_security_attributes()
    if sa is None:
        # Fail closed: never serve on an unsecured pipe. The helper already
        # logged the underlying win32security error (with exc_info) above.
        raise DaemonStartupError(
            "Cannot create a secure named pipe: failed to build an owner-only "
            "security descriptor. Refusing to start the daemon with default "
            "(world-readable) pipe security. See the preceding warning."
        )

    # 0x00080000 = FILE_FLAG_FIRST_PIPE_INSTANCE. Not exposed by pywin32 as a
    # named constant, so defined inline. winerror 5 (ERROR_ACCESS_DENIED) when
    # the name is already taken; winerror 231 (ERROR_PIPE_BUSY) is the
    # historical fallback on some builds.
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    # 0x40000000 = FILE_FLAG_OVERLAPPED. The server handle must be opened for
    # overlapped I/O so ``read_pipe`` can bound a client that connects but never
    # finishes sending — otherwise a stuck client blocks the single I/O thread
    # forever (SEC3-06). Synchronous WriteFile still completes fine on an
    # overlapped handle for the small response payloads written here.
    FILE_FLAG_OVERLAPPED = 0x40000000
    try:
        pipe = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX
            | FILE_FLAG_FIRST_PIPE_INSTANCE
            | FILE_FLAG_OVERLAPPED,
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1,  # max instances (serial: one client at a time)
            buffer_size,
            buffer_size,
            0,  # default timeout
            sa,  # security attributes (restricted to current user)
        )
    except pywintypes.error as e:
        if e.winerror in (5, 231):
            # Another daemon instance already owns this pipe. This process is a
            # duplicate (lost the spawn race) — raise DaemonStartupError so
            # _main_pipe_loop's except-DaemonStartupError arm exits cleanly
            # instead of the generic handler busy-spinning on the failure.
            raise DaemonStartupError(
                f"Another daemon instance already owns the named pipe "
                f"(winerror {e.winerror}). This process is a duplicate and will exit."
            ) from e
        raise
    logger.debug("Named Pipe created: %s", pipe_name)
    return pipe


def read_pipe(pipe, buffer_size: int = BUFFER_SIZE, timeout: float | None = None) -> str:
    """Server-side: read a newline-delimited request from a connected client.

    Uses overlapped I/O bounded by ``timeout`` so a client that connects but
    never finishes sending cannot block the single I/O thread forever (the
    daemon serves one client at a time — such a client would wedge the whole
    daemon). On timeout raises :class:`PipeReadTimeout` (the caller sends an
    error reply then disconnects); on a peer-gone pipe error raises
    :class:`ConnectionError`, matching the client side. (SEC3-06)

    Args:
        pipe: An overlapped-mode server pipe handle (``create_pipe_server``
            opens with ``FILE_FLAG_OVERLAPPED``).
        buffer_size: Per-read chunk size.
        timeout: Seconds to wait per read attempt. ``None`` →
            ``SERVER_READ_TIMEOUT_S``.
    """
    if timeout is None:
        timeout = SERVER_READ_TIMEOUT_S
    timeout_ms = int(timeout * 1000)

    # Read in chunks until we find a newline (JSON-RPC is newline-delimited).
    # Each chunk is an overlapped read bounded by the remaining deadline, so a
    # client that stalls — even mid-message, or drip-feeding one byte at a time
    # — trips the timeout rather than blocking indefinitely.
    data = b""
    deadline = time.monotonic() + timeout
    try:
        while True:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            chunk = _overlapped_read_chunk(pipe, remaining, buffer_size=buffer_size)
            data += chunk
            if len(data) > MAX_MESSAGE_SIZE:
                raise RuntimeError("Message exceeds maximum size")
            if b"\n" in data or not chunk:
                break
    except ConnectionError as e:
        # _overlapped_read_chunk raises ConnectionError ONLY on its
        # WaitForSingleObject timeout (broken-pipe/peer-closed surface as raw
        # pywintypes.error and propagate past this clause to the caller). So a
        # ConnectionError here unambiguously means the read deadline elapsed —
        # re-raise as the server-specific PipeReadTimeout so the main loop sends
        # an error reply before disconnecting, instead of a silent broken-pipe
        # drop. If that invariant ever changes, _overlapped_read_chunk should
        # gain a dedicated timeout subclass instead of this catch.
        raise PipeReadTimeout(str(e)) from e

    # Take everything up to the first newline
    message = data.split(b"\n")[0].decode("utf-8").strip()
    logger.debug("Pipe read: %s", message[:200])
    return message


def write_pipe(pipe, message: str, timeout: float | None = None) -> None:
    """Write a message to the pipe (appends newline).

    ``timeout`` selects the I/O model. When ``None`` (the legacy/default
    behavior) the write is a plain synchronous ``WriteFile`` — correct for
    non-overlapped handles and small payloads that complete immediately. When
    given (seconds), the write uses overlapped I/O bounded by ``timeout``,
    which is REQUIRED for the daemon's overlapped server handle: a synchronous
    ``WriteFile`` there returns ``ERROR_IO_PENDING`` for any response larger
    than the kernel pipe buffer (64 KB) and leaves the transfer unfinished, so
    a subsequent ``disconnect_pipe`` would truncate it. The overlapped path
    also bounds a client that stops reading (otherwise it could wedge the
    single I/O thread the way the read timeout bounds a stuck sender).
    (SEC3-06)

    Raises ``ConnectionError`` on overlapped-write timeout.
    """
    data = (message + "\n").encode("utf-8")
    if timeout is None:
        import win32file
        win32file.WriteFile(pipe, data)
    else:
        _overlapped_write_all(pipe, data, int(timeout * 1000))
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


def _overlapped_read_chunk(pipe, timeout_ms: int, buffer_size: int = BUFFER_SIZE) -> bytes:
    """Read one ``buffer_size`` chunk via overlapped I/O with a timeout.

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
            hr, buf = win32file.ReadFile(pipe, buffer_size, overlapped)
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

    # The daemon briefly tears down and recreates the named pipe between
    # connections (disconnect → create → ConnectNamedPipe). During that gap
    # CreateFile fails with ERROR_FILE_NOT_FOUND (winerror 2). Rather than
    # surfacing an error (or, upstream, spawning a rival daemon), retry the
    # connect a few times with a short backoff — the gap is sub-millisecond
    # under normal load. Bound it so a genuinely dead daemon still fails fast.
    CONNECT_RETRIES = 50
    CONNECT_BACKOFF_S = 0.01  # 10ms → up to ~0.5s total worst case

    import pywintypes  # noqa: F811 — already imported at module scope, kept for clarity

    try:
        # Open existing pipe (daemon must be listening) in overlapped mode
        # so reads can be bounded by a timeout.
        for attempt in range(CONNECT_RETRIES):
            try:
                pipe = win32file.CreateFile(
                    pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    win32file.FILE_FLAG_OVERLAPPED,
                    None,
                )
                break
            except pywintypes.error as e:
                # winerror 2 = ERROR_FILE_NOT_FOUND: pipe doesn't exist right now.
                # This is the expected transient state while a live daemon cycles
                # its pipe instance. Retry. Other errors (access denied, etc.)
                # propagate immediately.
                if e.winerror != 2 or attempt == CONNECT_RETRIES - 1:
                    raise
                time.sleep(CONNECT_BACKOFF_S)
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
        # 100ms timeout — WaitNamedPipe returns True if the pipe exists/opens
        # within the window, False (timeout) if not. (Non-zero to avoid a busy
        # spin when the daemon is mid-startup.)
        return ctypes.windll.kernel32.WaitNamedPipeW(pipe_name, 100) != 0
    except Exception:
        return False
