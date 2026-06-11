"""Tests for screenshot auto-cleanup: age-based, size-based, interval check."""

import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agent_windows.core.screenshot import cleanup_screenshots, clean_old_screenshots


@pytest.fixture(autouse=True)
def _reset_cleanup_time():
    """Reset module-level _last_cleanup_time before each test."""
    import agent_windows.core.screenshot as ss_mod
    ss_mod._last_cleanup_time = 0.0
    yield


def _create_file(directory: Path, name: str, size: int = 1024, age_days: float = 0) -> Path:
    """Create a dummy file with given size and modification age."""
    path = directory / name
    path.write_bytes(b"\x00" * size)
    if age_days > 0:
        # Set mtime to the past
        old_time = time.time() - age_days * 86400
        os.utime(path, (old_time, old_time))
    return path


class TestCleanupByAge:
    def test_deletes_old_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(Path(tmpdir), "old.png", age_days=10)
            _create_file(Path(tmpdir), "new.png", age_days=1)

            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 0, "cleanup_interval_minutes": 0}
                 }):
                result = cleanup_screenshots(force=True)

            assert result["deleted_by_age"] == 1
            assert result["total_kept"] == 1
            assert not (Path(tmpdir) / "old.png").exists()
            assert (Path(tmpdir) / "new.png").exists()

    def test_zero_days_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(Path(tmpdir), "old.png", age_days=365)

            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 0, "max_screenshot_dir_mb": 0, "cleanup_interval_minutes": 0}
                 }):
                result = cleanup_screenshots(force=True)

            assert result["deleted_by_age"] == 0
            assert (Path(tmpdir) / "old.png").exists()


class TestCleanupBySize:
    def test_deletes_oldest_until_under_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 3 files × 2 MB = 6 MB total, limit = 3 MB → delete 2 oldest
            _create_file(Path(tmpdir), "oldest.png", size=2 * 1024 * 1024, age_days=10)
            _create_file(Path(tmpdir), "middle.png", size=2 * 1024 * 1024, age_days=5)
            _create_file(Path(tmpdir), "newest.png", size=2 * 1024 * 1024, age_days=1)

            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 0, "max_screenshot_dir_mb": 3, "cleanup_interval_minutes": 0}
                 }):
                result = cleanup_screenshots(force=True)

            assert result["deleted_by_size"] == 2
            assert result["total_kept"] == 1
            assert not (Path(tmpdir) / "oldest.png").exists()
            assert not (Path(tmpdir) / "middle.png").exists()
            assert (Path(tmpdir) / "newest.png").exists()

    def test_zero_mb_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(Path(tmpdir), "big.png", size=100 * 1024 * 1024, age_days=0)

            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 0, "max_screenshot_dir_mb": 0, "cleanup_interval_minutes": 0}
                 }):
                result = cleanup_screenshots(force=True)

            assert result["deleted_by_size"] == 0
            assert (Path(tmpdir) / "big.png").exists()


class TestCleanupBothStrategies:
    def test_age_runs_first_then_size(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # 5 MB old → deleted by age
            _create_file(Path(tmpdir), "old.png", size=5 * 1024 * 1024, age_days=10)
            # 3 files × 2 MB = 6 MB remaining, limit = 3 MB → delete 2
            _create_file(Path(tmpdir), "mid1.png", size=2 * 1024 * 1024, age_days=5)
            _create_file(Path(tmpdir), "mid2.png", size=2 * 1024 * 1024, age_days=3)
            _create_file(Path(tmpdir), "new.png", size=2 * 1024 * 1024, age_days=1)

            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 3, "cleanup_interval_minutes": 0}
                 }):
                result = cleanup_screenshots(force=True)

            assert result["deleted_by_age"] == 1
            assert result["deleted_by_size"] == 2
            assert result["total_kept"] == 1
            assert (Path(tmpdir) / "new.png").exists()


class TestEmptyDir:
    def test_empty_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 500, "cleanup_interval_minutes": 0}
                 }):
                result = cleanup_screenshots(force=True)

            assert result["deleted_by_age"] == 0
            assert result["deleted_by_size"] == 0
            assert result["total_kept"] == 0
            assert result["dir_size_mb"] == 0.0

    def test_nonexistent_directory(self):
        with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path("/nonexistent/path")), \
             patch("agent_windows.core.screenshot.load_config", return_value={
                 "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 500, "cleanup_interval_minutes": 0}
             }):
            result = cleanup_screenshots(force=True)

        assert result["total_kept"] == 0


class TestIntervalSkip:
    def test_skips_when_interval_not_reached(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # First call (force) to set _last_cleanup_time
            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 500, "cleanup_interval_minutes": 60}
                 }):
                result1 = cleanup_screenshots(force=True)
                assert result1["skipped"] is False

                # Second call without force — should skip (interval = 60 min)
                result2 = cleanup_screenshots(force=False)
                assert result2["skipped"] is True

    def test_force_overrides_interval(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 500, "cleanup_interval_minutes": 60}
                 }):
                cleanup_screenshots(force=True)
                # Force should run regardless of interval
                result = cleanup_screenshots(force=True)
                assert result["skipped"] is False

    def test_zero_interval_always_runs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 500, "cleanup_interval_minutes": 0}
                 }):
                cleanup_screenshots(force=True)
                # With interval=0, even non-force should run
                result = cleanup_screenshots(force=False)
                assert result["skipped"] is False


class TestStatsFormat:
    def test_stats_have_expected_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 500, "cleanup_interval_minutes": 0}
                 }):
                result = cleanup_screenshots(force=True)

            for key in ("deleted_by_age", "deleted_by_size", "total_kept", "dir_size_mb", "skipped"):
                assert key in result

    def test_dir_size_calculation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(Path(tmpdir), "file.png", size=1024 * 1024)  # 1 MB

            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 0, "max_screenshot_dir_mb": 0, "cleanup_interval_minutes": 0}
                 }):
                result = cleanup_screenshots(force=True)

            assert result["dir_size_mb"] == 1.0


class TestLegacyWrapper:
    def test_clean_old_screenshots_returns_sum(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            _create_file(Path(tmpdir), "old.png", age_days=10)
            _create_file(Path(tmpdir), "new.png", age_days=1)

            with patch("agent_windows.core.screenshot.get_screenshot_save_dir", return_value=Path(tmpdir)), \
                 patch("agent_windows.core.screenshot.load_config", return_value={
                     "screenshot": {"max_screenshot_age_days": 7, "max_screenshot_dir_mb": 0, "cleanup_interval_minutes": 0}
                 }):
                deleted = clean_old_screenshots()

            assert deleted == 1
            assert not (Path(tmpdir) / "old.png").exists()
            assert (Path(tmpdir) / "new.png").exists()
