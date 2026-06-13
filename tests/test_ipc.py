"""Tests for JSON-RPC protocol layer and IPC pipe transport."""

import json
import threading
import time
from unittest.mock import patch

import pytest

from agent_windows.ipc.protocol import (
    JSONRPCRequest,
    JSONRPCResponse,
    make_error,
    PARSE_ERROR,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    INVALID_PARAMS,
    ELEMENT_NOT_FOUND,
)
from agent_windows.ipc.pipe import send_to_daemon, _get_read_timeout


class TestJSONRPCRequest:
    def test_to_json(self):
        req = JSONRPCRequest(id=1, method="click", params={"x": 100, "y": 200})
        data = json.loads(req.to_json())
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert data["method"] == "click"
        assert data["params"] == {"x": 100, "y": 200}

    def test_from_json(self):
        raw = '{"jsonrpc":"2.0","id":1,"method":"ping"}'
        req = JSONRPCRequest.from_json(raw)
        assert req.id == 1
        assert req.method == "ping"
        assert req.params == {}

    def test_roundtrip(self):
        req = JSONRPCRequest(id=42, method="screenshot", params={"output": "test.png"})
        req2 = JSONRPCRequest.from_json(req.to_json())
        assert req2.id == 42
        assert req2.method == "screenshot"
        assert req2.params == {"output": "test.png"}

    def test_params_default_empty(self):
        req = JSONRPCRequest.from_json('{"jsonrpc":"2.0","id":1,"method":"ping"}')
        assert req.params == {}

    def test_unicode_params(self):
        req = JSONRPCRequest(id=1, method="type", params={"text": "你好世界"})
        req2 = JSONRPCRequest.from_json(req.to_json())
        assert req2.params["text"] == "你好世界"

    # ── M-08: structural validation (valid JSON, invalid Request) ──

    def test_non_string_method_raises_value_error(self):
        """M-08: a non-string 'method' is a structurally invalid request, not a
        parse error. from_json must reject it so the daemon maps it to
        INVALID_REQUEST (-32600) rather than letting a non-string key reach the
        handler registry as METHOD_NOT_FOUND 'Unknown method: 123'."""
        with pytest.raises(ValueError):
            JSONRPCRequest.from_json('{"jsonrpc":"2.0","id":1,"method":123}')

    def test_non_dict_params_raises_value_error(self):
        """M-08: 'params' must be an object here (all handlers take a dict). A
        list would otherwise leak to a handler and surface as a cryptic
        DAEMON_INTERNAL_ERROR TypeError on params["key"]."""
        with pytest.raises(ValueError):
            JSONRPCRequest.from_json(
                '{"jsonrpc":"2.0","id":1,"method":"click","params":[1,2]}'
            )

    def test_non_object_body_raises_value_error(self):
        """M-08: valid JSON that isn't an object (e.g. a bare array) is not a
        valid Request object → INVALID_REQUEST, not PARSE_ERROR. Previously this
        raised AttributeError on obj.get."""
        with pytest.raises(ValueError):
            JSONRPCRequest.from_json("[1, 2, 3]")

    def test_missing_method_raises_value_error(self):
        """M-08: a request without 'method' is structurally invalid. Previously
        obj["method"] raised KeyError → PARSE_ERROR; now a clear ValueError →
        INVALID_REQUEST."""
        with pytest.raises(ValueError):
            JSONRPCRequest.from_json('{"jsonrpc":"2.0","id":1}')

    def test_garbage_raises_json_decode_error(self):
        """M-08 regression: bytes that aren't JSON at all must still raise
        JSONDecodeError (→ PARSE_ERROR), staying distinct from the
        structurally-invalid ValueError path. Guards the server's except
        ordering (JSONDecodeError before ValueError — the former is a subclass
        of the latter)."""
        with pytest.raises(json.JSONDecodeError):
            JSONRPCRequest.from_json("not json at all")

    # ── M-08 follow-up: validation lives in the constructor, not just from_json ──

    def test_constructor_rejects_non_string_method(self):
        """M-08 follow-up: the type invariant is enforced in __init__, so a
        request built directly (not via from_json) — e.g. a batch sub-command —
        also rejects a non-string method. This is why batch no longer leaks
        malformed sub-commands to the handler."""
        with pytest.raises(ValueError):
            JSONRPCRequest(id=1, method=123)

    def test_constructor_rejects_non_dict_params(self):
        """M-08 follow-up: __init__ rejects non-dict params for every caller,
        not just from_json. A list params would otherwise reach a handler and
        produce an inconsistent/cryptic error."""
        with pytest.raises(ValueError):
            JSONRPCRequest(id=1, method="click", params=[1, 2])

    def test_constructor_accepts_none_params(self):
        """M-08 follow-up: params=None is the documented default and must still
        normalize to {} — only non-dict, non-None params are rejected."""
        req = JSONRPCRequest(id=1, method="ping")
        assert req.params == {}


class TestJSONRPCResponse:
    def test_result_response(self):
        resp = JSONRPCResponse(id=1, result={"status": "ok"})
        data = json.loads(resp.to_json())
        assert data["jsonrpc"] == "2.0"
        assert data["id"] == 1
        assert data["result"] == {"status": "ok"}
        assert "error" not in data

    def test_error_response(self):
        resp = make_error(ELEMENT_NOT_FOUND, "Button not found", id=1)
        data = json.loads(resp.to_json())
        assert data["error"]["code"] == ELEMENT_NOT_FOUND
        assert "Button not found" in data["error"]["message"]

    def test_roundtrip_result(self):
        resp = JSONRPCResponse(id=1, result={"path": "/tmp/test.png", "width": 1920})
        resp2 = JSONRPCResponse.from_json(resp.to_json())
        assert resp2.result["path"] == "/tmp/test.png"

    def test_roundtrip_error(self):
        resp = make_error(INVALID_PARAMS, "Missing x coordinate", id=2)
        resp2 = JSONRPCResponse.from_json(resp.to_json())
        assert resp2.error["code"] == INVALID_PARAMS
        assert resp2.result is None


class TestReadTimeoutConfig:
    def test_default_150(self):
        with patch("agent_windows.utils.config.load_config", return_value={}):
            assert _get_read_timeout() == 150

    def test_override_from_config(self):
        cfg = {"ipc": {"read_timeout_seconds": 60}}
        with patch("agent_windows.utils.config.load_config", return_value=cfg):
            assert _get_read_timeout() == 60


class TestSendToDaemonTimeout:
    def test_hung_daemon_raises_connection_error(self):
        """H-02: a daemon that accepts but never responds must make the client
        raise ConnectionError within the timeout — not block forever."""
        import os
        import win32file
        import win32pipe

        # Unique pipe name so concurrent runs / leftover pipes don't collide
        pipe_name = rf"\\.\pipe\agent-windows-test-h2-{os.getpid()}"
        server_ready = threading.Event()

        def server():
            spipe = win32pipe.CreateNamedPipe(
                pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                1,
                65536,
                65536,
                0,
                None,
            )
            server_ready.set()
            try:
                win32pipe.ConnectNamedPipe(spipe, None)
                win32file.ReadFile(spipe, 4096)  # consume the request, then stay silent
                time.sleep(10)  # hold the connection, never respond
            finally:
                try:
                    win32pipe.DisconnectNamedPipe(spipe)
                except Exception:
                    pass
                try:
                    win32file.CloseHandle(spipe)
                except Exception:
                    pass

        t = threading.Thread(target=server, daemon=True)
        t.start()
        assert server_ready.wait(2.0)

        start = time.monotonic()
        with pytest.raises(ConnectionError):
            send_to_daemon("ping", pipe_name=pipe_name, timeout=0.5)
        # Returned within a few seconds → proves no infinite block
        assert time.monotonic() - start < 5.0


class TestSendToDaemonRoundtrip:
    def test_large_response_returned_intact(self):
        """H-02 regression: a daemon that responds normally must return the
        full payload — exercises the overlapped read *completion* path (which
        had zero coverage when the buffer-slicing bug slipped through). Uses a
        >64KB response so the client also spans the multi-chunk read loop."""
        import os
        import win32file
        import win32pipe

        pipe_name = rf"\\.\pipe\agent-windows-test-h2-ok-{os.getpid()}"
        ready = threading.Event()
        big = "X" * 70000  # > BUFFER_SIZE (65536) → multiple read chunks
        response = '{"jsonrpc":"2.0","id":1,"result":{"data":"' + big + '"}}\n'
        response_bytes = response.encode("utf-8")

        def server():
            sp = win32pipe.CreateNamedPipe(
                pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                1,
                65536,
                65536,
                0,
                None,
            )
            ready.set()
            try:
                win32pipe.ConnectNamedPipe(sp, None)
                win32file.ReadFile(sp, 4096)  # consume request
                win32file.WriteFile(sp, response_bytes)  # send full response
                time.sleep(1.0)  # keep open while client reads
            finally:
                try:
                    win32file.CloseHandle(sp)
                except Exception:
                    pass

        t = threading.Thread(target=server, daemon=True)
        t.start()
        assert ready.wait(2.0)

        result = send_to_daemon(
            '{"jsonrpc":"2.0","id":1,"method":"ping"}',
            pipe_name=pipe_name,
            timeout=3.0,
        )
        assert result == response.strip()  # full payload, byte-for-byte
        assert big in result  # explicit: the large field survived intact


class TestPipeServerSecurity:
    def test_refuses_when_dacl_unavailable(self):
        """M-02: if the owner-only DACL cannot be built, the daemon must refuse
        to start rather than silently fall back to the Windows default
        (Everyone-/Anonymous-readable) named pipe. Security fails closed. The
        raise happens before CreateNamedPipe, so no real pipe is created."""
        from agent_windows.ipc import pipe as pipe_mod
        from agent_windows.ipc.pipe import DaemonStartupError

        with patch(
            "agent_windows.ipc.pipe._create_pipe_security_attributes",
            return_value=None,
        ):
            with pytest.raises(DaemonStartupError):
                pipe_mod.create_pipe_server(r"\\.\pipe\agent-windows-test-m02")

    def test_security_attributes_actually_build(self):
        """M-02 regression: the security helper must succeed for real on Windows,
        not just return None into the fail-closed path. The sibling test mocks the
        helper, which hid a ``win32security.GetUserName`` AttributeError — the
        helper ALWAYS failed and returned None, so the daemon could never start
        securely. This exercises the real win32 calls end-to-end."""
        from agent_windows.ipc.pipe import _create_pipe_security_attributes

        sa = _create_pipe_security_attributes()
        assert sa is not None, (
            "owner-only SECURITY_ATTRIBUTES could not be built — the daemon would "
            "refuse to start (fail-closed). Check the win32 API calls."
        )

    def test_main_loop_aborts_without_spinning(self):
        """M-02 regression (caught by review): create_pipe_server raising must
        make _main_pipe_loop ABORT (call once → break + request_shutdown), not be
        swallowed by the broad `except Exception` and busy-spin re-calling it
        hundreds of times per second. Uses a dedicated DaemonStartupError so a
        real per-connection error is never mistaken for a startup failure."""
        import threading
        from unittest.mock import MagicMock
        from agent_windows.daemon import server as server_mod
        from agent_windows.ipc.pipe import DaemonStartupError

        shutdown_event = threading.Event()
        state = {
            "shutdown_event": shutdown_event,
            "sta_thread": MagicMock(),
            "idle_monitor": MagicMock(),
            "request_shutdown": shutdown_event.set,
        }

        call_count = {"n": 0}

        def boom():
            call_count["n"] += 1
            # Safety cap: if the fix is absent and the loop spins, stop it so the
            # test fails cleanly instead of burning a core until timeout.
            if call_count["n"] > 10:
                shutdown_event.set()
            raise DaemonStartupError("simulated DACL failure")

        done = threading.Event()

        def run():
            try:
                with patch.object(server_mod, "create_pipe_server", side_effect=boom):
                    server_mod._main_pipe_loop(state)
            finally:
                done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert done.wait(3.0), "_main_pipe_loop did not return — it is busy-spinning"
        t.join(timeout=2.0)

        assert call_count["n"] == 1, (
            f"loop retried create_pipe_server {call_count['n']} times — busy-spin"
        )
        assert shutdown_event.is_set(), "request_shutdown() was not called on abort"
