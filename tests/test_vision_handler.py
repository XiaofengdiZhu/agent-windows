"""Tests for vision handler image/output path validation (L-06).

ask-vision/detect open a user-supplied ``image`` path (read) and write a marked
screenshot to a user-supplied ``output`` path (write). Both must pass the
filesystem validators so the vision handlers can't read past the blacklist or
drop output into the agent's install/data tree — the same protection the
screenshot handlers got in L-05.
"""

from pathlib import Path

import pytest


_BLACKLISTED = r"C:\Users\alice\.ssh\leak.png"


class TestAskVisionImageBlacklist:
    def test_rejects_blacklisted_image(self):
        from agent_windows.daemon.router import HANDLERS

        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["ask-vision"]({"prompt": "what is this", "image": _BLACKLISTED})


class TestDetectImageBlacklist:
    def test_rejects_blacklisted_image(self):
        from agent_windows.daemon.router import HANDLERS

        with pytest.raises(ValueError, match="restricted"):
            HANDLERS["detect"]({"description": "buttons", "image": _BLACKLISTED})


class TestVisionOutputWriteProtection:
    """L-06: a user-supplied --output can't land in a protected directory."""

    # The agent's data dir (~/.agent-windows) is protected from user-directed writes.
    _BAD_OUTPUT = str(Path.home() / ".agent-windows" / "pwned.png")

    def test_ask_vision_rejects_protected_output(self):
        from agent_windows.daemon.router import HANDLERS

        # Validation runs before any capture/API call.
        with pytest.raises(ValueError, match="install tree"):
            HANDLERS["ask-vision"]({"prompt": "describe", "output": self._BAD_OUTPUT})

    def test_detect_rejects_protected_output(self):
        from agent_windows.daemon.router import HANDLERS

        with pytest.raises(ValueError, match="install tree"):
            HANDLERS["detect"]({"description": "icons", "output": self._BAD_OUTPUT})
