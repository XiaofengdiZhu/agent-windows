"""Tests for the shell runner: optional command blacklist and audit log (M-05)."""

import pytest
from unittest.mock import patch, MagicMock

from agent_windows.core.shell_runner import run_shell, _check_blacklist


def _cfg(blacklist):
    """Patch load_config to return a shell config with the given blacklist."""
    return patch(
        "agent_windows.core.shell_runner.load_config",
        return_value={"shell": {"blacklist": blacklist}},
    )


class TestBlacklist:
    """M-05: an optional regex blacklist rejects commands before execution."""

    def test_matching_pattern_blocks_case_insensitive(self):
        """A command matching a pattern (case-insensitive) returns the pattern."""
        with _cfg([r"\bRemove-Item\b"]):
            assert _check_blacklist("get-childitem; REMOVE-ITEM foo") == r"\bRemove-Item\b"

    def test_no_match_returns_none(self):
        with _cfg([r"\bRemove-Item\b"]):
            assert _check_blacklist("Get-Process") is None

    def test_empty_blacklist_allows_all(self):
        """Default (empty) blacklist disables filtering entirely."""
        with _cfg([]):
            assert _check_blacklist("Remove-Item -Force C:\\") is None

    def test_invalid_regex_skipped_not_crash(self):
        """An unparseable pattern is skipped (warned), not raised — a later
        valid pattern in the same list still applies."""
        with _cfg(["(unclosed", "Remove-Item"]):
            assert _check_blacklist("Remove-Item x") == "Remove-Item"

    def test_string_blacklist_treated_as_single_pattern(self):
        """M-05: ``config set`` only stores scalars, so a string blacklist must
        work as a single pattern (otherwise the documented interface is a
        silent no-op). Multiple patterns require editing config.toml into a list."""
        with patch(
            "agent_windows.core.shell_runner.load_config",
            return_value={"shell": {"blacklist": "Remove-Item"}},  # str, not list
        ):
            assert _check_blacklist("Remove-Item foo") == "Remove-Item"
            assert _check_blacklist("Get-Process") is None

    def test_non_string_non_list_blacklist_treated_as_empty(self):
        """A malformed (non-string, non-list) blacklist value must not crash."""
        with patch(
            "agent_windows.core.shell_runner.load_config",
            return_value={"shell": {"blacklist": 123}},
        ):
            assert _check_blacklist("Remove-Item") is None


class TestRunShellBlocked:
    def test_blocked_raises_permission_error_and_never_runs(self):
        """M-05: a blacklisted command raises PermissionError (→ PERMISSION_DENIED
        via route_request) and is never handed to subprocess."""
        with _cfg(["format"]), \
                patch("agent_windows.core.shell_runner._audit_shell") as mock_audit, \
                patch("subprocess.run") as mock_run:
            with pytest.raises(PermissionError, match="blacklist"):
                run_shell("Format-Volume")
            mock_run.assert_not_called()
            mock_audit.assert_called_once()
            assert mock_audit.call_args.args[1].startswith("BLOCKED")


class TestAuditLog:
    def test_allowed_command_recorded_full_untruncated(self, tmp_path):
        """M-05: the audit log records the FULL command (daemon.log truncates at
        500 chars); this is the forensic record, so nothing is cut."""
        with patch("agent_windows.core.shell_runner.get_log_dir", return_value=tmp_path), \
                _cfg([]), \
                patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok", stderr="", returncode=0)
            long_command = "Write-Output " + "X" * 2000
            run_shell(long_command)
        audit = (tmp_path / "shell_audit.log").read_text(encoding="utf-8")
        assert "ALLOWED" in audit
        assert long_command in audit  # full command survived, untruncated

    def test_multiline_command_collapsed_to_one_line(self, tmp_path):
        """M-05: embedded newlines are escaped so each audit entry is a single
        line, keeping the log line-addressable for forensics."""
        with patch("agent_windows.core.shell_runner.get_log_dir", return_value=tmp_path), \
                _cfg([]), \
                patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            run_shell("Get-Process;\nGet-Service")
        lines = (tmp_path / "shell_audit.log").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1  # one entry despite the embedded newline
        assert "\\n" in lines[0]  # newline was escaped, not literal

    def test_real_newline_distinct_from_literal_backslash_n(self, tmp_path):
        """M-05: the backslash-first escaping must be UNAMBIGUOUS — a real
        newline and a literal backslash-n in a command must produce different
        audit lines, so the forensic log is reversible. (Backslash is doubled
        first, so literal '\n' survives as '\\n' while a real newline becomes '\n'.)"""
        with patch("agent_windows.core.shell_runner.get_log_dir", return_value=tmp_path), \
                _cfg([]), \
                patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            run_shell("cmd;\nreal")      # real newline
            run_shell(r"cmd;\nliteral")  # literal backslash + n
        lines = (tmp_path / "shell_audit.log").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        assert lines[0] != lines[1]  # the two commands are distinguishable
        # Real newline -> single '\n' (backslash+n); literal -> '\\n' (two backslashes+n)
        assert lines[0].count("\\") < lines[1].count("\\")

    def test_blocked_command_also_audited(self, tmp_path):
        """M-05: a blocked command is recorded too — the audit trail captures
        attempts, not just successes."""
        with patch("agent_windows.core.shell_runner.get_log_dir", return_value=tmp_path), \
                _cfg(["Remove-Item"]), \
                patch("subprocess.run") as mock_run:
            with pytest.raises(PermissionError):
                run_shell("Remove-Item foo")
            mock_run.assert_not_called()
        audit = (tmp_path / "shell_audit.log").read_text(encoding="utf-8")
        assert "BLOCKED:Remove-Item" in audit
        assert "Remove-Item foo" in audit
