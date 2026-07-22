"""Tests for the destructive-op audit log (HMAC write/verify, key mgmt)."""
from pathlib import Path

import pytest

from agent_windows.core import audit


@pytest.fixture
def isolated_audit(tmp_path, monkeypatch):
    """Point the audit log + config at a temp dir; enable audit."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    monkeypatch.setattr(audit, "get_log_dir", lambda: log_dir)
    monkeypatch.setattr(
        "agent_windows.core.audit._audit_enabled", lambda: True)
    return log_dir / "action_audit.log"


def _stub_key(monkeypatch, key="fixed-test-key-material-0123456789abcdef"):
    """Pin _get_key to a fixed value so verify is deterministic (no DPAPI)."""
    monkeypatch.setattr(audit, "_get_key", lambda: key.encode("utf-8"))
    return key.encode("utf-8")


class TestWriteAndVerify:
    def test_roundtrip_verifies_ok(self, isolated_audit, monkeypatch):
        _stub_key(monkeypatch)
        audit.audit_destructive("fs_delete", {"path": "C:/x"}, "ok", caller="local")
        report = audit.verify_log()
        assert report.total == 1
        assert report.verified == 1
        assert report.mismatched == []
        assert report.malformed == []
        assert report.ok is True

    def test_tampered_field_is_mismatched(self, isolated_audit, monkeypatch):
        _stub_key(monkeypatch)
        audit.audit_destructive("fs_delete", {"path": "C:/orig"}, "ok", caller="local")
        text = isolated_audit.read_text(encoding="utf-8")
        text = text.replace("C:/orig", "C:/HACK")
        isolated_audit.write_text(text, encoding="utf-8")
        report = audit.verify_log()
        assert report.mismatched == [1]
        assert report.ok is False

    def test_tampered_hmac_is_mismatched(self, isolated_audit, monkeypatch):
        _stub_key(monkeypatch)
        audit.audit_destructive("fs_delete", {"path": "C:/x"}, "ok", caller="local")
        text = isolated_audit.read_text(encoding="utf-8").rstrip("\n")
        prefix, _, _mac = text.rpartition(" | ")
        isolated_audit.write_text(f"{prefix} | {'0' * 64}\n", encoding="utf-8")
        report = audit.verify_log()
        assert report.mismatched == [1]

    def test_malformed_line_reported_separately(self, isolated_audit, monkeypatch):
        _stub_key(monkeypatch)
        isolated_audit.write_text("not a valid line at all\n", encoding="utf-8")
        report = audit.verify_log()
        assert report.malformed == [1]
        assert report.mismatched == []

    def test_verify_missing_file_is_empty_ok(self, tmp_path, monkeypatch):
        _stub_key(monkeypatch)
        report = audit.verify_log(Path(tmp_path / "nope.log"))
        assert report.total == 0
        assert report.ok is True


class TestEscape:
    def test_pipe_in_value_does_not_split_or_inject(self, isolated_audit, monkeypatch):
        """A value carrying '|' / newlines can't forge fields or extra lines."""
        _stub_key(monkeypatch)
        audit.audit_destructive(
            "fs_delete", {"path": "a|b\nc\td"}, "ok", caller="local")
        text = isolated_audit.read_text(encoding="utf-8")
        assert text.count("\n") == 1  # one line, not split by embedded newline
        report = audit.verify_log()
        assert report.verified == 1  # HMAC over escaped content still verifies


class TestRotation:
    def test_rotates_over_cap(self, isolated_audit, monkeypatch):
        _stub_key(monkeypatch)
        monkeypatch.setattr(audit, "_MAX_BYTES", 200)
        for i in range(5):
            audit.audit_destructive("fs_delete", {"path": f"C:/{i}"}, "ok",
                                    caller="local")
        assert (isolated_audit.with_name("action_audit.log.1")).exists()


class TestKeyManagement:
    def test_lazy_key_created_and_dpapi_on_disk(self, tmp_path, monkeypatch):
        """No [audit] key -> first audit generates + persists one (DPAPI on disk)."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        monkeypatch.setattr(audit, "get_log_dir", lambda: log_dir)
        monkeypatch.setattr(audit, "_audit_enabled", lambda: True)
        import agent_windows.utils.config as cfgmod
        monkeypatch.setattr(cfgmod, "get_config_path",
                            lambda: tmp_path / "config.toml")
        monkeypatch.setattr(cfgmod, "_config_cache", None, raising=False)

        audit.audit_destructive("fs_delete", {"path": "C:/x"}, "ok", caller="local")
        raw = (tmp_path / "config.toml").read_text(encoding="utf-8")
        assert "dpapi:" in raw  # persisted key is DPAPI-encrypted at rest

    def test_key_persists_across_calls(self, tmp_path, monkeypatch):
        """Second call reuses the same key (does not regenerate)."""
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        monkeypatch.setattr(audit, "get_log_dir", lambda: log_dir)
        monkeypatch.setattr(audit, "_audit_enabled", lambda: True)
        import agent_windows.utils.config as cfgmod
        monkeypatch.setattr(cfgmod, "get_config_path",
                            lambda: tmp_path / "config.toml")
        monkeypatch.setattr(cfgmod, "_config_cache", None, raising=False)

        audit.audit_destructive("fs_delete", {"path": "C:/a"}, "ok", caller="local")
        audit.audit_destructive("fs_delete", {"path": "C:/b"}, "ok", caller="local")
        assert audit.verify_log().verified == 2


class TestDisabled:
    def test_disabled_writes_nothing(self, isolated_audit, monkeypatch):
        monkeypatch.setattr(audit, "_audit_enabled", lambda: False)
        _stub_key(monkeypatch)
        audit.audit_destructive("fs_delete", {"path": "C:/x"}, "ok", caller="local")
        assert not isolated_audit.exists()


class TestCurrentCaller:
    def test_default_pipe_when_unset(self):
        from agent_windows.core.audit import audit_caller, current_caller
        token = audit_caller.set(None)
        try:
            assert current_caller() == "pipe"
        finally:
            audit_caller.reset(token)


class TestRobustness:
    def test_audit_destructive_never_raises(self, isolated_audit, monkeypatch):
        """A non-serializable param must not escape audit_destructive (never raises)."""
        _stub_key(monkeypatch)
        # object() is not JSON-serializable; this must be swallowed, not raised.
        audit.audit_destructive("fs_delete", {"path": object()}, "ok", caller="local")

    def test_verify_empty_file_does_not_generate_key(self, isolated_audit, monkeypatch):
        """verify on an all-malformed log must NOT fetch (and thus never generate/
        persist) a key — verify is a read-only operator action."""
        import agent_windows.core.audit as aud

        def _no_key():
            raise AssertionError(
                "verify_log must not call _get_key for malformed-only logs")
        monkeypatch.setattr(aud, "_get_key", _no_key)
        isolated_audit.write_text("totally malformed line\n", encoding="utf-8")
        report = audit.verify_log()
        assert report.malformed == [1]
        assert report.total == 0
