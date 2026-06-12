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
