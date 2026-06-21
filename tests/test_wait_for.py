"""Tests for WaitFor multi-condition polling (TODO #7).

Covers three layers:
1. Pure functions in core/wait_for.py (normalize/validate/matches_condition) —
   no COM, no daemon, hand-built snapshots + ElementInfo.
2. Daemon handlers wait_for / wait_for_check (mocked get_tree_snapshot +
   matches_condition) — matches the existing test_wait.py style.
3. CLI wait_for command — runner.invoke against the real Typer app, matching
   test_cli_commands.py's pattern.

Matching is case-sensitive substring by design (see commands.md).
"""

from unittest.mock import patch, MagicMock

import pytest
import typer
from typer.testing import CliRunner

from agent_windows.core.tree import ElementInfo, BoundingBox, WindowInfo
from agent_windows.core.wait_for import (
    normalize_condition,
    validate_wait_for,
    matches_condition,
)


# ── fixtures ────────────────────────────────────────────────────────


def _el(
    name="OK",
    control_type="ButtonControl",
    window_name="Settings",
    is_enabled=True,
    is_focused=False,
    metadata=None,
) -> ElementInfo:
    return ElementInfo(
        name=name,
        control_type=control_type,
        bbox=BoundingBox(0, 0, 10, 10),
        is_interactive=True,
        is_enabled=is_enabled,
        is_focused=is_focused,
        window_name=window_name,
        metadata=metadata or {},
    )


def _snapshot(elements=None, windows=None, active=None) -> dict:
    return {
        "interactive_elements": elements or [],
        "windows": windows or [],
        "active_window": active,
    }


# ── normalize_condition ─────────────────────────────────────────────


class TestNormalizeCondition:
    @pytest.mark.parametrize("raw,expected", [
        ("text_exists", "text_exists"),
        ("TEXT_EXISTS", "text_exists"),
        ("  element_exists  ", "element_exists"),
        ("element-exists", "element_exists"),    # hyphen → underscore
        ("element exists", "element_exists"),    # space → underscore
        ("text", "text_exists"),                 # alias
        ("WINDOW", "active_window"),             # alias, uppercased
        ("Enabled", "element_enabled"),
        ("focused", "focused_element"),
    ])
    def test_canonical_and_aliases(self, raw, expected):
        assert normalize_condition(raw) == expected

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="condition must be one of"):
            normalize_condition("not_a_condition")

    def test_non_string_raises(self):
        with pytest.raises(ValueError, match="condition must be a string"):
            normalize_condition(123)


# ── validate_wait_for ───────────────────────────────────────────────


class TestValidateWaitFor:
    def test_valid_returns_normalized(self):
        assert validate_wait_for(
            "text", "ok", None, timeout=10, interval=0.5,
            max_timeout=300) == "text_exists"

    def test_timeout_zero_rejected(self):
        with pytest.raises(ValueError, match="timeout must be"):
            validate_wait_for("text", "x", None, timeout=0, interval=0.5,
                              max_timeout=300)

    def test_timeout_at_cap_allowed(self):
        # boundary: exactly max_timeout is allowed (<=)
        validate_wait_for("text", "x", None, timeout=300, interval=0.5,
                          max_timeout=300)

    def test_timeout_over_cap_rejected(self):
        with pytest.raises(ValueError, match="timeout must be"):
            validate_wait_for("text", "x", None, timeout=301, interval=0.5,
                              max_timeout=300)

    def test_interval_zero_rejected(self):
        with pytest.raises(ValueError, match="interval must be"):
            validate_wait_for("text", "x", None, timeout=10, interval=0,
                              max_timeout=300)

    def test_interval_over_five_rejected(self):
        with pytest.raises(ValueError, match="interval must be"):
            validate_wait_for("text", "x", None, timeout=10, interval=6,
                              max_timeout=300)

    def test_interval_none_skips_check(self):
        """A single probe (wait_for_check) passes interval=None to skip bounds."""
        validate_wait_for("text", "x", None, timeout=1, interval=None,
                          max_timeout=300)

    def test_text_exists_requires_text(self):
        with pytest.raises(ValueError, match="text is required"):
            validate_wait_for("text_exists", "", None, timeout=10, interval=0.5,
                              max_timeout=300)

    def test_active_window_requires_text_or_window(self):
        with pytest.raises(ValueError, match="text or window_name is required"):
            validate_wait_for("active_window", "", None, timeout=10,
                              interval=0.5, max_timeout=300)

    def test_active_window_accepts_window_name(self):
        validate_wait_for("active_window", "", "Notepad", timeout=10,
                          interval=0.5, max_timeout=300)

    @pytest.mark.parametrize("cond", ["element_exists", "element_enabled",
                                      "focused_element"])
    def test_element_conditions_require_text_or_window(self, cond):
        with pytest.raises(ValueError, match="text or window_name is required"):
            validate_wait_for(cond, "", None, timeout=10, interval=0.5,
                              max_timeout=300)


# ── matches_condition ───────────────────────────────────────────────


class TestMatchesCondition:
    def test_text_exists_match_in_element_name(self):
        snap = _snapshot(elements=[_el(name="Submit Button")])
        ok, detail = matches_condition(snap, "text_exists", "Submit", None)
        assert ok is True
        assert "Submit" in detail

    def test_text_exists_match_in_window_title(self):
        snap = _snapshot(windows=[WindowInfo("Notepad", "notepad", 1,
                                             BoundingBox(0, 0, 1, 1))])
        ok, _ = matches_condition(snap, "text_exists", "Notepad", None)
        assert ok is True

    def test_text_exists_absent(self):
        snap = _snapshot(elements=[_el(name="Loading")])
        ok, detail = matches_condition(snap, "text_exists", "Done", None)
        assert ok is False
        assert "absent" in detail

    def test_text_match_is_case_sensitive(self):
        """By design: 'OK' does NOT match 'ok' (case-sensitive substring)."""
        snap = _snapshot(elements=[_el(name="OK Button")])
        ok, _ = matches_condition(snap, "text_exists", "ok", None)
        assert ok is False
        ok, _ = matches_condition(snap, "text_exists", "OK", None)
        assert ok is True

    def test_text_exists_searches_metadata_values(self):
        snap = _snapshot(elements=[_el(name="", metadata={"value": "secret"})])
        ok, _ = matches_condition(snap, "text_exists", "secret", None)
        assert ok is True

    def test_active_window_match(self):
        active = WindowInfo("Settings", "app", 1, BoundingBox(0, 0, 1, 1),
                            is_active=True)
        snap = _snapshot(active=active)
        ok, _ = matches_condition(snap, "active_window", "Sett", None)
        assert ok is True

    def test_active_window_no_active(self):
        ok, detail = matches_condition(_snapshot(), "active_window", "X", None)
        assert ok is False
        assert "active window was" in detail

    def test_element_exists_match(self):
        snap = _snapshot(elements=[_el(name="OK")])
        ok, _ = matches_condition(snap, "element_exists", "OK", None)
        assert ok is True

    def test_element_exists_window_name_filter_excludes(self):
        """element matches text but window_name filter excludes it."""
        snap = _snapshot(elements=[_el(name="OK", window_name="Other")])
        ok, _ = matches_condition(snap, "element_exists", "OK", "Settings")
        assert ok is False

    def test_element_exists_window_name_filter_includes(self):
        snap = _snapshot(elements=[_el(name="OK", window_name="Settings App")])
        ok, _ = matches_condition(snap, "element_exists", "OK", "Settings")
        assert ok is True

    def test_element_enabled_excludes_disabled(self):
        snap = _snapshot(elements=[_el(name="OK", is_enabled=False)])
        ok, _ = matches_condition(snap, "element_enabled", "OK", None)
        assert ok is False

    def test_element_enabled_includes_enabled(self):
        snap = _snapshot(elements=[_el(name="OK", is_enabled=True)])
        ok, _ = matches_condition(snap, "element_enabled", "OK", None)
        assert ok is True

    def test_focused_element_excludes_unfocused(self):
        snap = _snapshot(elements=[_el(name="OK", is_focused=False)])
        ok, _ = matches_condition(snap, "focused_element", "OK", None)
        assert ok is False

    def test_focused_element_includes_focused(self):
        snap = _snapshot(elements=[_el(name="OK", is_focused=True)])
        ok, _ = matches_condition(snap, "focused_element", "OK", None)
        assert ok is True

    def test_empty_text_wildcard_matches_first_element(self):
        """Empty text filter = wildcard; element_exists with only window_name."""
        snap = _snapshot(elements=[_el(name="anything", window_name="App")])
        ok, _ = matches_condition(snap, "element_exists", "", "App")
        assert ok is True

    def test_unknown_condition_raises(self):
        with pytest.raises(ValueError, match="Unsupported WaitFor condition"):
            matches_condition(_snapshot(), "bogus", "x", None)

    def test_text_exists_empty_text_guard(self):
        """text_exists with empty text returns not-found (defensive guard) even
        if a caller bypasses validate_wait_for — an empty filter would otherwise
        wildcard-match the first source string."""
        snap = _snapshot(elements=[_el(name="anything")])
        ok, detail = matches_condition(snap, "text_exists", "", None)
        assert ok is False
        assert "no text filter" in detail


# ── daemon handlers ─────────────────────────────────────────────────


@patch("agent_windows.utils.config.load_config")
class TestWaitForHandlers:
    """wait_for / wait_for_check handlers (mock get_tree_snapshot)."""

    def _cfg(self, mock_config):
        mock_config.return_value = {"wait": {"poll_interval": 0.01}, "tree": {}}

    def test_wait_for_check_found(self, mock_config, make_element):
        from agent_windows.daemon.router import HANDLERS

        self._cfg(mock_config)
        el = ElementInfo(name="OK", control_type="ButtonControl",
                         bbox=BoundingBox(0, 0, 1, 1), window_name="App")
        with patch("agent_windows.core.tree.get_tree_snapshot",
                   return_value={"interactive_elements": [el],
                                 "windows": [], "active_window": None}):
            result = HANDLERS["wait_for_check"](
                {"condition": "element_exists", "text": "OK"})
        assert result["found"] is True
        assert result["condition"] == "element_exists"
        assert "OK" in result["detail"]

    def test_wait_for_check_not_found(self, mock_config):
        from agent_windows.daemon.router import HANDLERS

        self._cfg(mock_config)
        with patch("agent_windows.core.tree.get_tree_snapshot",
                   return_value={"interactive_elements": [],
                                 "windows": [], "active_window": None}):
            result = HANDLERS["wait_for_check"](
                {"condition": "text_exists", "text": "missing"})
        assert result["found"] is False

    def test_wait_for_check_bad_condition_raises(self, mock_config):
        from agent_windows.daemon.router import HANDLERS

        self._cfg(mock_config)
        with pytest.raises(ValueError, match="condition must be one of"):
            HANDLERS["wait_for_check"]({"condition": "bogus", "text": "x"})

    def test_wait_for_immediate_match(self, mock_config):
        from agent_windows.daemon.router import HANDLERS

        self._cfg(mock_config)
        el = ElementInfo(name="OK", control_type="ButtonControl",
                         bbox=BoundingBox(0, 0, 1, 1), window_name="App")
        with patch("agent_windows.core.tree.get_tree_snapshot",
                   return_value={"interactive_elements": [el],
                                 "windows": [], "active_window": None}), \
             patch("agent_windows.daemon.handlers.tree.time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0]
            result = HANDLERS["wait_for"](
                {"condition": "text_exists", "text": "OK", "timeout": 5})
        assert "satisfied" in result["output"]
        assert result["condition"] == "text_exists"

    def test_wait_for_timeout_raises(self, mock_config):
        from agent_windows.daemon.router import HANDLERS

        self._cfg(mock_config)
        with patch("agent_windows.core.tree.get_tree_snapshot",
                   return_value={"interactive_elements": [],
                                 "windows": [], "active_window": None}), \
             patch("agent_windows.daemon.handlers.tree.time") as mock_time:
            mock_time.time.side_effect = [0.0, 0.0, 100.0]
            mock_time.sleep = MagicMock()
            with pytest.raises(TimeoutError, match="not satisfied within"):
                HANDLERS["wait_for"](
                    {"condition": "text_exists", "text": "missing",
                     "timeout": 0.1})

    def test_wait_for_over_cap_rejected(self, mock_config):
        """timeout > MAX_WAIT_SECONDS surfaces as ValueError (SEC3-01)."""
        from agent_windows.daemon.router import HANDLERS
        from agent_windows.daemon.handlers import tree as tree_mod

        self._cfg(mock_config)
        with pytest.raises(ValueError, match="timeout must be"):
            HANDLERS["wait_for"]({
                "condition": "text_exists", "text": "x",
                "timeout": tree_mod.MAX_WAIT_SECONDS + 1,
            })

    def test_wait_for_bad_interval_rejected(self, mock_config):
        from agent_windows.daemon.router import HANDLERS

        self._cfg(mock_config)
        with pytest.raises(ValueError, match="interval must be"):
            HANDLERS["wait_for"]({
                "condition": "text_exists", "text": "x",
                "timeout": 5, "interval": 10,
            })

    @pytest.mark.parametrize("bad_interval", [0, -1, 6, 99])
    def test_wait_for_bad_config_interval_rejected(self, mock_config,
                                                   bad_interval):
        """A malicious/broken config poll_interval must surface as INVALID_PARAMS
        rather than cause a tight busy-loop (the SEC3-01 second bounds-check in
        _wait_for after the config fallback resolves)."""
        from agent_windows.daemon.router import HANDLERS

        mock_config.return_value = {
            "wait": {"poll_interval": bad_interval}, "tree": {}}
        with pytest.raises(ValueError, match="interval must be"):
            HANDLERS["wait_for"]({
                "condition": "text_exists", "text": "x", "timeout": 5,
            })


# ── CLI wait-for (runner.invoke, matching test_cli_commands.py) ─────

runner = CliRunner()


def _ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.obj = {"format": "text"}
    return ctx


class TestWaitForCli:
    @patch("agent_windows.utils.config.load_config",
           return_value={"wait": {"poll_interval": 0.5}})
    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    @patch("agent_windows.cli.commands.wait.dispatch",
           return_value={"found": True, "condition": "text_exists",
                         "detail": "text 'OK' appeared"})
    def test_polls_until_found(self, mock_dispatch, mock_print, mock_time,
                               mock_config):
        from agent_windows.cli.main import app

        mock_time.time.return_value = 1.0
        result = runner.invoke(
            app, ["wait-for", "text_exists", "--text", "OK"])
        assert result.exit_code == 0
        mock_dispatch.assert_called_once()
        sent = mock_dispatch.call_args.args[1]
        assert sent == {"condition": "text_exists", "text": "OK",
                        "window_name": ""}
        out = mock_print.call_args.args[0]
        assert "satisfied" in out["output"]
        assert out["condition"] == "text_exists"

    @patch("agent_windows.utils.config.load_config",
           return_value={"wait": {"poll_interval": 0.5}})
    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.dispatch",
           return_value={"found": False, "detail": "absent"})
    def test_timeout_exits_one(self, mock_dispatch, mock_time, mock_config):
        from agent_windows.cli.main import app

        mock_time.time.side_effect = [0.0, 100.0]
        result = runner.invoke(
            app, ["wait-for", "text_exists", "--text", "x", "--timeout", "5"])
        assert result.exit_code == 1
        assert "not satisfied within 5" in result.output
        mock_dispatch.assert_called()

    @patch("agent_windows.utils.config.load_config",
           return_value={"wait": {"poll_interval": 0.5}})
    @patch("agent_windows.cli.commands.wait.dispatch",
           return_value={"found": True, "condition": "element_exists",
                         "detail": "matched 'OK'"})
    @patch("agent_windows.cli.commands.wait.time")
    @patch("agent_windows.cli.commands.wait.print_result")
    def test_window_filter_forwarded(self, mock_print, mock_time, mock_dispatch,
                                     mock_config):
        from agent_windows.cli.main import app

        mock_time.time.return_value = 1.0
        result = runner.invoke(
            app, ["wait-for", "element", "--text", "OK", "--window", "Settings"])
        assert result.exit_code == 0
        sent = mock_dispatch.call_args.args[1]
        assert sent["window_name"] == "Settings"
        # The CLI forwards the RAW condition alias; normalization happens
        # server-side in wait_for_check (validate_wait_for). The response echoes
        # the canonical form, which the CLI surfaces (see test_polls_until_found).
        assert sent["condition"] == "element"

    @patch("agent_windows.cli.commands.wait.dispatch")
    def test_bad_condition_fails_fast_without_polling(self, mock_dispatch):
        """A typo'd condition must exit(1) before dispatching ANY probe — not
        busy-poll the daemon with INVALID_PARAMS for the whole timeout (NIT 7)."""
        from agent_windows.cli.main import app

        result = runner.invoke(
            app, ["wait-for", "tex_exists", "--text", "OK"])
        assert result.exit_code == 1
        assert "condition must be one of" in result.output
        mock_dispatch.assert_not_called()
