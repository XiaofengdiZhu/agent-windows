"""Process management: list and kill processes.

Listing supports ``sort_by`` (memory/cpu/name), ``limit`` (top-N), and a
``name`` substring filter. Killing is **graceful by default** (taskkill posts
``WM_CLOSE`` so the app can save) and only 强杀 (``/F``) when ``force=True``.

Both PowerShell listing and taskkill run as argv lists (never ``shell=True``),
so a process name is a literal argv element — no shell/PowerShell interpolation.
"""
import json
import logging
import os
import subprocess
from typing import Any, Literal

logger = logging.getLogger(__name__)

# System-critical PIDs / process names that must never be terminated via this
# tool — killing them can force a system reboot or BSOD. (SEC3-04)
_CRITICAL_PIDS = {0, 4}
_CRITICAL_NAMES = {
    "lsass", "csrss", "wininit", "services", "winlogon", "smss",
    "System", "Registry", "winload", "clipup", "idle",
}

# taskkill is always run as an argv list; this marks the safety review for the
# one place a user-controlled value reaches the process name position. (SEC3-06)
_TRUSTED_TASKKILL = True

_SORT_KEYS = ("memory", "cpu", "name")


def list_processes(
    sort_by: Literal["memory", "cpu", "name"] = "memory",
    limit: int = 0,
    name: str | None = None,
) -> dict[str, Any]:
    """List running processes with PID, name, memory (MB), and CPU (s).

    ``sort_by`` selects the sort key (memory/cpu descending, name ascending);
    ``limit`` caps the row count (0 = all); ``name`` filters to processes whose
    name *contains* it, case-insensitively (a dependency-free substitute for the
    reference project's fuzzy match). All optional — bare ``list_processes()``
    returns every process sorted by memory, as before.
    """
    if sort_by not in _SORT_KEYS:
        raise ValueError(
            f"sort_by must be one of {_SORT_KEYS}, got {sort_by!r}"
        )
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
        raise ValueError(f"limit must be a non-negative integer, got {limit!r}")

    try:
        # ConvertTo-Json emits structured output we can parse robustly — unlike
        # Format-Table, it survives process names containing spaces (e.g.
        # "Microsoft Edge") and stable column ordering. We fetch rows unsorted
        # and do sort/filter/limit in Python so they're unit-testable without
        # scripting PowerShell. (QUAL3-01, PERF3-09)
        #
        # .CPU is total CPU seconds consumed (lifetime) — an honest signal for
        # CPU-heavy processes, cheaper and more stable than sampling cpu_percent.
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | "
             "Select-Object Id, ProcessName, "
             "@{N='MemMB';E={[math]::Round($_.WorkingSet64/1MB,1)}}, "
             "@{N='CpuSec';E={[math]::Round($_.CPU,1)}} | "
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

        # Normalize each row. The display value (mem/cpu) is kept as PowerShell
        # emitted it (e.g. "12.3" or "10"); a separate numeric key drives sorting
        # so a missing/null field sorts as 0 instead of crashing. (QUAL3-01)
        cleaned = []
        for p in procs:
            pid = p.get("Id")
            pname = p.get("ProcessName", "")
            if pid is None or not pname:
                continue  # skip malformed rows (QUAL3-01)
            mem_disp = p.get("MemMB", "")
            cpu_disp = p.get("CpuSec", "")

            def _num(v, default=0.0):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return default

            cleaned.append({
                "pid": pid, "name": pname,
                "mem_disp": mem_disp, "cpu_disp": cpu_disp,
                "mem_key": _num(mem_disp), "cpu_key": _num(cpu_disp),
            })

        # name filter: case-insensitive substring (dependency-free; not fuzzy).
        if name:
            needle = name.lower()
            cleaned = [p for p in cleaned if needle in p["name"].lower()]

        # sort: memory/cpu descending (biggest first), name ascending (A→Z).
        if sort_by == "name":
            cleaned.sort(key=lambda p: p["name"].lower())
        elif sort_by == "cpu":
            cleaned.sort(key=lambda p: p["cpu_key"], reverse=True)
        else:  # memory
            cleaned.sort(key=lambda p: p["mem_key"], reverse=True)

        if limit:
            cleaned = cleaned[:limit]

        lines = ["## Process List"]
        lines.append("| PID | Name | Memory (MB) | CPU (s) |")
        lines.append("|-----|------|-------------|---------|")
        for p in cleaned:
            lines.append(
                f"| {p['pid']} | {p['name']} | {p['mem_disp']} | {p['cpu_disp']} |"
            )

        return {"output": "\n".join(lines)}
    except ValueError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to list processes: {e}") from e


def kill_process(pid_or_name: str | int, force: bool = False) -> dict[str, Any]:
    """Kill a process by PID (int) or name (str).

    Graceful by default (``taskkill`` posts ``WM_CLOSE`` so the app can save);
    ``force=True`` 强杀 via ``/F`` (immediate ``TerminateProcess``). Refuses
    system-critical processes (PID 0/4 and a name allowlist) to avoid forcing a
    reboot/BSOD. (SEC3-04)
    """
    # Try as PID first
    pid: int | None
    if isinstance(pid_or_name, bool):
        # bool is an int subclass but never a meaningful PID — reject explicitly.
        raise ValueError("pid_or_name must be a PID (int) or name (str)")
    try:
        pid = int(pid_or_name)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        pid = None

    # ── Critical-process protection (runs before any taskkill) ──────────
    if pid is not None:
        if pid in _CRITICAL_PIDS:
            raise ValueError(f"Refusing to kill system-critical PID {pid}")
        if pid == os.getpid():
            raise ValueError("Refusing to kill the current process")
    else:
        name_str = str(pid_or_name)
        if name_str.lower() in _CRITICAL_NAMES:
            raise ValueError(
                f"Refusing to kill system-critical process '{name_str}'"
            )

    # ── taskkill ────────────────────────────────────────────────────────
    # Run as an argv list: the name/PID is a literal element, so there is no
    # shell/PowerShell injection surface (SEC-06) and no quote escaping needed.
    # /F = 强杀 (TerminateProcess); without it taskkill posts WM_CLOSE (graceful).
    if pid is not None:
        argv = ["taskkill", "/PID", str(pid)]
        label = f"PID {pid}"
    else:
        name_str = str(pid_or_name)
        # taskkill /IM wants the image name with extension; our API takes bare
        # names (matching the old Stop-Process path). Append .exe if absent.
        image = name_str if name_str.lower().endswith(".exe") else f"{name_str}.exe"
        argv = ["taskkill", "/IM", image]
        label = f"name '{name_str}'"

    if force:
        argv.append("/F")

    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
    except Exception as e:
        raise RuntimeError(f"Failed to kill process {label}: {e}") from e

    rc = result.returncode
    combined = f"{result.stdout}\n{result.stderr}".lower()

    # taskkill exit codes: 0 success; 128 "not found"/"no running instance";
    # 1 "access denied" / generic failure. Message text is the reliable signal.
    if rc == 0 and "success" in combined:
        mode = "force-killed" if force else "terminated"
        return {"output": f"## Process Kill\n{label}\nStatus: {mode}"}

    # No such process (covers both "not found" and "no running instance of…").
    not_found_markers = ("not found", "no running instance", "not a valid")
    if any(m in combined for m in not_found_markers):
        raise ValueError(f"No such process with {label}")

    # Access denied → the process exists but we lack rights (often needs admin).
    if "access" in combined and "denied" in combined:
        return {"output": f"## Process Kill\n{label}\nStatus: failed (access denied)"}

    # Anything else (e.g. a graceful close refused, child still spawning) →
    # failed, surfacing taskkill's own wording for the agent to act on.
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else f"exit code {rc}"
    return {"output": f"## Process Kill\n{label}\nStatus: failed ({tail})"}
