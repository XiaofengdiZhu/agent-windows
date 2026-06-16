"""Process management: list and kill processes."""

import ctypes
import json
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)

# System-critical PIDs / process names that must never be terminated via this
# tool — killing them can force a system reboot or BSOD. (SEC3-04)
_CRITICAL_PIDS = {0, 4}
_CRITICAL_NAMES = {
    "lsass", "csrss", "wininit", "services", "winlogon", "smss",
    "System", "Registry", "winload", "clipup", "idle",
}


def list_processes() -> dict[str, Any]:
    """List running processes with PID, name, and memory (MB)."""
    try:
        # ConvertTo-Json emits structured output we can parse robustly — unlike
        # Format-Table, it survives process names containing spaces (e.g.
        # "Microsoft Edge") and stable column ordering. (QUAL3-01, PERF3-09)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | "
             "Sort-Object -Property WorkingSet64 -Descending | "
             "Select-Object Id, ProcessName, @{N='MemMB';E={[math]::Round($_.WorkingSet64/1MB,1)}} | "
             "ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )

        stdout = result.stdout.strip()
        if not stdout:
            procs = []
        else:
            # ConvertTo-Json emits a single object (dict) for one process and an
            # array (list) for many — normalize to a list.
            parsed = json.loads(stdout)
            procs = parsed if isinstance(parsed, list) else [parsed]

        lines = ["## Process List"]
        lines.append("| PID | Name | Memory (MB) |")
        lines.append("|-----|------|-------------|")
        for p in procs:
            pid = p.get("Id")
            name = p.get("ProcessName", "")
            mem = p.get("MemMB", "")
            if pid is None or not name:
                continue
            lines.append(f"| {pid} | {name} | {mem} |")

        return {"output": "\n".join(lines)}
    except Exception as e:
        raise RuntimeError(f"Failed to list processes: {e}") from e


def kill_process(pid_or_name: str) -> dict[str, Any]:
    """Kill a process by PID or name.

    Refuses system-critical processes (PID 0/4 and a name allowlist) to avoid
    forcing a reboot/BSOD. (SEC3-04)
    """
    # Try as PID first
    try:
        pid = int(pid_or_name)
    except ValueError:
        pid = None

    if pid is not None:
        if pid in _CRITICAL_PIDS:
            raise ValueError(f"Refusing to kill system-critical PID {pid}")
        if pid == os.getpid():
            raise ValueError("Refusing to kill the current process")
        PROCESS_TERMINATE = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
            return {"output": f"## Process Kill\nPID: {pid}\nStatus: terminated"}
        # OpenProcess returned 0 — no such process or no access. Surface that
        # instead of silently falling through to the name-based path. (QUAL3-02)
        raise ValueError(f"No such process with PID {pid}")

    # Kill by name via PowerShell
    # Escape single quotes to prevent PowerShell injection
    safe_name = pid_or_name.replace("'", "''")
    if safe_name.lower() in _CRITICAL_NAMES:
        raise ValueError(f"Refusing to kill system-critical process '{pid_or_name}'")
    try:
        # Two-phase query so we can tell three outcomes apart, which a single
        # post-kill count cannot: (QUAL3-02)
        #   pre == 0          → no such process (was never alive)
        #   post == 0         → killed successfully
        #   post > 0          → kill failed (still running, e.g. auto-restarting)
        # Note: PowerShell's $null.Count evaluates to 0 (not blank/$null), so a
        # missing name yields pre == "0" — distinct from a name that existed.
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"$pre = (Get-Process -Name '{safe_name}' -ErrorAction SilentlyContinue).Count; "
             f"if ($pre) {{ Stop-Process -Name '{safe_name}' -Force -ErrorAction SilentlyContinue }}; "
             f"\"$pre|$((Get-Process -Name '{safe_name}' -ErrorAction SilentlyContinue).Count)\""],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        out = result.stdout.strip()
        # Expect "pre|post", e.g. "1|0" (existed→killed), "0|0" (never existed),
        # "1|1" (kill failed). Fall back defensively if parsing fails.
        try:
            pre_str, post_str = out.split("|")
            pre, post = int(pre_str), int(post_str)
        except (ValueError, AttributeError):
            raise RuntimeError(f"Unexpected kill-process output: {out!r}") from None

        if pre == 0:
            raise ValueError(f"No such process with name '{pid_or_name}'")
        if post == 0:
            return {"output": f"## Process Kill\nName: {pid_or_name}\nStatus: killed"}
        return {"output": f"## Process Kill\nName: {pid_or_name}\nStatus: failed ({post} still running)"}
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to kill process '{pid_or_name}': {e}") from e
