"""Tests for element_state command (is visible / is enabled)."""

import pytest
from unittest.mock import patch

from agent_windows.core.tree import ElementInfo, BoundingBox


# ── element_state handler tests ─────────────────────────────────────


@patch("agent_windows.core.tree.resolve_element")
def test_is_visible_true(mock_resolve, make_element):
    """element_state returns True for visible (not offscreen) element."""
    from agent_windows.daemon.router import HANDLERS

    mock_resolve.return_value = make_element(is_offscreen=False)
    handler = HANDLERS["element_state"]
    result = handler({"selector": "@e1", "check": "visible"})

    assert result["result"] is True
    assert result["check"] == "visible"
    assert "True" in result["output"]


@patch("agent_windows.core.tree.resolve_element")
def test_is_visible_false(mock_resolve, make_element):
    """element_state returns False for offscreen element."""
    from agent_windows.daemon.router import HANDLERS

    mock_resolve.return_value = make_element(is_offscreen=True)
    handler = HANDLERS["element_state"]
    result = handler({"selector": "@e1", "check": "visible"})

    assert result["result"] is False
    assert "False" in result["output"]


@patch("agent_windows.core.tree.resolve_element")
def test_is_enabled_true(mock_resolve, make_element):
    """element_state returns True for enabled element."""
    from agent_windows.daemon.router import HANDLERS

    mock_resolve.return_value = make_element(is_enabled=True)
    handler = HANDLERS["element_state"]
    result = handler({"selector": "@e3", "check": "enabled"})

    assert result["result"] is True
    assert result["check"] == "enabled"


@patch("agent_windows.core.tree.resolve_element")
def test_is_enabled_false(mock_resolve, make_element):
    """element_state returns False for disabled element."""
    from agent_windows.daemon.router import HANDLERS

    mock_resolve.return_value = make_element(is_enabled=False)
    handler = HANDLERS["element_state"]
    result = handler({"selector": "@e3", "check": "enabled"})

    assert result["result"] is False


def test_element_state_no_selector():
    """element_state raises ValueError when selector is missing."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["element_state"]
    with pytest.raises(ValueError, match="selector is required"):
        handler({"check": "visible"})


def test_element_state_empty_selector():
    """element_state raises ValueError when selector is empty string."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["element_state"]
    with pytest.raises(ValueError, match="selector is required"):
        handler({"selector": "", "check": "visible"})


def test_element_state_invalid_check():
    """element_state raises ValueError for invalid check value."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["element_state"]
    with pytest.raises(ValueError, match="check must be"):
        handler({"selector": "@e1", "check": "checked"})


def test_element_state_missing_check():
    """element_state raises ValueError when check is missing."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["element_state"]
    with pytest.raises(ValueError, match="check must be"):
        handler({"selector": "@e1"})
