"""Tests for configuration management."""

import os
import tempfile
import pytest

from agent_windows.utils.config import (
    _deep_merge,
    _parse_value,
    DEFAULT_CONFIG,
)


class TestDeepMerge:
    def test_simple_override(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"grounding": {"model": "a", "width": 1000}}
        override = {"grounding": {"model": "b"}}
        result = _deep_merge(base, override)
        assert result["grounding"]["model"] == "b"
        assert result["grounding"]["width"] == 1000

    def test_no_mutate_base(self):
        base = {"a": 1}
        _deep_merge(base, {"a": 2})
        assert base["a"] == 1


class TestParseValue:
    def test_bool_true(self):
        assert _parse_value("true") is True
        assert _parse_value("True") is True
        assert _parse_value("yes") is True
        assert _parse_value("1") is True

    def test_bool_false(self):
        assert _parse_value("false") is False
        assert _parse_value("False") is False
        assert _parse_value("no") is False
        assert _parse_value("0") is False

    def test_int(self):
        assert _parse_value("42") == 42
        assert _parse_value("0") is False  # "0" matches bool check first, returns False

    def test_float(self):
        assert _parse_value("3.14") == 3.14
        assert _parse_value("1.0") == 1.0

    def test_string(self):
        assert _parse_value("hello") == "hello"
        assert _parse_value("gpt-4o-mini") == "gpt-4o-mini"


class TestDefaultConfig:
    def test_has_grounding(self):
        assert "grounding" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["grounding"]["enabled"] is True
        assert DEFAULT_CONFIG["grounding"]["width"] == 1000

    def test_has_screenshot(self):
        assert "screenshot" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["screenshot"]["backend"] == "auto"

    def test_has_tree(self):
        assert "tree" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["tree"]["max_depth"] == 30

    def test_has_daemon(self):
        assert "daemon" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["daemon"]["idle_timeout_minutes"] == 30
