"""Daemon main loop: Named Pipe listener with 3-thread model.

Threads:
1. I/O thread (main): listens on Named Pipe, receives JSON-RPC requests
2. COM STA worker thread: executes all UIA/COM operations
3. Idle monitor thread: watches for inactivity timeout

Usage: python -m agent_windows --daemon [--debug]
"""

import json
import logging
import os
import queue
import sys
import threading
import time
import ctypes

import pywintypes
import win32api
import win32event
import win32pipe

from agent_windows import __version__
from agent_windows.utils.debug import is_debug
from agent_windows.daemon.lifecycle import (
    write_pid_file,
    write_version_file,
    cleanup_stale_daemon,
    check_version_mismatch,
)
from agent_windows.daemon.router import route_request, HANDLERS
from agent_windows.ipc.protocol import JSONRPCRequest, JSONRPCResponse, PARSE_ERROR, INVALID_REQUEST, make_error
from agent_windows.ipc.pipe import (
    PIPE_NAME,
    BUFFER_SIZE,
    create_pipe_server,
    DaemonStartupError,
    PipeReadTimeout,
    SERVER_WRITE_TIMEOUT_S,
    read_pipe,
    write_pipe,
    disconnect_pipe,
)
from agent_windows.utils.config import get_pid_path as cfg_pid_path, get_version_path as cfg_version_path, get_log_dir, load_config

logger = logging.getLogger("agent_windows.daemon")

# Wall-clock budget the main loop waits for the STA worker to finish one
# command. Sized at 300s to match the longest SINGLE-command self-limit in the
# codebase — handlers/tree.py MAX_WAIT_SECONDS (300), which wait/wait_for
# reject beyond; shell is bounded by shell.timeout (default 30s) and tree
# traversal by tree.timeout (default 10s). For those single commands the
# worker always finishes first and submit never times out. (SEC3-08)
#
# This does NOT cover the COMPOUND handlers, which can run far past 300s by
# design: ``batch`` chains up to MAX_BATCH_SIZE (50) sub-commands — each of
# which may itself be a 300s wait/wait_for — plus an unbounded per-step
# ``delay``; ``multi_edit`` types up to MAX_TYPE_TEXT_LEN (10000) chars into
# up to MAX_MULTI_TARGETS (50) fields (each type_text ≈ len×0.02s). For such
# calls the advisory timeout IS reachable; that's accepted because the only
# alternative — a per-command abort flag — cannot actually cancel an in-flight
# UIA/SendInput/subprocess call on the COM STA thread either, so it would not
# help the wedge case the budget is meant to bound.
#
# The timeout is ADVISORY, not a hard cancellation: when it fires it only
# reports WAIT_TIMEOUT to the client and disconnects — it does NOT interrupt
# the in-flight command on the STA thread (COM STA cannot reliably cancel a
# running UIA/SendInput call; Python threads can't preempt C-extension calls).
# The orphaned command runs to completion; see submit() for what happens to
# its late result. The genuinely dangerous case — a single UIA call blocked
# past its internal SendMessageTimeout — is past ~30s and therefore past this
# 300s budget too, and is not something a shorter timeout or an abort flag
# could fix, so the trade-off is accepted.
STA_SUBMIT_TIMEOUT_S = 300.0

# ── COM STA Worker Thread ─────────────────────────────────────────


class COMWorkerThread(threading.Thread):
    """Dedicated STA thread for all COM/UIA operations.

    All UIA calls happen here. Commands are received via queue,
    results are placed back keyed by a per-submit monotonic seq (NOT the
    request.id — the single client reuses a fixed id, so keying by id would let
    an orphaned command's late write collide with a later request's slot; see
    submit()'s docstring and B1).

    This worker processes one command at a time, which together with the
    single-instance named pipe and the blocking I/O loop makes the daemon
    fully serial. That is by design: UIA/COM objects are apartment-bound to
    this thread, and SendInput input simulation mutates global GUI state, so
    concurrent commands would race and corrupt automation. A single client is
    naturally serial (request → response → reconnect), so serial execution
    costs nothing in normal use — only concurrent multi-client sessions
    contend. (H-06)
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.command_queue: queue.Queue[dict] = queue.Queue()
        self.results: dict[str | int | None, dict] = {}
        self._results_lock = threading.Lock()
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._result_events: dict[str | int | None, threading.Event] = {}
        # Monotonic per-submit handle. Results/events are keyed by this, NOT by
        # request.id — the single client reuses a fixed id (1) for every request,
        # so an orphaned (timed-out) command's late write would otherwise land on
        # the same key a later request reads. The seq makes every command's
        # result key unique, so an orphan's completion writes to a key no later
        # request is waiting on. (B1)
        self._submit_seq = 0

    def run(self):
        """Thread entry point: initialize COM STA and process commands."""
        import pythoncom

        pythoncom.CoInitialize()
        logger.info("COM STA worker thread initialized")
        try:
            # Set DPI awareness (must be done before any UIA calls)
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PerMonitorDpiAware
                logger.info("DPI awareness set (PerMonitorDpiAware)")
            except Exception as e:
                logger.warning("Failed to set DPI awareness: %s (may already be set)", e)

            # Initialize UIA on this thread
            try:
                from agent_windows.uia.core import _AutomationClient
                client = _AutomationClient.instance()
                logger.info("UIA initialized on STA thread")
            except Exception as e:
                logger.error("Failed to initialize UIA: %s", e)
                self._ready.set()
                return

            self._ready.set()

            # Command processing loop with COM message pump
            while not self._stop_event.is_set():
                # Pump COM messages (required for STA)
                try:
                    pythoncom.PumpWaitingMessages()
                except Exception:
                    logger.debug("PumpWaitingMessages failed", exc_info=True)

                try:
                    cmd = self.command_queue.get(timeout=0.05)
                    if cmd is None:
                        # Shutdown signal
                        break
                    self._execute_command(cmd)
                except queue.Empty:
                    continue
        finally:
            # Always pair CoUninitialize with the CoInitialize above — even on
            # the UIA-init-failure early return or an unexpected exception out
            # of the command loop. Leaving it unpaired can leak STA-apartment
            # resources until process exit.
            pythoncom.CoUninitialize()
            logger.info("COM STA worker thread stopped")

    def _execute_command(self, cmd: dict):
        """Execute a single command and store the result.

        Results and the completion event are keyed by the command's ``seq``
        (assigned by submit), NOT by request.id. The single client reuses a
        fixed request.id, so keying by id would let an orphaned command's late
        write collide with a later request's slot. Falls back to ``id`` only
        when ``seq`` is absent (legacy/test callers that don't go through
        submit). (B1)
        """
        cmd_id = cmd.get("id")
        # The result key: the per-submit seq if submit assigned one, else the
        # request id (for direct _execute_command callers without a seq).
        key = cmd.get("seq", cmd_id)
        request = cmd.get("request")

        try:
            # Route to the handler (handlers are registered in router.py)
            response = route_request(request)
            with self._results_lock:
                self.results[key] = {"response": response.to_json()}
        except Exception as e:
            error_resp = make_error(-1999, str(e), cmd_id)
            with self._results_lock:
                self.results[key] = {"response": error_resp.to_json()}
        finally:
            # Signal completion. _result_events is read here (STA worker thread)
            # and written in submit() (I/O thread) — guard both under the same
            # lock so a re-entrant submit or a future second client can't lose
            # the completion signal. (PERF3-01)
            with self._results_lock:
                evt = self._result_events.get(key)
            if evt:
                evt.set()

    def submit(self, request: JSONRPCRequest, timeout: float = 30.0) -> str:
        """Submit a request to the worker thread and wait for the result.

        Returns the JSON response string.
        Raises TimeoutError if the command doesn't complete in time.

        The timeout is ADVISORY: on expiry this returns/raises to the caller
        (the main loop surfaces WAIT_TIMEOUT and disconnects the client), but
        the STA worker keeps running the orphaned command to completion — COM
        STA cannot reliably cancel an in-flight UIA/SendInput call, so we don't
        pretend to. The default caller (_main_pipe_loop) passes
        STA_SUBMIT_TIMEOUT_S, sized for single-command self-limits (see there
        for the compound-handler caveat).

        On timeout the finally block pops this command's event + result slot,
        but the orphaned ``_execute_command`` still runs on the worker and
        writes its result to ``self.results[seq]`` AFTER that pop. That late
        write is now harmless: results and the completion event are keyed by a
        per-submit monotonic ``seq`` (assigned below), not by request.id, so the
        orphan's late write lands on its own seq key — a key no later submit
        reads (each gets a fresh seq). The single client (ipc/client.py) reuses
        a fixed request.id for every request, which is exactly what made the
        old id-keyed scheme vulnerable; the seq closes that cross-delivery
        path. (B1) In practice the window was already narrow because a timed-out
        request triggers disconnect (the client must reconnect), so the next
        request arrives after the orphan has had time to settle.
        """
        cmd_id = request.id
        event = threading.Event()
        with self._results_lock:
            # Assign a fresh, unique seq for THIS submit. Keying the result/event
            # slot by seq (not the reused request.id) is the B1 fix: an orphaned
            # command's late write can't collide with a later request's slot.
            self._submit_seq += 1
            seq = self._submit_seq
            # PERF3-01 single-in-flight guard, now keyed by seq. With per-submit
            # seqs a second submit can never collide on the slot by construction,
            # so this is a belt-and-braces check against a genuinely re-entrant
            # submit (same thread calling submit while one is already waiting) —
            # which would otherwise deadlock silently waiting on an event that
            # can never be set until the outer submit returns.
            if seq in self._result_events:
                raise RuntimeError(
                    f"Submit seq {seq} already in flight — unreachable (seq is "
                    "monotonic); indicates a re-entrant submit")
            self._result_events[seq] = event
            # Clear any result a previously-retired (timed-out) command with this
            # exact seq left behind. Unreachable in practice (seqs are unique and
            # monotonic), retained as a defensive no-op guard. (PERF3-02 lineage)
            self.results.pop(seq, None)

        try:
            self.command_queue.put({"id": cmd_id, "seq": seq, "request": request})

            # Wait for result
            if not event.wait(timeout=timeout):
                raise TimeoutError(f"Command {request.method} timed out after {timeout}s")

            with self._results_lock:
                result = self.results.pop(seq, None)

            if result is None:
                raise RuntimeError(f"No result for command {request.method}")

            return result["response"]
        finally:
            with self._results_lock:
                self._result_events.pop(seq, None)
                self.results.pop(seq, None)

    def stop(self):
        """Signal the worker thread to stop."""
        self._stop_event.set()
        self.command_queue.put(None)  # Poison pill

    def wait_ready(self, timeout: float = 10.0):
        """Wait until the worker thread is initialized."""
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError("COM STA worker thread failed to initialize")


# ── Idle Monitor Thread ───────────────────────────────────────────


class IdleMonitorThread(threading.Thread):
    """Watches for inactivity and triggers daemon shutdown."""

    def __init__(self, timeout_minutes: float = 30.0, shutdown_callback=None):
        super().__init__(daemon=True)
        self.timeout_seconds = timeout_minutes * 60
        self.shutdown_callback = shutdown_callback
        self._last_activity = time.time()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def run(self):
        logger.info("Idle monitor started (timeout=%dm)", self.timeout_seconds // 60)
        while not self._stop_event.is_set():
            time.sleep(10)  # Check every 10 seconds
            with self._lock:
                idle = time.time() - self._last_activity
            if idle >= self.timeout_seconds:
                logger.info("Idle timeout reached (%.0fs), shutting down", idle)
                if self.shutdown_callback:
                    self.shutdown_callback()
                break
        logger.info("Idle monitor stopped")

    def touch(self):
        """Update last activity timestamp."""
        with self._lock:
            self._last_activity = time.time()

    def stop(self):
        self._stop_event.set()


# ── Daemon Main Loop ──────────────────────────────────────────────


def _parse_config_override_from_argv(argv: list[str]) -> str | None:
    """Extract ``--config <path>`` / ``--config=<path>`` from argv.

    Mirrors the Rust launcher's flag forms so both sides agree. Returns the
    first match (left-to-right), or None when absent / when ``--config`` has no
    following token. Does NOT match lookalikes like ``--config-file``.

    Used by :func:`_init_daemon_state` to honor the ``--config`` the launcher
    appended when spawning this daemon process — the same way it reads
    ``--debug`` from argv.
    """
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--config="):
            return a[len("--config="):]
    return None


def _init_daemon_state():
    """Parse paths, load config, write PID/version files.

    Returns a dict with keys: pid_path, version_path, idle_timeout,
    shutdown_event, sta_thread, idle_monitor.
    """
    if "--debug" in sys.argv:
        from agent_windows.utils.debug import enable_debug
        enable_debug()
    _setup_logging(is_debug())

    # Honor --config the launcher appended when spawning us (TODO #21-1).
    # Must run before the first load_config() below so the daemon reads the
    # same file the CLI/MCP process is using.
    _cfg_override = _parse_config_override_from_argv(sys.argv)
    if _cfg_override:
        from agent_windows.utils.config import set_config_override
        set_config_override(_cfg_override)

    logger.info("agent-windows daemon v%s starting (PID=%d)", __version__, os.getpid())

    pid_path = cfg_pid_path()
    version_path = cfg_version_path()

    # Check for stale daemon
    cleanup_stale_daemon(pid_path, version_path)

    # Check version mismatch
    if check_version_mismatch(version_path, __version__):
        logger.info("Version mismatch, existing daemon may need restart")

    # Load config for idle timeout
    config = load_config()
    idle_timeout = config.get("daemon", {}).get("idle_timeout_minutes", 30)

    # Defense-in-depth tool policy (review #7 follow-up): resolve [tools]
    # include/exclude once at startup so the daemon itself rejects filtered
    # methods at route_request — closing the gap where a client connecting
    # directly to the named pipe (bypassing the MCP server's own filter) could
    # reach any handler. Pass the raw config values; configure_tool_policy
    # tolerates a bare string ("config set" writes scalars, so a TOML-array key
    # set via the setter lands as e.g. "shell,registry" not a list) and an empty
    # include+exclude = no policy = all tools reachable. Runs after all
    # @handler registrations (this module imports router, whose bottom imports
    # the handlers package, so HANDLERS is fully populated by the time we get
    # here).
    from agent_windows.daemon.router import configure_tool_policy
    tools_cfg = config.get("tools", {}) or {}
    configure_tool_policy(tools_cfg.get("include"), tools_cfg.get("exclude"))

    # Write version before PID: launchers (native + Python) treat a live PID
    # whose version file is absent as a stale/mismatched daemon and will kill
    # it. Writing version first closes that startup window.
    #
    # NOTE: the actual write is deferred until _main_pipe_loop successfully
    # creates the named pipe (see _write_state_files / the `first_iter` flag in
    # _main_pipe_loop). Writing here — before create_pipe_server — would let a
    # losing daemon (one that raced another for the single-instance pipe and is
    # about to hit FIRST_PIPE_INSTANCE) overwrite the winner's PID file. By
    # writing only after the pipe is held, we guarantee the PID file always
    # names the process that actually owns the pipe. cleanup_stale_daemon above
    # already cleared any stale file, and the spawn-wait loops key on pipe
    # existence (not the PID file), so the deferral is safe.

    # Clean old screenshots
    try:
        from agent_windows.core.screenshot import clean_old_screenshots
        deleted = clean_old_screenshots()
        if deleted:
            logger.info("Cleaned %d old screenshots", deleted)
    except Exception as e:
        logger.debug("Screenshot cleanup skipped: %s", e)

    return {
        "pid_path": pid_path,
        "version_path": version_path,
        "idle_timeout": idle_timeout,
        "shutdown_event": threading.Event(),
        "sta_thread": COMWorkerThread(),
        "idle_monitor": None,  # created in _start_workers after shutdown_callback is bound
    }


def _start_workers(state):
    """Start STA thread and idle monitor thread.

    ``state`` is the dict returned by :func:`_init_daemon_state`.
    Adds ``request_shutdown`` callback and sets ``idle_monitor``.
    Returns False if STA worker fails to initialize (caller should exit).
    """
    shutdown_event = state["shutdown_event"]

    def request_shutdown():
        logger.info("Shutdown requested")
        shutdown_event.set()

    # Start COM STA worker thread
    sta_thread = state["sta_thread"]
    sta_thread.start()
    try:
        sta_thread.wait_ready(timeout=15.0)
    except TimeoutError:
        logger.error("COM STA worker failed to initialize, exiting")
        return False

    # Start idle monitor
    idle_monitor = IdleMonitorThread(
        timeout_minutes=state["idle_timeout"],
        shutdown_callback=request_shutdown,
    )
    idle_monitor.start()
    state["idle_monitor"] = idle_monitor
    state["request_shutdown"] = request_shutdown

    # PID/version file write is deferred to _main_pipe_loop (after the pipe is
    # successfully created), so a losing daemon can't overwrite the winner's
    # state files. See the note in _init_daemon_state.

    logger.info("Daemon ready, listening on %s", PIPE_NAME)

    return True


def _main_pipe_loop(state):
    """Main Named Pipe listen loop.

    Blocks until ``shutdown_event`` is set or KeyboardInterrupt is received.

    Processes one client connection / one command at a time: after a client
    connects, this thread blocks in ``sta_thread.submit()`` until the command
    finishes before accepting the next connection. Combined with the
    single-instance pipe and the one-at-a-time STA worker, the daemon is fully
    serial by design (see ``COMWorkerThread`` for why). (H-06)
    """
    shutdown_event = state["shutdown_event"]
    sta_thread = state["sta_thread"]
    idle_monitor = state["idle_monitor"]
    request_shutdown = state["request_shutdown"]

    # Write PID/version files only after the pipe is successfully held, and
    # only once. A losing daemon (FIRST_PIPE_INSTANCE failure) exits before
    # this runs, so it can never overwrite the winner's state files.
    state_files_written = False

    try:
        while not shutdown_event.is_set():
            pipe = None
            overlapped_event = None
            request = None
            try:
                pipe = create_pipe_server()

                # First successful pipe creation → we won the single-instance
                # race. Now it's safe to publish our PID/version. Deferred from
                # _init_daemon_state so losers can't overwrite these.
                if not state_files_written:
                    write_version_file(state["version_path"], __version__)
                    write_pid_file(state["pid_path"])
                    state_files_written = True

                # Use overlapped I/O so ConnectNamedPipe can be interrupted on shutdown
                overlapped = pywintypes.OVERLAPPED()
                overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
                overlapped_event = overlapped.hEvent

                try:
                    win32pipe.ConnectNamedPipe(pipe, overlapped)

                    # Wait for client connection or shutdown, checking every 3 seconds
                    connect_timeout_ms = 3000
                    while not shutdown_event.is_set():
                        result = win32event.WaitForSingleObject(overlapped.hEvent, connect_timeout_ms)
                        if result == win32event.WAIT_OBJECT_0:
                            break  # Client connected
                        # Timeout — loop back to check shutdown_event
                    else:
                        # shutdown triggered, clean up and exit
                        disconnect_pipe(pipe)
                        break

                except pywintypes.error as e:
                    if e.winerror == 5:  # ERROR_ACCESS_DENIED
                        logger.warning("Pipe connect error, recreating")
                        disconnect_pipe(pipe)
                        continue
                    raise
                finally:
                    if overlapped_event is not None:
                        win32api.CloseHandle(overlapped_event)

                # Client connected, read request. read_pipe is bounded by
                # SERVER_READ_TIMEOUT_S so a client that connects but never
                # finishes sending cannot wedge the single-client daemon; on
                # timeout it raises PipeReadTimeout, handled below to send an
                # error reply before disconnecting. (SEC3-06)
                try:
                    request_str = read_pipe(pipe)
                except PipeReadTimeout as e:
                    # Best-effort -32603 so the client learns why the connection
                    # dropped (instead of blocking until its own read timeout).
                    # The write may itself fail if the client already left —
                    # swallow that; the disconnect is the real cleanup.
                    logger.warning("Client read timed out, disconnecting: %s", e)
                    try:
                        error_resp = make_error(
                            -32603, f"Request read timed out: {e}")
                        write_pipe(pipe, error_resp.to_json(),
                                   timeout=SERVER_WRITE_TIMEOUT_S)
                    except Exception:
                        logger.debug("Timeout error reply write failed", exc_info=True)
                    disconnect_pipe(pipe)
                    continue
                if not request_str:
                    disconnect_pipe(pipe)
                    continue

                logger.debug("Received: %s", request_str[:200])

                # Parse JSON-RPC request. Distinguish unparseable input
                # (PARSE_ERROR — the bytes weren't valid JSON) from a
                # structurally invalid Request object (INVALID_REQUEST — the
                # JSON parsed but method/params are the wrong type). The former
                # is json.JSONDecodeError, which is a subclass of ValueError, so
                # it MUST be caught first or it would be misfiled as
                # INVALID_REQUEST. (M-08)
                try:
                    request = JSONRPCRequest.from_json(request_str)
                except json.JSONDecodeError as e:
                    error_resp = make_error(PARSE_ERROR, f"Parse error: {e}")
                    write_pipe(pipe, error_resp.to_json(), timeout=SERVER_WRITE_TIMEOUT_S)
                    disconnect_pipe(pipe)
                    continue
                except ValueError as e:
                    error_resp = make_error(INVALID_REQUEST, f"Invalid request: {e}")
                    write_pipe(pipe, error_resp.to_json(), timeout=SERVER_WRITE_TIMEOUT_S)
                    disconnect_pipe(pipe)
                    continue

                # Check for shutdown command
                if request.method == "shutdown":
                    resp = route_request(request)
                    # Act on shutdown BEFORE writing the reply. The launcher's
                    # `daemon stop` uses a fire-and-forget client (send_no_wait:
                    # write the request, close the handle without reading the
                    # response) so it can't block on a busy daemon. With the
                    # client's end already closed, write_pipe below would raise
                    # ERROR_NO_DATA (232) — if request_shutdown() sat after it,
                    # the exception skipped shutdown entirely and the daemon
                    # stayed alive (observed: `daemon stop` always timed out).
                    # Ordering shutdown first honors the documented contract: the
                    # daemon acts on shutdown regardless of whether the client
                    # reads the reply. The reply write/disconnect is best-effort.
                    request_shutdown()
                    try:
                        write_pipe(pipe, resp.to_json(),
                                   timeout=SERVER_WRITE_TIMEOUT_S)
                        disconnect_pipe(pipe)
                    except Exception:
                        logger.debug(
                            "Shutdown reply write failed (client may have closed first)",
                            exc_info=True,
                        )
                    break

                # Submit to STA thread and wait for result. The timeout is
                # advisory (see STA_SUBMIT_TIMEOUT_S): on expiry it reports
                # WAIT_TIMEOUT and disconnects but does not cancel the worker.
                idle_monitor.touch()
                response_str = sta_thread.submit(request, timeout=STA_SUBMIT_TIMEOUT_S)
                write_pipe(pipe, response_str, timeout=SERVER_WRITE_TIMEOUT_S)
                idle_monitor.touch()

                disconnect_pipe(pipe)

            except DaemonStartupError as e:
                # Security failure is fatal — must NOT be swallowed by the broad
                # handler below and retried (that busy-spins). Abort the daemon.
                logger.critical("Cannot start daemon securely — aborting: %s", e)
                request_shutdown()
                break
            except TimeoutError as e:
                logger.error("Command timeout: %s", e)
                if pipe:
                    try:
                        error_resp = make_error(-1003, str(e))
                        write_pipe(pipe, error_resp.to_json(),
                                   timeout=SERVER_WRITE_TIMEOUT_S)
                    except Exception:
                        pass
                    disconnect_pipe(pipe)
            except Exception as e:
                # Unhandled error in the connection lifecycle (e.g. write_pipe
                # failed, an unexpected submit exception). logger.exception gives
                # the full traceback for an unanticipated failure (vs. the info/
                # debug levels on the typed branches above).
                logger.exception("Error in main loop: %s", e)
                if pipe:
                    # Best-effort -32603 so the client learns the connection is
                    # dropping instead of blocking until its own read timeout.
                    # The write may itself fail (client already left, pipe torn
                    # down) — swallow that; the disconnect is the real cleanup.
                    try:
                        error_resp = make_error(-32603, str(e), request and request.id)
                        write_pipe(pipe, error_resp.to_json(),
                                   timeout=SERVER_WRITE_TIMEOUT_S)
                    except Exception:
                        logger.debug("Generic-error reply write failed", exc_info=True)
                    try:
                        disconnect_pipe(pipe)
                    except Exception:
                        pass

    except KeyboardInterrupt:
        logger.info("Interrupted, shutting down")


def _shutdown_daemon(state):
    """Gracefully stop all threads and clean up the PID file."""
    sta_thread = state["sta_thread"]
    idle_monitor = state["idle_monitor"]
    pid_path = state["pid_path"]

    logger.info("Daemon shutting down...")
    sta_thread.stop()
    sta_thread.join(timeout=5.0)
    idle_monitor.stop()
    idle_monitor.join(timeout=2.0)

    # Release the cached capture-backend singletons now that the STA worker
    # (the only capture thread) has stopped — prompt, ordered cleanup rather
    # than waiting for the atexit safety net (which doesn't run on os._exit or
    # fatal signals). Idempotent, so the atexit re-call at teardown is a no-op.
    # (L-29)
    try:
        from agent_windows.core.screenshot import release_capture_backends
        release_capture_backends()
    except Exception:
        logger.debug("Capture-backend release on shutdown failed", exc_info=True)

    # Remove PID file
    pid_path.unlink(missing_ok=True)
    logger.info("Daemon stopped")


def _detach_console() -> None:
    """Detach the daemon from its hosting console so no conhost window stays open.

    The daemon is spawned with ``DETACHED_PROCESS``, but a venv ``python.exe``
    is a trampoline that re-execs the real interpreter via its own
    ``CreateProcess`` call — that re-exec does not propagate the detach flags
    (and ``CREATE_NO_WINDOW`` is silently ignored whenever ``DETACHED_PROCESS``
    is set, so adding it to the launcher buys nothing). The real interpreter
    therefore lands without a parent console and Windows attaches a fresh
    ``conhost.exe`` to it, which renders as a persistent visible window titled
    with the venv path. Calling ``FreeConsole`` here drops that attachment
    regardless of how the process was launched, closing the window.

    Skipped under ``--debug``: debug mode streams logs to stderr (see
    ``_setup_logging``), which needs the console, and the user is typically
    watching that terminal anyway.
    """
    if "--debug" in sys.argv:
        return
    # Runs before _setup_logging, so the logger has no handlers yet — keep this
    # silent on failure. FreeConsole failing is non-fatal (worst case: a
    # lingering window), and it only fails when there is no console to free,
    # which is itself the desired state.
    try:
        ctypes.windll.kernel32.FreeConsole()
    except Exception:
        pass


def run_daemon() -> None:
    """Daemon entry point -- orchestrates init, worker startup, main loop, and shutdown."""
    _detach_console()
    state = _init_daemon_state()

    if not _start_workers(state):
        return

    try:
        _main_pipe_loop(state)
    finally:
        _shutdown_daemon(state)


def _setup_logging(debug: bool) -> None:
    """Configure logging for the daemon."""
    level = logging.DEBUG if debug else logging.INFO
    log_dir = get_log_dir()

    # File handler
    file_handler = logging.FileHandler(
        log_dir / "daemon.log", encoding="utf-8"
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )

    # Root logger
    root_logger = logging.getLogger("agent_windows")
    root_logger.setLevel(level)
    root_logger.addHandler(file_handler)

    # Also log to stderr if debug
    if debug:
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setLevel(level)
        stderr_handler.setFormatter(
            logging.Formatter("[%(levelname)s] %(name)s: %(message)s")
        )
        root_logger.addHandler(stderr_handler)
