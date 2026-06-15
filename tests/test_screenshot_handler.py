"""Tests for screenshot handler image-path blacklist validation (L-05).

highlight/diff open user-supplied image paths via PIL; those paths must pass
the filesystem blacklist so the handlers can't be used to read past it.
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image


def _tmp_image(tmp_path: Path) -> Path:
    """Create a tiny valid PNG and return its path."""
    p = tmp_path / "src.png"
    Image.new("RGB", (4, 4), (10, 20, 30)).save(p)
    return p


_BLACKLISTED = r"C:\Users\alice\.ssh\leak.png"


class TestHighlightImageBlacklist:
    def test_rejects_blacklisted_image(self):
        from agent_windows.daemon.router import HANDLERS

        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["highlight"]({"image": _BLACKLISTED})

    def test_accepts_valid_image(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        img = _tmp_image(tmp_path)
        with patch("agent_windows.core.screenshot.highlight_region") as mock_hr:
            mock_hr.return_value = "/fake/out.png"
            HANDLERS["highlight"]({
                "image": str(img), "x1": 0, "y1": 0, "x2": 1, "y2": 1,
            })
        mock_hr.assert_called_once()


class TestDiffImageBlacklist:
    def test_rejects_blacklisted_baseline(self):
        from agent_windows.daemon.router import HANDLERS

        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["diff"]({"baseline": _BLACKLISTED})

    def test_rejects_blacklisted_compare(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        baseline = _tmp_image(tmp_path)  # valid, non-blacklisted
        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["diff"]({"baseline": str(baseline), "compare": _BLACKLISTED})


class TestOutputWriteProtection:
    """L-05-W1: a user-supplied --output can't land in a protected directory."""

    # The agent's data dir (~/.agent-windows) is protected from user-directed writes.
    _BAD_OUTPUT = str(Path.home() / ".agent-windows" / "pwned.png")

    def test_screenshot_rejects_protected_output(self):
        from agent_windows.daemon.router import HANDLERS

        # Validation runs before capture_screenshot, so no screen access needed.
        with pytest.raises(ValueError, match="install tree"):
            HANDLERS["screenshot"]({"output": self._BAD_OUTPUT})

    def test_highlight_rejects_protected_output(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        img = _tmp_image(tmp_path)
        with pytest.raises(ValueError, match="install tree"):
            HANDLERS["highlight"]({
                "image": str(img), "x1": 0, "y1": 0, "x2": 1, "y2": 1,
                "output": self._BAD_OUTPUT,
            })

    def test_diff_rejects_protected_output(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS

        baseline = _tmp_image(tmp_path)
        with pytest.raises(ValueError, match="install tree"):
            HANDLERS["diff"]({"baseline": str(baseline), "output": self._BAD_OUTPUT})
