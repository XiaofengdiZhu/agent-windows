"""Tests for daemon/handlers/window.py (P4.2).

Core functions (minimize/maximize/restore/close/focus_window) are patched so no
real Win32 call happens; audit_destructive is patched for the close test.
"""

from unittest.mock import patch

import pytest

from agent_windows.daemon.router import HANDLERS


_NON_DESTRUCTIVE = [
    ("window_minimize", "minimize_window"),
    ("window_maximize", "maximize_window"),
    ("window_restore", "restore_window"),
    ("window_focus", "focus_window"),
    ("window_enable", "enable_window"),
]


@pytest.mark.parametrize("method,core_fn", _NON_DESTRUCTIVE)
class TestNonDestructiveHandlers:
    def test_forwards_target_and_returns_dict(self, method, core_fn):
        sent = {"output": "## Window X", "hwnd": 7}
        with patch(f"agent_windows.core.window_manager.{core_fn}",
                   return_value=sent) as m:
            r = HANDLERS[method]({"target": "Notepad"})
        m.assert_called_once_with("Notepad")
        assert r is sent

    def test_default_empty_target_means_foreground(self, method, core_fn):
        with patch(f"agent_windows.core.window_manager.{core_fn}",
                   return_value={"output": "X", "hwnd": 7}) as m:
            HANDLERS[method]({})
        m.assert_called_once_with("")


class TestTargetValidation:
    def test_non_str_target_rejected(self):
        """A non-string target surfaces as INVALID_PARAMS (ValueError → -32602)."""
        with pytest.raises(ValueError, match="target"):
            HANDLERS["window_minimize"]({"target": 123})

    def test_none_target_treated_as_empty(self):
        """target=None (MCP drops None, but CLI may pass it) → foreground."""
        with patch("agent_windows.core.window_manager.minimize_window",
                   return_value={"output": "X", "hwnd": 7}) as m:
            HANDLERS["window_minimize"]({"target": None})
        m.assert_called_once_with("")


class TestCloseIsAudited:
    """window_close is destructive — it runs under _run_audited (records to the
    audit log), like process_kill. The other four ops are not audited."""

    def test_close_runs_audited_and_returns_dict(self):
        sent = {"output": "## Window Close", "hwnd": 7, "closed": True}
        with patch("agent_windows.core.window_manager.close_window",
                   return_value=sent) as m_core, \
             patch("agent_windows.core.audit.audit_destructive") as m_audit:
            r = HANDLERS["window_close"]({"target": "Notepad"})
        m_core.assert_called_once_with("Notepad")
        m_audit.assert_called_once_with(
            "window_close", {"target": "Notepad"}, "ok")
        assert r is sent

    def test_non_destructive_op_not_audited(self):
        with patch("agent_windows.core.audit.audit_destructive") as m_audit, \
             patch("agent_windows.core.window_manager.minimize_window",
                   return_value={"output": "X", "hwnd": 7}):
            HANDLERS["window_minimize"]({"target": "X"})
        m_audit.assert_not_called()
