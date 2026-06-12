"""Daemon main loop: Named Pipe listener with 3-thread model.

Threads:
1. I/O thread (main): listens on Named Pipe, receives JSON-RPC requests
2. COM STA worker thread: executes all UIA/COM operations
3. Idle monitor thread: watches for inactivity timeout

Usage: python -m agent_windows --daemon [--debug]
"""

import logging
import os
import queue
import signal
import sys
import threading
import time
import ctypes

import pywintypes
import win32api
import win32event
import win32pipe

from agent_windows import __version__
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
    wait_for_client,
    read_pipe,
    write_pipe,
    disconnect_pipe,
)
from agent_windows.utils.config import get_pid_path as cfg_pid_path, get_version_path as cfg_version_path, get_log_dir, load_config

logger = logging.getLogger("agent_windows.daemon")

# ── COM STA Worker Thread ─────────────────────────────────────────


class COMWorkerThread(threading.Thread):
    """Dedicated STA thread for all COM/UIA operations.

    All UIA calls happen here. Commands are received via queue,
    results are placed back keyed by command ID.
    """

    def __init__(self):
        super().__init__(daemon=True)
        self.command_queue: queue.Queue[dict] = queue.Queue()
        self.results: dict[str | int | None, dict] = {}
        self._results_lock = threading.Lock()
        self._ready = threading.Event()
        self._stop_event = threading.Event()
        self._result_events: dict[str | int | None, threading.Event] = {}

    def run(self):
        """Thread entry point: initialize COM STA and process commands."""
        import pythoncom

        pythoncom.CoInitialize()
        logger.info("COM STA worker thread initialized")

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
                pass

            try:
                cmd = self.command_queue.get(timeout=0.05)
                if cmd is None:
                    # Shutdown signal
                    break
                self._execute_command(cmd)
            except queue.Empty:
                continue

        pythoncom.CoUninitialize()
        logger.info("COM STA worker thread stopped")

    def _execute_command(self, cmd: dict):
        """Execute a single command and store the result."""
        cmd_id = cmd.get("id")
        request = cmd.get("request")

        try:
            # Route to the handler (handlers are registered in router.py)
            response = route_request(request)
            with self._results_lock:
                self.results[cmd_id] = {"response": response.to_json()}
        except Exception as e:
            error_resp = make_error(-1999, str(e), cmd_id)
            with self._results_lock:
                self.results[cmd_id] = {"response": error_resp.to_json()}
        finally:
            # Signal completion
            evt = self._result_events.get(cmd_id)
            if evt:
                evt.set()

    def submit(self, request: JSONRPCRequest, timeout: float = 30.0) -> str:
        """Submit a request to the worker thread and wait for the result.

        Returns the JSON response string.
        Raises TimeoutError if the command doesn't complete in time.
        """
        cmd_id = request.id
        event = threading.Event()
        self._result_events[cmd_id] = event

        try:
            # Clear previous result
            with self._results_lock:
                self.results.pop(cmd_id, None)

            self.command_queue.put({"id": cmd_id, "request": request})

            # Wait for result
            if not event.wait(timeout=timeout):
                raise TimeoutError(f"Command {request.method} timed out after {timeout}s")

            with self._results_lock:
                result = self.results.pop(cmd_id, None)

            if result is None:
                raise RuntimeError(f"No result for command {request.method}")

            return result["response"]
        finally:
            self._result_events.pop(cmd_id, None)

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


def _init_daemon_state():
    """Parse paths, load config, write PID/version files.

    Returns a dict with keys: pid_path, version_path, idle_timeout,
    shutdown_event, sta_thread, idle_monitor.
    """
    debug = "--debug" in sys.argv
    _setup_logging(debug)

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

    # Write PID and version files
    write_pid_file(pid_path)
    write_version_file(version_path, __version__)

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

    # Re-write PID/version (after threads confirmed healthy)
    write_pid_file(state["pid_path"])
    write_version_file(state["version_path"], __version__)

    logger.info("Daemon ready, listening on %s", PIPE_NAME)

    # Register command handlers from core modules
    _register_core_handlers()

    return True


def _main_pipe_loop(state):
    """Main Named Pipe listen loop.

    Blocks until ``shutdown_event`` is set or KeyboardInterrupt is received.
    """
    shutdown_event = state["shutdown_event"]
    sta_thread = state["sta_thread"]
    idle_monitor = state["idle_monitor"]
    request_shutdown = state["request_shutdown"]

    try:
        while not shutdown_event.is_set():
            pipe = None
            overlapped_event = None
            try:
                pipe = create_pipe_server()

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

                # Client connected, read request
                request_str = read_pipe(pipe)
                if not request_str:
                    disconnect_pipe(pipe)
                    continue

                logger.debug("Received: %s", request_str[:200])

                # Parse JSON-RPC request
                try:
                    request = JSONRPCRequest.from_json(request_str)
                except Exception as e:
                    error_resp = make_error(PARSE_ERROR, f"Parse error: {e}")
                    write_pipe(pipe, error_resp.to_json())
                    disconnect_pipe(pipe)
                    continue

                # Check for shutdown command
                if request.method == "shutdown":
                    resp = route_request(request)
                    write_pipe(pipe, resp.to_json())
                    disconnect_pipe(pipe)
                    request_shutdown()
                    break

                # Submit to STA thread and wait for result
                idle_monitor.touch()
                response_str = sta_thread.submit(request, timeout=60.0)
                write_pipe(pipe, response_str)
                idle_monitor.touch()

                disconnect_pipe(pipe)

            except TimeoutError as e:
                logger.error("Command timeout: %s", e)
                if pipe:
                    try:
                        error_resp = make_error(-1003, str(e))
                        write_pipe(pipe, error_resp.to_json())
                    except Exception:
                        pass
                    disconnect_pipe(pipe)
            except Exception as e:
                logger.error("Error in main loop: %s", e)
                if pipe:
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

    # Remove PID file
    pid_path.unlink(missing_ok=True)
    logger.info("Daemon stopped")


def run_daemon() -> None:
    """Daemon entry point -- orchestrates init, worker startup, main loop, and shutdown."""
    state = _init_daemon_state()

    if not _start_workers(state):
        return

    try:
        _main_pipe_loop(state)
    finally:
        _shutdown_daemon(state)


def _register_core_handlers():
    """Import and register command handlers from core modules.

    Handlers use @handler("method") decorator to register themselves.
    We import modules here to trigger registration, but only after the
    STA thread is ready (to ensure COM calls happen on the right thread).
    """
    # Core handlers will be registered when their modules are imported
    # Import them here so registration happens at daemon startup
    try:
        # These imports will grow as we implement phases
        pass
    except Exception as e:
        logger.warning("Failed to register some core handlers: %s", e)


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
