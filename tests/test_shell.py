"""Tests for the shell runner: optional command blacklist and audit log (M-05)."""

import subprocess

import pytest
from unittest.mock import patch, MagicMock

from agent_windows.core import shell_runner
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

    def test_redos_pattern_rejected_before_matching(self):
        """M-05 #3: a ReDoS-shaped pattern (nested quantifier like (a+)+) is
        rejected up front so it can't hang the daemon on adversarial input. A
        later valid pattern in the same list still applies."""
        with _cfg([r"(a+)+", "Remove-Item"]):
            assert _check_blacklist("Remove-Item foo") == "Remove-Item"
            # (a+)+ skipped — must not hang or crash on backtracking bait
            assert _check_blacklist("a" * 50 + "!") is None

    def test_benign_group_quantifier_not_rejected(self):
        """M-05 #3: a quantified group WITHOUT an inner quantifier (e.g. (\\w)+)
        is NOT a ReDoS vector and must still match."""
        with _cfg([r"(\w)+"]):
            assert _check_blacklist("hello world") == r"(\w)+"


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


class TestAccessDeniedHint:
    """TODO #14: a failed command with 'Access is denied' gets an elevation hint
    when the server isn't elevated."""

    def test_access_denied_not_elevated_appends_hint(self):
        """Non-zero exit + 'Access is denied' + not elevated → hint appended."""
        mock_result = MagicMock(stdout="", stderr="Access is denied", returncode=1)
        with _cfg([]), \
                patch("subprocess.run", return_value=mock_result), \
                patch("agent_windows.core.shell_runner.elevation_hint",
                      return_value="HINT: run elevated"):
            result = run_shell("Get-Process")
        assert "HINT: run elevated" in result["output"]
        assert result["exit_code"] == 1

    def test_access_denied_elevated_no_hint(self):
        """Even with 'Access is denied', an elevated server shows no hint — the
        failure isn't an elevation problem."""
        mock_result = MagicMock(stdout="", stderr="Access is denied", returncode=1)
        with _cfg([]), \
                patch("subprocess.run", return_value=mock_result), \
                patch("agent_windows.core.shell_runner.elevation_hint",
                      return_value=None):
            result = run_shell("Get-Process")
        assert "HINT" not in result["output"]

    def test_other_failure_no_hint(self):
        """A non-access-denied failure (even non-zero) gets no hint."""
        mock_result = MagicMock(stdout="", stderr="CommandNotFoundException", returncode=1)
        with _cfg([]), \
                patch("subprocess.run", return_value=mock_result), \
                patch("agent_windows.core.shell_runner.elevation_hint",
                      return_value="HINT: run elevated") as mock_hint:
            result = run_shell("Bad-Command")
        assert "HINT" not in result["output"]
        mock_hint.assert_not_called()  # short-circuit before checking elevation

    def test_success_no_hint(self):
        """A successful command (exit 0) never triggers the hint check."""
        mock_result = MagicMock(stdout="ok", stderr="", returncode=0)
        with _cfg([]), \
                patch("subprocess.run", return_value=mock_result), \
                patch("agent_windows.core.shell_runner.elevation_hint",
                      return_value="HINT: run elevated") as mock_hint:
            result = run_shell("Get-Process")
        assert "HINT" not in result["output"]
        mock_hint.assert_not_called()

    def test_access_denied_in_stdout_also_triggers(self):
        """'Access is denied' in stdout (not just stderr) triggers the hint —
        locks the two-stream combination in the guard."""
        mock_result = MagicMock(stdout="Access is denied", stderr="", returncode=1)
        with _cfg([]), \
                patch("subprocess.run", return_value=mock_result), \
                patch("agent_windows.core.shell_runner.elevation_hint",
                      return_value="HINT: run elevated"):
            result = run_shell("Get-Process")
        assert "HINT: run elevated" in result["output"]

    def test_access_denied_match_is_case_insensitive(self):
        """The guard lowercases the stream, so 'ACCESS IS DENIED' matches."""
        mock_result = MagicMock(stdout="", stderr="ACCESS IS DENIED", returncode=1)
        with _cfg([]), \
                patch("subprocess.run", return_value=mock_result), \
                patch("agent_windows.core.shell_runner.elevation_hint",
                      return_value="HINT: run elevated"):
            result = run_shell("Get-Process")
        assert "HINT: run elevated" in result["output"]


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

    def test_audit_log_rotates_when_exceeding_cap(self, tmp_path, monkeypatch):
        """M-05 #2: the audit log is bounded — once it exceeds the cap it
        rotates to .1 (keeping one backup) instead of growing without limit."""
        monkeypatch.setattr(shell_runner, "_SHELL_AUDIT_MAX_BYTES", 50)
        with patch("agent_windows.core.shell_runner.get_log_dir", return_value=tmp_path), \
                _cfg([]), \
                patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", stderr="", returncode=0)
            for i in range(5):
                run_shell(f"command-number-{i}-padding-padding-padding-padding")
        current = tmp_path / "shell_audit.log"
        backup = tmp_path / "shell_audit.log.1"
        assert current.exists() and backup.exists()
        # Only two files ever — rotation doesn't accumulate more backups.
        assert not (tmp_path / "shell_audit.log.2").exists()
        # The latest command is current; the oldest was evicted by rotation
        # (proving retention is bounded, not append-forever).
        assert "command-number-4" in current.read_text(encoding="utf-8")
        all_text = current.read_text(encoding="utf-8") + backup.read_text(encoding="utf-8")
        assert "command-number-0" not in all_text


# ── TODO #13: encoding, timeout, output truncation ──────────────────


def _shell_cfg(**over):
    """Patch load_config to return a shell config with arbitrary keys."""
    return patch(
        "agent_windows.core.shell_runner.load_config",
        return_value={"shell": {"blacklist": [], **over}},
    )


def _ok(stdout="", stderr="", returncode=0):
    """A MagicMock CompletedProcess for a successful run."""
    return MagicMock(stdout=stdout, stderr=stderr, returncode=returncode)


class TestEncoding:
    """TODO #13: PowerShell 5.1 emits the system codepage by default; a UTF-8
    prefix is prepended so the pipe bytes match the Python-side UTF-8 decode."""

    def test_command_has_utf8_prefix(self):
        """The UTF-8 setup snippet is prepended to the user command in the
        subprocess argv, so PS emits UTF-8 and the decode matches."""
        with _shell_cfg(), patch("subprocess.run", return_value=_ok("x")) as m:
            run_shell("Get-Process")
        argv = m.call_args.args[0]
        assert argv[:3] == ["powershell", "-NoProfile", "-NonInteractive"]
        assert argv[3] == "-Command"
        # The command argument carries the prefix BEFORE the user command.
        assert argv[4].startswith("$OutputEncoding")
        assert "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8" in argv[4]
        assert argv[4].endswith("Get-Process")

    def test_output_block_uses_original_command_not_prefixed(self):
        """The returned `output` shows the ORIGINAL command (not the prefixed
        internal form), so the agent isn't confused by the encoding boilerplate."""
        with _shell_cfg(), patch("subprocess.run", return_value=_ok("x")):
            result = run_shell("Get-Process")
        assert "Command: Get-Process" in result["output"]
        assert "$OutputEncoding" not in result["output"]

    def test_output_encoding_override(self):
        """An explicit shell.output_encoding overrides the decode (for a native
        exe that ignores the UTF-8 request and emits its legacy codepage)."""
        with _shell_cfg(output_encoding="gbk"), \
                patch("subprocess.run", return_value=_ok("ok")) as m:
            run_shell("Get-Date")
        assert m.call_args.kwargs["encoding"] == "gbk"

    def test_non_utf8_override_skips_utf8_prefix(self):
        """A non-UTF-8 decode override must NOT prepend the UTF-8 prefix —
        otherwise PS emits UTF-8 while Python decodes as GBK → mojibake, the
        exact bug this TODO fixes. The prefix only runs when we decode UTF-8."""
        with _shell_cfg(output_encoding="gbk"), \
                patch("subprocess.run", return_value=_ok("ok")) as m:
            run_shell("Get-Date")
        argv = m.call_args.args[0]
        # The -Command argument is the bare user command, no $OutputEncoding prefix.
        assert argv[4] == "Get-Date"

    def test_utf8_override_keeps_prefix(self):
        """An explicit 'utf-8' override still uses the prefix (they're
        consistent — PS emits UTF-8, Python decodes UTF-8)."""
        with _shell_cfg(output_encoding="utf-8"), \
                patch("subprocess.run", return_value=_ok("ok")) as m:
            run_shell("Get-Date")
        argv = m.call_args.args[0]
        assert argv[4].startswith("$OutputEncoding")

    def test_default_decode_is_utf8(self):
        """Default (no override) decodes as UTF-8 — matching the prefix."""
        with _shell_cfg(), patch("subprocess.run", return_value=_ok("ok")) as m:
            run_shell("Get-Date")
        assert m.call_args.kwargs["encoding"] == "utf-8"


class TestTimeout:
    """TODO #13: timeout comes from config when omitted, and a timeout now
    raises TimeoutError (WAIT_TIMEOUT -1003) instead of RuntimeError."""

    def test_timeout_defaults_from_config(self):
        """Omitted timeout → shell.timeout config value is used."""
        with _shell_cfg(timeout=7), patch("subprocess.run", return_value=_ok()) as m:
            run_shell("Get-Date")
        assert m.call_args.kwargs["timeout"] == 7

    def test_explicit_timeout_overrides_config(self):
        """An explicit timeout wins over the config default."""
        with _shell_cfg(timeout=7), patch("subprocess.run", return_value=_ok()) as m:
            run_shell("Get-Date", timeout=42)
        assert m.call_args.kwargs["timeout"] == 42

    def test_timeout_falls_back_to_30_when_config_absent(self):
        """No timeout in config and none passed → 30s legacy default."""
        with patch("agent_windows.core.shell_runner.load_config",
                   return_value={}), \
                patch("subprocess.run", return_value=_ok()) as m:
            run_shell("Get-Date")
        assert m.call_args.kwargs["timeout"] == 30.0

    def test_timeout_raises_timeouterror_not_runtimeerror(self):
        """A timeout raises TimeoutError (→ WAIT_TIMEOUT -1003 via the router),
        not RuntimeError (→ DAEMON_INTERNAL_ERROR + log spam). Mirrors
        notifier.py's deliberate choice."""
        with _shell_cfg(), patch("subprocess.run", side_effect=subprocess.TimeoutExpired(
                cmd="powershell", timeout=5)):
            with pytest.raises(TimeoutError, match="timed out"):
                run_shell("Start-Sleep 100", timeout=5)

    def test_powershell_missing_still_runtimeerror(self):
        """PowerShell not installed is an environment failure, not a wait
        condition — stays RuntimeError (→ DAEMON_INTERNAL_ERROR)."""
        with _shell_cfg(), patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(RuntimeError, match="PowerShell not found"):
                run_shell("Get-Date")


class TestOutputTruncation:
    """TODO #13: shell.max_output_bytes caps combined stdout+stderr so a
    runaway command can't flood the agent's context."""

    def test_no_cap_when_zero(self):
        """max_output_bytes=0 (default) = legacy behavior, no truncation."""
        big = "X" * 5000
        with _shell_cfg(max_output_bytes=0), \
                patch("subprocess.run", return_value=_ok(stdout=big)):
            result = run_shell("Get-Process")
        assert result["stdout"] == big
        assert "truncated" not in result["output"]

    def test_truncates_when_over_cap(self):
        """Output over the cap is truncated and a marker is appended."""
        big = "line\n" * 2000  # ~10 KB
        with _shell_cfg(max_output_bytes=1000), \
                patch("subprocess.run", return_value=_ok(stdout=big)):
            result = run_shell("Get-Process")
        assert "[output truncated:" in result["output"]
        assert "limit 1000" in result["output"]
        # Returned stdout shrank to fit the byte budget.
        assert len(result["stdout"].encode("utf-8")) <= 1000

    def test_truncation_under_cap_untouched(self):
        """Output under the cap passes through unchanged."""
        small = "ok\n"
        with _shell_cfg(max_output_bytes=10000), \
                patch("subprocess.run", return_value=_ok(stdout=small)):
            result = run_shell("Get-Date")
        assert result["stdout"] == small
        assert "truncated" not in result["output"]

    def test_truncation_keeps_exit_code(self):
        """Truncation doesn't lose the exit code line."""
        with _shell_cfg(max_output_bytes=100), \
                patch("subprocess.run", return_value=_ok(stdout="X" * 5000, returncode=2)):
            result = run_shell("Get-Process")
        assert "Exit code: 2" in result["output"]
        assert result["exit_code"] == 2

    def test_truncation_does_not_cut_multibyte_char(self):
        """Slicing on encoded bytes (not chars) avoids splitting a multi-byte
        CJK char in half — the cap boundary lands on errors='ignore' (the
        trailing partial char is dropped whole, not replaced with U+FFFD)."""
        cjk = "中" * 500  # 3 bytes each = 1500 bytes
        with _shell_cfg(max_output_bytes=500), \
                patch("subprocess.run", return_value=_ok(stdout=cjk)):
            result = run_shell("Get-Process")
        assert "[output truncated:" in result["output"]
        # No lone replacement chars from a half-cut CJK char.
        assert "\ufffd" not in result["stdout"]
        # Byte-budget holds even with multi-byte content (a char-based slice
        # at 500 chars would be 1500 bytes — over budget — so this is load-bearing).
        assert len(result["stdout"].encode("utf-8")) <= 500


class TestTruncateStreamsHelper:
    """Direct unit tests for the byte-budget split helper."""

    def test_stdout_priority(self):
        """stdout gets the budget first; stderr gets the remainder."""
        from agent_windows.core.shell_runner import _truncate_streams
        stdout, stderr = _truncate_streams("A" * 600, "B" * 600, budget=1000)
        assert len(stdout.encode("utf-8")) <= 1000
        assert len(stderr) < 600  # stderr was trimmed

    def test_stdout_alone_exceeds_budget_empties_stderr(self):
        from agent_windows.core.shell_runner import _truncate_streams
        stdout, stderr = _truncate_streams("A" * 2000, "B" * 100, budget=1000)
        assert len(stdout.encode("utf-8")) <= 1000
        assert stderr == ""
