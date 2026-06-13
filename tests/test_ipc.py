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
