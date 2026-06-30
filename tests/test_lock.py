"""Tests for the cross-process DaemonSpawnLock (LockFileEx-based)."""

import threading
import time

import pytest

from agent_windows.ipc.lock import DaemonSpawnLock


class TestDaemonSpawnLock:
    def test_acquire_release_roundtrip(self, tmp_path):
        """Basic acquire + release via the context manager. No exception, lock
        object reports acquired=True inside the block."""
        lock_file = tmp_path / "daemon.lock"
        with DaemonSpawnLock(lock_file) as lock:
            assert lock._acquired is True
        # After exit, state is cleared.
        assert lock._acquired is False

    def test_blocks_concurrent_acquirer_until_release(self, tmp_path):
        """A second acquirer must block in the kernel until the first releases.
        This is the whole point of the lock — without blocking it wouldn't
        serialize concurrent spawners."""
        lock_file = tmp_path / "daemon.lock"
        release_time = {"held_for": 0.0}

        def hold_then_release():
            with DaemonSpawnLock(lock_file):
                time.sleep(0.4)
                release_time["held_for"] = 0.4

        t = threading.Thread(target=hold_then_release)
        t.start()
        # Let the holder grab the lock first.
        time.sleep(0.1)

        start = time.monotonic()
        with DaemonSpawnLock(lock_file):
            elapsed = time.monotonic() - start
        t.join(timeout=2.0)

        # The second acquire should have blocked ~0.3s (0.4 hold − 0.1 head
        # start). Use a loose lower bound to avoid flakiness on slow CI; the
        # key assertion is that it blocked at all, not the exact duration.
        assert elapsed > 0.15, (
            f"second acquirer returned in {elapsed:.3f}s — it did not block on "
            "the held lock (LockFileEx exclusion is broken)"
        )

    def test_releases_on_exception(self, tmp_path):
        """If the body raises, __exit__ must still release the lock so a
        crashed spawner doesn't wedge every subsequent command."""
        lock_file = tmp_path / "daemon.lock"

        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            with DaemonSpawnLock(lock_file):
                raise Boom()

        # A fresh acquirer should get the lock immediately (no blocking).
        start = time.monotonic()
        with DaemonSpawnLock(lock_file):
            elapsed = time.monotonic() - start
        assert elapsed < 0.5, "lock was not released after an exception in the body"

    def test_failure_to_open_is_fail_open(self, tmp_path):
        """If the lock file can't be opened (e.g. directory doesn't exist),
        DaemonSpawnLock should proceed with _acquired=False rather than raising
        — orphaned daemons are a resource leak, not a correctness issue, so the
        CLI must stay responsive."""
        # A path whose parent doesn't exist → CreateFileW fails.
        bogus_path = tmp_path / "nonexistent_dir" / "daemon.lock"
        with DaemonSpawnLock(bogus_path) as lock:
            assert lock._acquired is False, (
                "lock should fail-open (acquired=False) when the file can't be "
                "opened, not pretend it holds the lock"
            )

    def test_close_runs_even_if_unlock_raises(self, tmp_path, monkeypatch):
        """G1: __exit__ must close the handle even when UnlockFileEx raises.

        LockFileEx byte ranges are only released on handle close / process
        death (no abandon), so a failing unlock that also skipped CloseHandle
        would orphan the handle for the process's lifetime AND wedge every
        subsequent CLI spawner in LockFileEx. The fix closes the handle
        unconditionally and attempts unlock independently of close.
        """
        import win32file

        lock_file = tmp_path / "daemon.lock"
        lock = DaemonSpawnLock(lock_file)
        lock.__enter__()
        recorded_handle = lock._handle
        assert recorded_handle is not None

        # Force the unlock to raise during __exit__. CloseHandle must still run.
        closed_handles = []
        real_close = win32file.CloseHandle

        def spy_close(h):
            closed_handles.append(h)
            return real_close(h)

        monkeypatch.setattr(
            win32file, "UnlockFileEx",
            lambda *a, **k: (_ for _ in ()).throw(OSError("unlock boom")),
        )
        monkeypatch.setattr(win32file, "CloseHandle", spy_close)

        lock.__exit__(None, None, None)

        # The handle recorded at __enter__ must have been closed despite the
        # UnlockFileEx failure (the bug: CloseHandle sat after the raise).
        assert recorded_handle in closed_handles, (
            "CloseHandle was skipped when UnlockFileEx raised — handle + lock "
            "byte range leaked"
        )

        # And the byte range was actually released (close happened), so a fresh
        # acquirer is NOT blocked. Restore CloseHandle for this check.
        monkeypatch.setattr(win32file, "CloseHandle", real_close)
        start = time.monotonic()
        with DaemonSpawnLock(lock_file):
            elapsed = time.monotonic() - start
        assert elapsed < 0.5, (
            "lock byte range was not released after the unlock failure — "
            "subsequent CLI spawners would wedge in LockFileEx"
        )
