"""Destructive-op audit wiring for system handlers.

Each destructive handler (fs_delete / fs_move / process_kill / registry set|delete
/ sleep / system_shutdown / system_restart) records an audit line via
``audit_destructive`` with the right param subset + status (ok/fail/blocked).
The core fn and ``audit_destructive`` are both mocked so no real desktop / file /
process / power side effect occurs.
"""
import pytest
from unittest.mock import patch

from agent_windows.daemon.router import HANDLERS


# ── destructive-op audit wiring ────────────────────────────────────────


class TestDestructiveAudit:
    """Each destructive handler calls audit_destructive with the right param
    subset + status. The core fn and audit_destructive are both mocked so no
    real desktop / file / process / power side effect occurs."""

    def _audited(self):
        """Patch the audit call site + return the mock to assert on."""
        return patch("agent_windows.core.audit.audit_destructive")

    def test_fs_delete_ok(self):
        with patch("agent_windows.core.filesystem.delete_path",
                   return_value={"output": "ok"}) as m, self._audited() as aud:
            HANDLERS["fs_delete"]({"path": "C:/x", "permanent": True, "recursive": True})
        m.assert_called_once()
        aud.assert_called_once_with(
            "fs_delete", {"path": "C:/x", "permanent": True, "recursive": True}, "ok")

    def test_fs_delete_dry_run_skips_audit(self):
        with patch("agent_windows.core.filesystem.delete_path",
                   return_value={"output": "ok"}), self._audited() as aud:
            HANDLERS["fs_delete"]({"path": "C:/x", "dry_run": True})
        aud.assert_not_called()  # no side effect → no audit

    def test_fs_delete_blocked(self):
        """A pre-effect PermissionError is audited as 'blocked' and re-raised."""
        with patch("agent_windows.core.filesystem.delete_path",
                   side_effect=PermissionError("blacklist")), self._audited() as aud:
            with pytest.raises(PermissionError):
                HANDLERS["fs_delete"]({"path": "C:/x"})
        aud.assert_called_once_with(
            "fs_delete", {"path": "C:/x", "permanent": False, "recursive": False},
            "blocked")

    def test_fs_move_ok(self):
        with patch("agent_windows.core.filesystem.move_path",
                   return_value={"output": "ok"}), self._audited() as aud:
            HANDLERS["fs_move"]({"path": "C:/a", "destination": "C:/b"})
        aud.assert_called_once_with(
            "fs_move", {"path": "C:/a", "destination": "C:/b"}, "ok")

    def test_process_kill_ok(self):
        with patch("agent_windows.core.process_manager.kill_process",
                   return_value={"output": "ok"}), self._audited() as aud:
            HANDLERS["process_kill"]({"pid_or_name": 1234, "force": True})
        aud.assert_called_once_with(
            "process_kill", {"pid_or_name": 1234, "force": True}, "ok")

    def test_registry_set_excludes_value(self):
        with patch("agent_windows.core.registry.set_value",
                   return_value={"output": "ok"}), self._audited() as aud:
            HANDLERS["registry"]({"mode": "set", "path": "HKCU:\\S",
                                  "name": "Lic", "value": "SECRET", "type": "String"})
        _method, subset, _status = aud.call_args.args
        assert _method == "registry_set"
        assert subset == {"path": "HKCU:\\S", "name": "Lic", "type": "String"}
        assert "value" not in subset  # value never recorded (secret risk)

    def test_registry_delete_whole_key(self):
        with patch("agent_windows.core.registry.delete_entry",
                   return_value={"output": "ok"}), self._audited() as aud:
            HANDLERS["registry"]({"mode": "delete", "path": "HKCU:\\S"})
        _method, subset, _status = aud.call_args.args
        assert _method == "registry_delete"
        assert subset == {"path": "HKCU:\\S", "name": "<whole key>"}

    def test_system_shutdown_ok(self):
        with patch("agent_windows.core.power.shutdown_system",
                   return_value={"output": "ok"}), self._audited() as aud:
            HANDLERS["system_shutdown"]({"delay": 0, "force": True})
        aud.assert_called_once_with(
            "system_shutdown", {"delay": 0, "force": True}, "ok")

    def test_system_restart_ok(self):
        with patch("agent_windows.core.power.restart_system",
                   return_value={"output": "ok"}), self._audited() as aud:
            HANDLERS["system_restart"]({"delay": 5})
        aud.assert_called_once_with(
            "system_restart", {"delay": 5, "force": False}, "ok")

    def test_sleep_ok(self):
        with patch("agent_windows.core.power.suspend_sleep",
                   return_value={"output": "ok"}), self._audited() as aud:
            HANDLERS["sleep"]({})
        aud.assert_called_once_with("sleep", {}, "ok")

    def test_fs_delete_fail(self):
        """A non-permission exception is audited as 'fail' and re-raised."""
        with patch("agent_windows.core.filesystem.delete_path",
                   side_effect=RuntimeError("boom")), self._audited() as aud:
            with pytest.raises(RuntimeError):
                HANDLERS["fs_delete"]({"path": "C:/x"})
        aud.assert_called_once_with(
            "fs_delete", {"path": "C:/x", "permanent": False, "recursive": False},
            "fail")
