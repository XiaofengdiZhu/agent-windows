"""Power operations: lock / sleep (ctypes) and shutdown / restart (shutdown.exe).

lock and sleep call Win32 APIs directly (no privilege needed). shutdown and
restart shell out to ``shutdown.exe`` (system-supplied privilege handling +
countdown UI + ``/t`` scheduling) and bypass ``run_shell`` deliberately — a
power op is not user PowerShell, so it must not be logged to
``shell_audit.log`` nor blocked by ``shell.blacklist``.
"""

from __future__ import annotations

import ctypes
import subprocess
from typing import Any

# Agent-friendly ceiling on the --delay schedule. shutdown.exe accepts far
# larger values, but anything past this has no agent use case and guards
# against fat-fingered inputs. Defined once; the handler imports it.
MAX_SHUTDOWN_DELAY = 600


def lock_workstation() -> dict[str, Any]:
    """Lock the workstation (Win+L). ``LockWorkStation`` is asynchronous — it
    returns immediately and the lock screen engages in the foreground. No
    privilege required."""
    ok = ctypes.windll.user32.LockWorkStation()
    if not ok:
        raise RuntimeError(
            f"LockWorkStation failed (GetLastError={ctypes.get_last_error()})"
        )
    return {"action": "lock", "output": "## Lock\nWorkstation locked."}


def suspend_sleep() -> dict[str, Any]:
    """Suspend the system to S3 sleep. ``SetSuspendState(bHibernate, bForce,
    bWakeDisable)`` with ``bHibernate=False`` sleeps (not hibernates/S4) even
    when hibernation is enabled. No privilege required."""
    ok = ctypes.windll.powrprof.SetSuspendState(False, False, False)
    if not ok:
        raise RuntimeError(
            f"SetSuspendState failed (GetLastError={ctypes.get_last_error()})"
        )
    return {"action": "sleep", "output": "## Sleep\nSystem suspending (sleep)."}


def _run_power(flag: str, delay: int, force: bool) -> dict[str, Any]:
    """Run ``shutdown.exe`` with ``/s`` (shutdown) or ``/r`` (restart).

    ``shutdown.exe /t N`` returns immediately after scheduling the action, so
    the daemon worker is not blocked. A non-zero exit is transparent (not
    raised) — the caller reads ``exit_code`` + ``stderr``.
    """
    argv = ["shutdown", flag, "/t", str(delay)]
    if force:
        argv.append("/f")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise RuntimeError("shutdown.exe not found")
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"shutdown.exe did not return within 10s: {argv}")
    action = "shutdown" if flag == "/s" else "restart"
    parts = [
        f"## {action.capitalize()}",
        f"Delay: {delay}s",
        f"Force: {str(force).lower()}",
    ]
    if proc.stderr.strip():
        parts.append(f"[stderr]\n{proc.stderr.strip()}")
    parts.append(f"Exit code: {proc.returncode}")
    return {
        "action": action,
        "delay": delay,
        "force": force,
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "output": "\n".join(parts),
    }


def shutdown_system(delay: int, force: bool) -> dict[str, Any]:
    """Shut down the system after ``delay`` seconds (0 = immediate)."""
    return _run_power("/s", delay, force)


def restart_system(delay: int, force: bool) -> dict[str, Any]:
    """Restart the system after ``delay`` seconds (0 = immediate)."""
    return _run_power("/r", delay, force)
