"""Cross-process lock for serializing daemon spawns.

Both the Python client and the Rust launcher acquire this lock around their
check-then-spawn critical section, so concurrent CLI invocations can't each
observe "daemon down" and spawn a rival that loses the single-instance pipe
race and leaks. The lock is a kernel-level ``LockFileEx`` on
``~/.agent-windows/daemon.lock`` — language-agnostic, so a Rust launcher
spawner and a Python ``ensure_daemon_running`` spawner correctly exclude each
other.

The lock is held only across the spawn decision + wait-for-pipe window
(seconds at most), not across the entire daemon lifetime.
"""

import logging
from pathlib import Path

from agent_windows.utils.config import get_lock_path

logger = logging.getLogger(__name__)

# LockFileEx flags. pywin32 doesn't expose these as named constants.
_LOCKFILE_EXCLUSIVE_LOCK = 0x00000002


class DaemonSpawnLock:
    """Exclusive cross-process lock held while deciding to spawn / spawning.

    Used as a context manager::

        with DaemonSpawnLock():
            if pipe_exists() or _daemon_process_alive():
                return
            spawn_and_wait()

    Blocks (in the kernel, no CPU spin) until any other holder — Python or
    Rust — releases. The lock is re-entrant from the same *file handle*: each
    ``__enter__`` opens its own handle, so nested acquisitions within one
    process DO block each other (don't nest). There's no timeout: a stuck
    holder would wedge every subsequent CLI command, so we rely on the critical
    section being short (spawn + pipe-ready wait, bounded by
    ``MAX_CONNECT_RETRIES``).

    Failures to acquire the lock are non-fatal: we log and proceed without it.
    This preserves the original (racy) behavior rather than blocking every
    command if the lock file becomes inaccessible — orphaned daemons are a
    resource leak, not a correctness issue, so failing open is the safer choice
    for a CLI that must stay responsive.
    """

    def __init__(self, lock_path: Path | None = None):
        self._lock_path = lock_path or get_lock_path()
        self._handle = None
        self._overlapped = None
        self._acquired = False

    def __enter__(self) -> "DaemonSpawnLock":
        try:
            import win32file
            import pywintypes

            # Create the lock file if it doesn't exist (OPEN_ALWAYS). Must be
            # shareable for read/write so other processes can open it too.
            self._handle = win32file.CreateFileW(
                str(self._lock_path),
                win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                win32file.FILE_SHARE_READ | win32file.FILE_SHARE_WRITE,
                None,
                win32file.OPEN_ALWAYS,
                win32file.FILE_ATTRIBUTE_NORMAL,
                None,
            )
            # LockFileEx(handle, flags, reserved, numBytesLow, overlapped).
            # Lock the low 32 bits of byte offset 0 (a 1-byte region is enough
            # for mutual exclusion; 0xFFFF0000 is the conventional "whole file"
            # length used by the SMB/Windows samples). Blocks until acquired.
            self._overlapped = pywintypes.OVERLAPPED()
            self._overlapped.Offset = 0
            self._overlapped.OffsetHigh = 0
            win32file.LockFileEx(
                self._handle,
                _LOCKFILE_EXCLUSIVE_LOCK,
                0,
                0xFFFF0000,
                self._overlapped,
            )
            self._acquired = True
            logger.debug("Acquired daemon spawn lock: %s", self._lock_path)
        except Exception:
            logger.warning(
                "Failed to acquire daemon spawn lock (%s); proceeding without it",
                self._lock_path,
                exc_info=True,
            )
            self._acquired = False
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if not self._acquired:
            return
        try:
            import win32file

            win32file.UnlockFileEx(
                self._handle, 0, 0xFFFF0000, self._overlapped
            )
            win32file.CloseHandle(self._handle)
            logger.debug("Released daemon spawn lock: %s", self._lock_path)
        except Exception:
            logger.warning(
                "Failed to release daemon spawn lock", exc_info=True
            )
        finally:
            self._handle = None
            self._overlapped = None
            self._acquired = False
