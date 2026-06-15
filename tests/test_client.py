"""Tests for the IPC client (call_daemon, unwrap_response)."""

import pytest
from types import SimpleNamespace

from agent_windows.ipc.client import unwrap_response
from agent_windows.ipc.protocol import JSONRPCResponse


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
        with pytest.raises(ConnectionError, match="code 42"):
            client.ensure_daemon_running()
        # Fail-fast: one top fast-path check + one loop iteration (where poll
        # detected the exit) — NOT the full 25-iteration window (26 calls).
        assert pipe_calls["n"] == 2

    def test_ready_after_spawn_when_daemon_alive(self, monkeypatch):
        import agent_windows.ipc.client as client

        # Top fast-path False (so we spawn), False once in the loop (daemon
        # alive but the pipe isn't up yet), then True — exercises the loop body
        # and confirms the alive poll() (None) does NOT trigger fail-fast.
        seq = [False, False, True]
        monkeypatch.setattr(client, "pipe_exists", lambda: seq.pop(0) if seq else True)
        monkeypatch.setattr(
            client,
            "_start_daemon_process",
            lambda debug=False: SimpleNamespace(poll=lambda: None),
        )
        monkeypatch.setattr(client.time, "sleep", lambda _: None)
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
        with pytest.raises(ConnectionError, match="did not become ready"):
            client.ensure_daemon_running()
