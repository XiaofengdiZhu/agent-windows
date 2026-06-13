"""Tests for batch handler."""

import pytest
from unittest.mock import patch, MagicMock
from agent_windows.daemon.router import HANDLERS


@pytest.fixture
def batch_handler():
    return HANDLERS["batch"]


class TestBatchValidation:
    def test_empty_commands_raises(self, batch_handler):
        with pytest.raises(ValueError, match="non-empty"):
            batch_handler({"commands": []})

    def test_non_list_commands_raises(self, batch_handler):
        with pytest.raises(ValueError, match="non-empty"):
            batch_handler({"commands": "not a list"})

    def test_too_many_commands_raises(self, batch_handler):
        with pytest.raises(ValueError, match="max 50"):
            batch_handler({"commands": [{"method": "ping"}] * 51})

    def test_negative_delay_raises(self, batch_handler):
        with pytest.raises(ValueError, match="non-negative"):
            batch_handler({"commands": [{"method": "ping"}], "delay": -1})

    def test_nested_batch_not_allowed(self, batch_handler):
        result = batch_handler({"commands": [{"method": "batch", "params": {"commands": []}}], "stop_on_error": False})
        assert result["success"] == 0
        assert "Nested batch" in result["results"][0]["error"]["message"]

    def test_missing_method_in_command(self, batch_handler):
        result = batch_handler({"commands": [{"params": {}}], "stop_on_error": False})
        assert result["results"][0]["ok"] is False
        assert "Missing method" in result["results"][0]["error"]["message"]

    def test_non_string_method_in_command_isolated(self, batch_handler):
        """M-08 follow-up: a batch sub-command with a non-string method (e.g.
        123) must surface as a per-entry INVALID_PARAMS error, not leak into the
        handler registry. JSONRPCRequest.__init__ validates the type."""
        result = batch_handler({"commands": [{"method": 123}], "stop_on_error": False})
        assert result["results"][0]["ok"] is False
        assert result["results"][0]["error"]["code"] == -32602
        assert "method" in result["results"][0]["error"]["message"]

    def test_non_dict_params_in_command_isolated(self, batch_handler):
        """M-08 follow-up: a batch sub-command with non-dict params (e.g. a
        list) must surface as a per-entry INVALID_PARAMS error, not leak a list
        into the handler as a cryptic DAEMON_INTERNAL_ERROR. Previously batch
        built the request via JSONRPCRequest(...) bypassing from_json's checks."""
        result = batch_handler(
            {"commands": [{"method": "ping", "params": [1, 2]}], "stop_on_error": False}
        )
        assert result["results"][0]["ok"] is False
        assert result["results"][0]["error"]["code"] == -32602
        assert "params" in result["results"][0]["error"]["message"]

    def test_non_dict_params_does_not_abort_whole_batch(self, batch_handler):
        """M-08 follow-up: a malformed sub-command isolates to its own entry and
        does NOT abort the batch (with stop_on_error=False) — the valid command
        after it still runs and succeeds."""
        result = batch_handler(
            {
                "commands": [
                    {"method": "ping", "params": [1, 2]},  # malformed
                    {"method": "ping"},                    # valid
                ],
                "stop_on_error": False,
            }
        )
        assert result["total"] == 2
        assert result["results"][0]["ok"] is False
        assert result["results"][1]["ok"] is True


class TestBatchExecution:
    @patch("agent_windows.daemon.router.route_request")
    def test_successful_batch(self, mock_route, batch_handler):
        mock_resp = MagicMock(error=None, result={"status": "ok"})
        mock_route.return_value = mock_resp
        result = batch_handler({"commands": [{"method": "ping"}, {"method": "ping"}]})
        assert result["total"] == 2
        assert result["success"] == 2

    @patch("agent_windows.daemon.router.route_request")
    def test_stop_on_error(self, mock_route, batch_handler):
        mock_resp_fail = MagicMock(error={"code": -1, "message": "fail"}, result=None)
        mock_resp_ok = MagicMock(error=None, result={"status": "ok"})
        mock_route.side_effect = [mock_resp_fail, mock_resp_ok]
        result = batch_handler({"commands": [{"method": "fail"}, {"method": "ping"}], "stop_on_error": True})
        assert result["total"] == 1
        assert result["success"] == 0

    @patch("agent_windows.daemon.router.route_request")
    def test_continue_on_error(self, mock_route, batch_handler):
        mock_resp_fail = MagicMock(error={"code": -1, "message": "fail"}, result=None)
        mock_resp_ok = MagicMock(error=None, result={"status": "ok"})
        mock_route.side_effect = [mock_resp_fail, mock_resp_ok]
        result = batch_handler({"commands": [{"method": "fail"}, {"method": "ping"}], "stop_on_error": False})
        assert result["total"] == 2
        assert result["success"] == 1
