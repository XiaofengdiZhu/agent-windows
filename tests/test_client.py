"""Tests for the IPC client (call_daemon, unwrap_response)."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.ipc.client import unwrap_response, call_daemon, _start_daemon_process
from agent_windows.ipc.protocol import JSONRPCRequest, JSONRPCResponse


class TestUnwrapResponse:
    """M-11: unwrap_response is the single error→exception mapping shared by the
    daemon path (call_daemon) and the --no-daemon path (dispatch). Test it
    directly so the 'identical errors for both paths' guarantee doesn't rest
    only on indirect integration coverage through dispatch."""

    def test_error_raises_runtime_error_with_code(self):
        resp = JSONRPCResponse(id=1, error={"code": -32602, "message": "bad params"})
        with pytest.raises(RuntimeError, match="-32602") as exc_info:
            unwrap_response(resp)
        assert "bad params" in str(exc_info.value)

    def test_success_returns_result(self):
        resp = JSONRPCResponse(id=1, result={"status": "ok"})
        assert unwrap_response(resp) == {"status": "ok"}


class TestEnsureDaemonRunning:
    """M-31: ensure_daemon_running fails fast when the spawned daemon exits
    during startup, instead of hanging the full retry window on a broken
    install."""

    def test_pipe_already_exists_does_not_spawn(self, monkeypatch):
        import agent_windows.ipc.client as client

        spawned = []
        monkeypatch.setattr(client, "pipe_exists", lambda: True)
        monkeypatch.setattr(
            client, "_start_daemon_process", lambda debug=False: spawned.append(1)
        )
        client.ensure_daemon_running()  # must not raise, must not spawn
        assert spawned == []

    def test_fail_fast_when_daemon_exits_during_startup(self, monkeypatch):
        import agent_windows.ipc.client as client

        pipe_calls = {"n": 0}

        def fake_pipe():
            pipe_calls["n"] += 1
            return False

        monkeypatch.setattr(client, "pipe_exists", fake_pipe)
        # Daemon "crashes" immediately: poll() returns an exit code.
        monkeypatch.setattr(
            client,
            "_start_daemon_process",
            lambda debug=False: SimpleNamespace(poll=lambda: 42),
        )
        monkeypatch.setattr(client.time, "sleep", lambda _: None)
        # DaemonSpawnLock is also acquired inside ensure_daemon_running (around
        # the spawn). Use a no-op stub so the test doesn't touch the real lock
        # file — we're testing the fail-fast path, not the lock.
        from contextlib import nullcontext
        monkeypatch.setattr(
            "agent_windows.ipc.lock.DaemonSpawnLock", lambda *a, **k: nullcontext()
        )
        with pytest.raises(ConnectionError, match="code 42"):
            client.ensure_daemon_running()
        # Fail-fast: top fast-path check + one in-lock double-check + one loop
        # iteration (where poll detected the exit) — NOT the full 25-iteration
        # window. The in-lock check is new (spawn serialization); it doesn't
        # change the fail-fast guarantee.
        assert pipe_calls["n"] == 3

    def test_ready_after_spawn_when_daemon_alive(self, monkeypatch):
        import agent_windows.ipc.client as client

        # Top fast-path False (so we spawn), then one in-lock double-check
        # (also False — daemon starting), then False once in the loop (daemon
        # alive but the pipe isn't up yet), then True — exercises the loop body
        # and confirms the alive poll() (None) does NOT trigger fail-fast.
        seq = [False, False, False, True]
        monkeypatch.setattr(client, "pipe_exists", lambda: seq.pop(0) if seq else True)
        monkeypatch.setattr(
            client,
            "_start_daemon_process",
            lambda debug=False: SimpleNamespace(poll=lambda: None),
        )
        monkeypatch.setattr(client.time, "sleep", lambda _: None)
        # DaemonSpawnLock stubbed out — see test_fail_fast_when_daemon_exits...
        from contextlib import nullcontext
        monkeypatch.setattr(
            "agent_windows.ipc.lock.DaemonSpawnLock", lambda *a, **k: nullcontext()
        )
        client.ensure_daemon_running()  # must not raise

    def test_full_window_timeout_when_daemon_hangs_alive(self, monkeypatch):
        """Daemon stays alive but the pipe never appears → the original
        full-window timeout still fires (Part A must not have broken it)."""
        import agent_windows.ipc.client as client

        monkeypatch.setattr(client, "pipe_exists", lambda: False)
        monkeypatch.setattr(
            client,
            "_start_daemon_process",
            lambda debug=False: SimpleNamespace(poll=lambda: None),
        )
        monkeypatch.setattr(client.time, "sleep", lambda _: None)
        from contextlib import nullcontext
        monkeypatch.setattr(
            "agent_windows.ipc.lock.DaemonSpawnLock", lambda *a, **k: nullcontext()
        )
        with pytest.raises(ConnectionError, match="did not become ready"):
            client.ensure_daemon_running()

    def test_pipe_gap_with_live_daemon_does_not_spawn(self, monkeypatch):
        """Regression: pipe_exists() is False during the daemon's inter-connection
        pipe-recreate gap, but the daemon process is still alive. Must NOT spawn a
        rival daemon (which would fail to bind the single-instance pipe and leak
        as an orphaned process). Instead return and let send_to_daemon's
        connect-retry handle the transient gap."""
        import agent_windows.ipc.client as client

        spawned = []
        monkeypatch.setattr(client, "pipe_exists", lambda: False)
        monkeypatch.setattr(
            client,
            "_start_daemon_process",
            lambda debug=False: spawned.append(1),
        )
        monkeypatch.setattr(client, "_daemon_process_alive", lambda: True)
        client.ensure_daemon_running()
        assert spawned == [], "spawned a rival daemon while the real one was alive"

    def test_pipe_gap_with_no_daemon_still_spawns(self, monkeypatch):
        """Regression counterpart: when the pipe is absent AND no daemon process
        is alive, ensure_daemon_running must still spawn (the gap check must not
        mask a genuinely dead daemon)."""
        import agent_windows.ipc.client as client

        spawned = []
        monkeypatch.setattr(client, "pipe_exists", lambda: False)
        monkeypatch.setattr(
            client,
            "_start_daemon_process",
            lambda debug=False: SimpleNamespace(poll=lambda: 0),  # exits immediately
        )
        monkeypatch.setattr(client, "_daemon_process_alive", lambda: False)
        monkeypatch.setattr(client.time, "sleep", lambda _: None)
        with pytest.raises(ConnectionError, match="code 0"):
            client.ensure_daemon_running()


class TestCallDaemon:
    """call_daemon orchestrates: build request → ensure_daemon_running →
    send_to_daemon → unwrap_response. Test the plumbing with each stage mocked."""

    def test_success_returns_unwrapped_result(self):
        """A successful round-trip returns the response's result dict."""
        from agent_windows.ipc import client

        ok_json = JSONRPCResponse(id=1, result={"status": "ok"}).to_json()
        with patch.object(client, "ensure_daemon_running") as m_ensure, \
             patch.object(client, "send_to_daemon", return_value=ok_json) as m_send:
            result = call_daemon("ping", params=None)
        m_ensure.assert_called_once_with(debug=False)  # default threads through
        m_send.assert_called_once()
        assert result == {"status": "ok"}

    def test_builds_jsonrpc_request_with_method_and_params(self):
        """The request sent over the wire carries the method + params."""
        from agent_windows.ipc import client

        captured = {}

        def fake_send(message, pipe_name):
            captured["message"] = message
            captured["pipe_name"] = pipe_name
            return JSONRPCResponse(id=1, result={}).to_json()

        with patch.object(client, "ensure_daemon_running"), \
             patch.object(client, "send_to_daemon", side_effect=fake_send):
            call_daemon("screenshot", params={"monitor": 1})
        req = JSONRPCRequest.from_json(captured["message"])
        assert req.method == "screenshot"
        assert req.params == {"monitor": 1}
        # send_to_daemon is called with the module's PIPE_NAME
        from agent_windows.ipc.pipe import PIPE_NAME
        assert captured["pipe_name"] == PIPE_NAME

    def test_daemon_error_response_raises_runtime_error(self):
        """An error response propagates via unwrap_response as RuntimeError."""
        from agent_windows.ipc import client

        err_json = JSONRPCResponse(id=1, error={"code": -32602, "message": "bad"}).to_json()
        with patch.object(client, "ensure_daemon_running"), \
             patch.object(client, "send_to_daemon", return_value=err_json):
            with pytest.raises(RuntimeError, match="-32602"):
                call_daemon("fs_read")

    def test_passes_debug_flag_to_ensure_daemon(self):
        from agent_windows.ipc import client

        ok_json = JSONRPCResponse(id=1, result={}).to_json()
        with patch.object(client, "ensure_daemon_running") as m_ensure, \
             patch.object(client, "send_to_daemon", return_value=ok_json):
            call_daemon("ping", debug=True)
        m_ensure.assert_called_once_with(debug=True)

    def test_connection_error_propagates_from_ensure(self):
        """If ensure_daemon_running can't reach the daemon, it raises and
        call_daemon surfaces it (no send attempted)."""
        from agent_windows.ipc import client

        with patch.object(client, "ensure_daemon_running",
                          side_effect=ConnectionError("no pipe")), \
             patch.object(client, "send_to_daemon") as m_send:
            with pytest.raises(ConnectionError, match="no pipe"):
                call_daemon("ping")
        m_send.assert_not_called()


class TestStartDaemonProcess:
    """_start_daemon_process builds the `[python -m agent_windows --daemon]`
    command, appends --debug when requested, and detaches the subprocess."""

    def test_base_command_uses_module_invocation(self):
        """Must invoke via `python -m agent_windows --daemon`, not a bare exe,
        so sys.executable + -m is used."""
        fake_proc = MagicMock(pid=999)
        with patch("agent_windows.ipc.client.subprocess.Popen", return_value=fake_proc) as m_popen:
            proc = _start_daemon_process()
        assert m_popen.call_args.args[0] == [sys.executable, "-m", "agent_windows", "--daemon"]
        assert proc.pid == 999

    def test_debug_flag_appended(self):
        fake_proc = MagicMock(pid=1)
        with patch("agent_windows.ipc.client.subprocess.Popen", return_value=fake_proc) as m_popen:
            _start_daemon_process(debug=True)
        assert m_popen.call_args.args[0][-1] == "--debug"

    def test_no_debug_does_not_append_flag(self):
        fake_proc = MagicMock(pid=1)
        with patch("agent_windows.ipc.client.subprocess.Popen", return_value=fake_proc) as m_popen:
            _start_daemon_process(debug=False)
        assert "--debug" not in m_popen.call_args.args[0]

    def test_subprocess_is_detached(self):
        """The daemon must survive the parent CLI exiting — DETACHED_PROCESS +
        CREATE_NEW_PROCESS_GROUP, and stdio redirected to DEVNULL."""
        fake_proc = MagicMock(pid=1)
        with patch("agent_windows.ipc.client.subprocess.Popen", return_value=fake_proc) as m_popen:
            _start_daemon_process()
        kw = m_popen.call_args.kwargs
        flags = kw["creationflags"]
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert flags & subprocess.DETACHED_PROCESS
        assert kw["stdout"] is subprocess.DEVNULL
        assert kw["stderr"] is subprocess.DEVNULL
        assert kw["stdin"] is subprocess.DEVNULL
