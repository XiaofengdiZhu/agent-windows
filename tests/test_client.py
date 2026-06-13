"""Tests for the IPC client (call_daemon, unwrap_response)."""

import pytest

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
