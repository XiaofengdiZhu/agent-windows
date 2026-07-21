"""Handler tests for lock/sleep/shutdown/restart."""
from unittest.mock import patch

import pytest

from agent_windows.daemon.router import HANDLERS

_RESULT = {"action": "shutdown", "delay": 0, "force": False, "exit_code": 0,
           "stdout": "", "stderr": "", "output": "## Shutdown\n..."}


def test_lock_handler_forwards():
    with patch("agent_windows.core.power.lock_workstation",
               return_value={"action": "lock", "output": "## Lock\nWorkstation locked."}) as m:
        r = HANDLERS["lock"]({})
    m.assert_called_once_with()
    assert r["action"] == "lock"


def test_sleep_handler_forwards():
    with patch("agent_windows.core.power.suspend_sleep",
               return_value={"action": "sleep", "output": "## Sleep\n..."}) as m:
        r = HANDLERS["sleep"]({})
    m.assert_called_once_with()
    assert r["action"] == "sleep"


def test_shutdown_handler_forwards_delay_and_force():
    with patch("agent_windows.core.power.shutdown_system", return_value=_RESULT) as m:
        r = HANDLERS["shutdown"]({"delay": 30, "force": True})
    m.assert_called_once_with(30, True)
    assert r["action"] == "shutdown"


def test_restart_handler_defaults_delay_zero_force_false():
    _restart_result = _RESULT.copy()
    _restart_result["action"] = "restart"
    with patch("agent_windows.core.power.restart_system", return_value=_restart_result) as m:
        r = HANDLERS["restart"]({})
    m.assert_called_once_with(0, False)
    assert r["action"] == "restart"


@pytest.mark.parametrize("delay", [601, -1, "5", 1.5, True])
def test_shutdown_bad_delay_invalid_params(delay):
    with pytest.raises(ValueError):
        HANDLERS["shutdown"]({"delay": delay})


def test_restart_bad_delay_invalid_params():
    with pytest.raises(ValueError):
        HANDLERS["restart"]({"delay": 601})
