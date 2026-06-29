"""Daemon lifecycle management: startup, shutdown, PID file, idle timeout."""

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def write_pid_file(pid_path: Path) -> None:
    """Write current PID and start time to PID file."""
    pid_path.write_text(f"{os.getpid()}\n{time.time()}\n", encoding="utf-8")
    logger.debug("PID file written: %s (pid=%d)", pid_path, os.getpid())


def read_pid_file(pid_path: Path) -> tuple[int, float] | None:
    """Read PID and start time from PID file. Returns None if file doesn't exist or is invalid."""
    if not pid_path.exists():
        return None
    try:
        parts = pid_path.read_text(encoding="utf-8").strip().split("\n")
        return int(parts[0]), float(parts[1])
    except (ValueError, IndexError):
        return None


def is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running.

    Liveness only — does NOT defend against PID recycling. ``STILL_ACTIVE=259``
    is returned for any live process owning that PID, including a recycled one.
    Callers that recorded the original process's start time should prefer
    :func:`is_our_daemon`, which also compares ``GetProcessTimes`` creation time.
    """
    import ctypes
    import ctypes.wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        # STILL_ACTIVE = 259
        return exit_code.value == 259
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def process_create_time_secs(pid: int) -> float | None:
    """Return the process creation time (Unix-epoch seconds) or None if it
    can't be queried (gone, access denied).

    Faithful port of the Rust launcher's ``win32::process_create_time_secs``
    (``launcher/src/win32.rs:420-441``), so the Python and Rust sides compute
    identical values from the same ``GetProcessTimes`` data and a PID-recycle
    guard built on this function agrees across both entry points.

    ``GetProcessTimes`` reports FILETIME (100ns intervals since 1601-01-01 UTC);
    convert to Unix epoch seconds by subtracting the 1601→1970 offset
    (11644473600 s) and dividing by 10_000_000. The float keeps microsecond
    resolution, finer than the daemon's recorded ``time.time()`` and the
    comparison tolerance used by :func:`is_our_daemon`.
    """
    import ctypes
    import ctypes.wintypes

    EPOCH_DIFF_SECS = 11_644_473_600.0
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.wintypes.DWORD),
                    ("dwHighDateTime", ctypes.wintypes.DWORD)]

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        creation = FILETIME()
        exit_ = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        ok = kernel32.GetProcessTimes(
            handle, ctypes.byref(creation), ctypes.byref(exit_),
            ctypes.byref(kernel), ctypes.byref(user),
        )
        if not ok:
            return None
        intervals = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return intervals / 10_000_000.0 - EPOCH_DIFF_SECS
    finally:
        kernel32.CloseHandle(handle)


def is_our_daemon(pid: int, recorded_start: float | None) -> bool:
    """True if ``pid`` is still the daemon we started: alive AND its creation
    time matches the start time recorded in the PID file (within a tolerance).

    Closes the PID-recycle race that :func:`is_process_alive` alone cannot: a
    recycled PID is alive, but its ``GetProcessTimes`` creation time is the new
    process's birth, not the recorded start. Faithful port of the Rust
    launcher's ``is_our_daemon`` (``launcher/src/daemon.rs:415-430``) — same
    tolerance (5 s), same fail-closed semantics — so a recycled PID is rejected
    identically whether the daemon is launched/spawn-checked via the Rust
    launcher or the Python fallback.

    The daemon's recorded ``time.time()`` is taken after the process is already
    running (in :func:`write_pid_file`), so it can lag the kernel's creation
    time by up to a few hundred ms; the 5 s tolerance absorbs that skew plus
    cross-process clock-read imprecision. A missing ``recorded_start`` (old or
    hand-written PID file) fails closed — we can't prove identity, so we don't
    treat it as our daemon.
    """
    if recorded_start is None:
        # No start time on record → can't confirm identity. Fail closed.
        return False
    if not is_process_alive(pid):
        return False
    created = process_create_time_secs(pid)
    if created is None:
        # Couldn't query (access denied / race) → don't claim identity.
        return False
    return abs(created - recorded_start) <= 5.0


def write_version_file(version_path: Path, version: str) -> None:
    """Write current tool version to version file."""
    version_path.write_text(version, encoding="utf-8")


def read_version_file(version_path: Path) -> str | None:
    """Read version from version file."""
    if not version_path.exists():
        return None
    return version_path.read_text(encoding="utf-8").strip()


def cleanup_stale_daemon(pid_path: Path, version_path: Path | None = None) -> None:
    """Clean up stale PID file and version file if daemon is dead."""
    info = read_pid_file(pid_path)
    if info is None:
        return

    pid, recorded_start = info
    if not is_our_daemon(pid, recorded_start):
        logger.info("Stale daemon found (pid=%d, dead or recycled), cleaning up", pid)
        pid_path.unlink(missing_ok=True)
        if version_path:
            version_path.unlink(missing_ok=True)
    else:
        logger.debug("Daemon already running (pid=%d)", pid)


def check_version_mismatch(version_path: Path, current_version: str) -> bool:
    """Check if the running daemon version matches current tool version."""
    stored = read_version_file(version_path)
    if stored is None:
        return False  # No version file, don't restart
    return stored != current_version
