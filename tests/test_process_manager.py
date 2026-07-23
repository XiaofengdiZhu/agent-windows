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
        """The table includes CPU (total s) — lifetime CPU seconds, not instant %
        (renamed from 'CPU (s)' to make the cumulative semantics explicit) — and a
        Parent column (parent_pid from CIM)."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"a","MemMB":1,"CpuSec":0}]'
        )
        out = pm.list_processes()["output"]
        assert "| PID | Name | Memory (MB) | CPU (total s) | Parent |" in out

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_single_process_json_object_normalized_to_list(self, mock_run):
        """ConvertTo-Json emits a bare object for one process — normalize it."""
        mock_run.return_value = _completed(
            '{"Id":42,"ProcessName":"calc","MemMB":5.0,"CpuSec":0.2}'
        )
        result = pm.list_processes()
        assert "| 42 | calc | 5.0 | 0.2 |" in result["output"]
        assert len(result["processes"]) == 1  # single object → 1-item sibling (review #2)

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_empty_output_yields_header_only(self, mock_run):
        """No processes (blank stdout) → just the header + separator, no data rows."""
        mock_run.return_value = _completed("")
        result = pm.list_processes()
        lines = result["output"].splitlines()
        assert lines[0] == "## Process List"
        assert lines[1] == "| PID | Name | Memory (MB) | CPU (total s) | Parent |"
        assert lines[2] == "|-----|------|-------------|---------------|--------|"
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
        assert "| 1 | ok | 1.0 | 0.0 |" in result["output"]
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
        r = pm.list_processes(limit=2)
        rows = _data_rows(r["output"])
        assert len(rows) == 2
        assert len(r["processes"]) == 2  # sibling capped too (review #1)
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

    # ── P3.3: processes[] sibling + CIM parent/cmdline/exe ──────────────

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_sibling_processes_array_present(self, mock_run):
        """A processes[] sibling carries the rows machine-readably so the agent
        needn't parse the markdown table. Each item has the full field set."""
        mock_run.return_value = _completed(
            '[{"Id":100,"ProcessName":"notepad","MemMB":12.3,"CpuSec":1.0,'
            '"PPid":4,"CmdLine":"notepad a.txt","ExePath":"C:\\\\win\\\\notepad.exe"}]'
        )
        procs = pm.list_processes()["processes"]
        assert isinstance(procs, list)
        assert len(procs) == 1
        assert set(procs[0]) == {
            "pid", "name", "mem_mb", "cpu_sec", "parent_pid",
            "cmdline", "exe_path",
        }

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_sibling_fields_numeric_snake_case(self, mock_run):
        """mem/cpu are floats (not the old str display values); keys are snake_case."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"a","MemMB":10,"CpuSec":2,'
            '"PPid":4,"CmdLine":null,"ExePath":null}]'
        )
        p = pm.list_processes()["processes"][0]
        assert p["mem_mb"] == 10.0
        assert p["cpu_sec"] == 2.0
        assert isinstance(p["mem_mb"], float)
        assert p["parent_pid"] == 4

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_parent_pid_column_from_cim(self, mock_run):
        """parent_pid lands in both the sibling and the markdown Parent column."""
        mock_run.return_value = _completed(
            '[{"Id":100,"ProcessName":"notepad","MemMB":1,"CpuSec":0,'
            '"PPid":1234,"CmdLine":null,"ExePath":null}]'
        )
        r = pm.list_processes()
        assert r["processes"][0]["parent_pid"] == 1234
        assert "| 100 | notepad | 1.0 | 0.0 | 1234 |" in r["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_cmdline_exe_path_only_in_sibling_not_table(self, mock_run):
        """cmdline/exe_path are long — they go in the sibling only, never the
        markdown table (keep the table narrow)."""
        long_cl = "chrome.exe --foo " + "x" * 200
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"chrome","MemMB":1,"CpuSec":0,'
            f'"PPid":4,"CmdLine":"{long_cl}","ExePath":"C:\\\\chr\\\\chrome.exe"}}]'
        )
        r = pm.list_processes()
        assert r["processes"][0]["cmdline"] == long_cl
        assert r["processes"][0]["exe_path"] == "C:\\chr\\chrome.exe"
        assert long_cl not in r["output"]
        assert "chrome.exe" not in r["output"]  # name col is bare "chrome"

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_cim_fields_null_for_protected_procs(self, mock_run):
        """Protected/system procs have no cmdline/exepath (CIM returns null); the
        sibling keeps None and the table Parent cell is blank, but Mem/CPU show."""
        mock_run.return_value = _completed(
            '[{"Id":4,"ProcessName":"System","MemMB":0,"CpuSec":100,'
            '"PPid":null,"CmdLine":null,"ExePath":null}]'
        )
        r = pm.list_processes()
        p = r["processes"][0]
        assert p["cmdline"] is None
        assert p["exe_path"] is None
        assert p["parent_pid"] is None
        assert "| 4 | System | 0.0 | 100.0 |  |" in r["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_two_same_name_distinguished_by_cmdline(self, mock_run):
        """P3.3 core: two chrome.exe are indistinguishable in the table, but the
        sibling cmdline tells them apart."""
        mock_run.return_value = _completed(
            '[{"Id":100,"ProcessName":"chrome","MemMB":1,"CpuSec":0,"PPid":4,'
            '"CmdLine":"chrome --profile=A","ExePath":null},'
            '{"Id":200,"ProcessName":"chrome","MemMB":1,"CpuSec":0,"PPid":4,'
            '"CmdLine":"chrome --profile=B","ExePath":null}]'
        )
        procs = pm.list_processes()["processes"]
        cmdlines = {p["pid"]: p["cmdline"] for p in procs}
        assert cmdlines[100] == "chrome --profile=A"
        assert cmdlines[200] == "chrome --profile=B"

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_mem_cpu_present_when_cim_fields_absent(self, mock_run):
        """Old-style JSON without CIM fields (or total CIM failure) still yields
        Mem/CPU from Get-Process; CIM fields default to None."""
        mock_run.return_value = _completed(
            '[{"Id":1,"ProcessName":"a","MemMB":5,"CpuSec":3}]'
        )
        p = pm.list_processes()["processes"][0]
        assert p["mem_mb"] == 5.0
        assert p["cpu_sec"] == 3.0
        assert p["cmdline"] is None
        assert p["parent_pid"] is None

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

    @pytest.fixture(autouse=True)
    def _stub_name_recheck(self, monkeypatch):
        # P1.2: a name-path success now rechecks via _count_by_image (tasklist),
        # which the mock_run below would intercept with taskkill's stdout and
        # miscount as 1 survivor. Stub it to 0 (confirmed gone) so these tests
        # stay focused on taskkill argv/classification — the recheck itself is
        # covered by TestKillRecheck and its real CSV parser by TestCountByImage.
        # (That isolation is also what let the C1 locale-safe-parse bug hide:
        # the real _count_by_image never ran under these mocks, so its INFO-line
        # miscount only surfaced once the parser was tested in isolation.)
        monkeypatch.setattr(pm, "_count_by_image", lambda image: 0)

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

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_name_path_single_taskkill_no_retry(self, mock_run):
        """M3: a name-path success runs taskkill exactly once. The recheck is a
        read-only tasklist count (_count_by_image, stubbed to 0 by this class's
        autouse fixture), never a second kill. Pins the no-retry design on the
        name path — the PID path's no-retry is already pinned in
        test_pid_still_running_no_retry; both paths must stay single-shot."""
        mock_run.return_value = _completed("SUCCESS: terminated", 0)
        r = pm.kill_process("notepad")
        assert "confirmed gone" in r["output"]
        assert mock_run.call_count == 1


# ── kill_process by PID ────────────────────────────────────────────────


class TestKillByPid:
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_terminates_pid_graceful(self, mock_run):
        """Graceful PID kill → taskkill /PID <pid>, no /F."""
        mock_run.return_value = _completed("SUCCESS: signal sent", 0)
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("notepad.exe", 0xDEADBEEF)):
            result = pm.kill_process(1234)
        argv = mock_run.call_args.args[0]
        assert argv == ["taskkill", "/PID", "1234"]
        assert "PID 1234" in result["output"]
        assert "Status: terminated" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_force_pid_adds_F(self, mock_run):
        """force PID kill → taskkill /PID <pid> /F."""
        mock_run.return_value = _completed("SUCCESS: forcibly", 0)
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("notepad.exe", 0xDEADBEEF)):
            result = pm.kill_process(1234, force=True)
        argv = mock_run.call_args.args[0]
        assert argv == ["taskkill", "/PID", "1234", "/F"]
        assert "Status: force-killed" in result["output"]

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_unknown_pid_raises_not_silent(self, mock_run):
        """taskkill 'not found' for a PID → raise (not silent success).

        _open_and_verify_pid is patched to a benign name so the PID passes the
        critical-name gate and reaches taskkill, which reports 'not found'."""
        mock_run.return_value = _completed("", 128, stderr="not found")
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("notepad.exe", 0xDEADBEEF)):
            with pytest.raises(ValueError, match="No such process with PID 9999"):
                pm.kill_process("9999")

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_pid_accepts_string_int(self, mock_run):
        """A numeric string is parsed as a PID (CLI/MCP pass-through)."""
        mock_run.return_value = _completed("SUCCESS: ok", 0)
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("notepad.exe", 0xDEADBEEF)):
            pm.kill_process("4321")
        argv = mock_run.call_args.args[0]
        assert argv == ["taskkill", "/PID", "4321"]


class TestKillRecheck:
    """P1.2: kill_process rechecks liveness after taskkill rc==0. PID path uses
    _image_name_for_pid (None=gone); name path uses _count_by_image (remaining
    count). A graceful close may be ignored or save-prompted → 'still running' is
    reported and NOT auto-retried (the agent decides whether to force)."""

    @patch("agent_windows.core.process_manager._image_name_for_pid",
           return_value=None)
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_pid_confirmed_dead_when_image_gone(self, mock_run, _img):
        # M2: _image_name_for_pid returns None for a dead PID AND for a live PID
        # we're access-denied to (OpenProcess fails either way) — the two are
        # indistinguishable. The recheck reads None optimistically as "confirmed
        # gone": kill already ran, so a best-effort confirmation beats failing
        # closed on the ambiguity. This pins that known limitation — a protected
        # survivor could be misreported gone. (The pre-kill identity check fails
        # CLOSED on the same None: different context, opposite call.)
        mock_run.return_value = _completed("SUCCESS: signal", 0)
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("notepad.exe", 0)):
            r = pm.kill_process(1234)
        assert "confirmed gone" in r["output"]
        assert r["killed"]["confirmed_dead"] is True

    @patch("agent_windows.core.process_manager._image_name_for_pid",
           return_value="notepad.exe")
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_pid_still_running_no_retry(self, mock_run, _img):
        mock_run.return_value = _completed("SUCCESS: signal", 0)
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("notepad.exe", 0)):
            r = pm.kill_process(1234)
        assert "still running" in r["output"]
        assert "force=True" in r["output"]
        assert r["killed"]["confirmed_dead"] is False
        # taskkill called exactly once — a graceful failure is NOT auto-retried
        assert mock_run.call_count == 1

    @patch("agent_windows.core.process_manager._image_name_for_pid",
           side_effect=Exception)
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_pid_recheck_failure_does_not_claim_dead(self, mock_run, _img):
        # fail-closed: a recheck that errors must not report confirmed gone
        mock_run.return_value = _completed("SUCCESS: signal", 0)
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("notepad.exe", 0)):
            r = pm.kill_process(1234)
        assert "confirmed gone" not in r["output"]
        assert r["killed"]["confirmed_dead"] is False

    @patch("agent_windows.core.process_manager._count_by_image", return_value=0)
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_name_zero_remaining_confirmed_gone(self, mock_run, _cnt):
        mock_run.return_value = _completed("SUCCESS: terminated", 0)
        r = pm.kill_process("notepad")
        assert "confirmed gone" in r["output"]
        assert r["killed"]["confirmed_dead"] is True

    @patch("agent_windows.core.process_manager._count_by_image", return_value=3)
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_name_counts_remaining(self, mock_run, _cnt):
        mock_run.return_value = _completed("SUCCESS: terminated", 0)
        r = pm.kill_process("notepad")
        assert "still running" in r["output"]
        assert "3 remaining" in r["output"]
        assert r["killed"]["confirmed_dead"] is False
        assert r["killed"]["remaining"] == 3

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_access_denied_does_not_recheck(self, mock_run):
        # non-success path (rc!=0) returns no killed sibling — no recheck
        mock_run.return_value = _completed("", 1, stderr="ERROR: Access is denied.")
        r = pm.kill_process("systemish")
        assert "killed" not in r
        assert "Status: failed (access denied)" in r["output"]

    @patch("agent_windows.core.process_manager._count_by_image", return_value=-1)
    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_name_recheck_failed_reports_unknown(self, mock_run, _cnt):
        # M1: _count_by_image -1 (recheck itself failed) must NOT read as
        # "still running (-1 remaining)" — the count is unknown, not a survivor
        # count. fail-closed on confirmed_dead, distinct recheck_failed sibling.
        mock_run.return_value = _completed("SUCCESS: terminated", 0)
        r = pm.kill_process("notepad")
        assert "still running" not in r["output"]
        assert "-1 remaining" not in r["output"]
        assert "unknown" in r["output"] or "recheck failed" in r["output"]
        assert r["killed"]["confirmed_dead"] is False
        assert r["killed"]["recheck_failed"] is True


class TestCountByImage:
    """C1 fix: _count_by_image's real parser. ``tasklist /FO CSV /NH`` emits a
    localized ``INFO: No tasks are running...`` line (rc 0) when nothing
    matches — the old count of non-empty lines miscounted that single INFO line
    as 1 survivor, so every successful by-name kill falsely reported 'still
    running (1 remaining)'. CSV data rows start with a double-quote; the
    localized INFO line does not, so counting ``startswith('"')`` is
    locale-independent."""

    def test_no_match_returns_zero_not_one(self):
        info = ("INFO: No tasks are running which match the specified "
                "criteria.\n")
        with patch("agent_windows.core.process_manager.subprocess.run",
                   return_value=_completed(info, 0)):
            assert pm._count_by_image("ghost_xyz.exe") == 0

    def test_counts_csv_data_rows(self):
        out = ('"notepad.exe","123","Console","1","5,120 K"\n'
               '"notepad.exe","456","Console","1","5,120 K"\n')
        with patch("agent_windows.core.process_manager.subprocess.run",
                   return_value=_completed(out, 0)):
            assert pm._count_by_image("notepad.exe") == 2

    def test_empty_stdout_zero(self):
        with patch("agent_windows.core.process_manager.subprocess.run",
                   return_value=_completed("", 0)):
            assert pm._count_by_image("x.exe") == 0

    def test_nonzero_returncode_is_unknown(self):
        with patch("agent_windows.core.process_manager.subprocess.run",
                   return_value=_completed("", 1)):
            assert pm._count_by_image("x.exe") == -1

    def test_subprocess_exception_is_unknown(self):
        with patch("agent_windows.core.process_manager.subprocess.run",
                   side_effect=Exception("boom")):
            assert pm._count_by_image("x.exe") == -1


class TestKillByPidImageNameGate:
    """SEC3-04 (round 2): the PID path must run the SAME critical-name check
    as the name path. A steered agent reads lsass's PID from list_processes
    and calls kill_process(<pid>) — the old code only checked _CRITICAL_PIDS
    {0,4}+self for PIDs, so any other critical process (lsass → reboot,
    csrss → BSOD) was reachable. Now the PID's image name is resolved and
    checked; if it can't be resolved, kill fails closed.

    (G7: kill_process now resolves via _open_and_verify_pid, which returns
    (name, handle) — handle held across taskkill. Tests patch that seam.)"""

    @pytest.mark.parametrize("image", ["lsass.exe", "csrss.exe", "wininit.exe"])
    def test_pid_resolving_to_critical_name_is_refused(self, image):
        """A PID whose image name is critical (resolved via win32) is refused
        before taskkill — closing the read-the-PID-from-the-listing bypass."""
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=(image, 0xDEADBEEF)):
            with patch("agent_windows.core.process_manager.subprocess.run") as mock_run:
                with pytest.raises(ValueError, match="system-critical"):
                    pm.kill_process(9876)
                mock_run.assert_not_called()

    def test_pid_resolving_to_critical_name_reports_pid(self):
        """The refusal message names the PID so the operator can trace it."""
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("lsass.exe", 0xDEADBEEF)):
            with pytest.raises(ValueError, match=r"PID 9876"):
                pm.kill_process(9876)

    def test_pid_with_unresolvable_image_name_fails_closed(self):
        """If the image name can't be resolved (gone / access denied / race)
        we must NOT proceed to taskkill — a recycled critical PID is exactly
        the case we can't confirm safe. Fail closed."""
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=(None, 0)):
            with patch("agent_windows.core.process_manager.subprocess.run") as mock_run:
                with pytest.raises(ValueError, match="could not be verified"):
                    pm.kill_process(9876)
                mock_run.assert_not_called()

    def test_pid_resolving_to_benign_name_proceeds(self):
        """A PID resolving to a normal app still reaches taskkill (not
        over-blocked)."""
        mock_run = _completed("SUCCESS: ok", 0)
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=("notepad.exe", 0xDEADBEEF)):
            with patch("agent_windows.core.process_manager.subprocess.run",
                       return_value=mock_run):
                result = pm.kill_process(5555)
        assert "Status: terminated" in result["output"]


class TestKillByPidHoldsHandleAcrossTaskkill:
    """G7 (TOCTOU): the OpenProcess handle resolved during verification must
    stay open UNTIL taskkill returns — not be closed right after the image-name
    query. Windows rule: a PID can't be freed/reused while ANY handle to the
    process object remains. Closing the handle before taskkill reopens the
    verify→kill window where the PID may exit and be reused (e.g. recycled to
    lsass), and taskkill /PID then kills the wrong process.

    kill_process now resolves the image via _open_and_verify_pid (returns
    (name, handle)) and closes the handle through _close_process_handle in a
    finally. We patch both module-level seam functions and assert CloseHandle
    is called AFTER subprocess.run (taskkill) — ordering is the TOCTOU fix."""

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_handle_closed_after_taskkill_graceful(self, mock_run):
        """Graceful path: the verify handle must outlive taskkill. The close
        seam records 'after' only if subprocess.run ran first."""
        seq = {"taskkill_done": False, "closed_at": None}

        def fake_open_and_verify(pid):
            return ("notepad.exe", 0xDEADBEEF)

        def fake_close(handle):
            seq["closed_at"] = "after" if seq["taskkill_done"] else "before"

        def run_spy(*a, **k):
            seq["taskkill_done"] = True
            return _completed("SUCCESS: signal sent", 0)

        mock_run.side_effect = run_spy
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   side_effect=fake_open_and_verify), \
                patch("agent_windows.core.process_manager._close_process_handle",
                      side_effect=fake_close):
            result = pm.kill_process(1234)
        assert "Status: terminated" in result["output"]
        assert seq["closed_at"] == "after", (
            f"handle closed {seq['closed_at']} taskkill — TOCTOU window open "
            "(handle must be held until taskkill returns so the PID can't be "
            "freed/reused between verify and kill)"
        )

    @patch("agent_windows.core.process_manager.subprocess.run")
    def test_handle_closed_even_when_taskkill_fails(self, mock_run):
        """The handle must be closed in a finally — even on the failure path
        (taskkill 'not found'), so we never leak the OpenProcess handle."""
        seq = {"taskkill_done": False, "closed_at": None}

        def fake_open_and_verify(pid):
            return ("notepad.exe", 0xDEADBEEF)

        def fake_close(handle):
            seq["closed_at"] = "after" if seq["taskkill_done"] else "before"

        def run_spy(*a, **k):
            seq["taskkill_done"] = True
            return _completed("", 128, stderr="not found")

        mock_run.side_effect = run_spy
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   side_effect=fake_open_and_verify), \
                patch("agent_windows.core.process_manager._close_process_handle",
                      side_effect=fake_close):
            with pytest.raises(ValueError, match="No such process"):
                pm.kill_process(1234)
        assert seq["closed_at"] == "after", (
            "handle not closed on the taskkill-failure path — leaked OpenProcess"
        )

    def test_unresolvable_pid_releases_no_handle(self):
        """If _open_and_verify_pid fails closed (returns handle 0 / name None),
        kill must refuse BEFORE taskkill and never call the close seam."""
        with patch("agent_windows.core.process_manager._open_and_verify_pid",
                   return_value=(None, 0)), \
                patch("agent_windows.core.process_manager._close_process_handle") as mock_close, \
                patch("agent_windows.core.process_manager.subprocess.run") as mock_run:
            with pytest.raises(ValueError, match="could not be verified"):
                pm.kill_process(9876)
            mock_run.assert_not_called()
            mock_close.assert_not_called()


class TestImageNameForPid:
    """The win32 resolver itself — real-process sanity (deterministic via
    os.getpid()), so a future refactor that breaks QueryFullProcessImageNameW
    wiring surfaces here rather than only via the monkeypatched gate tests."""

    def test_returns_current_process_image_lowercased(self):
        """Resolving our own PID returns our image name, lower-cased, with the
        .exe suffix (Python runs as python.exe)."""
        import sys
        name = pm._image_name_for_pid(__import__("os").getpid())
        # On Windows the interpreter is python.exe (or pythonw.exe); both end
        # in .exe. The resolver lower-cases, so the suffix is '.exe'.
        assert name is not None
        assert name.endswith(".exe"), name
        assert name == name.lower()

    def test_gone_pid_returns_none(self):
        """A PID that almost certainly doesn't exist resolves to None (fail
        signal), not an exception."""
        # PID 0xFFFF'FFFF wraps to uint32 -1; ~4e9 is well past any real PID.
        assert pm._image_name_for_pid(4_000_000_000) is None


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
