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
import time
from typing import Any, Literal

logger = logging.getLogger(__name__)

# System-critical PIDs / process names that must never be terminated via this
# tool — killing them can force a system reboot or BSOD. (SEC3-04)
# Lower-cased on purpose: kill_process compares ``name.lower() in
# _CRITICAL_NAMES``, so every entry must be lowercase or the match silently
# fails (this regressed for "System"/"Registry" before the set was normalized).
_CRITICAL_PIDS = {0, 4}
_CRITICAL_NAMES = {
    n.lower() for n in (
        "lsass", "csrss", "wininit", "services", "winlogon", "smss",
        "System", "Registry", "winload", "clipup", "idle",
    )
}

# taskkill is always run as an argv list; this marks the safety review for the
# one place a user-controlled value reaches the process name position. (SEC-06)
_TRUSTED_TASKKILL = True

_SORT_KEYS = ("memory", "cpu", "name")


def _query_image_name(handle: int) -> str | None:
    """Resolve the image name (e.g. ``"lsass.exe"``) for an OPEN process handle.

    Uses :c:func:`QueryFullProcessImageNameW` (Windows Vista+, no module
    snapshot needed). Returns the basename, lower-cased, with a trailing
    ``.exe`` preserved (matching how :func:`kill_process` normalizes the name
    path). Returns ``None`` when the query fails (access denied / race). The
    caller owns ``handle`` and must close it (:func:`_close_process_handle`).

    Split out of :func:`_image_name_for_pid` so :func:`kill_process` can hold
    the handle open across taskkill — Windows won't free/reuse a PID while any
    handle to it remains, which closes the verify→kill TOCTOU window (G7).
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    # QueryFullProcessImageNameW signature:
    #   BOOL QueryFullProcessImageNameW(HANDLE, DWORD flags, LPWSTR buf, PDWORD)
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    )
    buf = ctypes.create_unicode_buffer(1024)
    size = wintypes.DWORD(len(buf))
    # flags=0 → return the image path with a drive letter (not device path).
    if not kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
        return None
    return os.path.basename(buf.value).lower()


def _close_process_handle(handle: int) -> None:
    """Best-effort :c:func:`CloseHandle` on a process handle opened by
    :func:`_open_process_handle`. Never raises — a leaked handle is preferable
    to masking the real error (kill result, raise) that the caller is surfacing.
    Module-level seam so tests can assert close ordering (G7)."""
    if not handle:
        return
    try:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        logger.debug("CloseHandle failed for process handle %r", handle, exc_info=True)


def _open_process_handle(pid: int) -> int:
    """Open the process for query+terminate under the least privilege that
    still works against protected processes a bare ``OpenProcess`` would be
    denied. Returns the handle, or ``0`` on failure (gone / access denied).
    The caller MUST close the returned handle via :func:`_close_process_handle`."""
    import ctypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    return handle or 0


def _image_name_for_pid(pid: int) -> str | None:
    """Resolve the image name (e.g. ``"lsass.exe"``) for a live PID.

    Thin convenience wrapper: open → query → close. For callers that need the
    handle held (e.g. :func:`kill_process` closing the verify→kill TOCTOU
    window, G7), use :func:`_open_process_handle` + :func:`_query_image_name`
    directly so the handle stays open across the kill.

    Returns ``None`` when the PID is gone or can't be queried (access denied /
    race). Callers must treat ``None`` as fail-closed, since a PID whose image
    we can't confirm could be a recycled critical-process PID.
    """
    handle = _open_process_handle(pid)
    if not handle:
        return None
    try:
        return _query_image_name(handle)
    finally:
        _close_process_handle(handle)


def _count_by_image(image: str) -> int:
    """Count live processes matching an image name (e.g. ``'notepad.exe'``).

    Powers process_kill's name-path recheck (P1.2): ``taskkill /IM`` kills all
    matches, so the post-op count of remaining matches tells the agent whether
    anything survived a graceful close. Returns -1 when the count itself failed
    (callers must treat that as fail-closed, not zero)."""
    try:
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {image}",
                            "/FO", "CSV", "/NH"],
                           capture_output=True, text=True, timeout=5,
                           encoding="utf-8", errors="replace")
    except Exception:
        return -1
    if r.returncode != 0:
        return -1
    # CSV data rows start with a double-quote (``"notepad.exe",...``); the
    # localized "INFO: No tasks are running..." line tasklist emits on no match
    # (rc 0) does not. Counting quoted rows is locale-independent and avoids
    # miscounting that INFO line as a survivor (C1).
    return sum(1 for ln in r.stdout.splitlines() if ln.startswith('"'))


def _open_and_verify_pid(pid: int) -> tuple[str | None, int]:
    """Open ``pid`` and resolve its image name WITHOUT closing the handle.

    Returns ``(image_name, handle)`` where ``handle`` is an open process handle
    the caller must close via :func:`_close_process_handle`, or ``(None, 0)``
    if the PID is gone / access-denied (the handle is already closed in that
    case — nothing for the caller to release).

    Holding the handle across the subsequent taskkill is the G7 TOCTOU fix:
    Windows will not free or reuse a PID while any handle to the process object
    remains, so the identity verified here is the identity taskkill will act on.
    """
    handle = _open_process_handle(pid)
    if not handle:
        return (None, 0)
    name = _query_image_name(handle)
    if name is None:
        # Query failed but the handle is open — close before reporting failure.
        _close_process_handle(handle)
        return (None, 0)
    return (name, handle)


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce a PowerShell JSON field to float, defaulting on parse failure.

    A missing/null MemMB/CpuSec (e.g. the ``System`` process where ``.CPU`` can
    be null) must sort as 0, not crash the sort or render as literal ``None``.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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
            # `or ""` would clobber a legit 0 → render blank; only replace None.
            mem_raw = p.get("MemMB")
            cpu_raw = p.get("CpuSec")
            mem_disp = "" if mem_raw is None else mem_raw
            cpu_disp = "" if cpu_raw is None else cpu_raw
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
    # verify_handle stays open across taskkill below (closed in finally) so the
    # PID can't be freed/reused between identity verification and the kill — the
    # G7 TOCTOU fix. None for the name path (no PID to pin).
    verify_handle = 0
    if pid is not None:
        if pid in _CRITICAL_PIDS:
            raise ValueError(f"Refusing to kill system-critical PID {pid}")
        if pid == os.getpid():
            raise ValueError("Refusing to kill the current process")
        # The name-based critical guard below is meaningless for a bare PID —
        # but list_processes hands the caller every PID+name, so a steered agent
        # can read lsass's/csrss's PID and call kill_process(<pid>) to slip past
        # the name check entirely (kill lsass → reboot; csrss → BSOD). Resolve
        # the PID's image name and run the SAME critical-name check. If the name
        # can't be resolved (gone/access-denied/race) fail closed: a recycled
        # critical PID is exactly what we can't confirm safe. (SEC3-04)
        # _open_and_verify_pid returns the handle OPEN — held until taskkill
        # returns so the verified identity can't be swapped under us (G7).
        image_name, verify_handle = _open_and_verify_pid(pid)
        if image_name is None:
            raise ValueError(
                f"Refusing to kill PID {pid}: image name could not be verified "
                f"(process gone or access denied)"
            )
        check_name = image_name[:-4] if image_name.endswith(".exe") else image_name
        if check_name in _CRITICAL_NAMES:
            _close_process_handle(verify_handle)
            raise ValueError(
                f"Refusing to kill system-critical process '{image_name}' (PID {pid})"
            )
    else:
        name_str = str(pid_or_name)
        # Normalize before the critical-name check: taskkill is given the
        # ``.exe``-appended image name, so a caller passing ``"lsass.exe"``
        # would otherwise slip past the bare-name guard. Strip a trailing
        # ``.exe`` and lowercase for the membership test. (SEC3-04)
        check_name = name_str.lower()
        if check_name.endswith(".exe"):
            check_name = check_name[:-4]
        if check_name in _CRITICAL_NAMES:
            raise ValueError(
                f"Refusing to kill system-critical process '{name_str}'"
            )

    # ── taskkill ────────────────────────────────────────────────────────
    # Run as an argv list: the name/PID is a literal element, so there is no
    # shell/PowerShell injection surface (SEC-06) and no quote escaping needed.
    # /F = 强杀 (TerminateProcess); without it taskkill posts WM_CLOSE (graceful).
    # The whole block runs under finally so the G7 verify handle is released on
    # every path (success, not-found raise, access-denied return, generic fail).
    try:
        if pid is not None:
            argv = ["taskkill", "/PID", str(pid)]
            label = f"PID {pid}"
        else:
            # name_str was set in the critical-name check above; reuse it.
            # taskkill /IM wants the image name with extension; our API takes
            # bare names (matching the old Stop-Process path). Append .exe if
            # absent.
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

        # rc == 0 is the authoritative success signal for taskkill
        # (ERROR_SUCCESS), independent of locale — taskkill emits localized text
        # via MUI resources, so matching "success" would misreport a kill as
        # failed on German/Japanese/… Windows. Treat rc 0 as success
        # unconditionally.
        if rc == 0:
            mode = "force-killed" if force else "terminated"
            lines = ["## Process Kill", label]
            if pid is not None:
                # Let the graceful close settle, then confirm the PID is gone.
                # _image_name_for_pid returns None when the PID no longer
                # resolves (gone) — fail-closed on any read error.
                time.sleep(0.3)
                try:
                    still = _image_name_for_pid(pid)
                except Exception:
                    still = "unknown"
                if still is None:
                    lines.append(f"Status: {mode} (confirmed gone)")
                    killed = {"confirmed_dead": True}
                else:
                    lines.append("Status: still running (graceful close "
                                 "requested; retry with force=True)")
                    killed = {"confirmed_dead": False}
            else:
                # Name path: taskkill /IM kills all matches — count survivors.
                remaining = _count_by_image(image)
                if remaining < 0:
                    # Recheck itself failed (tasklist error/timeout) — distinct
                    # from "still running": we can't confirm dead, but we don't
                    # know a survivor count either. (M1)
                    lines.append("Status: unknown (recheck failed; "
                                 "retry with force=True)")
                    killed = {"confirmed_dead": False, "recheck_failed": True}
                elif remaining == 0:
                    lines.append(f"Status: {mode} (confirmed gone)")
                    killed = {"confirmed_dead": True}
                else:
                    lines.append(f"Status: still running ({remaining} "
                                 "remaining; retry with force=True)")
                    killed = {"confirmed_dead": False, "remaining": remaining}
            return {"output": "\n".join(lines), "killed": killed}

        # No such process (covers both "not found" and "no running instance
        # of…"). These English markers are best-effort sub-classification on the
        # failure path; a non-English message simply degrades to the generic
        # "failed" tail, which is safe (we never claim success we didn't get).
        not_found_markers = ("not found", "no running instance", "not a valid")
        if any(m in combined for m in not_found_markers):
            raise ValueError(f"No such process with {label}")

        # Access denied → the process exists but we lack rights (needs admin).
        if "access" in combined and "denied" in combined:
            return {"output": f"## Process Kill\n{label}\nStatus: failed (access denied)"}

        # Anything else (e.g. a graceful close refused, child still spawning) →
        # failed, surfacing taskkill's own wording for the agent to act on.
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {rc}"
        return {"output": f"## Process Kill\n{label}\nStatus: failed ({tail})"}
    finally:
        # G7: release the verify handle now that taskkill has resolved+acted on
        # the PID. Held across taskkill above so the PID couldn't be freed/reused
        # between identity verification and the kill. No-op on the name path
        # (verify_handle == 0) and on the verify-failed/critical-name path
        # (already closed before the raise).
        _close_process_handle(verify_handle)
