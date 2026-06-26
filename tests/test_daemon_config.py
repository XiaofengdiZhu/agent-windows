"""Daemon-side --config argv parsing (TODO #21-1)."""
from agent_windows.daemon.server import _parse_config_override_from_argv


class TestParseConfigOverrideFromArgv:
    def test_space_form(self):
        assert _parse_config_override_from_argv(
            ["-m", "agent_windows", "--daemon", "--config", "/tmp/x.toml"]
        ) == "/tmp/x.toml"

    def test_equals_form(self):
        assert _parse_config_override_from_argv(
            ["-m", "agent_windows", "--daemon", "--config=/tmp/x.toml"]
        ) == "/tmp/x.toml"

    def test_absent_returns_none(self):
        assert _parse_config_override_from_argv(
            ["-m", "agent_windows", "--daemon"]) is None

    def test_config_without_value_returns_none(self):
        # --config as the last token with no following value: safe None,
        # no IndexError. (Launcher never produces this, but be defensive.)
        assert _parse_config_override_from_argv(
            ["--daemon", "--config"]) is None

    def test_only_first_config_wins(self):
        # If somehow two --config appear, the first one wins (left-to-right).
        assert _parse_config_override_from_argv(
            ["--config", "a.toml", "--config", "b.toml"]) == "a.toml"

    def test_not_confused_by_lookalike(self):
        # --config-file (a different flag) must not match --config.
        assert _parse_config_override_from_argv(
            ["--config-file", "x.toml"]) is None
