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

    def test_constructor_rejects_non_hashable_id(self):
        """M-08 #2: a non-hashable id (list/dict) would crash
        COMWorkerThread.submit, which uses request.id as a dict key — dropping
        the connection with no JSON-RPC error. The constructor must reject it
        so the server maps it to INVALID_REQUEST instead."""
        with pytest.raises(ValueError):
            JSONRPCRequest(id=[1, 2], method="ping")

    def test_from_json_rejects_list_id(self):
        """M-08 #2: a wire request with a list id must raise ValueError at parse
        time (→ INVALID_REQUEST -32600 via the server's except ValueError),
        never reaching submit()."""
        with pytest.raises(ValueError):
            JSONRPCRequest.from_json('{"jsonrpc":"2.0","id":[1,2],"method":"ping"}')

    def test_constructor_accepts_valid_ids(self):
        """M-08 #2: None (notification), str, and int ids are all valid per
        JSON-RPC 2.0 (string/number/null) and must construct cleanly."""
        assert JSONRPCRequest(id=None, method="ping").id is None
        assert JSONRPCRequest(id="abc", method="ping").id == "abc"
        assert JSONRPCRequest(id=42, method="ping").id == 42


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

    def test_from_json_rejects_non_object(self):
        """A non-object JSON body (null, number, array) must raise ValueError
        rather than AttributeError, mirroring JSONRPCRequest.from_json's check.
        A client parsing an untrusted source shouldn't crash on shape."""
        with pytest.raises(ValueError):
            JSONRPCResponse.from_json("null")
        with pytest.raises(ValueError):
            JSONRPCResponse.from_json("[1, 2]")
        with pytest.raises(ValueError):
            JSONRPCResponse.from_json("42")


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

    def test_pipe_busy_is_retried_not_fatal(self, monkeypatch):
        """winerror 231 (ERROR_PIPE_BUSY) — the daemon's single instance is
        occupied/in the create→Connect window — is a transient connect state,
        not a permanent failure. A first 231 followed by a successful connect
        must return the daemon's response instead of raising ConnectionError.

        Pins the connect-retry set so a future change can't silently drop 231
        and reintroduce the "two concurrent CLI calls spuriously fail" bug.
        """
        import pywintypes
        import win32file
        import agent_windows.ipc.pipe as pipe_mod

        calls = {"connect": 0}

        def fake_createfile(*args, **kwargs):
            calls["connect"] += 1
            if calls["connect"] == 1:
                # ERROR_PIPE_BUSY — pipe exists but instance is busy.
                raise pywintypes.error(231, "CreateFile", "All pipe instances are busy.")
            return "sentinel-pipe-handle"

        monkeypatch.setattr(win32file, "CreateFile", fake_createfile)
        # The success path writes then reads; stub both at module scope (they
        # receive the sentinel handle), and stub CloseHandle in the finally.
        monkeypatch.setattr(pipe_mod, "_overlapped_write_all", lambda *a, **k: None)

        def fake_read_chunk(pipe, timeout_ms):
            # One chunk carrying the response line, then an empty chunk to end.
            if fake_read_chunk._sent:
                return b""
            fake_read_chunk._sent = True
            return b'{"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}\n'
        fake_read_chunk._sent = False
        monkeypatch.setattr(pipe_mod, "_overlapped_read_chunk", fake_read_chunk)
        monkeypatch.setattr(win32file, "CloseHandle", lambda h: None)

        resp = send_to_daemon("ping", pipe_name="\\\\.\\pipe\\unused", timeout=1.0)
        assert calls["connect"] == 2  # first 231, second success — no fatal raise
        assert json.loads(resp)["result"]["status"] == "ok"

    def test_drip_feed_response_is_bounded_by_total_deadline(self, monkeypatch):
        """F6 (round 2): the response read applies the timeout as a TOTAL
        deadline, not per-chunk. A daemon that drip-feeds bytes (each arriving
        just inside the per-chunk budget, never sending a newline) must trip the
        timeout within ~one timeout window, not keep the client alive forever.

        Under the old code each loop iteration got the full timeout_ms, so a
        one-byte-every-(timeout-ε) drip pinned the client open indefinitely.
        Now the deadline shrinks each iteration's budget, so the simulated
        overlapped-read (which raises ConnectionError once its budget is
        exhausted) fires within the window.

        The call is run on a worker thread with a hard join deadline so the
        buggy (no-total-deadline) variant fails the assertion instead of
        hanging the whole suite.
        """
        import threading
        import time as _time
        import win32file
        import agent_windows.ipc.pipe as pipe_mod

        monkeypatch.setattr(win32file, "CreateFile",
                            lambda *a, **k: "sentinel-pipe-handle")
        monkeypatch.setattr(pipe_mod, "_overlapped_write_all", lambda *a, **k: None)
        monkeypatch.setattr(win32file, "CloseHandle", lambda h: None)

        # Simulate the real overlapped read's contract: while the budget
        # comfortably allows a read, drip a non-newline byte; once the remaining
        # budget drops below a threshold, raise ConnectionError (a
        # WaitForSingleObject timeout). The loop only terminates via this raise
        # (no newline is ever sent), so it exercises the total-deadline path.
        def fake_read_chunk(pipe, timeout_ms, *a, **k):
            if timeout_ms <= 50:
                raise ConnectionError("read timed out")
            _time.sleep(0.02)  # let wall-clock advance a little
            return b"x"  # never a newline → loop would run forever without a deadline
        monkeypatch.setattr(pipe_mod, "_overlapped_read_chunk", fake_read_chunk)

        outcome: dict = {}

        def _call():
            try:
                send_to_daemon("ping", pipe_name="\\\\.\\pipe\\unused", timeout=0.4)
                outcome["err"] = None
            except BaseException as e:  # noqa: BLE001 — record for the main thread
                outcome["err"] = e

        start = _time.monotonic()
        t = threading.Thread(target=_call, daemon=True)
        t.start()
        t.join(3.0)  # hard cap: the fixed code finishes well under 1s
        elapsed = _time.monotonic() - start

        # The call must have returned (raised ConnectionError) promptly.
        assert not t.is_alive(), "send_to_daemon hung — total deadline not applied"
        assert elapsed < 2.0, f"call took {elapsed:.1f}s — not bounded by one timeout window"
        assert isinstance(outcome.get("err"), ConnectionError), \
            f"expected ConnectionError, got {outcome.get('err')!r}"

    def test_partial_then_eof_raises_connection_error(self, monkeypatch):
        """G11: a daemon that writes a partial response then closes the pipe
        (no newline terminator) must make send_to_daemon raise ConnectionError —
        mirroring the _PIPE_CLOSED_ERRORS arm in the SAME function (a winerror
        109/232 mid-response disconnect maps to ConnectionError). Without this,
        the empty/partial resp_data flows on and JSONRPCResponse.from_json("")
        raises an uncaught json.JSONDecodeError that escapes the CLI's error
        boundary (callers' except ConnectionError / the error-code mapping never
        see it). The EOF (`not chunk`) is the only loop exit, so this exercises
        the exact path."""
        import win32file
        import agent_windows.ipc.pipe as pipe_mod

        monkeypatch.setattr(win32file, "CreateFile",
                            lambda *a, **k: "sentinel-pipe-handle")
        monkeypatch.setattr(pipe_mod, "_overlapped_write_all", lambda *a, **k: None)
        monkeypatch.setattr(win32file, "CloseHandle", lambda h: None)

        # First chunk: partial JSON, no newline. Second: empty (EOF → `not chunk`
        # breaks the loop). The current code then decode()s b"" and returns "",
        # which call_daemon feeds to from_json → JSONDecodeError.
        chunks = iter([b'{"jsonrpc":"2.0","id":1}', b""])
        monkeypatch.setattr(pipe_mod, "_overlapped_read_chunk",
                            lambda pipe, timeout_ms: next(chunks))

        with pytest.raises(ConnectionError):
            send_to_daemon("ping", pipe_name="\\\\.\\pipe\\unused", timeout=1.0)


class TestServerReadTimeout:
    """SEC3-06: server-side read_pipe must be bounded — a client that connects
    but never sends (or sends a partial request) cannot block the single I/O
    thread forever. read_pipe raises PipeReadTimeout within the deadline."""

    def test_silent_client_triggers_read_timeout(self):
        """A client that connects but never writes must make server read_pipe
        raise PipeReadTimeout within the deadline, not block forever."""
        import os
        import win32file
        import win32pipe
        from agent_windows.ipc.pipe import (
            read_pipe, PipeReadTimeout, BUFFER_SIZE,
        )

        # Reuse the production pipe-creation path so the test exercises the real
        # overlapped flag (FILE_FLAG_OVERLAPPED) that makes the bounded read work.
        pipe_name = rf"\\.\pipe\agent-windows-test-srv-to-{os.getpid()}"
        sp = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX | 0x40000000,  # FILE_FLAG_OVERLAPPED
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1, BUFFER_SIZE, BUFFER_SIZE, 0, None)

        connected = threading.Event()

        def client():
            cp = win32file.CreateFile(
                pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
            connected.set()
            # Hold the connection open WITHOUT writing — the stuck-client case.
            time.sleep(10)
            win32file.CloseHandle(cp)

        t = threading.Thread(target=client, daemon=True)
        t.start()

        # Wait for the client to connect (blocking ConnectNamedPipe).
        win32pipe.ConnectNamedPipe(sp, None)
        assert connected.wait(2.0)

        start = time.monotonic()
        with pytest.raises(PipeReadTimeout):
            read_pipe(sp, timeout=0.5)  # short deadline for the test
        elapsed = time.monotonic() - start
        # Tripped within a small multiple of the 0.5s deadline — not a hang.
        assert elapsed < 3.0

        try:
            win32pipe.DisconnectNamedPipe(sp)
        except Exception:
            pass
        win32file.CloseHandle(sp)

    def test_drip_feed_client_trips_total_deadline(self):
        """SEC3-06: the timeout is a TOTAL deadline from the first read, not
        per-chunk. A client that drip-feeds one byte at a time (slowly enough
        that each individual overlapped chunk completes within the per-chunk
        wait) must still trip PipeReadTimeout within ~timeout — otherwise the
        DoS this change targets would survive via a slow trickle."""
        import os
        import win32file
        import win32pipe
        from agent_windows.ipc.pipe import read_pipe, PipeReadTimeout, BUFFER_SIZE

        pipe_name = rf"\\.\pipe\agent-windows-test-srv-drip-{os.getpid()}"
        sp = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX | 0x40000000,  # FILE_FLAG_OVERLAPPED
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1, BUFFER_SIZE, BUFFER_SIZE, 0, None)

        connected = threading.Event()

        def client():
            cp = win32file.CreateFile(
                pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
            connected.set()
            # Drip-feed one byte at a time, far slower than the total deadline
            # but each write completes instantly (well within any per-chunk
            # wait). Never send the terminating newline.
            for _ in range(1000):
                try:
                    win32file.WriteFile(cp, b"x")
                except Exception:
                    break
                time.sleep(0.05)  # 50ms between bytes — never trips a chunk wait
            win32file.CloseHandle(cp)

        threading.Thread(target=client, daemon=True).start()
        win32pipe.ConnectNamedPipe(sp, None)
        assert connected.wait(2.0)

        start = time.monotonic()
        with pytest.raises(PipeReadTimeout):
            read_pipe(sp, timeout=0.5)  # total deadline, not per-chunk
        elapsed = time.monotonic() - start
        # Fires within a small multiple of the 0.5s total deadline — proving the
        # drip-feed cannot extend it indefinitely.
        assert elapsed < 2.0

        try:
            win32pipe.DisconnectNamedPipe(sp)
        except Exception:
            pass
        win32file.CloseHandle(sp)

    def test_normal_request_read_through_overlapped(self):
        """A well-formed newline-delimited request must read back exactly
        through the new overlapped read_pipe — covering the normal (non-timeout)
        path that the silent-client test doesn't exercise. Uses a >64 KB body so
        the multi-chunk read loop (and the b'\\n' / not-chunk breaks) is hit."""
        import os
        import win32file
        import win32pipe
        from agent_windows.ipc.pipe import read_pipe, BUFFER_SIZE

        pipe_name = rf"\\.\pipe\agent-windows-test-srv-ok-{os.getpid()}"
        sp = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX | 0x40000000,  # FILE_FLAG_OVERLAPPED
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1, BUFFER_SIZE, BUFFER_SIZE, 0, None)

        # > BUFFER_SIZE so the read spans multiple overlapped chunks.
        big = "X" * (BUFFER_SIZE + 5000)
        request = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"d":"' + big + '"}}\n'
        request_bytes = request.encode("utf-8")
        connected = threading.Event()

        def client():
            cp = win32file.CreateFile(
                pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
            connected.set()
            # The write is larger than the pipe buffer, so it completes only as
            # the server-side read_pipe drains it — hence the server must read
            # concurrently, not after this returns.
            win32file.WriteFile(cp, request_bytes)
            time.sleep(2)
            win32file.CloseHandle(cp)

        threading.Thread(target=client, daemon=True).start()
        win32pipe.ConnectNamedPipe(sp, None)
        assert connected.wait(2.0)

        # Generous timeout: the data drains as we read; this just must not
        # timeout or corrupt the multi-chunk reassembly.
        result = read_pipe(sp, timeout=5.0)
        assert result == request.strip()
        assert big in result  # the large field survived intact across chunks

        try:
            win32pipe.DisconnectNamedPipe(sp)
        except Exception:
            pass
        win32file.CloseHandle(sp)
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

    def test_read_pipe_rejects_oversize_before_accumulating(self):
        """REVIEW B2: the size cap fires BEFORE appending the over-limit chunk,
        not after. A client that streams far more than MAX_MESSAGE_SIZE must be
        rejected as soon as the running total would cross the cap — the fix is
        specifically that `data` never balloons past MAX while the read loop
        keeps draining. Asserts the read raises and completes promptly (a
        post-append check would still eventually raise, so the promptness +
        no-hang is the real signal here)."""
        import os
        import win32file
        import win32pipe
        from agent_windows.ipc.pipe import (
            read_pipe, MAX_MESSAGE_SIZE, BUFFER_SIZE,
        )

        pipe_name = rf"\\.\pipe\agent-windows-test-srv-oversize-{os.getpid()}"
        sp = win32pipe.CreateNamedPipe(
            pipe_name,
            win32pipe.PIPE_ACCESS_DUPLEX | 0x40000000,  # FILE_FLAG_OVERLAPPED
            win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
            1, BUFFER_SIZE, BUFFER_SIZE, 0, None)

        connected = threading.Event()

        def client():
            cp = win32file.CreateFile(
                pipe_name, win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                0, None, win32file.OPEN_EXISTING, 0, None)
            connected.set()
            # Stream well past MAX, never sending the terminating newline. The
            # read loop must reject once the cap is reached.
            sent = 0
            payload = b"x" * BUFFER_SIZE
            try:
                while sent < MAX_MESSAGE_SIZE + BUFFER_SIZE * 2:
                    win32file.WriteFile(cp, payload)
                    sent += BUFFER_SIZE
            except Exception:
                pass
            time.sleep(1.0)
            win32file.CloseHandle(cp)

        threading.Thread(target=client, daemon=True).start()
        win32pipe.ConnectNamedPipe(sp, None)
        assert connected.wait(2.0)

        start = time.monotonic()
        with pytest.raises(RuntimeError, match="exceeds maximum size"):
            read_pipe(sp, timeout=10.0)
        elapsed = time.monotonic() - start
        # Must reject promptly — not wait out the 10s deadline. The cap is hit
        # once ~MAX bytes arrive, which at local-pipe speed is sub-second.
        assert elapsed < 5.0, f"size cap took {elapsed:.1f}s — did it accumulate past MAX first?"

        try:
            win32pipe.DisconnectNamedPipe(sp)
        except Exception:
            pass
        win32file.CloseHandle(sp)

    def test_send_to_daemon_rejects_oversize_response(self):
        """REVIEW B2 (client side): send_to_daemon applies the same pre-append
        size cap to the daemon's response, so a runaway response can't balloon
        resp_data past MAX before the cap fires."""
        import os
        import win32file
        import win32pipe
        from agent_windows.ipc.pipe import MAX_MESSAGE_SIZE, BUFFER_SIZE

        pipe_name = rf"\\.\pipe\agent-windows-test-cli-oversize-{os.getpid()}"
        ready = threading.Event()
        # No terminating newline; far larger than MAX.
        response_bytes = (b"X" * (MAX_MESSAGE_SIZE + BUFFER_SIZE * 2))

        def server():
            sp = win32pipe.CreateNamedPipe(
                pipe_name,
                win32pipe.PIPE_ACCESS_DUPLEX,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
                1, 65536, 65536, 0, None)
            ready.set()
            try:
                win32pipe.ConnectNamedPipe(sp, None)
                win32file.ReadFile(sp, 4096)  # consume request
                # Write more than MAX with no newline; keep the pipe open.
                win32file.WriteFile(sp, response_bytes)
                time.sleep(2.0)
            finally:
                try:
                    win32file.CloseHandle(sp)
                except Exception:
                    pass

        t = threading.Thread(target=server, daemon=True)
        t.start()
        assert ready.wait(2.0)

        with pytest.raises(RuntimeError, match="exceeds maximum size"):
            send_to_daemon(
                '{"jsonrpc":"2.0","id":1,"method":"ping"}',
                pipe_name=pipe_name,
                timeout=10.0,
            )


class TestSendToDaemonConnectRetry:
    """Regression: send_to_daemon must retry CreateFile when the pipe is briefly
    absent (ERROR_FILE_NOT_FOUND). The daemon tears down and recreates its
    single-instance pipe between connections; the client used to fail
    immediately on that transient gap, which upstream caused spurious daemon
    spawns. Now it retries with a short backoff until the pipe appears."""

    def test_retries_until_pipe_appears(self):
        """Server creates the pipe after a short delay. The client's CreateFile
        must retry through the gap and succeed once the pipe exists."""
        import os
        import win32file
        import win32pipe

        pipe_name = rf"\\.\pipe\agent-windows-test-retry-{os.getpid()}"
        response = '{"jsonrpc":"2.0","id":1,"result":{"status":"ok"}}\n'
        response_bytes = response.encode("utf-8")
        started = threading.Event()

        def server():
            started.set()
            time.sleep(0.15)  # hold off creating the pipe → client must retry
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
            try:
                win32pipe.ConnectNamedPipe(sp, None)
                win32file.ReadFile(sp, 4096)  # consume request
                win32file.WriteFile(sp, response_bytes)
                time.sleep(1.0)
            finally:
                try:
                    win32file.CloseHandle(sp)
                except Exception:
                    pass

        t = threading.Thread(target=server, daemon=True)
        t.start()
        assert started.wait(2.0)

        result = send_to_daemon(
            '{"jsonrpc":"2.0","id":1,"method":"ping"}',
            pipe_name=pipe_name,
            timeout=3.0,
        )
        assert '"ok"' in result  # got the response despite the delayed pipe

    def test_raises_after_retries_exhausted(self):
        """When the pipe never appears, send_to_daemon must still surface a
        ConnectionError — the retry is bounded, not infinite."""
        import os

        # A pipe name nothing will ever create
        pipe_name = rf"\\.\pipe\agent-windows-test-none-{os.getpid()}"
        start = time.monotonic()
        with pytest.raises(ConnectionError):
            send_to_daemon("ping", pipe_name=pipe_name, timeout=0.5)
        elapsed = time.monotonic() - start
        # Retried up to ~0.5s (50 × 10ms), then raised — not instant, not infinite
        assert 0.1 < elapsed < 2.0


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

    def test_create_pipe_maps_duplicate_instance_to_DaemonStartupError(self):
        """FIRST_PIPE_INSTANCE: when CreateNamedPipe fails because another
        daemon already owns the pipe (winerror 5 ACCESS_DENIED, or the
        historical 231 PIPE_BUSY), create_pipe_server must raise
        DaemonStartupError — not a bare pywintypes.error. That's what makes a
        losing daemon exit cleanly via _main_pipe_loop's
        except-DaemonStartupError arm instead of busy-spinning on the failure.
        Locks the mapping added to fix orphaned-daemon accumulation."""
        from agent_windows.ipc import pipe as pipe_mod
        from agent_windows.ipc.pipe import DaemonStartupError
        import pywintypes

        # winerror 5 (ACCESS_DENIED) is what FIRST_PIPE_INSTANCE yields when the
        # name is taken; 231 is the historical alternative. Both must map.
        for winerror in (5, 231):
            with patch("win32pipe.CreateNamedPipe",
                       side_effect=pywintypes.error(winerror, "CreateNamedPipe", "taken")):
                with patch("agent_windows.ipc.pipe._create_pipe_security_attributes",
                           return_value="fake_sa"):
                    with pytest.raises(DaemonStartupError) as exc_info:
                        pipe_mod.create_pipe_server(r"\\.\pipe\aw-test-dup")
                    assert "duplicate" in str(exc_info.value).lower(), (
                        f"winerror {winerror}: message should say this is a duplicate"
                    )


class TestOverlappedTimeoutDrainsCompletion:
    """G10: when WaitForSingleObject returns WAIT_TIMEOUT, CancelIo only
    SCHEDULES cancellation — the kernel still completes the I/O (writing the
    OVERLAPPED's Internal/InternalHigh) asynchronously. MSDN forbids freeing
    an OVERLAPPED before the operation completes. The fix: after CancelIo,
    call GetOverlappedResult(handle, overlapped, bWait=True) to block until
    the cancel completes, THEN raise. Without the drain, the local OVERLAPPED
    can be GC'd while the kernel still holds its address (UAF).

    These tests wire a timeout branch and assert GetOverlappedResult is called
    (with bWait=True) BETWEEN CancelIo and the raise — locking the drain."""

    def _patch_win32_timeout(self, monkeypatch):
        """Patch win32 so ReadFile/WriteFile return ERROR_IO_PENDING,
        WaitForSingleObject returns WAIT_TIMEOUT, and CancelIo /
        GetOverlappedResult are spied. Returns a recorder dict."""
        import win32event
        import win32file
        import pywintypes

        rec = {
            "cancel_calls": 0,
            "result_calls": [],     # list of bWait args
            "cancel_before_result": None,
        }

        def fake_write(pipe, data, overlapped):
            raise pywintypes.error(997, "WriteFile", "pending")

        def fake_read(pipe, n, overlapped):
            raise pywintypes.error(997, "ReadFile", "pending")

        def fake_wait(event, timeout_ms):
            return win32event.WAIT_TIMEOUT

        def fake_cancel(pipe):
            rec["cancel_calls"] += 1

        def fake_result(pipe, overlapped, wait):
            rec["result_calls"].append(wait)
            # Record ordering: CancelIo must have run first.
            rec["cancel_before_result"] = rec["cancel_calls"] > 0
            return 0

        monkeypatch.setattr(win32file, "WriteFile", fake_write)
        monkeypatch.setattr(win32file, "ReadFile", fake_read)
        monkeypatch.setattr(win32file, "CancelIo", fake_cancel)
        monkeypatch.setattr(win32file, "GetOverlappedResult", fake_result)
        monkeypatch.setattr(win32event, "WaitForSingleObject", fake_wait)
        # CreateEvent + CloseHandle must not touch real handles. CreateEvent
        # must return a PyHANDLE-compatible object (OVERLAPPED.hEvent requires
        # it); pywintypes.HANDLE(1) satisfies that without opening a real event.
        import win32api
        import pywintypes as _pwt
        monkeypatch.setattr(win32event, "CreateEvent", lambda *a, **k: _pwt.HANDLE(1))
        monkeypatch.setattr(win32api, "CloseHandle", lambda h: None)
        return rec

    def test_write_timeout_drains_completion_before_raise(self, monkeypatch):
        """G10: _overlapped_write_all on WAIT_TIMEOUT must call
        GetOverlappedResult(pipe, overlapped, True) AFTER CancelIo and BEFORE
        raising ConnectionError. The drain blocks until the cancel actually
        completes, so the OVERLAPPED isn't freed under the kernel."""
        import agent_windows.ipc.pipe as pipe_mod
        rec = self._patch_win32_timeout(monkeypatch)
        with pytest.raises(ConnectionError, match="Timed out"):
            pipe_mod._overlapped_write_all("fake-pipe", b"data", timeout_ms=50)
        assert rec["cancel_calls"] == 1, "CancelIo must be called once"
        assert rec["result_calls"] == [True], (
            f"GetOverlappedResult must be called once with bWait=True to drain "
            f"the cancel; got bWait={rec['result_calls']!r}"
        )
        assert rec["cancel_before_result"] is True, (
            "GetOverlappedResult must be called AFTER CancelIo"
        )

    def test_read_timeout_drains_completion_before_raise(self, monkeypatch):
        """G10: same drain requirement on the read path."""
        import agent_windows.ipc.pipe as pipe_mod
        rec = self._patch_win32_timeout(monkeypatch)
        with pytest.raises(ConnectionError, match="Timed out"):
            pipe_mod._overlapped_read_chunk("fake-pipe", timeout_ms=50)
        assert rec["cancel_calls"] == 1, "CancelIo must be called once"
        assert rec["result_calls"] == [True], (
            f"GetOverlappedResult must be called once with bWait=True to drain "
            f"the cancel; got bWait={rec['result_calls']!r}"
        )
        assert rec["cancel_before_result"] is True, (
            "GetOverlappedResult must be called AFTER CancelIo"
        )

