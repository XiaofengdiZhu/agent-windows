"""Process management: list and kill processes."""

import ctypes
import logging
import os
import subprocess
from typing import Any

logger = logging.getLogger(__name__)


def list_processes() -> dict[str, Any]:
    """List running processes with PID and name."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object { $_.MainWindowTitle -or $_.Name } | "
             "Select-Object Id, ProcessName, @{N='Mem(MB)';E={[math]::Round($_.WorkingSet64/1MB,1)}} | "
             "Sort-Object -Property 'Mem(MB)' -Descending | "
             "Format-Table -AutoSize -HideTableHeaders"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )

        lines = ["## Process List"]
        lines.append("| PID | Name | Memory (MB) |")
        lines.append("|-----|------|-------------|")
        for line in result.stdout.strip().splitlines():
            parts = line.strip().split()
            if len(parts) >= 2:
                lines.append(f"| {' | '.join(p for p in parts[:3])} |")

        return {"output": "\n".join(lines)}
    except Exception as e:
        raise RuntimeError(f"Failed to list processes: {e}")


def kill_process(pid_or_name: str) -> dict[str, Any]:
    """Kill a process by PID or name."""
    # Try as PID first
    try:
        pid = int(pid_or_name)
        PROCESS_TERMINATE = 0x0001
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            ctypes.windll.kernel32.TerminateProcess(handle, 1)
            ctypes.windll.kernel32.CloseHandle(handle)
            return {"output": f"## Process Kill\nPID: {pid}\nStatus: terminated"}
    except ValueError:
        pass

    # Kill by name via PowerShell
    try:
        # Escape single quotes to prevent PowerShell injection
        safe_name = pid_or_name.replace("'", "''")
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"Stop-Process -Name '{safe_name}' -Force -ErrorAction SilentlyContinue; "
             f"(Get-Process -Name '{safe_name}' -ErrorAction SilentlyContinue).Count"],
            capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        remaining = result.stdout.strip()
        return {"output": f"## Process Kill\nName: {pid_or_name}\nStatus: killed"}
    except Exception as e:
        raise RuntimeError(f"Failed to kill process '{pid_or_name}': {e}")
