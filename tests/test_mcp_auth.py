"""Tests for `mcp auth` Claude Desktop detection + MSIX-aware snippet (TODO #21-4).

Uses CliRunner against the real mcp_app. Config reads/writes are redirected to
tmp_path via monkeypatch on the config module (get_config_path/get_data_dir), so
no real config.toml is touched. LOCALAPPDATA/APPDATA are set per-test so the
detection logic is deterministic and independent of the dev box.
"""
import json
from pathlib import Path

from typer.testing import CliRunner

from agent_windows.cli.commands import mcp_cmd
from agent_windows.utils import config as cfg

runner = CliRunner()


def _obj_block(text: str, start: str, end: str) -> str:
    """Slice the substring between the stdio snippet header and the end marker.

    The stdio branch returns early, so ``end`` may be absent — in that case slice
    to the end of the string.
    """
    i = text.index(start) + len(start)
    j = text.find(end, i)
    return text[i:j] if j != -1 else text[i:]


def _invoke_auth(monkeypatch, tmp_path):
    """Run `mcp auth --force --transport stdio` with config redirected to tmp."""
    monkeypatch.setattr(cfg, "get_data_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg, "get_config_path", lambda: tmp_path / "config.toml")
    monkeypatch.setattr(mcp_cmd, "load_config", lambda: {})
    return runner.invoke(mcp_cmd.app, ["auth", "--force", "--transport", "stdio"])


class TestAuthDetection:
    def test_prints_detected_msix_path(self, monkeypatch, tmp_path):
        local = tmp_path / "Local"
        msix = (local / "Packages" / "Claude_pzs8sxrjxfjjc"
                / "LocalCache" / "Roaming" / "Claude")
        msix.mkdir(parents=True)
        cfg_file = msix / "claude_desktop_config.json"
        cfg_file.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.delenv("APPDATA", raising=False)

        result = _invoke_auth(monkeypatch, tmp_path)
        assert result.exit_code == 0
        assert "Detected Claude Desktop config:" in result.output
        assert str(cfg_file) in result.output
        assert "MSIX install" in result.output

    def test_prints_detected_standard_path(self, monkeypatch, tmp_path):
        roaming = tmp_path / "Roaming"
        std = roaming / "Claude"
        std.mkdir(parents=True)
        monkeypatch.setenv("APPDATA", str(roaming))
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        result = _invoke_auth(monkeypatch, tmp_path)
        assert result.exit_code == 0
        assert "Detected Claude Desktop config:" in result.output
        assert str(std / "claude_desktop_config.json") in result.output
        assert "MSIX install" not in result.output

    def test_not_detected_falls_back_to_standard_location(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

        result = _invoke_auth(monkeypatch, tmp_path)
        assert result.exit_code == 0
        assert "Claude Desktop not detected" in result.output
        assert "Standard config location:" in result.output


class TestAuthStdioSnippet:
    def test_msix_snippet_uses_absolute_command(self, monkeypatch, tmp_path):
        """MSIX → snippet command is an absolute path (not bare 'agent-windows')."""
        local = tmp_path / "Local"
        (local / "Packages" / "Claude_pzs8sxrjxfjjc"
         / "LocalCache" / "Roaming" / "Claude").mkdir(parents=True)
        monkeypatch.setenv("LOCALAPPDATA", str(local))
        monkeypatch.delenv("APPDATA", raising=False)

        result = _invoke_auth(monkeypatch, tmp_path)
        assert result.exit_code == 0

        block = _obj_block(result.output,
                           "─── Claude Desktop config (stdio) ───",
                           "─── Start the server ───")
        data = json.loads(block)
        cmd = data["mcpServers"]["agent-windows"]["command"]
        # Absolute path: either a .exe next to the interpreter, or the interpreter
        # itself (fallback). Either way NOT the bare "agent-windows".
        assert cmd != "agent-windows"
        assert Path(cmd).is_absolute()
        args = data["mcpServers"]["agent-windows"]["args"]
        assert args[-2:] == ["mcp", "serve"]

    def test_non_msix_snippet_uses_bare_command(self, monkeypatch, tmp_path):
        """Non-MSIX → snippet keeps the bare 'agent-windows' command."""
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))

        result = _invoke_auth(monkeypatch, tmp_path)
        assert result.exit_code == 0

        block = _obj_block(result.output,
                           "─── Claude Desktop config (stdio) ───",
                           "─── Start the server ───")
        data = json.loads(block)
        assert data["mcpServers"]["agent-windows"]["command"] == "agent-windows"
        assert data["mcpServers"]["agent-windows"]["args"] == ["mcp", "serve"]
