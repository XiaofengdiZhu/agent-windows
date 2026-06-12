"""Tests for daemon lifecycle: PID file, version file, process detection."""
import os
import pytest
from pathlib import Path
from unittest.mock import patch

from agent_windows.daemon.lifecycle import (
    write_pid_file,
    read_pid_file,
    write_version_file,
    read_version_file,
    check_version_mismatch,
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
