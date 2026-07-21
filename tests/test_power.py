"""Tests for core/power.py — lock/sleep (ctypes) and shutdown/restart (shutdown.exe)."""
import subprocess
from unittest.mock import patch

import pytest

from agent_windows.core import power


@patch("agent_windows.core.power.ctypes")
def test_lock_success(mock_ctypes):
    mock_ctypes.windll.user32.LockWorkStation.return_value = 1
    r = power.lock_workstation()
    mock_ctypes.windll.user32.LockWorkStation.assert_called_once()
    assert r == {"action": "lock", "output": "## Lock\nWorkstation locked."}


@patch("agent_windows.core.power.ctypes")
def test_lock_failure_raises(mock_ctypes):
    mock_ctypes.windll.user32.LockWorkStation.return_value = 0
    mock_ctypes.get_last_error.return_value = 5
    with pytest.raises(RuntimeError, match="LockWorkStation failed"):
        power.lock_workstation()


@patch("agent_windows.core.power.ctypes")
def test_suspend_sleep_success(mock_ctypes):
    mock_ctypes.windll.powrprof.SetSuspendState.return_value = 1
    r = power.suspend_sleep()
    # bHibernate=False → sleep (not hibernate/S4)
    mock_ctypes.windll.powrprof.SetSuspendState.assert_called_once_with(False, False, False)
    assert r == {"action": "sleep", "output": "## Sleep\nSystem suspending (sleep)."}


@patch("agent_windows.core.power.ctypes")
def test_suspend_sleep_failure_raises(mock_ctypes):
    mock_ctypes.windll.powrprof.SetSuspendState.return_value = 0
    with pytest.raises(RuntimeError, match="SetSuspendState failed"):
        power.suspend_sleep()


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@patch("agent_windows.core.power.subprocess.run")
def test_shutdown_system_default(mock_run):
    mock_run.return_value = _completed(returncode=0)
    r = power.shutdown_system(0, False)
    mock_run.assert_called_once_with(
        ["shutdown", "/s", "/t", "0"], capture_output=True, text=True, timeout=10
    )
    assert r["action"] == "shutdown"
    assert r["delay"] == 0 and r["force"] is False
    assert "## Shutdown" in r["output"] and "Delay: 0s" in r["output"] and "Force: false" in r["output"]


@patch("agent_windows.core.power.subprocess.run")
def test_restart_system_force_and_delay(mock_run):
    mock_run.return_value = _completed(returncode=0)
    r = power.restart_system(30, True)
    mock_run.assert_called_once_with(
        ["shutdown", "/r", "/t", "30", "/f"], capture_output=True, text=True, timeout=10
    )
    assert r["action"] == "restart" and r["delay"] == 30 and r["force"] is True
    assert "Force: true" in r["output"]


@patch("agent_windows.core.power.subprocess.run")
def test_shutdown_nonzero_exit_is_transparent(mock_run):
    mock_run.return_value = _completed(returncode=119, stderr="Access denied")
    r = power.shutdown_system(0, False)
    assert r["exit_code"] == 119  # not raised
    assert "Access denied" in r["output"] and "Exit code: 119" in r["output"]


@patch("agent_windows.core.power.subprocess.run")
def test_shutdown_timeout_raises(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd=["shutdown"], timeout=10)
    with pytest.raises(TimeoutError):
        power.shutdown_system(0, False)


@patch("agent_windows.core.power.subprocess.run")
def test_shutdown_missing_exe_raises(mock_run):
    mock_run.side_effect = FileNotFoundError
    with pytest.raises(RuntimeError, match="shutdown.exe not found"):
        power.shutdown_system(0, False)


def test_max_delay_constant():
    assert power.MAX_SHUTDOWN_DELAY == 600
