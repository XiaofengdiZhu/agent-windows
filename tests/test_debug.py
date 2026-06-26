"""Tests for the global debug-state module (TODO #21-3)."""
import logging
import os

import pytest

from agent_windows.utils import debug as debug_mod


_TRUTHY = ("1", "true", "TRUE", "True", "yes", "on", "On")
_FALSY = ("0", "false", "no", "off", "", "garbage")


@pytest.fixture
def clean_debug_env(monkeypatch):
    """Remove AGENT_WINDOWS_DEBUG so tests start from a known unset state.

    Teardown pops the key directly (not via monkeypatch) because some tests
    mutate os.environ through enable_debug(), which writes outside
    monkeypatch's tracking — monkeypatch.delenv at teardown only reliably
    undoes ITS OWN setup deletion, leaving a stale value behind that would
    leak into subprocess-based tests (test_uia_debug.py) run later in the
    same pytest session.
    """
    monkeypatch.delenv("AGENT_WINDOWS_DEBUG", raising=False)
    yield
    os.environ.pop("AGENT_WINDOWS_DEBUG", None)


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


class TestConfigureLogging:
    @pytest.fixture(autouse=True)
    def _save_log_levels(self):
        root = logging.getLogger()
        saved_root = root.level
        saved_verbose = {n: logging.getLogger(n).level
                         for n in debug_mod._VERBOSE_LOGGERS}
        yield
        root.setLevel(saved_root)
        for n, lvl in saved_verbose.items():
            logging.getLogger(n).setLevel(lvl)

    def test_debug_raises_root_to_debug(self, clean_debug_env, monkeypatch):
        logging.getLogger().setLevel(logging.INFO)
        monkeypatch.setenv("AGENT_WINDOWS_DEBUG", "1")
        debug_mod.configure_logging()
        assert logging.getLogger().getEffectiveLevel() == logging.DEBUG

    def test_debug_raises_verbose_loggers(self, clean_debug_env, monkeypatch):
        for name in debug_mod._VERBOSE_LOGGERS:
            logging.getLogger(name).setLevel(logging.INFO)
        monkeypatch.setenv("AGENT_WINDOWS_DEBUG", "1")
        debug_mod.configure_logging()
        for name in debug_mod._VERBOSE_LOGGERS:
            assert logging.getLogger(name).level == logging.DEBUG

    def test_noop_when_not_debug(self, clean_debug_env):
        logging.getLogger().setLevel(logging.INFO)
        debug_mod.configure_logging()  # unset → not debug
        assert logging.getLogger().getEffectiveLevel() == logging.INFO

    def test_idempotent_no_handler_accumulation(self, clean_debug_env, monkeypatch):
        root = logging.getLogger()
        n_handlers_before = len(root.handlers)
        monkeypatch.setenv("AGENT_WINDOWS_DEBUG", "1")
        debug_mod.configure_logging()
        debug_mod.configure_logging()
        assert len(root.handlers) == n_handlers_before
        assert root.getEffectiveLevel() == logging.DEBUG
