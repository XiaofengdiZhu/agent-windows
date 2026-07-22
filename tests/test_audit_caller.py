"""Tests for __caller envelope threading: router pop + MCP/CLI injection."""
import asyncio
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


class TestMcpCallerInject:
    def test_call_injects_caller_from_contextvar(self):
        """_call injects __caller from audit_caller (HTTP edge sets it)."""
        from agent_windows.core.audit import audit_caller
        from agent_windows.mcp._call import _call
        token = audit_caller.set("oauth:10.0.0.4")
        captured = {}
        try:
            with patch("agent_windows.mcp._call.call_daemon",
                       lambda method, params: captured.update(m=method, p=params) or {}):
                _call("fs_delete", path="C:/x")
        finally:
            audit_caller.reset(token)
        assert captured["p"]["__caller"] == "oauth:10.0.0.4"
        assert captured["m"] == "fs_delete"

    def test_call_defaults_stdio(self):
        """No contextvar set (stdio transport) → __caller = 'stdio'."""
        from agent_windows.core.audit import audit_caller
        from agent_windows.mcp._call import _call
        token = audit_caller.set(None)
        captured = {}
        try:
            with patch("agent_windows.mcp._call.call_daemon",
                       lambda method, params: captured.update(p=params) or {}):
                _call("fs_delete", path="C:/x")
        finally:
            audit_caller.reset(token)
        assert captured["p"]["__caller"] == "stdio"


class TestAuditCallerMiddleware:
    """The middleware sets audit_caller from scope client IP + auth tag.

    Drives the raw-ASGI middleware with ``asyncio.run`` in a sync test, matching
    the suite's convention (see ``tests/test_mcp_server._drive``); no
    pytest-asyncio plugin is on the dev path.
    """

    def test_oauth_tag(self):
        from agent_windows.mcp.server import AuditCallerMiddleware
        from agent_windows.core.audit import audit_caller
        inner_called = {}

        async def app(scope, receive, send):
            inner_called["caller"] = audit_caller.get()

        asyncio.run(AuditCallerMiddleware(app)(
            {"type": "http", "client": ("10.0.0.4", 5000),
             "audit_auth": "oauth"}, None, None))
        assert inner_called["caller"] == "oauth:10.0.0.4"

    def test_auth_key_tag(self):
        from agent_windows.mcp.server import AuditCallerMiddleware
        from agent_windows.core.audit import audit_caller
        inner_called = {}

        async def app(scope, receive, send):
            inner_called["caller"] = audit_caller.get()

        asyncio.run(AuditCallerMiddleware(app)(
            {"type": "http", "client": ("203.0.113.9", 5000),
             "audit_auth": "auth_key"}, None, None))
        assert inner_called["caller"] == "auth_key:203.0.113.9"

    def test_anon_default(self):
        from agent_windows.mcp.server import AuditCallerMiddleware
        from agent_windows.core.audit import audit_caller
        inner_called = {}

        async def app(scope, receive, send):
            inner_called["caller"] = audit_caller.get()

        asyncio.run(AuditCallerMiddleware(app)(
            {"type": "http", "client": ("1.2.3.4", 5000)}, None, None))
        assert inner_called["caller"] == "anon:1.2.3.4"

    def test_non_http_no_set(self):
        from agent_windows.mcp.server import AuditCallerMiddleware
        from agent_windows.core.audit import audit_caller
        token = audit_caller.set(None)

        async def app(scope, receive, send):
            pass

        try:
            asyncio.run(AuditCallerMiddleware(app)({"type": "lifespan"}, None, None))
            assert audit_caller.get() is None  # lifespan doesn't set it
        finally:
            audit_caller.reset(token)
