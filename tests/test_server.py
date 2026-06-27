"""Tests for daemon/server.py: COMWorkerThread and IdleMonitorThread.

These are the two threads whose non-Win32 logic (command/result handshake,
activity tracking, timeout→shutdown) was previously untested (per
docs/CODE_REVIEW_REPORT_2.md).

We do NOT start the real COM STA worker's run() (it calls pythoncom + UIA
init). Instead we drive _execute_command directly and exercise submit()'s
handshake against a real queue + events. IdleMonitorThread.run() is driven for
real but with sleep patched out so it doesn't burn wall-clock.
"""

import json
import queue
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.daemon.server import COMWorkerThread, IdleMonitorThread
from agent_windows.daemon.router import HANDLERS
from agent_windows.ipc.protocol import JSONRPCRequest


# ── IdleMonitorThread ────────────────────────────────────────────────


class TestIdleMonitorInit:
    def test_timeout_minutes_converted_to_seconds(self):
        m = IdleMonitorThread(timeout_minutes=5)
        assert m.timeout_seconds == 300

    def test_default_timeout_is_30_minutes(self):
        m = IdleMonitorThread()
        assert m.timeout_seconds == 30 * 60

    def test_fractional_minutes(self):
        m = IdleMonitorThread(timeout_minutes=0.5)
        assert m.timeout_seconds == 30


class TestIdleMonitorTouch:
    def test_touch_updates_last_activity(self):
        m = IdleMonitorThread(timeout_minutes=1)
        old = m._last_activity
        time.sleep(0.01)
        m.touch()
        assert m._last_activity > old

    def test_touch_is_thread_safe(self):
        """Concurrent touches must not raise and must leave a valid timestamp."""
        m = IdleMonitorThread(timeout_minutes=1)
        errors = []

        def pound():
            try:
                for _ in range(200):
                    m.touch()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=pound) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert m._last_activity > 0


class TestIdleMonitorRun:
    def test_triggers_shutdown_callback_on_timeout(self):
        """An idle monitor whose last_activity is already past the timeout fires
        the callback exactly once and then exits run()."""
        called = []
        m = IdleMonitorThread(timeout_minutes=1, shutdown_callback=lambda: called.append(1))
        # Backdate last_activity so idle >= timeout on the first check.
        with m._lock:
            m._last_activity = time.time() - m.timeout_seconds - 1
        with patch.object(time, "sleep"):  # no real 10s waits
            m.run()
        assert called == [1]

    def test_does_not_trigger_before_timeout(self):
        """A recently-touched monitor must not fire the callback."""
        called = []
        m = IdleMonitorThread(timeout_minutes=1, shutdown_callback=lambda: called.append(1))
        m.touch()

        # Stop the loop after ~3 iterations so run() returns.
        iterations = {"n": 0}

        def fake_sleep(_s):
            iterations["n"] += 1
            if iterations["n"] >= 3:
                m.stop()

        with patch.object(time, "sleep", side_effect=fake_sleep):
            m.run()
        assert called == []

    def test_stop_terminates_run(self):
        m = IdleMonitorThread(timeout_minutes=1)
        with patch.object(time, "sleep", side_effect=lambda _: m.stop()):
            m.run()  # should return promptly
        assert m._stop_event.is_set()


# ── COMWorkerThread: _execute_command (no COM needed) ────────────────


class TestExecuteCommand:
    """_execute_command routes a request, stores the JSON response under cmd_id,
    and sets the result event. Driven directly (no run()/COM)."""

    def test_success_stores_response_and_signals(self):
        w = COMWorkerThread()
        req = JSONRPCRequest(id=7, method="ping")
        evt = threading.Event()
        w._result_events[7] = evt
        w._execute_command({"id": 7, "request": req})
        assert evt.is_set()
        stored = w.results[7]["response"]
        assert json.loads(stored)["result"]["status"] == "ok"

    def test_handler_error_stored_as_error_response(self):
        """A handler raising surfaces as a JSON-RPC error (via route_request),
        not an exception out of _execute_command."""
        w = COMWorkerThread()
        # fs_read with no path → require() raises ValueError → INVALID_PARAMS
        req = JSONRPCRequest(id=8, method="fs_read", params={})
        evt = threading.Event()
        w._result_events[8] = evt
        w._execute_command({"id": 8, "request": req})  # must not raise
        assert evt.is_set()
        stored = json.loads(w.results[8]["response"])
        assert "error" in stored
        assert stored["error"]["code"] == -32602  # INVALID_PARAMS

    def test_route_exception_becomes_internal_error(self):
        """If route_request itself raises (not a handler ValueError), the
        catch-all wraps it as make_error(-1999). Patch route_request to raise."""
        w = COMWorkerThread()
        req = JSONRPCRequest(id=9, method="ping")
        evt = threading.Event()
        w._result_events[9] = evt
        with patch("agent_windows.daemon.server.route_request",
                   side_effect=RuntimeError("boom")):
            w._execute_command({"id": 9, "request": req})  # must not raise
        assert evt.is_set()
        stored = json.loads(w.results[9]["response"])
        assert stored["error"]["code"] == -1999
        assert "boom" in stored["error"]["message"]

    def test_signal_event_even_on_failure(self):
        """The finally block must set the event so submit() doesn't hang."""
        w = COMWorkerThread()
        req = JSONRPCRequest(id=10, method="ping")
        evt = threading.Event()
        w._result_events[10] = evt
        with patch("agent_windows.daemon.server.route_request",
                   side_effect=RuntimeError("x")):
            w._execute_command({"id": 10, "request": req})
        assert evt.is_set()

    def test_missing_result_event_does_not_crash(self):
        """If no event was registered for cmd_id, finally skips cleanly."""
        w = COMWorkerThread()
        req = JSONRPCRequest(id=11, method="ping")
        # note: no w._result_events[11] entry
        w._execute_command({"id": 11, "request": req})  # must not raise
        assert 11 in w.results


# ── COMWorkerThread: submit() handshake ──────────────────────────────


class TestSubmitHandshake:
    """submit() puts a command on the queue and waits for _execute_command's
    result. We run a background thread that drains the queue and calls
    _execute_command, simulating the worker loop without COM."""

    def _start_drainer(self, worker):
        """Background thread that mimics the run() command-processing loop:
        pull cmds and dispatch to _execute_command, treating None as stop."""
        def loop():
            while True:
                try:
                    cmd = worker.command_queue.get(timeout=0.5)
                except queue.Empty:
                    if worker._stop_event.is_set():
                        return
                    continue
                if cmd is None:
                    return
                worker._execute_command(cmd)
        t = threading.Thread(target=loop, daemon=True)
        t.start()
        return t

    def test_submit_returns_response_json(self):
        w = COMWorkerThread()
        drainer = self._start_drainer(w)
        try:
            req = JSONRPCRequest(id=1, method="ping")
            resp = w.submit(req, timeout=5.0)
            assert json.loads(resp)["result"]["status"] == "ok"
        finally:
            w.stop()
            drainer.join(timeout=2.0)

    def test_submit_times_out(self):
        """If no drainer consumes the command, submit() raises TimeoutError."""
        w = COMWorkerThread()
        req = JSONRPCRequest(id=2, method="ping")
        with pytest.raises(TimeoutError, match="timed out"):
            w.submit(req, timeout=0.2)

    def test_submit_cleans_up_result_events(self):
        """The finally block pops the per-cmd event so it doesn't leak."""
        w = COMWorkerThread()
        drainer = self._start_drainer(w)
        try:
            w.submit(JSONRPCRequest(id=3, method="ping"), timeout=5.0)
        finally:
            w.stop()
            drainer.join(timeout=2.0)
        assert 3 not in w._result_events
        assert 3 not in w.results

    def test_reentrant_submit_rejected(self):
        """PERF3-01: a second submit with an already-in-flight cmd_id is rejected
        rather than racing on the event slot. The daemon serializes one command
        per id; a re-entrant submit must surface clearly instead of silently
        dropping a completion signal."""
        w = COMWorkerThread()
        # Pre-seed an in-flight event for cmd_id 5 (simulating an active submit).
        import threading as _t
        w._result_events[5] = _t.Event()
        with pytest.raises(RuntimeError, match="already in flight"):
            w.submit(JSONRPCRequest(id=5, method="ping"), timeout=0.1)

    def test_submit_clears_stale_result_from_prior_timeout(self):
        """PERF3-02: a timed-out command leaves a late result in self.results;
        the next submit with the same id must clear it before waiting, so the
        stale response can't be returned for the new request."""
        w = COMWorkerThread()
        # Simulate a leftover result from a previous timed-out command.
        w.results[6] = {"response": '{"jsonrpc":"2.0","id":6,"result":{"stale":true}}'}
        drainer = self._start_drainer(w)
        try:
            resp = w.submit(JSONRPCRequest(id=6, method="ping"), timeout=5.0)
        finally:
            w.stop()
            drainer.join(timeout=2.0)
        # The new ping response must be returned, not the stale one.
        assert json.loads(resp)["result"]["status"] == "ok"
        assert "stale" not in json.loads(resp)["result"]


# ── COMWorkerThread: stop / wait_ready ───────────────────────────────


class TestStopAndWaitReady:
    def test_stop_sets_stop_event(self):
        w = COMWorkerThread()
        assert not w._stop_event.is_set()
        w.stop()
        assert w._stop_event.is_set()

    def test_stop_enqueues_poison_pill(self):
        w = COMWorkerThread()
        w.stop()
        # The None poison pill should be on the queue
        assert w.command_queue.get_nowait() is None

    def test_wait_ready_times_out_if_never_set(self):
        w = COMWorkerThread()
        with pytest.raises(TimeoutError, match="failed to initialize"):
            w.wait_ready(timeout=0.1)

    def test_wait_ready_returns_once_event_set(self):
        w = COMWorkerThread()
        threading.Thread(target=lambda: (time.sleep(0.05), w._ready.set()), daemon=True).start()
        w.wait_ready(timeout=2.0)  # must not raise


# ── _main_pipe_loop: request parsing & shutdown ──────────────────────


class TestMainPipeLoop:
    """The loop's non-I/O logic: parse-error vs invalid-request distinction,
    shutdown command handling, command dispatch to the STA worker. The pipe
    I/O + pywin32 overlapped machinery is patched out."""

    def _make_state(self, shutdown_event=None):
        """Build a minimal state dict with mocked threads."""
        shutdown_event = shutdown_event or threading.Event()
        return {
            "shutdown_event": shutdown_event,
            "sta_thread": MagicMock(),
            "idle_monitor": MagicMock(),
            "request_shutdown": MagicMock(side_effect=shutdown_event.set),
            "pid_path": MagicMock(),
            "version_path": MagicMock(),
        }

    def _run_one_request(self, request_str, *, state):
        """Patch the pipe/pywin32 boundary, feed ``request_str`` exactly once
        (subsequent reads return '' so the loop's `continue` paths don't
        re-process the same bad input forever), then set shutdown_event on the
        next create_pipe_server call so the outer loop exits.

        Returns the list of messages written to the pipe.
        """
        from agent_windows.daemon import server as srv

        written = []
        reads = {"n": 0}

        def fake_read(_pipe):
            reads["n"] += 1
            return request_str if reads["n"] == 1 else ""

        creates = {"n": 0}

        def fake_create():
            creates["n"] += 1
            # 1st create → process the one request. 2nd create → stop the loop.
            if creates["n"] >= 2:
                state["shutdown_event"].set()
            return "fake_pipe"

        with patch.object(srv, "create_pipe_server", side_effect=fake_create), \
             patch.object(srv.win32pipe, "ConnectNamedPipe"), \
             patch.object(srv.win32event, "CreateEvent", return_value="evt"), \
             patch.object(srv.win32event, "WaitForSingleObject", return_value=0), \
             patch.object(srv.win32api, "CloseHandle"), \
             patch.object(srv, "read_pipe", side_effect=fake_read), \
             patch.object(srv, "disconnect_pipe"), \
             patch.object(srv, "write_pipe", side_effect=lambda _p, msg, **kw: written.append(msg)), \
             patch.object(srv, "write_version_file"), \
             patch.object(srv, "write_pid_file"), \
             patch.object(srv.pywintypes, "OVERLAPPED", return_value=MagicMock(hEvent="evt")):
            srv._main_pipe_loop(state)
        return written

    def test_parse_error_for_bad_json(self):
        """M-08: unparseable bytes → PARSE_ERROR (-32700), not INVALID_REQUEST."""
        state = self._make_state()
        written = self._run_one_request("not valid json {{{", state=state)
        assert written, "loop should have written a parse-error response"
        resp = json.loads(written[-1])
        assert resp["error"]["code"] == -32700  # PARSE_ERROR

    def test_invalid_request_for_wrong_types(self):
        """Valid JSON but wrong request structure → INVALID_REQUEST (-32600)."""
        state = self._make_state()
        # method must be a string; a number makes JSONRPCRequest raise ValueError
        written = self._run_one_request(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": 5}), state=state)
        resp = json.loads(written[-1])
        assert resp["error"]["code"] == -32600  # INVALID_REQUEST

    def test_shutdown_command_triggers_request_shutdown(self):
        """A 'shutdown' method routes the response, calls request_shutdown, and
        breaks the loop. (request_shutdown is invoked BEFORE the reply write so
        a fire-and-forget client that closes before reading — e.g. the
        launcher's `daemon stop` — can't skip shutdown via a write failure.)"""
        state = self._make_state()
        written = self._run_one_request(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "shutdown"}), state=state)
        resp = json.loads(written[-1])
        assert resp["result"]["status"] == "ok"
        assert resp["result"]["action"] == "shutting_down"
        assert state["shutdown_event"].is_set()

    def test_shutdown_triggers_request_shutdown_even_if_write_fails(self):
        """The launcher's `daemon stop` uses a fire-and-forget client (write the
        shutdown request, close the handle without reading the reply). With the
        client end already closed, write_pipe raises ERROR_NO_DATA (232).
        request_shutdown() must still fire — otherwise the daemon never stops
        and `daemon stop` always reports a graceful-shutdown timeout.

        Regression for the bug where request_shutdown sat AFTER write_pipe in
        the shutdown branch: the raised exception skipped it entirely and the
        outer `except Exception` just logged + continued the loop.
        """
        from agent_windows.daemon import server as srv

        state = self._make_state()
        # Drive one shutdown request, then stop the loop on the next create.
        request_str = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "shutdown"})
        reads = {"n": 0}

        def fake_read(_pipe):
            reads["n"] += 1
            return request_str if reads["n"] == 1 else ""

        creates = {"n": 0}

        def fake_create():
            creates["n"] += 1
            if creates["n"] >= 2:
                state["shutdown_event"].set()
            return "fake_pipe"

        def write_raises(_pipe, _msg):
            raise OSError(232, "ERROR_NO_DATA: pipe is closing")

        with patch.object(srv, "create_pipe_server", side_effect=fake_create), \
             patch.object(srv.win32pipe, "ConnectNamedPipe"), \
             patch.object(srv.win32event, "CreateEvent", return_value="evt"), \
             patch.object(srv.win32event, "WaitForSingleObject", return_value=0), \
             patch.object(srv.win32api, "CloseHandle"), \
             patch.object(srv, "read_pipe", side_effect=fake_read), \
             patch.object(srv, "disconnect_pipe"), \
             patch.object(srv, "write_pipe", side_effect=write_raises), \
             patch.object(srv, "write_version_file"), \
             patch.object(srv, "write_pid_file"), \
             patch.object(srv.pywintypes, "OVERLAPPED", return_value=MagicMock(hEvent="evt")):
            srv._main_pipe_loop(state)

        # The real contract under test: shutdown fired despite the write failure.
        assert state["shutdown_event"].is_set(), \
            "request_shutdown must fire even when the shutdown reply write fails"

    def test_normal_command_dispatched_to_sta_worker(self):
        """A normal command is submitted to sta_thread.submit and the response
        written back; idle_monitor is touched."""
        state = self._make_state()
        state["sta_thread"].submit.return_value = '{"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}'
        written = self._run_one_request(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}), state=state)
        state["sta_thread"].submit.assert_called_once()
        assert json.loads(written[-1])["result"] == {"ok": 1}
        # idle monitor touched before + after the command
        assert state["idle_monitor"].touch.call_count == 2

    def test_empty_request_skipped(self):
        """read_pipe returning '' → disconnect + continue, no submit."""
        state = self._make_state()
        written = self._run_one_request("", state=state)
        state["sta_thread"].submit.assert_not_called()
        assert written == []

    def test_daemon_startup_error_aborts_loop(self):
        """M-02: a security/startup failure from create_pipe_server (e.g. secure-
        pipe refusal) must NOT be swallowed by the broad except handler and
        busy-spin — it must request_shutdown and break the loop."""
        from agent_windows.daemon import server as srv
        from agent_windows.ipc.pipe import DaemonStartupError

        state = self._make_state()
        with patch.object(srv, "create_pipe_server",
                          side_effect=DaemonStartupError("secure pipe refused")), \
             patch.object(srv, "read_pipe"), \
             patch.object(srv, "disconnect_pipe"), \
             patch.object(srv, "write_pipe"):
            srv._main_pipe_loop(state)
        # request_shutdown was invoked (sets the shutdown_event) and loop broke
        assert state["shutdown_event"].is_set()

    def test_loser_daemon_never_writes_pid_file(self):
        """A losing daemon (FIRST_PIPE_INSTANCE → DaemonStartupError on first
        create_pipe_server) must NOT write the PID/version files. The deferral
        to after first successful pipe creation guarantees the PID file always
        names the pipe owner — a loser that overwrote it would point
        _daemon_process_alive at a process that's about to exit, wedging the
        system. This locks the state_files_written guard in _main_pipe_loop."""
        from agent_windows.daemon import server as srv
        from agent_windows.ipc.pipe import DaemonStartupError

        state = self._make_state()
        pid_writes = []
        version_writes = []
        with patch.object(srv, "create_pipe_server",
                          side_effect=DaemonStartupError("duplicate instance")), \
             patch.object(srv, "read_pipe"), \
             patch.object(srv, "disconnect_pipe"), \
             patch.object(srv, "write_pipe"), \
             patch.object(srv, "write_pid_file",
                          side_effect=lambda *a: pid_writes.append(1)), \
             patch.object(srv, "write_version_file",
                          side_effect=lambda *a: version_writes.append(1)):
            srv._main_pipe_loop(state)
        assert pid_writes == [], "loser wrote the PID file (would overwrite winner's)"
        assert version_writes == [], "loser wrote the version file"
        assert state["shutdown_event"].is_set()

    def test_winner_writes_pid_file_exactly_once(self):
        """The winning daemon writes PID/version files exactly once — after the
        first successful create_pipe_server. Across multiple client connections
        (multiple loop iterations), the files must not be re-written. Locks the
        state_files_written once-only guard in _main_pipe_loop."""
        from agent_windows.daemon import server as srv

        state = self._make_state()
        pid_writes = []
        version_writes = []
        # Feed two requests then stop, so the loop iterates at least twice
        # past the first create_pipe_server.
        reads = {"n": 0}

        def fake_read(_pipe):
            reads["n"] += 1
            # Two real requests, then '' to end.
            if reads["n"] <= 2:
                return json.dumps({"jsonrpc": "2.0", "id": reads["n"], "method": "ping"})
            return ""

        creates = {"n": 0}

        def fake_create():
            creates["n"] += 1
            if creates["n"] >= 4:  # let it serve both, then stop
                state["shutdown_event"].set()
            return "fake_pipe"

        with patch.object(srv, "create_pipe_server", side_effect=fake_create), \
             patch.object(srv.win32pipe, "ConnectNamedPipe"), \
             patch.object(srv.win32event, "CreateEvent", return_value="evt"), \
             patch.object(srv.win32event, "WaitForSingleObject", return_value=0), \
             patch.object(srv.win32api, "CloseHandle"), \
             patch.object(srv, "read_pipe", side_effect=fake_read), \
             patch.object(srv, "disconnect_pipe"), \
             patch.object(srv, "write_pipe"), \
             patch.object(srv, "write_pid_file",
                          side_effect=lambda *a: pid_writes.append(1)), \
             patch.object(srv, "write_version_file",
                          side_effect=lambda *a: version_writes.append(1)), \
             patch.object(srv.pywintypes, "OVERLAPPED",
                          return_value=MagicMock(hEvent="evt")):
            srv._main_pipe_loop(state)

        assert len(pid_writes) == 1, (
            f"PID file written {len(pid_writes)} times — must be exactly once "
            "(state_files_written guard is broken)"
        )
        assert len(version_writes) == 1

    def test_command_timeout_writes_error_and_disconnects(self):
        """A TimeoutError from sta_thread.submit → write a -1003 (WAIT_TIMEOUT)
        error response, then disconnect the pipe (no busy-spin)."""
        from agent_windows.daemon import server as srv

        state = self._make_state()
        state["sta_thread"].submit.side_effect = TimeoutError("stalled")

        written = []
        reads = {"n": 0}

        def fake_read(_pipe):
            reads["n"] += 1
            return json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}) if reads["n"] == 1 else ""

        creates = {"n": 0}

        def fake_create():
            creates["n"] += 1
            if creates["n"] >= 2:
                state["shutdown_event"].set()
            return "fake_pipe"

        with patch.object(srv, "create_pipe_server", side_effect=fake_create), \
             patch.object(srv.win32pipe, "ConnectNamedPipe"), \
             patch.object(srv.win32event, "CreateEvent", return_value="evt"), \
             patch.object(srv.win32event, "WaitForSingleObject", return_value=0), \
             patch.object(srv.win32api, "CloseHandle"), \
             patch.object(srv, "read_pipe", side_effect=fake_read), \
             patch.object(srv, "disconnect_pipe") as mock_disc, \
             patch.object(srv, "write_pipe", side_effect=lambda _p, m, **kw: written.append(m)), \
             patch.object(srv.pywintypes, "OVERLAPPED", return_value=MagicMock(hEvent="evt")):
            srv._main_pipe_loop(state)
        # a -1003 WAIT_TIMEOUT error was written
        assert any(json.loads(m).get("error", {}).get("code") == -1003 for m in written)
        # pipe was disconnected (cleanup), loop continued and exited cleanly
        assert mock_disc.called

    def test_read_timeout_writes_error_and_disconnects(self):
        """SEC3-06: a PipeReadTimeout from read_pipe (client connected but never
        finished sending) → write a -32603 error response, then disconnect —
        NOT swallow it as a generic loop error that drops the client silently.
        The daemon must stay alive (loop continues to the next connection)."""
        from agent_windows.daemon import server as srv
        from agent_windows.ipc.pipe import PipeReadTimeout

        state = self._make_state()
        written = []
        creates = {"n": 0}

        def fake_create():
            creates["n"] += 1
            if creates["n"] >= 2:
                state["shutdown_event"].set()
            return "fake_pipe"

        with patch.object(srv, "create_pipe_server", side_effect=fake_create), \
             patch.object(srv.win32pipe, "ConnectNamedPipe"), \
             patch.object(srv.win32event, "CreateEvent", return_value="evt"), \
             patch.object(srv.win32event, "WaitForSingleObject", return_value=0), \
             patch.object(srv.win32api, "CloseHandle"), \
             patch.object(srv, "read_pipe",
                          side_effect=PipeReadTimeout("Timed out 30000ms")), \
             patch.object(srv, "disconnect_pipe") as mock_disc, \
             patch.object(srv, "write_pipe",
                          side_effect=lambda _p, m, **kw: written.append(m)), \
             patch.object(srv.pywintypes, "OVERLAPPED",
                          return_value=MagicMock(hEvent="evt")):
            srv._main_pipe_loop(state)
        # a -32603 internal-error response was written for the timeout
        assert any(json.loads(m).get("error", {}).get("code") == -32603
                   for m in written)
        # the stuck client's pipe was disconnected
        assert mock_disc.called
        # the command was never submitted (read failed before parse/submit)
        state["sta_thread"].submit.assert_not_called()

    def test_generic_loop_error_writes_response_before_disconnect(self):
        """An unexpected (non-typed) Exception in the connection lifecycle —
        e.g. sta_thread.submit raising something other than TimeoutError — must
        write a -32603 error response (carrying the request id) before
        disconnecting, so the client doesn't block until its own 150s read
        timeout. Previously this branch logged + disconnected with no reply."""
        from agent_windows.daemon import server as srv

        state = self._make_state()
        # A generic (non-Timeout) exception escapes submit → falls through the
        # TimeoutError clause to the broad except Exception.
        state["sta_thread"].submit.side_effect = RuntimeError(" STA worker blew up")

        written = []
        reads = {"n": 0}

        def fake_read(_pipe):
            reads["n"] += 1
            return json.dumps({"jsonrpc": "2.0", "id": 42, "method": "ping"}) if reads["n"] == 1 else ""

        creates = {"n": 0}

        def fake_create():
            creates["n"] += 1
            if creates["n"] >= 2:
                state["shutdown_event"].set()
            return "fake_pipe"

        with patch.object(srv, "create_pipe_server", side_effect=fake_create), \
             patch.object(srv.win32pipe, "ConnectNamedPipe"), \
             patch.object(srv.win32event, "CreateEvent", return_value="evt"), \
             patch.object(srv.win32event, "WaitForSingleObject", return_value=0), \
             patch.object(srv.win32api, "CloseHandle"), \
             patch.object(srv, "read_pipe", side_effect=fake_read), \
             patch.object(srv, "disconnect_pipe") as mock_disc, \
             patch.object(srv, "write_pipe",
                          side_effect=lambda _p, m, **kw: written.append(m)), \
             patch.object(srv.pywintypes, "OVERLAPPED",
                          return_value=MagicMock(hEvent="evt")):
            srv._main_pipe_loop(state)
        # a -32603 internal-error response was written...
        errors = [json.loads(m) for m in written
                  if json.loads(m).get("error", {}).get("code") == -32603]
        assert errors, "generic loop error wrote no -32603 response"
        # ...and it carried the request id so the client can correlate it
        assert errors[0]["id"] == 42
        # the pipe was disconnected (cleanup)
        assert mock_disc.called
