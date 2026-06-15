"""Tests for daemon router: request routing and error handling."""

import pytest
from unittest.mock import MagicMock, patch
from agent_windows.daemon.router import route_request, HANDLERS, require
from agent_windows.ipc.protocol import JSONRPCRequest, PERMISSION_DENIED


class TestRouteRequest:
    def test_ping(self):
        req = JSONRPCRequest(id=1, method="ping")
        resp = route_request(req)
        assert resp.error is None
        assert resp.result["status"] == "ok"

    def test_unknown_method_returns_method_not_found(self):
        req = JSONRPCRequest(id=1, method="nonexistent")
        resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32601

    def test_empty_method_returns_invalid_request(self):
        req = JSONRPCRequest(id=1, method="")
        resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32600

    def test_handler_value_error_maps_to_invalid_params(self):
        req = JSONRPCRequest(id=1, method="get_text", params={})
        resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32602

    def test_missing_required_param_maps_to_invalid_params(self):
        """H-10: a bare params[key] gap must surface as INVALID_PARAMS, not
        DAEMON_INTERNAL_ERROR. fs_read with no path → require() raises
        ValueError before read_file runs."""
        req = JSONRPCRequest(id=1, method="fs_read", params={})
        resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32602
        assert "missing required parameter 'path'" in resp.error["message"]

    def test_wrong_param_type_maps_to_invalid_params(self):
        """H-10: require() type checks map to INVALID_PARAMS too."""
        req = JSONRPCRequest(id=1, method="fs_read", params={"path": 123})
        resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32602
        assert "must be str" in resp.error["message"]

    def test_permission_error_maps_to_permission_denied(self):
        """M-05: a handler raising PermissionError (e.g. shell blacklist block)
        maps to PERMISSION_DENIED (-1004), not the generic DAEMON_INTERNAL_ERROR."""
        with patch.dict(HANDLERS, {"forbidden": MagicMock(side_effect=PermissionError("blocked by policy"))}):
            req = JSONRPCRequest(id=1, method="forbidden")
            resp = route_request(req)
            assert resp.error is not None
            assert resp.error["code"] == PERMISSION_DENIED
            assert "blocked by policy" in resp.error["message"]


class TestRequire:
    def test_missing_and_none_raise(self):
        with pytest.raises(ValueError, match="missing required parameter 'x'"):
            require({}, "x")
        with pytest.raises(ValueError, match="missing required parameter 'x'"):
            require({"x": None}, "x")

    def test_wrong_type_raises(self):
        with pytest.raises(ValueError, match="must be int"):
            require({"x": "5"}, "x", int)

    def test_valid_returns_value_and_tuple_type_accepted(self):
        assert require({"x": 5}, "x", int) == 5
        assert require({"pid": "abc"}, "pid", (str, int)) == "abc"
        assert require({"pid": 123}, "pid", (str, int)) == 123
        # expected_type=None accepts anything present
        assert require({"x": object()}, "x") is not None

    def test_handlers_registered(self):
        """Verify key handlers are registered."""
        expected = {"ping", "shutdown", "screenshot", "snapshot", "observe",
                    "click", "type", "scroll", "shortcut", "batch",
                    "ground", "detect", "ask-vision",
                    "shell", "fs_read", "fs_write", "fs_list",
                    "clipboard_get", "clipboard_set",
                    "process_list", "process_kill",
                    "app_launch", "app_switch", "app_list",
                    "get_text", "element_state", "wait", "wait_check",
                    "highlight", "diff", "cleanup_screenshots",
                    "move", "drag", "hover", "dblclick",
                    "mouse_down", "mouse_up", "app_resize"}
        registered = set(HANDLERS.keys())
        missing = expected - registered
        assert not missing, f"Missing handlers: {missing}"
