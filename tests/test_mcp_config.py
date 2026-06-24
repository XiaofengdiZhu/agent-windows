"""Tests for the [mcp] config section: defaults, DPAPI round-trip, masking."""
import toml
from unittest.mock import patch

from agent_windows.utils import config as cfgmod


def test_default_config_has_mcp_section():
    assert "mcp" in cfgmod.DEFAULT_CONFIG
    mcp = cfgmod.DEFAULT_CONFIG["mcp"]
    assert mcp["transport"] == "stdio"
    assert mcp["host"] == "127.0.0.1"
    assert mcp["port"] == 8000
    assert mcp["auth_key"] == ""
    assert mcp["ssl_certfile"] == ""
    assert mcp["ssl_keyfile"] == ""


def test_save_config_dpapi_encrypts_mcp_auth_key(tmp_path, monkeypatch):
    """save_config must DPAPI-encrypt mcp.auth_key the same way it does vlm.api_key."""
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: tmp_path / "config.toml")
    cfg = dict(cfgmod.DEFAULT_CONFIG)
    cfg["mcp"] = dict(cfg["mcp"], auth_key="super-secret-key")
    cfgmod._config_cache = None

    with patch("agent_windows.utils.config._encrypt_secret",
               side_effect=lambda s: "dpapi:" + s) as enc:
        cfgmod.save_config(cfg)
    # auth_key was passed to the encryptor
    enc.assert_any_call("super-secret-key")

    # the on-disk file holds the dpapi-prefixed blob (i.e. the value went
    # through the encryptor rather than being written as plaintext). With the
    # real DPAPI _encrypt_secret the blob is base64 and never leaks plaintext;
    # here the mock prefixes "dpapi:" to the input, so we assert the prefix
    # and that a non-mocked key would NOT appear as a bare TOML string.
    raw = toml.loads((tmp_path / "config.toml").read_text(encoding="utf-8"))
    assert raw["mcp"]["auth_key"] == "dpapi:super-secret-key"
    # a plaintext (un-encrypted) auth_key would serialize as a bare value with
    # no "dpapi:" prefix — confirm the encryptor was the path taken.
    assert raw["mcp"]["auth_key"] != "super-secret-key"


def test_load_config_decrypts_mcp_auth_key(tmp_path, monkeypatch):
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: tmp_path / "config.toml")
    # write a config with a dpapi-prefixed auth_key
    (tmp_path / "config.toml").write_text(
        toml.dumps({"mcp": {"auth_key": "dpapi:ENCODEDBLOB"}}), encoding="utf-8")
    cfgmod._config_cache = None

    with patch("agent_windows.utils.config._decrypt_secret",
               side_effect=lambda s: s[len("dpapi:"):] if s.startswith("dpapi:") else s):
        cfg = cfgmod.load_config()
    assert cfg["mcp"]["auth_key"] == "ENCODEDBLOB"


def test_config_show_masks_mcp_auth_key(tmp_path, monkeypatch):
    """`config show` must mask mcp.auth_key just like vlm.api_key."""
    monkeypatch.setattr(cfgmod, "get_config_path", lambda: tmp_path / "config.toml")
    cfg = dict(cfgmod.DEFAULT_CONFIG)
    cfg["mcp"] = dict(cfg["mcp"], auth_key="1234567890abcdef")
    cfgmod._config_cache = None
    cfgmod.save_config(cfg)
    cfgmod._config_cache = None

    from typer.testing import CliRunner
    from agent_windows.cli.commands.config_cmd import app
    result = CliRunner().invoke(app, ["show"])
    # the full key must not appear; the masked form (first4...last4) should
    assert "1234567890abcdef" not in result.output
    assert "1234" in result.output and "cdef" in result.output
