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
        base = {"vlm": {"model": "a", "width": 1000}}
        override = {"vlm": {"model": "b"}}
        result = _deep_merge(base, override)
        assert result["vlm"]["model"] == "b"
        assert result["vlm"]["width"] == 1000

    def test_no_mutate_base(self):
        base = {"a": 1}
        _deep_merge(base, {"a": 2})
        assert base["a"] == 1

    def test_nested_base_not_aliased(self):
        """_deep_merge must not alias base's nested dicts into the result —
        DEFAULT_CONFIG is the validation schema and must never be mutated by a
        caller that writes into the merged config (e.g. set_config_value)."""
        base = {"shell": {"blacklist": []}}
        result = _deep_merge(base, {})
        assert result["shell"] is not base["shell"]
        result["shell"]["blacklist"] = "contaminated"
        assert base["shell"]["blacklist"] == []


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
        assert "vlm" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["vlm"]["enabled"] is True
        assert DEFAULT_CONFIG["vlm"]["width"] == 1000

    def test_has_screenshot(self):
        assert "screenshot" in DEFAULT_CONFIG
        assert DEFAULT_CONFIG["screenshot"]["backend"] == "auto"
        assert DEFAULT_CONFIG["screenshot"]["resampling"] == "BILINEAR"

    def test_grounding_image_encoding_defaults(self):
        """Vision API defaults to JPEG encoding."""
        g = DEFAULT_CONFIG["vlm"]
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
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None

        src = {"vlm": {"api_key": "sk-secret"}}
        cfg.save_config(src)

        # On disk: encrypted blob; plaintext absent.
        raw = cfg_path.read_text(encoding="utf-8")
        assert "sk-secret" not in raw
        assert "dpapi:" in raw
        # Caller's dict is not mutated.
        assert src["vlm"]["api_key"] == "sk-secret"
        # Load round-trips back to plaintext.
        loaded = cfg.load_config()
        assert loaded["vlm"]["api_key"] == "sk-secret"

    def test_save_applies_owner_only_acl(self, tmp_path, monkeypatch):
        """save_config must actually set an owner-only DACL on the file
        (replacing the POSIX chmod no-op). ``_secure_config_file`` is best-effort
        and swallows errors, so the round-trip test above wouldn't catch a broken
        ACL path — this asserts the DACL state directly. Catches the same class
        of bug as the pipe helper (``win32security.GetUserName`` doesn't exist)."""
        import win32api
        import win32security
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None
        cfg.save_config({"vlm": {"api_key": "sk-acl"}})

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
        cfg.save_config({"vlm": {"api_key": 123}})  # must not raise
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
    """set_config_value rejects arbitrary keys, accepts only the schema."""

    def test_known_leaves_are_settable(self):
        from agent_windows.utils.config import _settable_config_keys
        keys = _settable_config_keys()
        assert "vlm.model" in keys
        # shell.blacklist is documented as string-settable (shell_runner reads str|list).
        assert "shell.blacklist" in keys
        # Optional key read with a fallback, not in DEFAULT_CONFIG.
        assert "screenshot.dir" in keys

    def test_section_names_are_not_settable(self):
        """Setting a bare section name would clobber the whole section."""
        from agent_windows.utils.config import _settable_config_keys
        keys = _settable_config_keys()
        assert "vlm" not in keys
        assert "screenshot" not in keys

    def test_rejects_unknown_section(self):
        from agent_windows.utils.config import set_config_value
        with pytest.raises(ValueError, match="unknown config key"):
            set_config_value("evil.payload", "pwned")

    def test_rejects_unknown_key_in_known_section(self):
        from agent_windows.utils.config import set_config_value
        with pytest.raises(ValueError, match="unknown config key"):
            set_config_value("vlm.evil_key", "pwned")

    def test_rejects_bare_section_name(self):
        from agent_windows.utils.config import set_config_value
        with pytest.raises(ValueError, match="unknown config key"):
            set_config_value("vlm", "pwned")

    def test_accepts_known_key(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None

        cfg.set_config_value("vlm.model", "gpt-test")
        assert cfg.load_config()["vlm"]["model"] == "gpt-test"

    def test_accepts_optional_key(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None

        cfg.set_config_value("screenshot.dir", str(tmp_path))
        assert cfg.load_config()["screenshot"]["dir"] == str(tmp_path)

    def test_accepts_shell_blacklist_as_string(self, tmp_path, monkeypatch):
        """shell.blacklist is documented as settable as a single string pattern."""
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None

        cfg.set_config_value("shell.blacklist", "rm -rf")
        assert cfg.load_config()["shell"]["blacklist"] == ["rm -rf"]


class TestParseValueTyped:
    """_parse_value_typed parses a CLI string by the key's expected type."""

    def test_int_zero_is_int_not_false(self):
        from agent_windows.utils.config import _parse_value_typed
        assert _parse_value_typed("0", int) == 0
        assert _parse_value_typed("0", int) is not False

    def test_int_value(self):
        from agent_windows.utils.config import _parse_value_typed
        assert _parse_value_typed("8000", int) == 8000

    def test_int_rejects_non_number(self):
        from agent_windows.utils.config import _parse_value_typed
        with pytest.raises(ValueError):
            _parse_value_typed("abc", int)

    def test_bool_variants(self):
        from agent_windows.utils.config import _parse_value_typed
        assert _parse_value_typed("true", bool) is True
        assert _parse_value_typed("yes", bool) is True
        assert _parse_value_typed("1", bool) is True
        assert _parse_value_typed("false", bool) is False
        assert _parse_value_typed("no", bool) is False
        assert _parse_value_typed("0", bool) is False

    def test_bool_rejects_garbage(self):
        from agent_windows.utils.config import _parse_value_typed
        with pytest.raises(ValueError):
            _parse_value_typed("maybe", bool)

    def test_float_accepts_int_string(self):
        from agent_windows.utils.config import _parse_value_typed
        assert _parse_value_typed("1", float) == 1.0

    def test_str_not_coerced(self):
        # Regression: "true" must stay a string for a str key, NOT become bool.
        from agent_windows.utils.config import _parse_value_typed
        assert _parse_value_typed("true", str) == "true"
        assert _parse_value_typed("42", str) == "42"

    def test_list_wraps_scalar(self):
        from agent_windows.utils.config import _parse_value_typed
        assert _parse_value_typed("rm -rf", list) == ["rm -rf"]

    def test_unknown_type_falls_back(self):
        # None expected type (optional key like screenshot.dir) -> legacy _parse_value.
        from agent_windows.utils.config import _parse_value_typed
        assert _parse_value_typed("/tmp/x", None) == "/tmp/x"

    def test_default_leaf_type_lookup(self):
        from agent_windows.utils.config import _default_leaf_type
        assert _default_leaf_type("mcp.port") is int
        assert _default_leaf_type("vlm.enabled") is bool
        assert _default_leaf_type("shell.blacklist") is list
        assert _default_leaf_type("screenshot.dir") is None  # optional, unknown
        assert _default_leaf_type("vlm") is None  # section, not a leaf


class TestLoadConfigValidation:
    """load_config rejects type-mismatched configs (TODO #10)."""

    def test_bad_type_raises(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None

        cfg_path.write_text('[mcp]\nport = true\n', encoding="utf-8")
        with pytest.raises(ValueError, match=r"mcp\.port"):
            cfg.load_config()

    def test_good_config_loads(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None

        cfg_path.write_text('[mcp]\nport = 9000\n', encoding="utf-8")
        loaded = cfg.load_config()
        assert loaded["mcp"]["port"] == 9000

    def test_recover_after_fix(self, tmp_path, monkeypatch):
        """A config that fails validation loads fine once corrected (mtime
        cache does not get stuck on the bad state)."""
        import agent_windows.utils.config as cfg

        cfg_path = tmp_path / "config.toml"
        monkeypatch.setattr(cfg, "get_config_path", lambda: cfg_path)
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        cfg._config_cache = None

        cfg_path.write_text('[mcp]\nport = true\n', encoding="utf-8")
        with pytest.raises(ValueError):
            cfg.load_config()
        # Correct and rewrite with a new mtime.
        cfg_path.write_text('[mcp]\nport = 9000\n', encoding="utf-8")
        assert cfg.load_config()["mcp"]["port"] == 9000


class TestTrimToNonDefaults:
    def test_all_defaults_to_empty(self):
        from agent_windows.utils.config import _trim_to_non_defaults
        import copy
        assert _trim_to_non_defaults(copy.deepcopy(DEFAULT_CONFIG)) == {}

    def test_only_non_default_kept(self):
        from agent_windows.utils.config import _trim_to_non_defaults
        cfg = {"mcp": {"port": 9000, "transport": "stdio", "host": "127.0.0.1"}}
        # Only port differs from defaults; transport/host equal -> dropped.
        assert _trim_to_non_defaults(cfg) == {"mcp": {"port": 9000}}

    def test_unknown_key_preserved(self):
        from agent_windows.utils.config import _trim_to_non_defaults
        cfg = {"vlm": {"model": "gpt-4o-mini", "prompts": {"x": 1}}, "evil": 1}
        # vlm.model equals default -> dropped; unknown vlm.prompts and [evil] kept.
        assert _trim_to_non_defaults(cfg) == {"vlm": {"prompts": {"x": 1}}, "evil": 1}

    def test_empty_section_dropped(self):
        from agent_windows.utils.config import _trim_to_non_defaults
        cfg = {"mcp": {"transport": "stdio"}}  # all equal default -> empty section
        assert _trim_to_non_defaults(cfg) == {}


class TestWriteConfig:
    def test_writes_only_non_defaults(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        path = tmp_path / "config.toml"
        cfg.write_config({"mcp": {"port": 9000}}, path)
        text = path.read_text(encoding="utf-8")
        assert "port = 9000" in text
        assert "transport" not in text  # default value not written

    def test_default_config_writes_empty_file(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg
        import copy

        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        path = tmp_path / "config.toml"
        cfg.write_config(copy.deepcopy(cfg.DEFAULT_CONFIG), path)
        assert path.read_text(encoding="utf-8").strip() == ""

    def test_encrypts_secret(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        monkeypatch.setattr(cfg, "_encrypt_secret", lambda s: "dpapi:ENC")
        path = tmp_path / "config.toml"
        cfg.write_config({"vlm": {"api_key": "sk-secret"}}, path)
        text = path.read_text(encoding="utf-8")
        assert "dpapi:ENC" in text
        assert "sk-secret" not in text

    def test_round_trip_preserves_values(self, tmp_path, monkeypatch):
        import agent_windows.utils.config as cfg

        monkeypatch.setattr(cfg, "get_config_path", lambda: tmp_path / "config.toml")
        monkeypatch.delenv("AGENT_WINDOWS_VLM_API_KEY", raising=False)
        monkeypatch.setattr(cfg, "_encrypt_secret", lambda s: "dpapi:" + s)
        monkeypatch.setattr(cfg, "_decrypt_secret", lambda v: v[len("dpapi:"):] if v.startswith("dpapi:") else v)
        cfg._config_cache = None

        src = {"mcp": {"port": 9000}, "vlm": {"api_key": "sk-secret"}}
        cfg.write_config(src, tmp_path / "config.toml")
        loaded = cfg.load_config()
        assert loaded["mcp"]["port"] == 9000
        assert loaded["vlm"]["api_key"] == "sk-secret"


class TestCheckLeaf:
    """_check_leaf enforces the per-type rules (bool/int disambiguation etc.)."""

    def _leaf(self, default_val, actual_val):
        from agent_windows.utils.config import _check_leaf
        errors: list[str] = []
        _check_leaf("k", default_val, actual_val, errors)
        return errors

    def test_bool_accepts_bool(self):
        assert self._leaf(True, True) == []
        assert self._leaf(False, False) == []

    def test_bool_rejects_int(self):
        assert len(self._leaf(False, 1)) == 1 and self._leaf(False, 1)[0].startswith("k")

    def test_bool_rejects_str(self):
        assert len(self._leaf(True, "yes")) == 1

    def test_int_accepts_int(self):
        assert self._leaf(8000, 8000) == []

    def test_int_rejects_bool(self):
        # TOML bool is an int subclass — must be rejected explicitly.
        assert len(self._leaf(8000, True)) == 1

    def test_int_rejects_str(self):
        assert len(self._leaf(8000, "8000")) == 1

    def test_float_accepts_int(self):
        assert self._leaf(0.5, 1) == []  # int is acceptable for a float default

    def test_float_rejects_bool(self):
        assert len(self._leaf(0.5, True)) == 1

    def test_float_rejects_str(self):
        assert len(self._leaf(0.5, "high")) == 1

    def test_str_accepts_str(self):
        assert self._leaf("x", "y") == []

    def test_str_rejects_int(self):
        assert len(self._leaf("x", 1)) == 1

    def test_list_accepts_list(self):
        assert self._leaf([], ["a"]) == []

    def test_list_rejects_str(self):
        assert len(self._leaf([], "a")) == 1

    def test_dict_accepts_dict(self):
        assert self._leaf({"a": 1}, {"b": 2}) == []


class TestValidateConfig:
    """_validate_config walks DEFAULT_CONFIG and collects all type errors."""

    def test_valid_default_config_passes(self):
        from agent_windows.utils.config import _validate_config
        import copy
        _validate_config(copy.deepcopy(DEFAULT_CONFIG))  # must not raise

    def test_empty_user_config_passes(self):
        from agent_windows.utils.config import _validate_config
        _validate_config({})  # all leaves missing -> skipped

    def test_bool_when_int_expected_rejected(self):
        from agent_windows.utils.config import _validate_config
        with pytest.raises(ValueError, match=r"mcp\.port"):
            _validate_config({"mcp": {"port": True}})

    def test_str_when_list_expected_rejected(self):
        from agent_windows.utils.config import _validate_config
        with pytest.raises(ValueError, match=r"shell\.blacklist"):
            _validate_config({"shell": {"blacklist": "rm -rf"}})

    def test_str_when_bool_expected_rejected(self):
        from agent_windows.utils.config import _validate_config
        with pytest.raises(ValueError, match=r"telemetry\.enabled"):
            _validate_config({"telemetry": {"enabled": "yes"}})

    def test_unknown_key_passthrough(self):
        from agent_windows.utils.config import _validate_config
        # vlm.prompts and [evil] are not in DEFAULT_CONFIG -> not validated.
        _validate_config({"vlm": {"prompts": {"x": 1}}, "evil": {"payload": 42}})

    def test_collects_multiple_errors(self):
        from agent_windows.utils.config import _validate_config
        with pytest.raises(ValueError) as exc_info:
            _validate_config({"mcp": {"port": True}, "telemetry": {"enabled": "yes"}})
        msg = str(exc_info.value)
        assert "mcp.port" in msg
        assert "telemetry.enabled" in msg
