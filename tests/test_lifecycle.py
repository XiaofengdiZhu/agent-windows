"""Tests for daemon lifecycle: PID file, version file, process detection."""
import os
import time
import pytest
from pathlib import Path
from unittest.mock import patch

from agent_windows.daemon.lifecycle import (
    write_pid_file,
    read_pid_file,
    write_version_file,
    read_version_file,
    check_version_mismatch,
    cleanup_stale_daemon,
    is_our_daemon,
    is_process_alive,
    process_create_time_secs,
)


class TestPidFile:
    def test_write_and_read(self, tmp_path):
        pid_path = tmp_path / "daemon.pid"
        write_pid_file(pid_path)
        result = read_pid_file(pid_path)
        assert result is not None
        pid, start_time = result
        assert pid == os.getpid()
        assert isinstance(start_time, float)

    def test_read_nonexistent_returns_none(self, tmp_path):
        result = read_pid_file(tmp_path / "nonexistent.pid")
        assert result is None

    def test_read_corrupt_file_returns_none(self, tmp_path):
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("corrupt\n", encoding="utf-8")
        result = read_pid_file(pid_path)
        assert result is None

    def test_read_partial_file_returns_none(self, tmp_path):
        """File with only PID but no timestamp should return None."""
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("12345\n", encoding="utf-8")
        result = read_pid_file(pid_path)
        assert result is None

    def test_write_overwrites_existing(self, tmp_path):
        """Writing should overwrite any existing PID file."""
        pid_path = tmp_path / "daemon.pid"
        pid_path.write_text("99999\n0.0\n", encoding="utf-8")
        write_pid_file(pid_path)
        pid, _ = read_pid_file(pid_path)
        assert pid == os.getpid()


class TestVersionFile:
    def test_write_and_read(self, tmp_path):
        path = tmp_path / "daemon.version"
        write_version_file(path, "1.0.0")
        assert read_version_file(path) == "1.0.0"

    def test_read_nonexistent_returns_none(self, tmp_path):
        assert read_version_file(tmp_path / "nonexistent") is None

    def test_write_and_read_another_version(self, tmp_path):
        path = tmp_path / "daemon.version"
        write_version_file(path, "0.1.0")
        assert read_version_file(path) == "0.1.0"

    def test_write_strips_whitespace(self, tmp_path):
        """Written version should be readable back (no extra whitespace)."""
        path = tmp_path / "daemon.version"
        write_version_file(path, "2.3.4")
        raw = path.read_text(encoding="utf-8")
        assert raw == "2.3.4"

    def test_read_strips_whitespace(self, tmp_path):
        """read_version_file should strip whitespace."""
        path = tmp_path / "daemon.version"
        path.write_text("  1.2.3  \n", encoding="utf-8")
        assert read_version_file(path) == "1.2.3"


class TestVersionMismatch:
    def test_mismatch_returns_true(self, tmp_path):
        path = tmp_path / "daemon.version"
        write_version_file(path, "0.0.1")
        assert check_version_mismatch(path, "0.1.0") is True

    def test_match_returns_false(self, tmp_path):
        path = tmp_path / "daemon.version"
        write_version_file(path, "1.0.0")
        assert check_version_mismatch(path, "1.0.0") is False

    def test_no_version_file_returns_false(self, tmp_path):
        assert check_version_mismatch(tmp_path / "nonexistent", "1.0.0") is False

    def test_same_version_string_returns_false(self, tmp_path):
        path = tmp_path / "daemon.version"
        write_version_file(path, "0.5.0")
        assert check_version_mismatch(path, "0.5.0") is False


class TestProcessIdentity:
    """is_our_daemon defeats PID recycling by comparing GetProcessTimes creation
    time against the start time recorded in the PID file — the Python port of the
    Rust launcher's is_our_daemon (launcher/src/daemon.rs:415-430).

    These tests run against the CURRENT process (os.getpid()), which is
    guaranteed alive and queryable, so the assertions are deterministic with no
    need to spawn or kill anything. They exercise the contract directly:

      - alive + creation time within 5 s tolerance → True
      - creation time far off → False (recycled PID, the bug this fixes)
      - missing recorded_start → False (fail closed, like the Rust side)
      - nonexistent PID → False (OpenProcess returns 0)
    """

    def test_real_create_time_returns_a_unix_seconds_float(self):
        created = process_create_time_secs(os.getpid())
        assert created is not None
        # Sanity: this process was created within the last hour, so it's a
        # plausible Unix epoch seconds value (not 1970, not the year 2200).
        now = time.time()
        assert now - 3600 < created <= now

    def test_our_daemon_matches_with_real_creation_time(self):
        created = process_create_time_secs(os.getpid())
        assert is_our_daemon(os.getpid(), created) is True

    def test_our_daemon_accepts_tolerance_skew(self):
        # The daemon writes time.time() after the process is already running, so
        # recorded_start may lag the kernel's creation time. A 3 s skew (well
        # inside the 5 s tolerance) must still be accepted.
        created = process_create_time_secs(os.getpid())
        assert is_our_daemon(os.getpid(), created - 3) is True

    def test_our_daemon_rejects_far_off_creation_time(self):
        # A creation time 100 s off the recorded start means the PID is a
        # recycled different process — must be rejected.
        created = process_create_time_secs(os.getpid())
        assert is_our_daemon(os.getpid(), created + 100) is False

    def test_our_daemon_rejects_missing_recorded_start(self):
        # No start time on record (old/hand-written PID file) → can't prove
        # identity → fail closed, matching the Rust force-kill path.
        assert is_our_daemon(os.getpid(), None) is False

    def test_our_daemon_rejects_nonexistent_pid(self):
        # A PID that doesn't exist can't be ours. 99999999 is well above the
        # typical PID range on Windows and OpenProcess returns 0 for it.
        import time
        assert is_our_daemon(99999999, time.time()) is False

    def test_our_daemon_rejects_zero_pid(self):
        # PID 0 (System Idle Process) is not a user process we'd launch; even
        # though it's technically queryable, it can't be our daemon.
        assert is_our_daemon(0, time.time()) is False

    def test_process_create_time_secs_returns_none_for_gone_pid(self):
        # Same nonexistent PID as above: OpenProcess returns 0 → None.
        assert process_create_time_secs(99999999) is None

    def test_is_process_alive_still_works_as_primitive(self):
        # is_process_alive is retained as the underlying liveness primitive
        # (is_our_daemon composes it); verify it still reports the current
        # process as alive so the layered design isn't accidentally broken.
        assert is_process_alive(os.getpid()) is True


class TestCleanupStaleDaemon:
    """cleanup_stale_daemon must distinguish 'confirmed dead' from 'can't tell'.

    is_our_daemon fails CLOSED: when a process is alive but its creation time
    can't be queried (protected process / PPL / transient GetProcessTimes
    failure), or recorded_start is missing, it returns False because identity
    can't be confirmed. That is the correct call for an identity check.

    But cleanup_stale_daemon previously treated *every* is_our_daemon=False as
    'confirmed stale' and unlinked the PID file. For a LIVE-but-unqueryable
    process that is wrong: deleting the PID file makes _daemon_process_alive()
    return False (no file), so during the pipe-recreate window
    ensure_daemon_running spawns a rival daemon — the loser fails
    FIRST_PIPE_INSTANCE and leaks as an orphan python.exe (the exact leak
    client._daemon_process_alive's docstring warns about).

    The fix layers a second, read-only check: only delete when the process is
    confirmed dead (is_process_alive False). A live process — even one whose
    daemon identity we can't confirm — keeps its PID file.
    """

    def _write_pid_file(self, pid_path, pid, start_time):
        pid_path.write_text(f"{pid}\n{start_time}\n", encoding="utf-8")

    def test_live_but_unconfirmable_process_keeps_pid_file(self, tmp_path):
        """F9: is_our_daemon=False because identity is unconfirmable (alive but
        creation time unqueryable), yet is_process_alive=True. The process is
        LIVE — deleting its PID file would make _daemon_process_alive() return
        False and let a rival spawn during the pipe-recreate window. Must keep
        the file."""
        import agent_windows.daemon.lifecycle as lc

        pid_path = tmp_path / "daemon.pid"
        version_path = tmp_path / "daemon.version"
        self._write_pid_file(pid_path, 4242, time.time())
        version_path.write_text("1.0.0", encoding="utf-8")

        with patch.object(lc, "is_our_daemon", return_value=False), \
             patch.object(lc, "is_process_alive", return_value=True):
            cleanup_stale_daemon(pid_path, version_path)

        assert pid_path.exists(), \
            "PID file of a live-but-unconfirmable process was deleted (would enable rival-spawn leak)"
        assert version_path.exists()

    def test_confirmed_dead_process_is_cleaned_up(self, tmp_path):
        """When the process is genuinely dead (is_process_alive False), the PID
        file and version file must still be cleaned up — the live-but-unqueryable
        guard must not regress the normal stale-cleanup path."""
        import agent_windows.daemon.lifecycle as lc

        pid_path = tmp_path / "daemon.pid"
        version_path = tmp_path / "daemon.version"
        self._write_pid_file(pid_path, 4242, time.time())
        version_path.write_text("1.0.0", encoding="utf-8")

        with patch.object(lc, "is_our_daemon", return_value=False), \
             patch.object(lc, "is_process_alive", return_value=False):
            cleanup_stale_daemon(pid_path, version_path)

        assert not pid_path.exists(), "dead process PID file was not cleaned up"
        assert not version_path.exists()

    def test_our_daemon_running_keeps_pid_file(self, tmp_path):
        """When is_our_daemon confirms the daemon is ours and running, neither
        file is touched (the existing happy path — guards against the new
        liveness check accidentally widening the delete condition)."""
        import agent_windows.daemon.lifecycle as lc

        pid_path = tmp_path / "daemon.pid"
        version_path = tmp_path / "daemon.version"
        self._write_pid_file(pid_path, 4242, time.time())
        version_path.write_text("1.0.0", encoding="utf-8")

        with patch.object(lc, "is_our_daemon", return_value=True), \
             patch.object(lc, "is_process_alive", return_value=True) as alive:
            cleanup_stale_daemon(pid_path, version_path)
        # When identity is confirmed, the liveness second-check is not needed.
        alive.assert_not_called()
        assert pid_path.exists()
        assert version_path.exists()

    def test_missing_pid_file_is_noop(self, tmp_path):
        """No PID file → nothing to clean (read_pid_file returns None early).
        Sanity guard on the early return."""
        pid_path = tmp_path / "nonexistent.pid"
        cleanup_stale_daemon(pid_path, tmp_path / "daemon.version")
        assert not pid_path.exists()
