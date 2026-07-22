"""Tests for __caller envelope threading: router pop + MCP/CLI injection."""
import asyncio
import types
from unittest.mock import patch

from agent_windows.daemon.router import route_request
from agent_windows.ipc.protocol import JSONRPCRequest


class TestRouterCallerPop:
    def test_caller_popped_before_dispatch(self):
        """A handler never observes __caller — it is popped before dispatch."""
        from agent_windows.daemon.router import handler as handler_dec
        seen = {}

        @handler_dec("__test_probe__")
        def _probe(params):
            seen["keys"] = sorted(params.keys())
            return {"output": "ok"}

        resp = route_request(JSONRPCRequest(
            id=1, method="__test_probe__",
            params={"__caller": "oauth:1.2.3.4", "foo": "bar"}))
        assert resp.error is None
        assert seen["keys"] == ["foo"]  # __caller gone

    def test_default_pipe_when_no_caller(self):
        """No __caller injected → current_caller() is 'pipe' during dispatch."""
        from agent_windows.core.audit import current_caller
        from agent_windows.daemon.router import handler as handler_dec
        seen = {}

        @handler_dec("__test_probe2__")
        def _probe(params):
            seen["caller"] = current_caller()
            return {"output": "ok"}

        route_request(JSONRPCRequest(id=1, method="__test_probe2__", params={}))
        assert seen["caller"] == "pipe"

    def test_caller_contextvar_set_then_reset(self):
        """The injected caller is visible during dispatch but reset after."""
        from agent_windows.core.audit import audit_caller, current_caller
        from agent_windows.daemon.router import handler as handler_dec
        seen = {}

        @handler_dec("__test_probe3__")
        def _probe(params):
            seen["during"] = current_caller()
            return {"output": "ok"}

        route_request(JSONRPCRequest(
            id=1, method="__test_probe3__", params={"__caller": "auth_key:9.9.9.9"}))
        assert seen["during"] == "auth_key:9.9.9.9"
        assert audit_caller.get() is None  # reset after dispatch


class TestBatchInheritsCaller:
    """A batch sub-dispatch (nested route_request) inherits the outer caller.

    ``batch.py`` re-dispatches each sub-command via ``route_request(req)``,
    where ``req.params`` comes from the batch's ``commands[]`` and does NOT
    carry ``__caller``. Without inheritance, the inner ``route_request`` would
    reset the contextvar to ``"pipe"``, clobbering the outer request's caller —
    so a destructive op run via MCP ``batch`` (e.g. "delete these 5 files in
    one call") would be mis-attributed as ``"pipe"`` instead of the real
    authenticated caller. The fix: when no explicit ``__caller`` is injected,
    inherit the already-set contextvar (the outer caller).
    """

    def test_inner_dispatch_inherits_outer_caller(self):
        """A nested route_request with no __caller inherits the outer caller.

        This mimics batch exactly: the outer route_request (for the batch
        method) has set ``audit_caller`` from the batch request's ``__caller``
        and has not yet reset it (we are inside the batch handler's
        re-dispatch). The inner sub-command params do NOT carry ``__caller``.
        On the old code the inner caller was reset to ``"pipe"``; the fix
        inherits the outer value.
        """
        from agent_windows.core.audit import audit_caller, current_caller
        from agent_windows.daemon.router import handler as handler_dec
        seen = {}

        @handler_dec("__test_batch_inner__")
        def _probe(params):
            seen["caller"] = current_caller()
            return {"output": "ok"}

        token = audit_caller.set("oauth:1.2.3.4")
        try:
            resp = route_request(JSONRPCRequest(
                id=1, method="__test_batch_inner__", params={}))
        finally:
            audit_caller.reset(token)
        assert resp.error is None
        # Inherited from the outer caller, NOT reset to "pipe".
        assert seen["caller"] == "oauth:1.2.3.4"


class TestResolveAuditCaller:
    """Unit tests for mcp/_call.py::_resolve_audit_caller.

    The helper reads the LIVE per-request HTTP scope via FastMCP's
    ``get_http_request`` (falling back to ``"stdio"`` when there is no active
    request). These tests monkeypatch ``get_http_request`` directly.
    """

    def test_returns_stdio_when_get_http_request_raises(self):
        """No active HTTP request (stdio / unit test) → 'stdio'.

        get_http_request() RAISES RuntimeError when no request is bound (it
        does not return None); the helper must catch that and fall back.
        """
        from agent_windows.mcp._call import _resolve_audit_caller
        with patch("fastmcp.server.dependencies.get_http_request",
                   side_effect=RuntimeError("No active HTTP request found.")):
            assert _resolve_audit_caller() == "stdio"

    def test_returns_tag_and_ip_from_live_scope(self):
        """A live request scope yields '<tag>:<ip>' from audit_auth + client."""
        from agent_windows.mcp._call import _resolve_audit_caller
        fake = types.SimpleNamespace(scope={
            "type": "http", "client": ("10.0.0.4", 5000), "audit_auth": "oauth"})
        with patch("fastmcp.server.dependencies.get_http_request",
                   return_value=fake):
            assert _resolve_audit_caller() == "oauth:10.0.0.4"

    def test_anon_default_when_no_audit_auth_tag(self):
        """Missing audit_auth tag → 'anon' (auth middlewares tag on success)."""
        from agent_windows.mcp._call import _resolve_audit_caller
        fake = types.SimpleNamespace(scope={
            "type": "http", "client": ("203.0.113.9", 5000)})
        with patch("fastmcp.server.dependencies.get_http_request",
                   return_value=fake):
            assert _resolve_audit_caller() == "anon:203.0.113.9"

    def test_unknown_ip_when_no_client(self):
        """A scope without a client tuple → 'unknown' IP (best-effort)."""
        from agent_windows.mcp._call import _resolve_audit_caller
        fake = types.SimpleNamespace(scope={
            "type": "http", "audit_auth": "auth_key"})
        with patch("fastmcp.server.dependencies.get_http_request",
                   return_value=fake):
            assert _resolve_audit_caller() == "auth_key:unknown"

    def test_call_injects_resolved_caller(self):
        """_call injects __caller from _resolve_audit_caller (live scope)."""
        from agent_windows.mcp._call import _call
        fake = types.SimpleNamespace(scope={
            "type": "http", "client": ("10.0.0.4", 5000), "audit_auth": "oauth"})
        captured = {}
        with patch("agent_windows.mcp._call.call_daemon",
                   lambda method, params: captured.update(m=method, p=params) or {}), \
             patch("fastmcp.server.dependencies.get_http_request",
                   return_value=fake):
            _call("fs_delete", path="C:/x")
        assert captured["p"]["__caller"] == "oauth:10.0.0.4"
        assert captured["m"] == "fs_delete"

    def test_call_defaults_stdio_without_http_request(self):
        """No HTTP request active → __caller = 'stdio' (stdio transport)."""
        from agent_windows.mcp._call import _call
        captured = {}
        with patch("agent_windows.mcp._call.call_daemon",
                   lambda method, params: captured.update(p=params) or {}), \
             patch("fastmcp.server.dependencies.get_http_request",
                   side_effect=RuntimeError("No active HTTP request found.")):
            _call("fs_delete", path="C:/x")
        assert captured["p"]["__caller"] == "stdio"


class TestMcpCallerSpawnsAcrossTaskBoundary:
    """Integration proof: _call reads the LIVE request scope, not a stale
    contextvar, across the stateful streamable-http task boundary.

    The bug (commit 3d59d31): ``AuditCallerMiddleware`` set ``audit_caller``
    in the request task, but the tool handler runs inside a long-lived
    ``run_server`` task that the SDK spawned via ``TaskGroup.start`` at session
    creation — which COPIES the parent (session-creator) context. The later
    request's contextvar ``.set()`` is invisible to that already-running task,
    so every mid-session IP/auth change was mis-attributed.

    This test reproduces that task topology with ``anyio`` (the same library
    the SDK uses): a long-lived task spawned via ``tg.start()`` (copy-on-spawn
    context) runs ``_call``, while the live per-request identity comes from
    ``get_http_request`` (the real path the SDK sets per-request via message
    metadata → ``request_ctx``). It asserts BOTH that the contextvar is stale
    (proving the old approach mis-attributes) AND that ``_call`` still returns
    the live IP (proving the fix).
    """

    def test_call_reads_live_scope_not_stale_contextvar(self):
        """_call attributes to the live scope (IP_B), not stale contextvar."""
        import anyio
        from agent_windows.core.audit import audit_caller
        from agent_windows.mcp._call import _call

        ip_a = "10.0.0.1"   # session-creator IP (stale contextvar)
        ip_b = "10.0.0.2"   # second-request IP (live scope)
        captured: dict = {}

        async def main() -> None:
            # Session-creator context: the old AuditCallerMiddleware would set
            # audit_caller here, on the session-creating request.
            token = audit_caller.set(f"oauth:{ip_a}")
            try:
                async def run_server(*, task_status) -> None:
                    # Inside the spawned task: context was COPIED at spawn, so
                    # audit_caller still reads the session-creator's IP_A. This
                    # is exactly the run_server task the SDK spawns once per
                    # session. A later request arrives: the SDK carries the live
                    # Request in message metadata and sets request_ctx from it
                    # inside this task; get_http_request reads that live scope.
                    task_status.started()
                    live_scope = {
                        "type": "http",
                        "client": (ip_b, 5000),
                        "audit_auth": "oauth",
                    }
                    fake_req = types.SimpleNamespace(scope=live_scope)
                    # Prove the contextvar is STALE inside the spawned task —
                    # the old approach (audit_caller.get()) would mis-attribute.
                    captured["stale_contextvar"] = audit_caller.get()
                    with patch("agent_windows.mcp._call.call_daemon",
                               lambda method, params: (
                                   captured.update(p=params) or {})), \
                         patch(
                             "fastmcp.server.dependencies.get_http_request",
                             return_value=fake_req):
                        # _call runs here, INSIDE the spawned run_server task.
                        _call("fs_delete", path="C:/x")

                async with anyio.create_task_group() as tg:
                    await tg.start(run_server)
            finally:
                audit_caller.reset(token)

        asyncio.run(main())

        # The contextvar is stale (IP_A) inside the spawned task — this is the
        # bug. The old _call read audit_caller.get() and would emit IP_A here.
        assert captured["stale_contextvar"] == f"oauth:{ip_a}"
        # _call read the LIVE per-request scope (IP_B), not the stale
        # contextvar (IP_A). This is the fix.
        assert captured["p"]["__caller"] == f"oauth:{ip_b}"


class TestCliCallerInject:
    def test_dispatch_injects_local(self):
        """The CLI path stamps __caller='local' on every forwarded request."""
        from agent_windows.cli import dispatcher
        captured = {}
        with patch("agent_windows.cli.dispatcher.call_daemon",
                   lambda method, params: captured.update(m=method, p=params) or {}):
            dispatcher.dispatch("fs_delete", {"path": "C:/x"})
        assert captured["p"]["__caller"] == "local"
        assert captured["m"] == "fs_delete"

    def test_dispatch_none_params_handled(self):
        """dispatch(method, None) still injects local without a TypeError."""
        from agent_windows.cli import dispatcher
        captured = {}
        with patch("agent_windows.cli.dispatcher.call_daemon",
                   lambda method, params: captured.update(p=params) or {}):
            dispatcher.dispatch("screenshot", None)
        assert captured["p"] == {"__caller": "local"}

    def test_no_daemon_path_injects_local(self):
        """The --no-daemon path also carries __caller through route_request.

        ``_dispatch_no_daemon`` is reachable directly (not only via
        :func:`dispatch`), so it must inject ``__caller`` itself. The COM/DPI
        calls are mocked at the real modules — ``pythoncom``/``ctypes`` are
        lazy-imported inside the function body (CLI import-latency guard), so
        there is no ``dispatcher.pythoncom`` attribute to patch.
        """
        from agent_windows.cli import dispatcher
        captured = {}

        def fake_route(req):
            captured["params"] = dict(req.params or {})
            from agent_windows.ipc.protocol import JSONRPCResponse
            return JSONRPCResponse(id=req.id, result={"output": "ok"})

        with patch("agent_windows.daemon.router.route_request", fake_route), \
             patch("pythoncom.CoInitialize"), \
             patch("pythoncom.CoUninitialize"), \
             patch("ctypes.windll"):
            dispatcher._dispatch_no_daemon("screenshot", {"x": 1, "y": 2})
        assert captured["params"]["__caller"] == "local"
