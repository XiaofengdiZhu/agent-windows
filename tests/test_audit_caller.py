"""Tests for __caller envelope threading: router pop + MCP/CLI injection."""
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
