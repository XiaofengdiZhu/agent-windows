"""Tests for configuration management."""

import os
import tempfile
import pytest

from agent_windows.utils.config import (
    _deep_merge,
    _decrypt_secret,
    _encrypt_secret,
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
        assert DEFAULT_CONFIG["screenshot"]["resampling"] == "BILINEAR"

    def test_grounding_image_encoding_defaults(self):
        """H-08: vision API defaults to JPEG encoding."""
        g = DEFAULT_CONFIG["grounding"]
        assert g["image_format"] == "JPEG"
        assert g["jpeg_quality"] == 85

    def test_has_tree(self):
        assert "tree" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["tree"]["max_depth"] == 15  # H-04: lowered from 30
        assert DEFAULT_CONFIG["tree"]["max_elements"] == 200  # H-04
        assert DEFAULT_CONFIG["tree"]["timeout"] == 10  # H-04

    def test_has_daemon(self):
        assert "daemon" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["daemon"]["idle_timeout_minutes"] == 30


class TestSecretProtection:
    """M-01: api_key is DPAPI-encrypted at rest and decrypted on load."""

    def test_encrypt_decrypt_roundtrip(self):
        blob = _encrypt_secret("sk-test-12345")
        assert blob.startswith("dpapi:")
        assert "sk-test-12345" not in blob  # plaintext must not appear in the blob
        assert _decrypt_secret(blob) == "sk-test-12345"

    def test_decrypt_passes_plaintext_through(self):
        # Legacy plaintext keys and empty values are returned unchanged.
        assert _decrypt_secret("sk-legacy") == "sk-legacy"
        assert _decrypt_secret("") == ""

    def test_decrypt_corrupt_blob_returns_empty(self):
        # A blob that can't be unprotected is treated as "no key", never leaked.
        assert _decrypt_secret("dpapi:not-valid-base64!!!") == ""

    def test_save_encrypts_and_load_decrypts(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_GROUNDING_API_KEY", raising=False)
        cfg._config_cache = None

        src = {"grounding": {"api_key": "sk-secret"}}
        cfg.save_config(src)

        # On disk: encrypted blob; plaintext absent.
        raw = cfg_path.read_text(encoding="utf-8")
        assert "sk-secret" not in raw
        assert "dpapi:" in raw
        # Caller's dict is not mutated.
        assert src["grounding"]["api_key"] == "sk-secret"
        # Load round-trips back to plaintext.
        loaded = cfg.load_config()
        assert loaded["grounding"]["api_key"] == "sk-secret"

    def test_save_applies_owner_only_acl(self, tmp_path, monkeypatch):
        """M-01: save_config must actually set an owner-only DACL on the file
        (replacing the POSIX chmod no-op). ``_secure_config_file`` is best-effort
        and swallows errors, so the round-trip test above wouldn't catch a broken
        ACL path — this asserts the DACL state directly. Catches the same class
        of bug as the pipe helper (``win32security.GetUserName`` doesn't exist)."""
        import win32api
        import win32security
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_GROUNDING_API_KEY", raising=False)
        cfg._config_cache = None
        cfg.save_config({"grounding": {"api_key": "sk-acl"}})

        sd = win32security.GetFileSecurity(
            str(cfg_path), win32security.DACL_SECURITY_INFORMATION
        )
        dacl = sd.GetSecurityDescriptorDacl()
        assert dacl is not None, "no DACL was set on the config file"
        assert dacl.GetAceCount() == 1, "DACL must grant exactly the owner — no groups"
        # The DACL must be protected from parent-dir inheritance — otherwise a
        # future deletion of the SetSecurityDescriptorControl line would silently
        # let inherited group ACEs broaden access. GetSecurityDescriptorControl
        # reflects this bit regardless of the query info class.
        ctrl = sd.GetSecurityDescriptorControl()[0]
        assert ctrl & win32security.SE_DACL_PROTECTED, (
            "DACL must be protected from parent-dir inheritance (SE_DACL_PROTECTED)"
        )
        # The single ACE grants the current user full access.
        ace_sid = dacl.GetAce(0)[2]
        ace_user = win32security.LookupAccountSid(None, ace_sid)[0]
        assert ace_user.lower() == win32api.GetUserName().lower()

    def test_save_with_nonstring_key_does_not_crash(self, tmp_path, monkeypatch):
        """A malformed config (e.g. hand-edited ``api_key = 123``) must not crash
        save_config — the encrypt guard requires ``str``, so a non-string key is
        written through verbatim instead of hitting ``int.encode()``."""
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        cfg._config_cache = None
        cfg.save_config({"grounding": {"api_key": 123}})  # must not raise
        assert cfg_path.exists()


class TestSecureDirectory:
    """M-01: lock logs/ with an inheritable owner-only DACL. The audit log
    records full commands that may carry secrets; unlike config.toml there are
    many files, so a single directory-level inheritable ACL secures them all."""

    def test_directory_gets_protected_owner_only_dacl(self, tmp_path):
        import win32api
        import win32security
        from agent_windows.utils.config import _secure_directory

        _secure_directory(tmp_path)
        sd = win32security.GetFileSecurity(
            str(tmp_path), win32security.DACL_SECURITY_INFORMATION
        )
        dacl = sd.GetSecurityDescriptorDacl()
        assert dacl is not None and dacl.GetAceCount() == 1, (
            "logs dir must grant exactly the owner — no group ACEs leak history"
        )
        ctrl = sd.GetSecurityDescriptorControl()[0]
        assert ctrl & win32security.SE_DACL_PROTECTED, (
            "DACL must be protected from parent-dir inheritance"
        )
        ace_user = win32security.LookupAccountSid(None, dacl.GetAce(0)[2])[0]
        assert ace_user.lower() == win32api.GetUserName().lower()

    def test_files_in_dir_inherit_owner_only(self, tmp_path):
        """The inheritable ACE is the whole point: a log file created in the
        secured dir (daemon.log, shell_audit.log, a rotated .1 backup) is
        owner-only with no per-file ACL call."""
        import win32api
        import win32security
        from agent_windows.utils.config import _secure_directory

        _secure_directory(tmp_path)
        (tmp_path / "shell_audit.log").write_text("entry\n", encoding="utf-8")
        sd = win32security.GetFileSecurity(
            str(tmp_path / "shell_audit.log"),
            win32security.DACL_SECURITY_INFORMATION,
        )
        dacl = sd.GetSecurityDescriptorDacl()
        assert dacl is not None and dacl.GetAceCount() == 1, (
            "log file must inherit exactly the owner ACE"
        )
        ace_user = win32security.LookupAccountSid(None, dacl.GetAce(0)[2])[0]
        assert ace_user.lower() == win32api.GetUserName().lower()

    def test_get_log_dir_secures_once_per_process(self, tmp_path, monkeypatch):
        """get_log_dir secures logs/ on first call and skips thereafter (the flag
        avoids a SetFileSecurity per shell command). The dir is owner-only
        regardless of whether it pre-existed — covering upgrades."""
        import win32security
        import agent_windows.utils.config as cfg

        monkeypatch.setattr(cfg, "get_data_dir", lambda: tmp_path)
        cfg._logs_dir_secured = False  # reset per-process flag

        log_dir = cfg.get_log_dir()
        assert log_dir == tmp_path / "logs"

        sd = win32security.GetFileSecurity(
            str(log_dir), win32security.DACL_SECURITY_INFORMATION
        )
        assert sd.GetSecurityDescriptorControl()[0] & win32security.SE_DACL_PROTECTED
        assert sd.GetSecurityDescriptorDacl().GetAceCount() == 1
        assert cfg._logs_dir_secured is True  # set so later calls skip the win32 call

    def test_pre_existing_files_re_secured_on_first_call(self, tmp_path, monkeypatch):
        """NTFS inheritance applies at creation time, so a log file that predates
        the hardening (created under a broader inherited ACL) does NOT
        retroactively inherit the directory's new owner-only DACL. get_log_dir's
        first call walks the dir and locks existing files down explicitly — this
        is the upgrade path, the scenario the fix must actually cover. Where the
        test temp dir already grants broader ACEs this is observable; on any
        system the post-condition is owner-only."""
        import win32api
        import win32security
        import agent_windows.utils.config as cfg

        logs = tmp_path / "logs"
        logs.mkdir()
        pre_existing = logs / "daemon.log"  # predates the securing call
        pre_existing.write_text("old entry\n", encoding="utf-8")

        monkeypatch.setattr(cfg, "get_data_dir", lambda: tmp_path)
        cfg._logs_dir_secured = False

        cfg.get_log_dir()  # first call — must walk and lock the pre-existing file

        sd = win32security.GetFileSecurity(
            str(pre_existing), win32security.DACL_SECURITY_INFORMATION
        )
        dacl = sd.GetSecurityDescriptorDacl()
        assert dacl is not None and dacl.GetAceCount() == 1, (
            "pre-existing log must be re-secured to owner-only, not left at its "
            "old broader inherited ACL"
        )
        ace_user = win32security.LookupAccountSid(None, dacl.GetAce(0)[2])[0]
        assert ace_user.lower() == win32api.GetUserName().lower()


class TestSetConfigAllowlist:
    """L-07: set_config_value rejects arbitrary keys, accepts only the schema."""

    def test_known_leaves_are_settable(self):
        from agent_windows.utils.config import _settable_config_keys
        keys = _settable_config_keys()
        assert "grounding.model" in keys
        # shell.blacklist is documented as string-settable (shell_runner reads str|list).
        assert "shell.blacklist" in keys
        # Optional key read with a fallback, not in DEFAULT_CONFIG.
        assert "screenshot.dir" in keys

    def test_section_names_are_not_settable(self):
        """Setting a bare section name would clobber the whole section."""
        from agent_windows.utils.config import _settable_config_keys
        keys = _settable_config_keys()
        assert "grounding" not in keys
        assert "screenshot" not in keys

    def test_rejects_unknown_section(self):
        from agent_windows.utils.config import set_config_value
        with pytest.raises(ValueError, match="unknown config key"):
            set_config_value("evil.payload", "pwned")

    def test_rejects_unknown_key_in_known_section(self):
        from agent_windows.utils.config import set_config_value
        with pytest.raises(ValueError, match="unknown config key"):
            set_config_value("grounding.evil_key", "pwned")

    def test_rejects_bare_section_name(self):
        from agent_windows.utils.config import set_config_value
        with pytest.raises(ValueError, match="unknown config key"):
            set_config_value("grounding", "pwned")

    def test_accepts_known_key(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_GROUNDING_API_KEY", raising=False)
        cfg._config_cache = None

        cfg.set_config_value("grounding.model", "gpt-test")
        assert cfg.load_config()["grounding"]["model"] == "gpt-test"

    def test_accepts_optional_key(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_GROUNDING_API_KEY", raising=False)
        cfg._config_cache = None

        cfg.set_config_value("screenshot.dir", str(tmp_path))
        assert cfg.load_config()["screenshot"]["dir"] == str(tmp_path)

    def test_accepts_shell_blacklist_as_string(self, tmp_path, monkeypatch):
        """shell.blacklist is documented as settable as a single string pattern."""
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_GROUNDING_API_KEY", raising=False)
        cfg._config_cache = None

        cfg.set_config_value("shell.blacklist", "rm -rf")
        assert cfg.load_config()["shell"]["blacklist"] == "rm -rf"
