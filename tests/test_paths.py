"""Tests for agent_windows.utils.paths — Claude Desktop data dir resolution.

All tests isolate LOCALAPPDATA/APPDATA to tmp_path via monkeypatch.setenv (NOT
patch.dict(clear=False) — that leaks the host's real values on Windows and makes
results non-deterministic). No real install is required.
"""
from agent_windows.utils.paths import (
    get_claude_config_dir_or_default,
    get_claude_config_path,
    get_claude_data_dir,
    is_msix_install,
)


def _env(tmp_path, *, local_appdata=None, appdata=None):
    """Point LOCALAPPDATA/APPDATA at tmp subdirs; only set what's given."""
    out = {}
    if local_appdata is not None:
        p = tmp_path / local_appdata
        p.mkdir(parents=True, exist_ok=True)
        out["LOCALAPPDATA"] = str(p)
    if appdata is not None:
        p = tmp_path / appdata
        p.mkdir(parents=True, exist_ok=True)
        out["APPDATA"] = str(p)
    return out


class TestGetClaudeDataDir:
    def test_msix_wins_over_standard(self, tmp_path, monkeypatch):
        """MSIX path takes priority even when the standard path also exists."""
        local = tmp_path / "Local"
        msix = (local / "Packages" / "Claude_pzs8sxrjxfjjc"
                / "LocalCache" / "Roaming" / "Claude")
        msix.mkdir(parents=True)
        roaming = tmp_path / "Roaming"
        (roaming / "Claude").mkdir(parents=True)
        for k, v in _env(tmp_path, local_appdata="Local", appdata="Roaming").items():
            monkeypatch.setenv(k, v)
        assert get_claude_data_dir() == msix

    def test_standard_when_no_msix(self, tmp_path, monkeypatch):
        roaming = tmp_path / "Roaming"
        std = roaming / "Claude"
        std.mkdir(parents=True)
        for k, v in _env(tmp_path, local_appdata="Local", appdata="Roaming").items():
            monkeypatch.setenv(k, v)
        assert get_claude_data_dir() == std

    def test_none_when_neither(self, tmp_path, monkeypatch):
        for k, v in _env(tmp_path, local_appdata="Local", appdata="Roaming").items():
            monkeypatch.setenv(k, v)
        assert get_claude_data_dir() is None

    def test_none_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        assert get_claude_data_dir() is None

    def test_matches_any_publisher_suffix(self, tmp_path, monkeypatch):
        local = tmp_path / "Local"
        msix = (local / "Packages" / "Claude_abc123xyz"
                / "LocalCache" / "Roaming" / "Claude")
        msix.mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.delenv("APPDATA", raising=False)
        assert get_claude_data_dir() == msix

    def test_ignores_msix_dir_without_localcache(self, tmp_path, monkeypatch):
        """A Claude_* package dir without LocalCache\\Roaming\\Claude is skipped."""
        local = tmp_path / "Local"
        (local / "Packages" / "Claude_pzs8sxrjxfjjc").mkdir(parents=True)  # no LocalCache
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.delenv("APPDATA", raising=False)
        assert get_claude_data_dir() is None


class TestGetClaudeConfigPath:
    def test_returns_path_when_file_exists(self, tmp_path, monkeypatch):
        local = tmp_path / "Local"
        msix = (local / "Packages" / "Claude_pzs8sxrjxfjjc"
                / "LocalCache" / "Roaming" / "Claude")
        msix.mkdir(parents=True)
        cfg = msix / "claude_desktop_config.json"
        cfg.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.delenv("APPDATA", raising=False)
        assert get_claude_config_path() == cfg

    def test_none_when_file_missing(self, tmp_path, monkeypatch):
        local = tmp_path / "Local"
        msix = (local / "Packages" / "Claude_pzs8sxrjxfjjc"
                / "LocalCache" / "Roaming" / "Claude")
        msix.mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.delenv("APPDATA", raising=False)
        assert get_claude_config_path() is None

    def test_none_when_not_installed(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.delenv("APPDATA", raising=False)
        assert get_claude_config_path() is None


class TestIsMsixInstall:
    def test_true_when_msix_dir_exists(self, tmp_path, monkeypatch):
        local = tmp_path / "Local"
        (local / "Packages" / "Claude_pzs8sxrjxfjjc"
         / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        assert is_msix_install() is True

    def test_false_when_standard_only(self, tmp_path, monkeypatch):
        local = tmp_path / "Local"
        local.mkdir()
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        assert is_msix_install() is False


class TestGetClaudeConfigDirOrDefault:
    def test_returns_detected_msix_dir(self, tmp_path, monkeypatch):
        local = tmp_path / "Local"
        msix = (local / "Packages" / "Claude_pzs8sxrjxfjjc"
                / "LocalCache" / "Roaming" / "Claude")
        msix.mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.delenv("APPDATA", raising=False)
        assert get_claude_config_dir_or_default() == msix

    def test_falls_back_to_standard_without_creating(self, tmp_path, monkeypatch):
        """No install detected → returns %APPDATA%\\Claude path, does NOT create it."""
        roaming = tmp_path / "Roaming"
        roaming.mkdir()
        monkeypatch.setenv("APPDATA", str(roaming))
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        result = get_claude_config_dir_or_default()
        assert result == roaming / "Claude"
        assert not (roaming / "Claude").exists()  # never created
