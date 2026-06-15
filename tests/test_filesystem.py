"""Tests for filesystem operations: read, write, list, and path validation."""
import pytest
from pathlib import Path


class TestReadFile:
    def test_read_existing_file(self, tmp_path):
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "test.txt"
        f.write_text("hello world", encoding="utf-8")
        result = read_file(str(f))
        assert result["content"] == "hello world"
        assert result["size"] == 11

    def test_read_nonexistent_raises(self):
        from agent_windows.core.filesystem import read_file
        with pytest.raises(FileNotFoundError):
            read_file("C:\\nonexistent\\file.txt")

    def test_read_directory_raises(self, tmp_path):
        from agent_windows.core.filesystem import read_file
        with pytest.raises(ValueError, match="directory"):
            read_file(str(tmp_path))


class TestWriteFile:
    def test_write_creates_file(self, tmp_path):
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "new.txt"
        write_file(str(f), "content")
        assert f.read_text(encoding="utf-8") == "content"

    def test_write_creates_parent_dirs(self, tmp_path):
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "sub" / "dir" / "file.txt"
        write_file(str(f), "deep")
        assert f.exists()

    def test_write_overwrites_existing(self, tmp_path):
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "existing.txt"
        f.write_text("old", encoding="utf-8")
        write_file(str(f), "new")
        assert f.read_text(encoding="utf-8") == "new"


class TestListDir:
    def test_list_with_files_and_dirs(self, tmp_path):
        from agent_windows.core.filesystem import list_dir
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        (tmp_path / "subdir").mkdir()
        result = list_dir(str(tmp_path))
        assert result["count"] == 2

    def test_list_empty_dir(self, tmp_path):
        from agent_windows.core.filesystem import list_dir
        result = list_dir(str(tmp_path))
        assert result["count"] == 0

    def test_list_nonexistent_raises(self):
        from agent_windows.core.filesystem import list_dir
        with pytest.raises(FileNotFoundError):
            list_dir("C:\\nonexistent_dir_xyz")


class TestPathBlacklist:
    """Tests for _validate_path blocking sensitive paths."""

    def test_block_ssh_dir(self):
        from agent_windows.core.filesystem import _validate_path
        with pytest.raises(ValueError, match="restricted"):
            _validate_path(r"C:\Users\alice\.ssh\id_rsa")

    def test_block_system32(self):
        from agent_windows.core.filesystem import _validate_path
        with pytest.raises(ValueError, match="Access denied"):
            _validate_path(r"C:\Windows\System32\cmd.exe")

    def test_block_gnupg(self):
        from agent_windows.core.filesystem import _validate_path
        with pytest.raises(ValueError, match="restricted"):
            _validate_path(r"C:\Users\alice\.gnupg\pubring.kbx")

    def test_block_ntuser_dat(self):
        from agent_windows.core.filesystem import _validate_path
        with pytest.raises(ValueError, match="restricted"):
            _validate_path(r"C:\Users\alice\NTUSER.DAT")

    def test_allow_normal_path(self):
        from agent_windows.core.filesystem import _validate_path
        p = _validate_path(r"C:\Users\alice\Documents\report.txt")
        assert isinstance(p, Path)

    def test_allow_security_in_path_component(self):
        """Paths containing 'security' as part of a folder name should be allowed."""
        from agent_windows.core.filesystem import _validate_path
        # This should NOT be blocked since 'security' is not a path component
        p = _validate_path(r"C:\code\security-scanner\main.py")
        assert isinstance(p, Path)

    def test_allow_default_in_path_component(self):
        """Paths containing 'default' as part of a folder name should be allowed."""
        from agent_windows.core.filesystem import _validate_path
        p = _validate_path(r"C:\Users\alice\my_default_folder\notes.txt")
        assert isinstance(p, Path)


class TestWriteProtection:
    """M-04: write_file refuses the agent's install tree and data directory."""

    def test_refuses_data_dir(self):
        """Writing into ~/.agent-windows/ (config/pid/logs) is rejected."""
        from agent_windows.core.filesystem import write_file
        target = Path.home() / ".agent-windows" / "evil_m04_marker.txt"
        assert not target.exists()
        with pytest.raises(ValueError, match="install tree"):
            write_file(str(target), "pwned")
        assert not target.exists()

    def test_refuses_install_tree(self):
        """Writing into the agent's own package directory is rejected."""
        from agent_windows.core.filesystem import write_file, _protected_write_roots
        install = _protected_write_roots()[0]  # the agent_windows package dir
        target = install / "evil_m04_marker.py"
        with pytest.raises(ValueError, match="install tree"):
            write_file(str(target), "pwned")
        assert not target.exists()

    def test_refuses_install_parent(self):
        """Writing into the source/site-packages root is rejected."""
        from agent_windows.core.filesystem import write_file, _protected_write_roots
        install = _protected_write_roots()[0]  # the agent_windows package dir
        parent = install.parent
        if len(parent.parts) <= 1:
            pytest.skip("install parent is a drive root in this layout")
        with pytest.raises(ValueError, match="install tree"):
            write_file(str(parent / "evil_m04_marker.txt"), "pwned")

    def test_install_parent_never_drive_root(self):
        """No protected root may be a bare drive root (would block all writes)."""
        from agent_windows.core.filesystem import _protected_write_roots
        for root in _protected_write_roots():
            assert len(root.parts) > 1, f"{root} is a drive root — would over-block"

    def test_validate_write_path_public(self, tmp_path):
        """Public validate_write_path mirrors write_file's protection."""
        from agent_windows.core.filesystem import validate_write_path
        p = validate_write_path(str(tmp_path / "ok.txt"))
        assert isinstance(p, Path)

    def test_allows_normal_write(self, tmp_path):
        """A write to an ordinary temp dir is still permitted."""
        from agent_windows.core.filesystem import _validate_write_path
        p = _validate_write_path(str(tmp_path / "ok.txt"))
        assert isinstance(p, Path)


class TestExecutableLaunchProtection:
    """L-03: launch_app refuses binaries from credential dirs / agent tree."""

    @pytest.mark.parametrize("cred", [".ssh", ".aws", ".azure", ".kube", ".docker", ".gnupg"])
    def test_refuses_credential_dir(self, cred):
        from agent_windows.core.filesystem import validate_executable_path
        with pytest.raises(ValueError, match="credential"):
            validate_executable_path(rf"C:\Users\alice\{cred}\payload.exe")

    def test_refuses_data_dir(self):
        from agent_windows.core.filesystem import validate_executable_path
        target = Path.home() / ".agent-windows" / "payload.exe"
        with pytest.raises(ValueError, match="install tree"):
            validate_executable_path(str(target))

    def test_refuses_install_tree(self):
        from agent_windows.core.filesystem import validate_executable_path, _protected_write_roots
        install = _protected_write_roots()[0]
        with pytest.raises(ValueError, match="install tree"):
            validate_executable_path(str(install / "payload.exe"))

    def test_allows_bare_name(self):
        """A PATH-resolved bare name (no dir) never matches a credential dir."""
        from agent_windows.core.filesystem import validate_executable_path
        validate_executable_path("notepad")  # must not raise

    def test_bare_name_passes_even_from_install_cwd(self, monkeypatch):
        """L-03-O1: a bare name must pass even when the daemon cwd is inside the
        install tree — resolve() would otherwise anchor it there and falsely
        deny a legit launch."""
        from agent_windows.core.filesystem import validate_executable_path, _protected_write_roots
        monkeypatch.chdir(str(_protected_write_roots()[0]))
        validate_executable_path("notepad")  # must not raise

    def test_allows_normal_path(self, tmp_path):
        from agent_windows.core.filesystem import validate_executable_path
        validate_executable_path(str(tmp_path / "app.exe"))  # must not raise

    def test_allows_system32(self):
        """Legit system apps under System32 stay launchable (not blocklisted)."""
        from agent_windows.core.filesystem import validate_executable_path
        validate_executable_path(r"C:\Windows\System32\notepad.exe")  # must not raise
