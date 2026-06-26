"""Tests for the global debug-state module (TODO #21-3)."""
import logging
import os

import pytest

from agent_windows.utils import debug as debug_mod


_TRUTHY = ("1", "true", "TRUE", "True", "yes", "on", "On")
_FALSY = ("0", "false", "no", "off", "", "garbage")


@pytest.fixture
def clean_debug_env(monkeypatch):
    """Remove AGENT_WINDOWS_DEBUG so tests start from a known unset state."""
    monkeypatch.delenv("AGENT_WINDOWS_DEBUG", raising=False)
    yield
    monkeypatch.delenv("AGENT_WINDOWS_DEBUG", raising=False)


class TestIsDebug:
    @pytest.mark.parametrize("val", _TRUTHY)
    def test_truthy(self, clean_debug_env, monkeypatch, val):
        monkeypatch.setenv("AGENT_WINDOWS_DEBUG", val)
        assert debug_mod.is_debug() is True

    @pytest.mark.parametrize("val", _FALSY)
    def test_falsy(self, clean_debug_env, monkeypatch, val):
        monkeypatch.setenv("AGENT_WINDOWS_DEBUG", val)
        assert debug_mod.is_debug() is False

    def test_unset_is_false(self, clean_debug_env):
        assert debug_mod.is_debug() is False


class TestEnableDebug:
    def test_sets_env_and_is_debug_true(self, clean_debug_env):
        assert debug_mod.is_debug() is False
        debug_mod.enable_debug()
        assert os.environ["AGENT_WINDOWS_DEBUG"] == "true"
        assert debug_mod.is_debug() is True

    def test_idempotent(self, clean_debug_env):
        debug_mod.enable_debug()
        debug_mod.enable_debug()
        assert debug_mod.is_debug() is True
