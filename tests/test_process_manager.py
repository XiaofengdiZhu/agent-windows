"""Direct unit tests for core/process_manager.py.

The handler-level tests in test_input_system_handlers.py mock
process_manager entirely, so the real PowerShell construction, JSON parsing,
result reporting, and critical-PID refusal are otherwise unexercised.
This file locks those paths down. (TEST3-03, QUAL3-01/02, PERF3-09, SEC3-04)
"""
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.core import process_manager as pm


def _completed(stdout: str, returncode: int = 0):
    """Build a fake subprocess.CompletedProcess."""
    return MagicMock(stdout=stdout, stderr="", returncode=returncode)


# ── list_processes: JSON parsing ────────────────────────────────────────


class TestListProcesses:
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_parses_multi_process_json_array(self, mock_run):
        """ConvertTo-Json emits an array for many processes — parse all rows."""
        mock_run.return_value = _completed(
            '[{"Id":100,"ProcessName":"notepad","MemMB":12.3},'
            '{"Id":200,"ProcessName":"Microsoft Edge","MemMB":450.7}]'
        )
        result = pm.list_processes()
        out = result["output"]
        # Multi-word process name survives intact (the old Format-Table parser
        # would have split it across columns). (QUAL3-01)
        assert "| 100 | notepad | 12.3 |" in out
        assert "| 200 | Microsoft Edge | 450.7 |" in out
        assert out.startswith("## Process List")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_single_process_json_object_normalized_to_list(self, mock_run):
        """ConvertTo-Json emits a bare object for one process — normalize it."""
        mock_run.return_value = _completed(
            '{"Id":42,"ProcessName":"calc","MemMB":5.0}'
        )
        result = pm.list_processes()
        assert "| 42 | calc | 5.0 |" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_empty_output_yields_header_only(self, mock_run):
        """No processes (blank stdout) → just the header + separator, no data rows."""
        mock_run.return_value = _completed("")
        result = pm.list_processes()
        lines = result["output"].splitlines()
        assert lines[0] == "## Process List"
        assert lines[1] == "| PID | Name | Memory (MB) |"
        assert lines[2] == "|-----|------|-------------|"
        assert len(lines) == 3  # no data rows

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_skips_rows_missing_id_or_name(self, mock_run):
        """Malformed entries (no Id / no ProcessName) are skipped, not crashed."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"ok","MemMB":1},'
            '{"ProcessName":"noid","MemMB":2},'
            '{"Id":3,"MemMB":3}]'
        )
        result = pm.list_processes()
        assert "| 1 | ok | 1 |" in result["output"]
        assert "noid" not in result["output"]
        assert "| 3 |" not in result["output"]


# ── kill_process: real outcome reporting ───────────────────────────────


class TestKillByName:
    """The name path emits a "pre|post" pair (process count before and after
    Stop-Process). PowerShell's $null.Count is 0, so a nonexistent name yields
    "0|0" — distinct from "1|0" (existed→killed) and "1|1" (kill failed)."""

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_reports_killed_when_existed_and_terminated(self, mock_run):
        """pre>0, post==0 → existed and was killed → Status: killed."""
        mock_run.return_value = _completed("1|0")
        result = pm.kill_process("notepad")
        assert "Status: killed" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_reports_failed_when_still_running(self, mock_run):
        """pre>0, post>0 → still alive (e.g. auto-restarting) → Status: failed."""
        mock_run.return_value = _completed("2|1")
        result = pm.kill_process("stubborn")
        assert "Status: failed" in result["output"]
        assert "1 still running" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_no_such_name_raises_not_silent_success(self, mock_run):
        """pre==0 (process never existed) must raise, not report killed. This
        is the QUAL3-02 regression lock: PowerShell's $null.Count is 0, so a
        missing name emits "0|0", which the OLD post-only-count code
        misread as "killed". (QUAL3-02)"""
        mock_run.return_value = _completed("0|0")
        with pytest.raises(ValueError, match="No such process"):
            pm.kill_process("nonexistent_xyz")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_unparseable_output_raises_runtime_error(self, mock_run):
        """Malformed PowerShell output → RuntimeError, not a wrong status."""
        mock_run.return_value = _completed("garbage")
        with pytest.raises(RuntimeError, match="Unexpected kill-process output"):
            pm.kill_process("whatever")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_single_quote_injection_is_escaped(self, mock_run):
        """A name containing a single quote must be doubled to escape it in the
        PowerShell single-quoted string (SEC-06 regression lock)."""
        mock_run.return_value = _completed("1|0")
        pm.kill_process("a'b")
        argv = mock_run.call_args.args[0]
        cmd = argv[3]  # ["powershell", "-NoProfile", "-Command", <cmd>]
        # 'a''b' — quote doubled, not interpolated unsafely
        assert "'a''b'" in cmd


# ── kill_process by PID ────────────────────────────────────────────────


class TestKillByPid:
    @patch("agent_windows.core.process_manager.ctypes.windll")
    def test_terminates_pid_and_closes_handle(self, mock_windll):
        """Successful PID kill calls TerminateProcess then CloseHandle."""
        kernel = mock_windll.kernel32
        kernel.OpenProcess.return_value = 999  # nonzero handle
        result = pm.kill_process("1234")
        kernel.OpenProcess.assert_called_once()
        kernel.TerminateProcess.assert_called_once_with(999, 1)
        kernel.CloseHandle.assert_called_once_with(999)
        assert "PID: 1234" in result["output"]
        assert "Status: terminated" in result["output"]

    @patch("agent_windows.core.process_manager.ctypes.windll")
    def test_unknown_pid_raises_not_silent_fallthrough(self, mock_windll):
        """OpenProcess returns 0 (no such / no access) → raise instead of
        silently dropping to the name-based path. (QUAL3-02)"""
        mock_windll.kernel32.OpenProcess.return_value = 0
        with pytest.raises(ValueError, match="No such process with PID 9999"):
            pm.kill_process("9999")


# ── SEC3-04: critical process protection ──────────────────────────────


class TestCriticalProcessProtection:
    @pytest.mark.parametrize("pid", ["0", "4"])
    def test_refuses_critical_pid(self, pid):
        with pytest.raises(ValueError, match="system-critical"):
            pm.kill_process(pid)

    @pytest.mark.parametrize("name", ["lsass", "csrss", "wininit", "services", "winlogon"])
    def test_refuses_critical_name(self, name):
        with pytest.raises(ValueError, match="system-critical"):
            pm.kill_process(name)

    @patch("agent_windows.core.process_manager.os.getpid", return_value=777)
    def test_refuses_self_pid(self, _mock):
        with pytest.raises(ValueError, match="current process"):
            pm.kill_process("777")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_critical_name_check_case_insensitive(self, mock_run):
        """Uppercase LSASS is still refused."""
        with pytest.raises(ValueError, match="system-critical"):
            pm.kill_process("LSASS")
        mock_run.assert_not_called()  # never reached PowerShell
