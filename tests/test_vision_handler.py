"""Tests for vision handler image/output path validation (L-06).

ask-vision/detect open a user-supplied ``image`` path (read) and write a marked
screenshot to a user-supplied ``output`` path (write). Both must pass the
filesystem validators so the vision handlers can't read past the blacklist or
drop output into the agent's install/data tree — the same protection the
screenshot handlers got in L-05.
"""

from pathlib import Path
from unittest.mock import patch

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


# ── G17: description/prompt length cap (DoS bound) ──────────────────


class TestVisionInputLengthCap:
    """G17: ground/detect ``description`` and ask-vision ``prompt`` flow into a
    blocking vision-API call (ground fires it TWICE). With no cap, a 1MB input
    still completes but bloats tokens/latency and pins the single STA worker.
    Like ``type`` (MAX_TYPE_TEXT_LEN), bound them at the handler edge so an
    oversized input is rejected as INVALID_PARAMS before any image work or API
    call. (G17)
    """

    def _oversized(self, cap: int) -> str:
        return "x" * (cap + 1)

    def test_ground_rejects_oversized_description(self):
        from agent_windows.daemon.handlers import vision
        from agent_windows.daemon.router import HANDLERS

        with patch("agent_windows.core.grounding.ground_element") as mock_ground:
            with pytest.raises(ValueError, match="maximum"):
                HANDLERS["ground"](
                    {"description": self._oversized(vision.MAX_DESC_LEN)})
        mock_ground.assert_not_called()

    def test_detect_rejects_oversized_description(self):
        from agent_windows.daemon.handlers import vision
        from agent_windows.daemon.router import HANDLERS

        with patch("agent_windows.core.grounding.detect_elements") as mock_detect:
            with pytest.raises(ValueError, match="maximum"):
                HANDLERS["detect"](
                    {"description": self._oversized(vision.MAX_DESC_LEN)})
        mock_detect.assert_not_called()

    def test_ask_vision_rejects_oversized_prompt(self):
        from agent_windows.daemon.handlers import vision
        from agent_windows.daemon.router import HANDLERS

        with patch("agent_windows.core.grounding.ask_vision") as mock_ask:
            with pytest.raises(ValueError, match="maximum"):
                HANDLERS["ask-vision"](
                    {"prompt": self._oversized(vision.MAX_PROMPT_LEN)})
        mock_ask.assert_not_called()

    def test_ground_at_cap_is_accepted(self):
        """Exactly MAX_DESC_LEN chars must NOT be rejected by the cap itself."""
        from agent_windows.daemon.handlers import vision
        from agent_windows.daemon.router import HANDLERS

        with patch("agent_windows.core.grounding.ground_element", return_value={
            "x": 1, "y": 2, "confidence": 0.9, "method": "fine",
            "screenshot_path": "/o.png",
        }) as mock_ground:
            HANDLERS["ground"](
                {"description": "x" * vision.MAX_DESC_LEN})
        mock_ground.assert_called_once()

    def test_detect_at_cap_is_accepted(self):
        """Exactly MAX_DESC_LEN chars must NOT be rejected by the cap itself."""
        from agent_windows.daemon.handlers import vision
        from agent_windows.daemon.router import HANDLERS

        with patch("agent_windows.core.grounding.detect_elements", return_value={
            "description": "", "count": 0, "targets": [],
            "screenshot_path": "/o.png",
        }) as mock_detect:
            HANDLERS["detect"](
                {"description": "x" * vision.MAX_DESC_LEN})
        mock_detect.assert_called_once()

    def test_ask_vision_at_cap_is_accepted(self):
        """Exactly MAX_PROMPT_LEN chars must NOT be rejected by the cap itself."""
        from agent_windows.daemon.handlers import vision
        from agent_windows.daemon.router import HANDLERS

        with patch("agent_windows.core.grounding.ask_vision", return_value={
            "response": "ok", "screenshot_path": "/o.png", "model_used": "m",
        }) as mock_ask:
            HANDLERS["ask-vision"](
                {"prompt": "x" * vision.MAX_PROMPT_LEN})
        mock_ask.assert_called_once()

