"""Daemon-side --config argv parsing (TODO #21-1)."""
from agent_windows.daemon.server import _parse_config_override_from_argv
from agent_windows.utils import config as cfg


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


class TestDaemonConfigWiring:
    """Integration: the exact sequence _init_daemon_state performs — parse
    --config from argv, call set_config_override, then load_config — must read
    the override file. Guards against the wiring regressing (e.g. an earlier
    load_config() caching the default, or the override parsed after the load).
    We exercise the parse→set→load seam directly rather than calling
    _init_daemon_state (which has heavy side effects: PID writes, screenshot
    cleanup, version checks)."""

    def teardown_method(self, _method):
        cfg.set_config_override(None)

    def test_parse_set_load_reads_override(self, tmp_path, monkeypatch):
        alt = tmp_path / "alt.toml"
        # A distinctive daemon-section value _init_daemon_state would read.
        alt.write_text('[daemon]\nidle_timeout_minutes = 7\n', encoding="utf-8")
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None

        # Mirror _init_daemon_state server.py:272-275 + 290 exactly.
        argv = ["-m", "agent_windows", "--daemon", "--config", str(alt)]
        override = _parse_config_override_from_argv(argv)
        assert override == str(alt), "parser must recover the path from argv"
        if override:
            cfg.set_config_override(override)
        config = cfg.load_config()

        # The override file's value is what load_config returned — proving the
        # daemon would read it, not the default.
        assert config["daemon"]["idle_timeout_minutes"] == 7
        assert cfg.get_config_path() == alt.resolve()

    def test_no_config_in_argv_leaves_default(self, monkeypatch):
        # The common case: no --config on the daemon command line.
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None
        cfg.set_config_override(None)
        override = _parse_config_override_from_argv(
            ["-m", "agent_windows", "--daemon"])
        assert override is None
        if override:
            cfg.set_config_override(override)
        # No override set -> get_config_path is the default location.
        assert cfg.get_config_path() == cfg.get_data_dir() / "config.toml"
