"""Test configuration and shared fixtures."""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_windows.core.tree import ElementInfo, BoundingBox


@pytest.fixture
def make_element():
    """Factory fixture for creating ElementInfo instances."""
    def _make(
        name: str = "Button",
        control_type: str = "ButtonControl",
        bbox: BoundingBox | None = None,
        is_enabled: bool = True,
        is_offscreen: bool = False,
    ) -> ElementInfo:
        return ElementInfo(
            name=name,
            control_type=control_type,
            bbox=bbox or BoundingBox(100, 200, 300, 250),
            is_interactive=True,
            is_enabled=is_enabled,
            is_offscreen=is_offscreen,
        )
    return _make


@pytest.fixture
def tmp_dir():
    """Provide a temporary directory that is cleaned up after test."""
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def mock_config():
    """Provide a mock for load_config returning a standard config."""
    with patch("agent_windows.utils.config.load_config") as m:
        m.return_value = {
            "vlm": {"enabled": True, "model": "test", "engine": "openai", "width": 1000, "height": 1000},
            "screenshot": {"backend": "auto", "scale": 1.0, "max_screenshot_age_days": 7, "max_screenshot_dir_mb": 500, "cleanup_interval_minutes": 10},
            # Match the production default (utils/config.py DEFAULT_CONFIG,
            # H-04): max_depth was lowered from 30 to 15. A stale 30 here would
            # run depth-sensitive tests under a different limit than production
            # and could mask a regression.
            "tree": {"max_depth": 15},
            "daemon": {"idle_timeout_minutes": 30},
            "wait": {"poll_interval": 0.5},
            "clipboard": {"method": "system"},
        }
        yield m
