"""Tests for get_text command and resolve_element selector resolution."""

import pytest
from unittest.mock import patch, MagicMock

from agent_windows.core.tree import BoundingBox, resolve_element, get_tree_snapshot


# ── resolve_element tests ───────────────────────────────────────────


# Non-empty _tree_cache so resolve_element's no-snapshot fail-fast passes;
# get_tree_snapshot is still mocked to return the fake elements.
@patch("agent_windows.core.tree._tree_cache", {"interactive_elements": []})
@patch("agent_windows.core.tree.get_tree_snapshot")
@patch("agent_windows.utils.config.load_config")
def test_resolve_element_valid(mock_config, mock_snapshot, make_element):
    """resolve_element with valid @e{N} returns correct ElementInfo."""
    mock_config.return_value = {"tree": {}}
    elements = [make_element(name=f"el{i}") for i in range(5)]
    mock_snapshot.return_value = {"interactive_elements": elements}

    result = resolve_element("@e3")
    assert result.name == "el2"


# Non-empty _tree_cache so resolve_element's no-snapshot fail-fast passes;
# get_tree_snapshot is still mocked to return the fake elements.
@patch("agent_windows.core.tree._tree_cache", {"interactive_elements": []})
@patch("agent_windows.core.tree.get_tree_snapshot")
@patch("agent_windows.utils.config.load_config")
def test_resolve_element_first(mock_config, mock_snapshot, make_element):
    """resolve_element @e1 returns the first element."""
    mock_config.return_value = {"tree": {}}
    elements = [make_element(name="first"), make_element(name="second")]
    mock_snapshot.return_value = {"interactive_elements": elements}

    result = resolve_element("@e1")
    assert result.name == "first"


# Non-empty _tree_cache so resolve_element's no-snapshot fail-fast passes;
# get_tree_snapshot is still mocked to return the fake elements.
@patch("agent_windows.core.tree._tree_cache", {"interactive_elements": []})
@patch("agent_windows.core.tree.get_tree_snapshot")
@patch("agent_windows.utils.config.load_config")
def test_resolve_element_out_of_range(mock_config, mock_snapshot, make_element):
    """resolve_element raises IndexError for out-of-range index."""
    mock_config.return_value = {"tree": {}}
    elements = [make_element(name="only")]
    mock_snapshot.return_value = {"interactive_elements": elements}

    with pytest.raises(IndexError, match="not found"):
        resolve_element("@e5")


def test_resolve_element_invalid_format():
    """resolve_element raises ValueError for bad selector format."""
    with pytest.raises(ValueError, match="Invalid selector"):
        resolve_element("button1")


def test_resolve_element_invalid_index_zero():
    """resolve_element raises ValueError for @e0."""
    with pytest.raises(ValueError, match="index must be"):
        resolve_element("@e0")


def test_resolve_element_not_a_number():
    """resolve_element raises ValueError for @eabc."""
    with pytest.raises(ValueError, match="Invalid selector"):
        resolve_element("@eabc")


# ── get_text handler tests ──────────────────────────────────────────


@patch("agent_windows.core.tree.resolve_element")
def test_get_text_handler(mock_resolve, make_element):
    """get_text handler returns element name as text."""
    from agent_windows.daemon.router import HANDLERS

    mock_resolve.return_value = make_element(name="Submit", control_type="ButtonControl")

    handler = HANDLERS["get_text"]
    result = handler({"selector": "@e3"})

    assert result["text"] == "Submit"
    assert result["selector"] == "@e3"
    assert result["control_type"] == "ButtonControl"
    assert "Submit" in result["output"]


@patch("agent_windows.core.tree.resolve_element")
def test_get_text_empty_name(mock_resolve, make_element):
    """get_text handler returns empty string for element with no name."""
    from agent_windows.daemon.router import HANDLERS

    mock_resolve.return_value = make_element(name="", control_type="PaneControl")

    handler = HANDLERS["get_text"]
    result = handler({"selector": "@e1"})

    assert result["text"] == ""
    assert "Text" in result["output"]


def test_get_text_handler_no_selector():
    """get_text handler raises ValueError when selector is missing."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["get_text"]
    with pytest.raises(ValueError, match="selector is required"):
        handler({})


def test_get_text_handler_empty_selector():
    """get_text handler raises ValueError when selector is empty string."""
    from agent_windows.daemon.router import HANDLERS

    handler = HANDLERS["get_text"]
    with pytest.raises(ValueError, match="selector is required"):
        handler({"selector": ""})
