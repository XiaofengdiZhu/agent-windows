"""Tests for daemon router: request routing and error handling."""

import logging

import pytest
from unittest.mock import MagicMock, patch
from agent_windows.daemon.router import (
    route_request, HANDLERS, require, configure_tool_policy,
)
from agent_windows.ipc.protocol import JSONRPCRequest, PERMISSION_DENIED, INVALID_PARAMS


def caplog_at_level(logger_name: str, level: int):
    """Context manager that captures records from *logger_name* at *level*.

    pytest's ``caplog`` fixture captures at WARNING by default; the router logs
    transient UIA errors at INFO, so raise the capture level. Returns an object
    exposing ``.records`` (used via the returned caplog inside the with-block).
    """
    import contextlib

    class _Sink:
        records = []

    sink = _Sink()

    @contextlib.contextmanager
    def _ctx():
        lg = logging.getLogger(logger_name)
        prev = lg.level
        handler = logging.Handler()
        handler.records = []

        def _emit(record):
            handler.records.append(record)

        handler.emit = _emit
        lg.addHandler(handler)
        lg.setLevel(level)
        sink.records = handler.records
        try:
            yield sink
        finally:
            lg.removeHandler(handler)
            lg.setLevel(prev)

    return _ctx()


@pytest.fixture(autouse=True)
def _reset_tool_policy():
    """Ensure the process-global tool policy never leaks between tests.

    ``configure_tool_policy`` mutates module state in ``router``; without this
    reset, a test that installs a policy would silently change every later
    test's routing. Restore the default (None = no enforcement) after each test.
    """
    yield
    configure_tool_policy(None, None)


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

    def test_bad_encoding_maps_to_invalid_params(self):
        """TODO #20b: an unknown codec surfaces as INVALID_PARAMS (-32602), not
        DAEMON_INTERNAL_ERROR — read_file re-raises LookupError as ValueError."""
        req = JSONRPCRequest(id=1, method="fs_read",
                             params={"path": "x.txt", "encoding": "not-a-codec"})
        resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == -32602

    def test_permission_error_maps_to_permission_denied(self):
        """M-05: a handler raising PermissionError (e.g. shell blacklist block)
        maps to PERMISSION_DENIED (-1004), not the generic DAEMON_INTERNAL_ERROR."""
        with patch.dict(HANDLERS, {"forbidden": MagicMock(side_effect=PermissionError("blocked by policy"))}):
            req = JSONRPCRequest(id=1, method="forbidden")
            resp = route_request(req)
            assert resp.error is not None
            assert resp.error["code"] == PERMISSION_DENIED
            assert "blocked by policy" in resp.error["message"]

    def test_file_not_found_maps_to_element_not_found(self):
        """A handler raising FileNotFoundError (e.g. fs_read on a missing path)
        maps to ELEMENT_NOT_FOUND (-1001), not DAEMON_INTERNAL_ERROR (-1999) with
        log spam. TODO #4 added 6 new raise sites widening this blast radius."""
        from agent_windows.ipc.protocol import ELEMENT_NOT_FOUND
        with patch.dict(HANDLERS, {"missing": MagicMock(side_effect=FileNotFoundError("gone"))}):
            req = JSONRPCRequest(id=1, method="missing")
            resp = route_request(req)
            assert resp.error is not None
            assert resp.error["code"] == ELEMENT_NOT_FOUND
            assert "gone" in resp.error["message"]

    def test_uia_dead_element_maps_to_element_not_found(self):
        """TODO #6 decision 2: a UIADeadElementError (element/process gone or
        disconnected during a live-COM call like expand/collapse) surfaces as
        ELEMENT_NOT_FOUND (-1001), the actionable "re-snapshot and retry"
        signal, not a generic DAEMON_INTERNAL_ERROR."""
        from agent_windows.ipc.protocol import ELEMENT_NOT_FOUND
        from agent_windows.uia.exceptions import UIADeadElementError
        from agent_windows.uia.enums import UIAError
        exc = UIADeadElementError(int(UIAError.UIA_E_ELEMENTNOTAVAILABLE))
        with patch.dict(HANDLERS, {"gone": MagicMock(side_effect=exc)}):
            req = JSONRPCRequest(id=1, method="gone")
            resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == ELEMENT_NOT_FOUND

    def test_uia_retryable_maps_to_internal(self):
        """TODO #6 decision 2: a UIARetryableError (target busy) is transient
        and NOT a dead element — the client may retry the same request. It
        surfaces the typed reason via DAEMON_INTERNAL_ERROR and logs at info
        (expected under load) rather than .exception (which implies a crash)."""
        from agent_windows.ipc.protocol import DAEMON_INTERNAL_ERROR
        from agent_windows.uia.exceptions import UIARetryableError
        from agent_windows.uia.enums import UIAError
        exc = UIARetryableError(int(UIAError.RPC_E_CALL_REJECTED))
        with patch.dict(HANDLERS, {"busy": MagicMock(side_effect=exc)}):
            req = JSONRPCRequest(id=1, method="busy")
            with caplog_at_level("agent_windows.daemon.router", logging.INFO) as cap:
                resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == DAEMON_INTERNAL_ERROR
        # Logged at info (transient/expected), not .exception (crash-level).
        assert any(r.levelno == logging.INFO and "busy" in r.getMessage()
                   for r in cap.records)

    def test_uia_not_supported_maps_to_invalid_params(self):
        """TODO #6 decision 2: an unsupported property/pattern shares the
        (UIANotEnabledError, UIANotSupportedError) clause with the disabled-
        element case — both → INVALID_PARAMS. Locks the tuple membership."""
        from agent_windows.uia.exceptions import UIANotSupportedError
        from agent_windows.uia.enums import UIAError
        exc = UIANotSupportedError(int(UIAError.UIA_E_NOTSUPPORTED))
        with patch.dict(HANDLERS, {"unsupported": MagicMock(side_effect=exc)}):
            req = JSONRPCRequest(id=1, method="unsupported")
            resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == INVALID_PARAMS

    def test_uia_timeout_maps_to_wait_timeout(self):
        """TODO #6 decision 2: a UIATimeoutError is the typed mirror of the
        stdlib TimeoutError clause → WAIT_TIMEOUT (-1003)."""
        from agent_windows.ipc.protocol import WAIT_TIMEOUT
        from agent_windows.uia.exceptions import UIATimeoutError
        from agent_windows.uia.enums import UIAError
        exc = UIATimeoutError(int(UIAError.UIA_E_TIMEOUT))
        with patch.dict(HANDLERS, {"uia_timeout": MagicMock(side_effect=exc)}):
            req = JSONRPCRequest(id=1, method="uia_timeout")
            resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == WAIT_TIMEOUT

    def test_uia_access_denied_maps_to_permission_denied(self):
        """TODO #6 decision 2: a UIAAccessDeniedError (elevated target from a
        non-elevated daemon) is the typed mirror of the stdlib PermissionError
        clause → PERMISSION_DENIED (-1004)."""
        from agent_windows.uia.exceptions import UIAAccessDeniedError
        from agent_windows.uia.enums import UIAError
        exc = UIAAccessDeniedError(int(UIAError.E_ACCESSDENIED))
        with patch.dict(HANDLERS, {"denied": MagicMock(side_effect=exc)}):
            req = JSONRPCRequest(id=1, method="denied")
            resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == PERMISSION_DENIED

    def test_uia_not_enabled_maps_to_invalid_params(self):
        """TODO #6 decision 2: invoking a pattern on a disabled element
        (UIA_E_ELEMENTNOTENABLED) is a request-state mismatch → INVALID_PARAMS,
        not a generic internal error."""
        from agent_windows.uia.exceptions import UIANotEnabledError
        from agent_windows.uia.enums import UIAError
        exc = UIANotEnabledError(int(UIAError.UIA_E_ELEMENTNOTENABLED))
        with patch.dict(HANDLERS, {"disabled": MagicMock(side_effect=exc)}):
            req = JSONRPCRequest(id=1, method="disabled")
            resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == INVALID_PARAMS

    def test_uia_invalid_operation_maps_to_invalid_params(self):
        """TODO #6 decision 2: UIA_E_INVALIDOPERATION (element live but the
        call is invalid in its current state) maps to INVALID_PARAMS."""
        from agent_windows.uia.exceptions import UIAInvalidOperationError
        from agent_windows.uia.enums import UIAError
        exc = UIAInvalidOperationError(int(UIAError.UIA_E_INVALIDOPERATION))
        with patch.dict(HANDLERS, {"badstate": MagicMock(side_effect=exc)}):
            req = JSONRPCRequest(id=1, method="badstate")
            resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == INVALID_PARAMS

    def test_uia_generic_maps_to_internal_and_not_before_subclasses(self):
        """TODO #6 decision 2: the catch-all UIAException clause handles the
        subclasses with no protocol-specific code (thread / no-clickable-point /
        unknown) → DAEMON_INTERNAL_ERROR with the typed message. It must come
        AFTER every specific subclass clause, so they never fall through."""
        from agent_windows.ipc.protocol import DAEMON_INTERNAL_ERROR
        from agent_windows.uia.exceptions import UIAException, UIAThreadError
        from agent_windows.uia.enums import UIAError
        exc = UIAThreadError(int(UIAError.RPC_E_WRONG_THREAD))
        assert isinstance(exc, UIAException)  # caught by the catch-all, not a
        # specific clause — locks that UIAThreadError has no dedicated mapping.
        with patch.dict(HANDLERS, {"wrongthread": MagicMock(side_effect=exc)}):
            req = JSONRPCRequest(id=1, method="wrongthread")
            resp = route_request(req)
        assert resp.error is not None
        assert resp.error["code"] == DAEMON_INTERNAL_ERROR


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
                    "fs_copy", "fs_move", "fs_delete", "fs_search", "fs_info",
                    "clipboard_get", "clipboard_set",
                    "process_list", "process_kill",
                    "app_launch", "app_switch", "app_list",
                    "get_text", "element_state", "wait", "wait_check",
                    "wait_for", "wait_for_check",
                    "highlight", "diff", "cleanup_screenshots",
                    "move", "drag", "hover", "dblclick",
                    "mouse_down", "mouse_up", "app_resize",
                    "key_down", "key_up",
                    "expand", "collapse"}
        registered = set(HANDLERS.keys())
        missing = expected - registered
        assert not missing, f"Missing handlers: {missing}"


class TestToolPolicy:
    """Defense-in-depth tool policy (review #7 follow-up): the daemon itself
    honors ``[tools] include/exclude`` so a client connecting directly to the
    named pipe (bypassing the MCP server's own filter) is still bound by the
    operator policy. ``route_request`` rejects a method outside the resolved
    allowlist with PERMISSION_DENIED (-1004) before dispatch.
    """

    def test_no_policy_allows_all_by_default(self):
        """No [tools] config → no enforcement → every registered method reachable."""
        configure_tool_policy(None, None)
        # ping is a safe, side-effect-free handler to assert reachability.
        resp = route_request(JSONRPCRequest(id=1, method="ping"))
        assert resp.error is None
        assert resp.result["status"] == "ok"

    def test_exclude_blocks_method(self):
        """exclude=["shell"] → a shell request is PERMISSION_DENIED (-1004)."""
        configure_tool_policy(None, ["shell"])
        # Use a stub handler so the test never runs a real shell; the policy
        # rejects before dispatch, so the stub body is never reached.
        with patch.dict(HANDLERS, {"shell": MagicMock(return_value={"ok": True})}):
            resp = route_request(JSONRPCRequest(id=1, method="shell", params={}))
        assert resp.error is not None
        assert resp.error["code"] == PERMISSION_DENIED
        assert "shell" in resp.error["message"]

    def test_exclude_leaves_others_reachable(self):
        """exclude=["shell"] → ping still works."""
        configure_tool_policy(None, ["shell"])
        resp = route_request(JSONRPCRequest(id=1, method="ping"))
        assert resp.error is None
        assert resp.result["status"] == "ok"

    def test_include_blocks_everything_not_listed(self):
        """include=["screenshot"] → an unlisted method is PERMISSION_DENIED."""
        configure_tool_policy(["screenshot"], None)
        with patch.dict(HANDLERS, {"shell": MagicMock(return_value={"ok": True})}):
            resp = route_request(JSONRPCRequest(id=1, method="shell", params={}))
        assert resp.error is not None
        assert resp.error["code"] == PERMISSION_DENIED

    def test_include_overrides_exclude(self):
        """include wins over exclude (matches MCP-layer apply_tool_filter).

        include=["screenshot"], exclude=["shell"]: include wins, so a registered
        method in NEITHER list (e.g. fs_read) is denied. (ping/shutdown are
        control-plane-exempt, so they're always allowed regardless — they can't
        be used to demonstrate include-overrides-exclude.)
        """
        configure_tool_policy(["screenshot"], ["shell"])
        with patch.dict(HANDLERS, {"fs_read": MagicMock(return_value={"ok": True})}):
            resp = route_request(JSONRPCRequest(id=1, method="fs_read", params={}))
        assert resp.error is not None
        assert resp.error["code"] == PERMISSION_DENIED

    def test_control_plane_exempt_under_include(self):
        """ping/shutdown are always reachable, even under a tight include — a
        [tools] typo must not brick the daemon into an unkillable state."""
        configure_tool_policy(["screenshot"], None)
        ping = route_request(JSONRPCRequest(id=1, method="ping"))
        assert ping.error is None
        assert ping.result["status"] == "ok"
        shutdown = route_request(JSONRPCRequest(id=1, method="shutdown"))
        assert shutdown.error is None
        assert shutdown.result["action"] == "shutting_down"

    def test_control_plane_exempt_under_exclude(self):
        """ping/shutdown are always reachable, even when 'excluded' — exclude
        can't lock the operator out of health/shutdown."""
        configure_tool_policy(None, ["ping", "shutdown"])
        ping = route_request(JSONRPCRequest(id=1, method="ping"))
        assert ping.error is None
        shutdown = route_request(JSONRPCRequest(id=1, method="shutdown"))
        assert shutdown.error is None

    def test_unknown_include_names_silently_ignored(self):
        """A typo in include shouldn't crash startup; unknown names are dropped
        (matches apply_tool_filter). ping still works because it's exempt."""
        configure_tool_policy(["nonexistent_tool"], None)
        resp = route_request(JSONRPCRequest(id=1, method="ping"))
        assert resp.error is None

    def test_unknown_method_still_method_not_found(self):
        """The policy gate runs AFTER the handler lookup, so a genuinely unknown
        method still returns METHOD_NOT_FOUND (-32601), not PERMISSION_DENIED —
        the policy only restricts registered methods."""
        configure_tool_policy(None, ["shell"])
        resp = route_request(JSONRPCRequest(id=1, method="totally_made_up"))
        assert resp.error is not None
        assert resp.error["code"] == -32601  # METHOD_NOT_FOUND

    def test_scalar_string_config_value_normalized(self):
        """config set writes scalars, so [tools] exclude lands as a comma-joined
        string ("shell,registry") not a list. configure_tool_policy tolerates it."""
        configure_tool_policy(None, "shell,registry")
        with patch.dict(HANDLERS, {
            "shell": MagicMock(),
            "registry": MagicMock(return_value={"ok": True}),
        }):
            shell_resp = route_request(JSONRPCRequest(id=1, method="shell", params={}))
            reg_resp = route_request(JSONRPCRequest(id=1, method="registry", params={}))
        assert shell_resp.error["code"] == PERMISSION_DENIED
        assert reg_resp.error["code"] == PERMISSION_DENIED

    def test_batch_subcommand_inherits_policy(self):
        """The whole point of daemon-side enforcement: a batch whose sub-command
        names an excluded method has that sub-command rejected at the daemon
        router (batch forwards each sub-call through route_request). The batch
        itself runs, but the offending sub-command fails inside it."""
        configure_tool_policy(None, ["shell"])
        resp = route_request(JSONRPCRequest(id=1, method="batch", params={
            "commands": [
                {"method": "ping", "params": {}},
                {"method": "shell", "params": {"command": "echo hi"}},
            ],
            "stop_on_error": True,
        }))
        # batch itself is allowed (it's a registered method, not excluded).
        assert resp.error is None
        out = (resp.result or {}).get("output", "")
        # 2 sub-commands attempted; the shell one was denied at the router →
        # exactly 1 failed inside the batch.
        assert "Failed: 1" in out

    def test_configure_is_idempotent_and_replaceable(self):
        """Each call replaces the previous policy (no accumulation)."""
        configure_tool_policy(None, ["shell"])
        configure_tool_policy(None, ["fs_delete"])  # replaces, doesn't add
        # shell is now allowed again (not in the new exclude set).
        with patch.dict(HANDLERS, {
            "shell": MagicMock(return_value={"ok": True}),
            "fs_delete": MagicMock(),
        }):
            shell_resp = route_request(JSONRPCRequest(id=1, method="shell", params={}))
            fs_resp = route_request(JSONRPCRequest(id=1, method="fs_delete", params={}))
        assert shell_resp.error is None  # no longer excluded
        assert fs_resp.error["code"] == PERMISSION_DENIED
