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


class TestDataclassDefaults:
    """Dataclass defaults must equal DEFAULT_CONFIG sections byte-for-byte
    (drift guard — they are the source of truth for both)."""

    def test_server_config_matches(self):
        from agent_windows.utils.config import ServerConfig
        s = ServerConfig()
        d = cfgmod.DEFAULT_CONFIG["mcp"]
        assert s.transport == d["transport"]
        assert s.host == d["host"]
        assert s.port == d["port"]
        assert s.auth_key == d["auth_key"]
        assert s.ssl_certfile == d["ssl_certfile"]
        assert s.ssl_keyfile == d["ssl_keyfile"]
        assert s.allow_insecure_remote == d["allow_insecure_remote"]
        assert s.stateless_http == d["stateless_http"]

    def test_security_config_matches(self):
        from agent_windows.utils.config import SecurityConfig
        s = SecurityConfig()
        d = cfgmod.DEFAULT_CONFIG["security"]
        assert s.ip_allowlist == d["ip_allowlist"]
        assert s.cors_origins == d["cors_origins"]

    def test_tools_config_matches(self):
        from agent_windows.utils.config import ToolsConfig
        t = ToolsConfig()
        d = cfgmod.DEFAULT_CONFIG["tools"]
        assert t.include == d["include"]
        assert t.exclude == d["exclude"]


class TestBuildMcpConfig:
    def test_defaults_from_empty(self):
        from agent_windows.utils.config import build_mcp_config, ServerConfig
        view = build_mcp_config({})
        assert isinstance(view.server, ServerConfig)
        assert view.server.port == 8000
        assert view.security.ip_allowlist == []
        assert view.tools.include == []

    def test_maps_custom_values(self):
        from agent_windows.utils.config import build_mcp_config
        view = build_mcp_config({
            "mcp": {"port": 9000, "host": "0.0.0.0", "stateless_http": True},
            "security": {"ip_allowlist": ["10.0.0.0/8"], "cors_origins": ["https://x"]},
            "tools": {"include": ["screenshot"], "exclude": ["shell"]},
        })
        assert view.server.port == 9000
        assert view.server.host == "0.0.0.0"
        assert view.server.stateless_http is True
        assert view.security.ip_allowlist == ["10.0.0.0/8"]
        assert view.security.cors_origins == ["https://x"]
        assert view.tools.include == ["screenshot"]
        assert view.tools.exclude == ["shell"]

    def test_oauth_optional_fields(self):
        from agent_windows.utils.config import build_mcp_config
        view = build_mcp_config({"security": {"oauth_client_id": "abc", "oauth_client_secret": "def"}})
        assert view.security.oauth_client_id == "abc"
        assert view.security.oauth_client_secret == "def"

    def test_list_fields_are_lists(self):
        """Even if a section is missing, list fields are real lists."""
        from agent_windows.utils.config import build_mcp_config
        view = build_mcp_config({})
        assert isinstance(view.security.ip_allowlist, list)
        assert isinstance(view.tools.include, list)


class TestBuildMcpConfigTlsResolution:
    """build_mcp_config 把 ssl_certfile/ssl_keyfile 相对值相对配置文件目录解析。
    绝对路径原样、空值保持空。模块全局 override 状态——每个测试复位。"""

    def teardown_method(self, _method):
        from agent_windows.utils import config as cfg
        cfg.set_config_override(None)

    def test_relative_tls_resolved_against_config_dir(self, tmp_path, monkeypatch):
        from agent_windows.utils.config import build_mcp_config
        from agent_windows.utils import config as cfg
        # 配置文件在 tmp_path/cfg.toml → 父目录 tmp_path。相对 "certs/c.pem"
        # 应解析为 tmp_path/certs/c.pem。
        monkeypatch.setattr(cfg, "get_config_path", lambda: tmp_path / "cfg.toml")
        view = build_mcp_config({"mcp": {"ssl_certfile": "certs/c.pem",
                                         "ssl_keyfile": "certs/k.pem"}})
        assert view.server.ssl_certfile == str((tmp_path / "certs" / "c.pem").resolve())
        assert view.server.ssl_keyfile == str((tmp_path / "certs" / "k.pem").resolve())

    def test_absolute_tls_not_mangled(self, tmp_path, monkeypatch):
        from agent_windows.utils.config import build_mcp_config
        from agent_windows.utils import config as cfg
        monkeypatch.setattr(cfg, "get_config_path", lambda: tmp_path / "cfg.toml")
        abs_cert = tmp_path / "abs-cert.pem"
        abs_key = tmp_path / "abs-key.pem"
        view = build_mcp_config({"mcp": {"ssl_certfile": str(abs_cert),
                                         "ssl_keyfile": str(abs_key)}})
        assert view.server.ssl_certfile == str(abs_cert.resolve())
        assert view.server.ssl_keyfile == str(abs_key.resolve())

    def test_default_empty_tls_stays_empty(self, monkeypatch, tmp_path):
        from agent_windows.utils.config import build_mcp_config
        from agent_windows.utils import config as cfg
        monkeypatch.setattr(cfg, "get_config_path", lambda: tmp_path / "cfg.toml")
        view = build_mcp_config({})
        assert view.server.ssl_certfile == ""
        assert view.server.ssl_keyfile == ""

    def test_resolution_honors_config_override(self, tmp_path, monkeypatch):
        from agent_windows.utils.config import build_mcp_config
        from agent_windows.utils import config as cfg
        # override 到 tmp_path/alt.toml → 解析基准应是 tmp_path
        cfg.set_config_override(str(tmp_path / "alt.toml"))
        view = build_mcp_config({"mcp": {"ssl_certfile": "c.pem"}})
        assert view.server.ssl_certfile == str((tmp_path / "c.pem").resolve())
