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
    """Check if a process with the given PID is still running."""
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

    pid, _ = info
    if not is_process_alive(pid):
        logger.info("Stale daemon found (pid=%d, dead), cleaning up", pid)
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
