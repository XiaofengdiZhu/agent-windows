"""Direct unit tests for core/process_manager.py.

The handler-level tests in test_input_system_handlers.py mock
process_manager entirely, so the real subprocess construction, JSON parsing,
result reporting, and critical-PID refusal are otherwise unexercised.
This file locks those paths down. (TEST3-03, QUAL3-01/02, PERF3-09, SEC3-04)
"""
from unittest.mock import patch, MagicMock

import pytest

from agent_windows.core import process_manager as pm


def _completed(stdout: str, returncode: int = 0, stderr: str = ""):
    """Build a fake subprocess.CompletedProcess for the taskkill/Get-Process calls."""
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


def _data_rows(table_output: str) -> list[str]:
    """Extract just the data rows from a process table (drop title/header/sep).

    A data row starts with '| ' followed by a digit (the PID); the title is
    '## ...', the header '| PID | Name ...' and separator '|-----...' are dropped.
    """
    return [ln for ln in table_output.splitlines()
            if ln.startswith("| ") and ln[2:3].isdigit()]


# ── list_processes: JSON parsing + sort/filter/limit ────────────────────


class TestListProcesses:
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_parses_multi_process_json_array(self, mock_run):
        """ConvertTo-Json emits an array for many processes — parse all rows."""
        mock_run.return_value = _completed(
            '[{"Id":100,"ProcessName":"notepad","MemMB":12.3,"CpuSec":1.0},'
            '{"Id":200,"ProcessName":"Microsoft Edge","MemMB":450.7,"CpuSec":9.9}]'
        )
        result = pm.list_processes()
        out = result["output"]
        # Multi-word process name survives intact (the old Format-Table parser
        # would have split it across columns). (QUAL3-01)
        assert "| 100 | notepad | 12.3 | 1.0 |" in out
        assert "| 200 | Microsoft Edge | 450.7 | 9.9 |" in out
        assert out.startswith("## Process List")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_table_has_cpu_column_header(self, mock_run):
        """The 4-column table now includes CPU (s) — sort_by=cpu needs it visible."""
        mock_run.return_value = _completed("[]") if False else _completed(
            '[{"Id":1,"ProcessName":"a","MemMB":1,"CpuSec":0}]'
        )
        out = pm.list_processes()["output"]
        assert "| PID | Name | Memory (MB) | CPU (s) |" in out

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_single_process_json_object_normalized_to_list(self, mock_run):
        """ConvertTo-Json emits a bare object for one process — normalize it."""
        mock_run.return_value = _completed(
            '{"Id":42,"ProcessName":"calc","MemMB":5.0,"CpuSec":0.2}'
        )
        result = pm.list_processes()
        assert "| 42 | calc | 5.0 | 0.2 |" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_empty_output_yields_header_only(self, mock_run):
        """No processes (blank stdout) → just the header + separator, no data rows."""
        mock_run.return_value = _completed("")
        result = pm.list_processes()
        lines = result["output"].splitlines()
        assert lines[0] == "## Process List"
        assert lines[1] == "| PID | Name | Memory (MB) | CPU (s) |"
        assert lines[2] == "|-----|------|-------------|---------|"
        assert len(lines) == 3  # no data rows

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_skips_rows_missing_id_or_name(self, mock_run):
        """Malformed entries (no Id / no ProcessName) are skipped, not crashed."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"ok","MemMB":1,"CpuSec":0},'
            '{"ProcessName":"noid","MemMB":2,"CpuSec":0},'
            '{"Id":3,"MemMB":3,"CpuSec":0}]'
        )
        result = pm.list_processes()
        assert "| 1 | ok | 1 | 0 |" in result["output"]
        assert "noid" not in result["output"]
        assert "| 3 |" not in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_sort_by_memory_descending(self, mock_run):
        """Default sort: memory descending (biggest first)."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"a","MemMB":10,"CpuSec":0},'
            '{"Id":2,"ProcessName":"b","MemMB":500,"CpuSec":0},'
            '{"Id":3,"ProcessName":"c","MemMB":100,"CpuSec":0}]'
        )
        rows = _data_rows(pm.list_processes()["output"])
        # biggest (500, id 2) first
        assert "| 2 |" in rows[0]
        assert "| 1 |" in rows[2]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_sort_by_cpu_descending(self, mock_run):
        """sort_by=cpu → CPU descending."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"a","MemMB":1,"CpuSec":0.5},'
            '{"Id":2,"ProcessName":"b","MemMB":1,"CpuSec":9.0},'
            '{"Id":3,"ProcessName":"c","MemMB":1,"CpuSec":3.0}]'
        )
        rows = _data_rows(pm.list_processes(sort_by="cpu")["output"])
        assert "| 2 |" in rows[0]  # 9.0 first
        assert "| 1 |" in rows[2]  # 0.5 last

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_sort_by_name_ascending(self, mock_run):
        """sort_by=name → alphabetical ascending (A→Z), not descending."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"zebra","MemMB":1,"CpuSec":0},'
            '{"Id":2,"ProcessName":"apple","MemMB":1,"CpuSec":0},'
            '{"Id":3,"ProcessName":"Mango","MemMB":1,"CpuSec":0}]'
        )
        rows = _data_rows(pm.list_processes(sort_by="name")["output"])
        # apple, Mango, zebra (case-insensitive)
        assert "apple" in rows[0]
        assert "Mango" in rows[1]
        assert "zebra" in rows[2]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_limit_caps_row_count(self, mock_run):
        """limit=N keeps only the top N after sorting."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"a","MemMB":10,"CpuSec":0},'
            '{"Id":2,"ProcessName":"b","MemMB":500,"CpuSec":0},'
            '{"Id":3,"ProcessName":"c","MemMB":100,"CpuSec":0}]'
        )
        rows = _data_rows(pm.list_processes(limit=2)["output"])
        assert len(rows) == 2
        assert "| 2 |" in rows[0]  # 500 (biggest)

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_limit_zero_returns_all(self, mock_run):
        """limit=0 (default) returns every process."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"a","MemMB":1,"CpuSec":0},'
            '{"Id":2,"ProcessName":"b","MemMB":2,"CpuSec":0}]'
        )
        rows = _data_rows(pm.list_processes(limit=0)["output"])
        assert len(rows) == 2

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_name_filter_case_insensitive_substring(self, mock_run):
        """name filters to processes whose name contains it, case-insensitively."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"MicrosoftEdge","MemMB":1,"CpuSec":0},'
            '{"Id":2,"ProcessName":"edgehelper","MemMB":1,"CpuSec":0},'
            '{"Id":3,"ProcessName":"notepad","MemMB":1,"CpuSec":0}]'
        )
        rows = _data_rows(pm.list_processes(name="EDGE")["output"])
        assert len(rows) == 2
        assert "notepad" not in "".join(rows)

    def test_invalid_sort_by_raises(self):
        """An unknown sort_by surfaces as ValueError → INVALID_PARAMS."""
        with pytest.raises(ValueError, match="sort_by must be one of"):
            pm.list_processes(sort_by="bogus")

    @pytest.mark.parametrize("bad_limit", [-1, 1.5, "5", True])
    def test_invalid_limit_raises(self, bad_limit):
        """Non-int / negative / bool limit → ValueError."""
        with pytest.raises(ValueError, match="limit must be a non-negative integer"):
            pm.list_processes(limit=bad_limit)


# ── kill_process: taskkill-based (graceful / force) ─────────────────────


class TestKillByName:
    """kill_process runs ``taskkill [/IM name.exe] [/F]`` and classifies the
    result from taskkill's exit code + message text."""

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_graceful_success_reports_terminated(self, mock_run):
        """rc 0 + 'success' → Status: terminated (not force-killed)."""
        mock_run.return_value = _completed(
            "SUCCESS: Sent termination signal to the process ...", 0
        )
        result = pm.kill_process("notepad")
        assert "Status: terminated" in result["output"]
        argv = mock_run.call_args.args[0]
        assert argv == ["taskkill", "/IM", "notepad.exe"]  # no /F (graceful)

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_force_success_reports_force_killed_and_adds_F(self, mock_run):
        """force=True appends /F and reports force-killed."""
        mock_run.return_value = _completed(
            "SUCCESS: process ... was forcibly terminated", 0
        )
        result = pm.kill_process("notepad", force=True)
        assert "Status: force-killed" in result["output"]
        argv = mock_run.call_args.args[0]
        assert argv == ["taskkill", "/IM", "notepad.exe", "/F"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_rc_zero_is_success_regardless_of_locale(self, mock_run):
        """rc==0 is the authoritative taskkill success signal, independent of the
        (locale-specific) message text. A German/Chinese stdout that lacks the
        English word 'success' must still classify as terminated. Regression
        lock for the locale-independent success classification."""
        for localized_stdout in [
            "ERFOLG: ...",          # German
            "成功: 已终止 ...",      # Chinese
            "",                     # empty stdout
            "成功: プロセス ...",    # Japanese
        ]:
            mock_run.return_value = _completed(localized_stdout, 0)
            result = pm.kill_process("notepad")
            assert "Status: terminated" in result["output"], (
                f"rc==0 must be success regardless of stdout: {localized_stdout!r}"
            )

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_name_with_exe_extension_not_doubled(self, mock_run):
        """A name already ending in .exe is passed through unchanged."""
        mock_run.return_value = _completed("SUCCESS: ...", 0)
        pm.kill_process("notepad.exe")
        argv = mock_run.call_args.args[0]
        assert argv[2] == "notepad.exe"

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_no_such_name_raises_not_silent_success(self, mock_run):
        """A 'not found' taskkill must raise, not report killed. (QUAL3-02)"""
        mock_run.return_value = _completed(
            "ERROR: ... not found.", 128, stderr="not found"
        )
        with pytest.raises(ValueError, match="No such process"):
            pm.kill_process("nonexistent_xyz")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_no_running_instance_raises(self, mock_run):
        """'no running instance' (name has no live process) → no-such error."""
        mock_run.return_value = _completed(
            "", 128, stderr="ERROR: no running instance of the task"
        )
        with pytest.raises(ValueError, match="No such process"):
            pm.kill_process("ghost")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_access_denied_reports_failed(self, mock_run):
        """Access denied (exists, lacks rights) → failed (not raised)."""
        mock_run.return_value = _completed(
            "", 1, stderr="ERROR: Access is denied."
        )
        result = pm.kill_process("systemish")
        assert "Status: failed (access denied)" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_other_failure_reports_failed_with_tail(self, mock_run):
        """A generic non-success → failed, surfacing taskkill's last line."""
        mock_run.return_value = _completed("", 1, stderr="ERROR: the child still hangs")
        result = pm.kill_process("stubborn")
        assert "Status: failed" in result["output"]
        assert "the child still hangs" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_subprocess_run_uses_argv_list_not_shell(self, mock_run):
        """taskkill is invoked as an argv list (shell=False implicit) — the name
        is a literal element, so there is no shell-injection surface. (SEC-06)"""
        mock_run.return_value = _completed("SUCCESS: ok", 0)
        pm.kill_process("a'b; rm -rf")
        argv = mock_run.call_args.args[0]
        assert argv[0] == "taskkill"
        # the malicious name is passed as-is, never interpreted by a shell
        assert "rm -rf" in argv[2]
        assert mock_run.call_args.kwargs.get("shell") in (None, False)


# ── kill_process by PID ────────────────────────────────────────────────


class TestKillByPid:
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_terminates_pid_graceful(self, mock_run):
        """Graceful PID kill → taskkill /PID <pid>, no /F."""
        mock_run.return_value = _completed("SUCCESS: signal sent", 0)
        result = pm.kill_process(1234)
        argv = mock_run.call_args.args[0]
        assert argv == ["taskkill", "/PID", "1234"]
        assert "PID 1234" in result["output"]
        assert "Status: terminated" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_force_pid_adds_F(self, mock_run):
        """force PID kill → taskkill /PID <pid> /F."""
        mock_run.return_value = _completed("SUCCESS: forcibly", 0)
        result = pm.kill_process(1234, force=True)
        argv = mock_run.call_args.args[0]
        assert argv == ["taskkill", "/PID", "1234", "/F"]
        assert "Status: force-killed" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_unknown_pid_raises_not_silent(self, mock_run):
        """taskkill 'not found' for a PID → raise (not silent success)."""
        mock_run.return_value = _completed("", 128, stderr="not found")
        with pytest.raises(ValueError, match="No such process with PID 9999"):
            pm.kill_process("9999")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_pid_accepts_string_int(self, mock_run):
        """A numeric string is parsed as a PID (CLI/MCP pass-through)."""
        mock_run.return_value = _completed("SUCCESS: ok", 0)
        pm.kill_process("4321")
        argv = mock_run.call_args.args[0]
        assert argv == ["taskkill", "/PID", "4321"]


# ── SEC3-04: critical process protection ──────────────────────────────


class TestCriticalProcessProtection:
    @pytest.mark.parametrize("pid", ["0", "4", 0, 4])
    def test_refuses_critical_pid(self, pid):
        with pytest.raises(ValueError, match="system-critical"):
            pm.kill_process(pid)

    @pytest.mark.parametrize("name", ["lsass", "csrss", "wininit", "services", "winlogon"])
    def test_refuses_critical_name(self, name):
        with pytest.raises(ValueError, match="system-critical"):
            pm.kill_process(name)

    @pytest.mark.parametrize("name", ["System", "Registry", "SYSTEM", "ReGiStRy"])
    def test_refuses_capitalized_critical_name(self, name):
        """The two capitalized entries (System/Registry) must also be refused in
        any casing — the set is lower-cased and the lookup lower-cases input, so
        a case mismatch here would silently bypass SEC3-04 for PID-4/Registry.
        Regression lock for the case-normalization fix."""
        with patch("agent_windows.core.process_manager.subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="system-critical"):
                pm.kill_process(name)
            mock_run.assert_not_called()

    @pytest.mark.parametrize("name", ["lsass.exe", "csrss.EXE", "System.exe"])
    def test_refuses_critical_name_with_exe_suffix(self, name):
        """Passing the .exe form of a critical name must still be refused — the
        guard strips a trailing .exe before the membership test, else the image-
        name path would bypass SEC3-04. Regression lock for the .exe-normalization
        fix."""
        with patch("agent_windows.core.process_manager.subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="system-critical"):
                pm.kill_process(name)
            mock_run.assert_not_called()

    @patch("agent_windows.core.process_manager.os.getpid", return_value=777)
    def test_refuses_self_pid(self, _mock):
        with pytest.raises(ValueError, match="current process"):
            pm.kill_process("777")

    def test_critical_name_check_case_insensitive(self):
        """Uppercase LSASS is still refused (before any taskkill)."""
        with patch("agent_windows.core.process_manager.subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="system-critical"):
                pm.kill_process("LSASS")
            mock_run.assert_not_called()  # never reached taskkill

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_refuses_critical_pid_before_taskkill(self, mock_run):
        """Critical PID 4 is refused before taskkill is ever spawned."""
        with pytest.raises(ValueError, match="system-critical"):
            pm.kill_process(4, force=True)
        mock_run.assert_not_called()

    @pytest.mark.parametrize("bad", [True, False])
    def test_bool_pid_or_name_rejected(self, bad):
        """bool is an int subclass, so require()'s (str,int) check lets it through
        (True → PID 1). kill_process rejects it explicitly — pin that guard."""
        with pytest.raises(ValueError, match="must be a PID"):
            pm.kill_process(bad)
