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

    def test_read_large_file_truncated(self, tmp_path):
        """A file exceeding MAX_READ_BYTES is truncated, not read whole. (SEC3-03)"""
        from agent_windows.core import filesystem
        from agent_windows.core.filesystem import read_file
        # Write a file just over the cap.
        cap = filesystem.MAX_READ_BYTES
        f = tmp_path / "big.txt"
        f.write_bytes(b"x" * (cap + 1000))
        result = read_file(str(f))
        assert result["truncated"] is True
        assert result["size"] == cap + 1000  # full size reported
        assert len(result["content"]) == cap  # content capped

    def test_read_under_cap_not_truncated(self, tmp_path):
        """A file under the cap reads fully and reports truncated=False."""
        from agent_windows.core import filesystem
        from agent_windows.core.filesystem import read_file
        cap = filesystem.MAX_READ_BYTES
        f = tmp_path / "small.txt"
        f.write_bytes(b"y" * (cap - 1))
        result = read_file(str(f))
        assert result["truncated"] is False
        assert len(result["content"]) == cap - 1


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

    def test_list_huge_dir_truncated(self, tmp_path):
        """A directory with more than MAX_LIST_ENTRIES is truncated, and the full
        total is still reported in count. (SEC3-03)"""
        from agent_windows.core import filesystem
        from agent_windows.core.filesystem import list_dir
        cap = filesystem.MAX_LIST_ENTRIES
        for i in range(cap + 50):
            (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        result = list_dir(str(tmp_path))
        assert result["truncated"] is True
        assert result["count"] == cap + 50  # real total
        # output lists only the first `cap` entries (plus header/notice lines)
        listed = [l for l in result["output"].splitlines() if l.startswith("  ")]
        assert len(listed) == cap


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


class TestReparsePointEvasion:
    """M-03: junctions, 8.3 short names, and case must not bypass the blacklist.

    ``resolve()`` in :func:`_validate_path` follows directory junctions (and
    symlinks, via the same reparse-point traversal) and expands 8.3 names + case,
    so a benign-named link into a credential dir still resolves to the blocked
    ``.ssh`` component. These tests exercise junctions — symlinks need elevation
    on Windows and are skipped, but they hit the identical resolve() code path.
    They lock the defense in so a future refactor can't silently swap
    ``resolve()`` for a non-resolving check and reopen the bypass.
    """

    @staticmethod
    def _make_junction(link: Path, target: Path) -> None:
        """Create a Windows directory junction (needs no elevation); skip on failure."""
        import subprocess

        r = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
        )
        if r.returncode != 0 or not link.exists():
            pytest.skip(f"cannot create junction on this host (rc={r.returncode})")

    @staticmethod
    def _short_name(path: Path) -> Path | None:
        """8.3 short form of ``path`` via GetShortPathName, or None when 8.3
        generation is off on the volume (short name == long name)."""
        try:
            import win32api
        except ImportError:
            pytest.skip("pywin32 not available")
        short = Path(win32api.GetShortPathName(str(path)))
        if short.name.lower() == path.name.lower():
            return None  # 8.3 generation disabled on this volume
        return short

    def test_junction_into_credential_dir_blocked(self, tmp_path):
        """A benign-named junction directly into .ssh resolves to .ssh → blocked."""
        from agent_windows.core.filesystem import _validate_path

        home = tmp_path / "home"
        cred = home / ".ssh"
        cred.mkdir(parents=True)
        (cred / "id_rsa").write_text("x")
        link = home / "datalink"
        self._make_junction(link, cred)
        with pytest.raises(ValueError, match=r"\.ssh"):
            _validate_path(str(link / "id_rsa"))

    def test_junction_as_intermediate_blocked(self, tmp_path):
        """A junction used as a mid-path component still resolves to .ssh."""
        from agent_windows.core.filesystem import _validate_path

        home = tmp_path / "home"
        cred = home / ".ssh"
        cred.mkdir(parents=True)
        (cred / "id_rsa").write_text("x")
        proj = home / "project" / "data"
        proj.mkdir(parents=True)
        link = proj / "misc"
        self._make_junction(link, cred)
        with pytest.raises(ValueError, match=r"\.ssh"):
            _validate_path(str(link / "id_rsa"))

    def test_short_name_8_3_blocked(self, tmp_path):
        """An 8.3 alias (e.g. SSH~1) of .ssh expands back to .ssh → blocked."""
        from agent_windows.core.filesystem import _validate_path

        home = tmp_path / "home"
        cred = home / ".ssh"
        cred.mkdir(parents=True)
        (cred / "id_rsa").write_text("x")
        short_cred = self._short_name(cred)
        if short_cred is None:
            pytest.skip("8.3 short names disabled on this volume")
        victim = short_cred / "id_rsa"
        assert victim.exists(), "short-name path should resolve to the same file"
        with pytest.raises(ValueError, match=r"\.ssh"):
            _validate_path(str(victim))

    def test_case_variant_blocked(self):
        """A different-case component (.SSH) lower-cases to .ssh → blocked."""
        from agent_windows.core.filesystem import _validate_path

        with pytest.raises(ValueError, match="restricted"):
            _validate_path(r"C:\Users\alice\.SSH\id_rsa")

    @pytest.mark.parametrize(
        "validator, fname",
        [
            ("validate_read_path", "id_rsa"),
            ("validate_write_path", "evil.txt"),
            ("validate_executable_path", "payload.exe"),
        ],
    )
    def test_public_validators_defeat_junction(self, tmp_path, validator, fname):
        """The handler-facing validators (not just _validate_path) block a
        junction into .ssh — the defense reaches the actual attack surface."""
        import agent_windows.core.filesystem as fs

        fn = getattr(fs, validator)
        home = tmp_path / "home"
        cred = home / ".ssh"
        cred.mkdir(parents=True)
        (cred / fname).write_text("x")
        link = home / "datalink"
        self._make_junction(link, cred)
        with pytest.raises(ValueError):
            fn(str(link / fname))


# ── TODO #4: enhanced existing ops + 5 new ops ────────────────────────


class TestReadOffsetLimit:
    def test_offset_limit_slices_lines(self, tmp_path):
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "lines.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
        r = read_file(str(f), offset=2, limit=2)
        assert r["content"] == "l2\nl3\n"
        assert r["lines_total"] == 5
        assert r["lines_shown"] == "2-3"

    def test_offset_only_to_eof(self, tmp_path):
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "lines.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        r = read_file(str(f), offset=2)
        assert r["content"] == "b\nc\n"

    def test_offset_beyond_end_empty(self, tmp_path):
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "lines.txt"
        f.write_text("only\n", encoding="utf-8")
        r = read_file(str(f), offset=99)
        assert r["content"] == ""

    def test_no_offset_no_limit_reads_whole(self, tmp_path):
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "whole.txt"
        f.write_text("hello world", encoding="utf-8")
        r = read_file(str(f))
        assert r["content"] == "hello world"
        assert "lines_total" not in r  # whole-file path, not line-range path


class TestWriteAppend:
    def test_append_adds_to_existing(self, tmp_path):
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "log.txt"
        f.write_text("line1\n", encoding="utf-8")
        write_file(str(f), "line2\n", append=True)
        assert f.read_text(encoding="utf-8") == "line1\nline2\n"

    def test_append_creates_if_missing(self, tmp_path):
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "new.txt"
        write_file(str(f), "first", append=True)
        assert f.read_text(encoding="utf-8") == "first"

    def test_default_overwrites(self, tmp_path):
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "x.txt"
        f.write_text("old", encoding="utf-8")
        write_file(str(f), "new")
        assert f.read_text(encoding="utf-8") == "new"

    def test_append_str_truthiness(self, tmp_path):
        """append='true' (string) is tolerated per _as_bool."""
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "x.txt"
        f.write_text("a", encoding="utf-8")
        write_file(str(f), "b", append="true")
        assert f.read_text(encoding="utf-8") == "ab"


class TestListEnhancements:
    def test_pattern_filter(self, tmp_path):
        from agent_windows.core.filesystem import list_dir
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        (tmp_path / "c.py").write_text("x", encoding="utf-8")
        r = list_dir(str(tmp_path), pattern="*.py")
        assert r["count"] == 2
        assert "a.py" in r["output"]
        assert "c.py" in r["output"]
        assert "b.txt" not in r["output"]

    def test_recursive_walks_subdirs(self, tmp_path):
        from agent_windows.core.filesystem import list_dir
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.txt").write_text("x", encoding="utf-8")
        (tmp_path / "top.txt").write_text("x", encoding="utf-8")
        r = list_dir(str(tmp_path), recursive=True)
        # rglob("*") yields sub, sub/deep.txt, and top.txt = 3 entries.
        assert r["count"] == 3
        assert "deep.txt" in r["output"]
        assert "top.txt" in r["output"]

    def test_show_hidden_includes_dotfiles(self, tmp_path):
        from agent_windows.core.filesystem import list_dir
        (tmp_path / ".secret").write_text("x", encoding="utf-8")
        (tmp_path / "visible.txt").write_text("x", encoding="utf-8")
        hidden = list_dir(str(tmp_path))
        assert ".secret" not in hidden["output"]
        shown = list_dir(str(tmp_path), show_hidden=True)
        assert ".secret" in shown["output"]


class TestCopyPath:
    def test_copy_file(self, tmp_path):
        from agent_windows.core.filesystem import copy_path
        src = tmp_path / "a.txt"
        src.write_text("data", encoding="utf-8")
        dst = tmp_path / "b.txt"
        copy_path(str(src), str(dst))
        assert dst.read_text(encoding="utf-8") == "data"
        assert src.exists()  # source preserved

    def test_copy_directory(self, tmp_path):
        from agent_windows.core.filesystem import copy_path
        src = tmp_path / "d"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dst = tmp_path / "d2"
        copy_path(str(src), str(dst))
        assert (dst / "f.txt").read_text(encoding="utf-8") == "x"

    def test_refuses_existing_without_overwrite(self, tmp_path):
        from agent_windows.core.filesystem import copy_path
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        dst = tmp_path / "b.txt"
        dst.write_text("old", encoding="utf-8")
        with pytest.raises(ValueError, match="already exists"):
            copy_path(str(src), str(dst))

    def test_overwrite_replaces(self, tmp_path):
        from agent_windows.core.filesystem import copy_path
        src = tmp_path / "a.txt"
        src.write_text("new", encoding="utf-8")
        dst = tmp_path / "b.txt"
        dst.write_text("old", encoding="utf-8")
        copy_path(str(src), str(dst), overwrite=True)
        assert dst.read_text(encoding="utf-8") == "new"

    def test_source_not_found(self, tmp_path):
        from agent_windows.core.filesystem import copy_path
        with pytest.raises(FileNotFoundError):
            copy_path(str(tmp_path / "nope"), str(tmp_path / "dst"))

    def test_dest_blocked_by_blacklist(self, tmp_path):
        """Copy into a blacklisted dir (System32) is refused via write-validate."""
        from agent_windows.core.filesystem import copy_path
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="Access denied"):
            copy_path(str(src), r"C:\Windows\System32\evil.txt")

    def test_overwrite_dir_with_file_refused(self, tmp_path):
        """B3: copying a FILE onto a dir with overwrite=True is rejected before
        any rmtree, preserving the directory's contents."""
        from agent_windows.core.filesystem import copy_path
        src = tmp_path / "file.txt"
        src.write_text("x", encoding="utf-8")
        dst = tmp_path / "dir"
        dst.mkdir()
        (dst / "keep.txt").write_text("precious", encoding="utf-8")
        with pytest.raises(ValueError, match="Cannot overwrite a directory with a file"):
            copy_path(str(src), str(dst), overwrite=True)
        assert (dst / "keep.txt").exists()


class TestMovePath:
    def test_move_file(self, tmp_path):
        from agent_windows.core.filesystem import move_path
        src = tmp_path / "a.txt"
        src.write_text("data", encoding="utf-8")
        dst = tmp_path / "b.txt"
        move_path(str(src), str(dst))
        assert dst.read_text(encoding="utf-8") == "data"
        assert not src.exists()

    def test_move_refuses_existing_without_overwrite(self, tmp_path):
        from agent_windows.core.filesystem import move_path
        src = tmp_path / "a.txt"
        src.write_text("x", encoding="utf-8")
        dst = tmp_path / "b.txt"
        dst.write_text("old", encoding="utf-8")
        with pytest.raises(ValueError, match="already exists"):
            move_path(str(src), str(dst))

    def test_move_overwrite_replaces(self, tmp_path):
        from agent_windows.core.filesystem import move_path
        src = tmp_path / "a.txt"
        src.write_text("new", encoding="utf-8")
        dst = tmp_path / "b.txt"
        dst.write_text("old", encoding="utf-8")
        move_path(str(src), str(dst), overwrite=True)
        assert dst.read_text(encoding="utf-8") == "new"
        assert not src.exists()

    def test_move_refuses_install_tree_source(self):
        """B2: move source is write-validated — can't move the agent's own files
        away. Mirrors delete_path protection (moving IS a write to the source)."""
        from agent_windows.core.filesystem import move_path, _protected_write_roots
        install = _protected_write_roots()[0]
        with pytest.raises(ValueError, match="install tree"):
            move_path(str(install / "anything"), str("C:/safe/dst"))

    def test_move_refuses_data_dir_source(self):
        from agent_windows.core.filesystem import move_path
        with pytest.raises(ValueError, match="install tree"):
            move_path(str(Path.home() / ".agent-windows" / "config.toml"), "C:/safe/dst")

    def test_move_overwrite_file_into_dir_refused(self, tmp_path):
        """B3: moving a FILE onto a non-empty dir with overwrite=True must NOT
        rmtree the dir then fail — it's rejected before any destructive op."""
        from agent_windows.core.filesystem import move_path
        src = tmp_path / "file.txt"
        src.write_text("x", encoding="utf-8")
        dst = tmp_path / "dir"
        dst.mkdir()
        (dst / "keep.txt").write_text("precious", encoding="utf-8")
        with pytest.raises(ValueError, match="Cannot overwrite a directory with a file"):
            move_path(str(src), str(dst), overwrite=True)
        # The directory and its contents survive (no premature rmtree).
        assert (dst / "keep.txt").read_text(encoding="utf-8") == "precious"

    def test_move_overwrite_dir_into_file_refused(self, tmp_path):
        """B3: moving a DIR onto a file with overwrite=True is rejected."""
        from agent_windows.core.filesystem import move_path
        src = tmp_path / "srcdir"
        src.mkdir()
        (src / "f.txt").write_text("x", encoding="utf-8")
        dst = tmp_path / "file.txt"
        dst.write_text("old", encoding="utf-8")
        with pytest.raises(ValueError, match="Cannot overwrite a file with a directory"):
            move_path(str(src), str(dst), overwrite=True)
        assert dst.read_text(encoding="utf-8") == "old"  # file untouched


class TestDeletePath:
    def test_delete_file(self, tmp_path):
        from agent_windows.core.filesystem import delete_path
        f = tmp_path / "a.txt"
        f.write_text("x", encoding="utf-8")
        delete_path(str(f))
        assert not f.exists()

    def test_delete_empty_dir(self, tmp_path):
        from agent_windows.core.filesystem import delete_path
        d = tmp_path / "empty"
        d.mkdir()
        delete_path(str(d))
        assert not d.exists()

    def test_non_empty_dir_refused_without_recursive(self, tmp_path):
        from agent_windows.core.filesystem import delete_path
        d = tmp_path / "full"
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="not empty"):
            delete_path(str(d))
        assert d.exists()  # not deleted

    def test_recursive_deletes_non_empty(self, tmp_path):
        from agent_windows.core.filesystem import delete_path
        d = tmp_path / "full"
        d.mkdir()
        (d / "f.txt").write_text("x", encoding="utf-8")
        delete_path(str(d), recursive=True)
        assert not d.exists()

    def test_refuses_install_tree(self):
        """delete_path treats the target as a write → install/data tree blocked."""
        from agent_windows.core.filesystem import delete_path, _protected_write_roots
        install = _protected_write_roots()[0]
        target = install / "evil_delete_marker.txt"
        with pytest.raises(ValueError, match="install tree"):
            delete_path(str(target))

    def test_refuses_data_dir(self):
        from agent_windows.core.filesystem import delete_path
        target = Path.home() / ".agent-windows" / "evil_delete.txt"
        with pytest.raises(ValueError, match="install tree"):
            delete_path(str(target))


class TestSearchFiles:
    def test_glob_match(self, tmp_path):
        from agent_windows.core.filesystem import search_files
        (tmp_path / "a.py").write_text("x", encoding="utf-8")
        (tmp_path / "b.txt").write_text("x", encoding="utf-8")
        r = search_files(str(tmp_path), "*.py", recursive=False)
        assert r["count"] == 1
        assert "a.py" in r["output"]

    def test_recursive_finds_nested(self, tmp_path):
        from agent_windows.core.filesystem import search_files
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "deep.py").write_text("x", encoding="utf-8")
        r = search_files(str(tmp_path), "*.py")
        assert r["count"] == 1
        assert "deep.py" in r["output"]

    def test_no_matches(self, tmp_path):
        from agent_windows.core.filesystem import search_files
        r = search_files(str(tmp_path), "*.xyz")
        assert r["count"] == 0
        assert "No matches" in r["output"]

    def test_truncated_at_cap(self, tmp_path):
        from agent_windows.core import filesystem
        from agent_windows.core.filesystem import search_files
        cap = filesystem.MAX_SEARCH_RESULTS
        for i in range(cap + 10):
            (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
        r = search_files(str(tmp_path), "*.txt", recursive=False)
        assert r["truncated"] is True
        assert r["count"] >= cap

    def test_refuses_blacklisted_path(self):
        """M2: searching inside a blacklisted dir (.ssh) is refused."""
        from agent_windows.core.filesystem import search_files
        with pytest.raises(ValueError, match="Access denied"):
            search_files(r"C:\Users\alice\.ssh", "*")

    def test_refuses_system32(self):
        from agent_windows.core.filesystem import search_files
        with pytest.raises(ValueError, match="Access denied"):
            search_files(r"C:\Windows\System32", "*.dll")


class TestGetFileInfo:
    def test_file_info(self, tmp_path):
        from agent_windows.core.filesystem import get_file_info
        f = tmp_path / "doc.txt"
        f.write_text("hello", encoding="utf-8")
        r = get_file_info(str(f))
        assert r["type"] == "File"
        assert r["size"] == 5
        assert r["extension"] == ".txt"
        assert "Extension: .txt" in r["output"]

    def test_dir_info_with_contents(self, tmp_path):
        from agent_windows.core.filesystem import get_file_info
        (tmp_path / "a.txt").write_text("x", encoding="utf-8")
        (tmp_path / "sub").mkdir()
        r = get_file_info(str(tmp_path))
        assert r["type"] == "Directory"
        assert r["contents_files"] == 1
        assert r["contents_dirs"] == 1
        assert "Contents: 1 files, 1 directories" in r["output"]

    def test_not_found(self, tmp_path):
        from agent_windows.core.filesystem import get_file_info
        with pytest.raises(FileNotFoundError):
            get_file_info(str(tmp_path / "nope.txt"))

    def test_no_extension(self, tmp_path):
        from agent_windows.core.filesystem import get_file_info
        f = tmp_path / "README"
        f.write_text("x", encoding="utf-8")
        r = get_file_info(str(f))
        assert r["extension"] == "(none)"

    def test_refuses_blacklisted_path(self):
        """M2: info on a blacklisted path (.ssh) is refused."""
        from agent_windows.core.filesystem import get_file_info
        with pytest.raises(ValueError, match="Access denied"):
            get_file_info(r"C:\Users\alice\.ssh\id_rsa")

    def test_refuses_system32(self):
        from agent_windows.core.filesystem import get_file_info
        with pytest.raises(ValueError, match="Access denied"):
            get_file_info(r"C:\Windows\System32\cmd.exe")


# ── handlers: param plumbing + new handler registration (TODO #4) ─────


class TestFsHandlers:
    def test_all_eight_fs_handlers_registered(self):
        from agent_windows.daemon.router import HANDLERS
        for name in ("fs_read", "fs_write", "fs_list", "fs_copy",
                     "fs_move", "fs_delete", "fs_search", "fs_info"):
            assert name in HANDLERS, f"{name} not registered"

    def test_fs_read_forwards_offset_limit(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS
        f = tmp_path / "x.txt"
        f.write_text("l1\nl2\nl3\n", encoding="utf-8")
        r = HANDLERS["fs_read"]({"path": str(f), "offset": 2, "limit": 1})
        assert r["content"] == "l2\n"

    def test_fs_write_append(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS
        f = tmp_path / "x.txt"
        f.write_text("a", encoding="utf-8")
        HANDLERS["fs_write"]({"path": str(f), "content": "b", "append": True})
        assert f.read_text(encoding="utf-8") == "ab"

    def test_fs_copy_handler(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS
        src = tmp_path / "a.txt"
        src.write_text("d", encoding="utf-8")
        HANDLERS["fs_copy"]({"path": str(src), "destination": str(tmp_path / "b.txt")})
        assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "d"

    def test_fs_search_requires_pattern(self):
        from agent_windows.daemon.router import HANDLERS
        with pytest.raises(ValueError, match="missing required parameter 'pattern'"):
            HANDLERS["fs_search"]({"path": "."})

    def test_fs_info_handler(self, tmp_path):
        from agent_windows.daemon.router import HANDLERS
        f = tmp_path / "x.txt"
        f.write_text("d", encoding="utf-8")
        r = HANDLERS["fs_info"]({"path": str(f)})
        assert r["type"] == "File"


class TestElevationHint:
    """TODO #14: a PermissionError carries an elevation hint when not elevated."""

    def test_permission_error_appends_hint_when_not_elevated(self, tmp_path):
        """A PermissionError from a read, when not elevated, is re-raised with
        the hint appended."""
        from unittest.mock import patch
        from agent_windows.core.filesystem import read_file
        with patch("agent_windows.core.filesystem.elevation_hint",
                   return_value="HINT: run elevated"):
            with patch("pathlib.Path.exists", side_effect=PermissionError("denied")):
                with pytest.raises(PermissionError, match="HINT: run elevated"):
                    read_file(str(tmp_path / "x.txt"))

    def test_permission_error_chains_original_via_cause(self, tmp_path):
        """The re-raised PermissionError chains the original via __cause__, so
        the traceback stays intact (from exc)."""
        from unittest.mock import patch
        from agent_windows.core.filesystem import read_file
        with patch("agent_windows.core.filesystem.elevation_hint",
                   return_value="HINT: run elevated"):
            with patch("pathlib.Path.exists", side_effect=PermissionError("original")):
                with pytest.raises(PermissionError) as excinfo:
                    read_file(str(tmp_path / "x.txt"))
        assert excinfo.value.__cause__ is not None
        assert str(excinfo.value.__cause__) == "original"

    def test_permission_error_no_hint_when_elevated(self, tmp_path):
        """A PermissionError when elevated keeps its original message — no hint."""
        from unittest.mock import patch
        from agent_windows.core.filesystem import read_file
        with patch("agent_windows.core.filesystem.elevation_hint", return_value=None):
            with patch("pathlib.Path.exists", side_effect=PermissionError("denied")):
                with pytest.raises(PermissionError, match="^denied$"):
                    read_file(str(tmp_path / "x.txt"))

    def test_other_exceptions_not_affected(self, tmp_path):
        """FileNotFoundError and ValueError pass through unchanged — the
        decorator only enriches PermissionError."""
        from agent_windows.core.filesystem import read_file
        with pytest.raises(FileNotFoundError):
            read_file(str(tmp_path / "missing.txt"))

    def test_hint_applies_to_all_decorated_ops(self, tmp_path):
        """Sanity: the decorator is applied to every public op, so a
        PermissionError from any of them carries the hint."""
        from unittest.mock import patch
        from agent_windows.core import filesystem
        ops = [
            ("read_file", ("",), {}),
            ("write_file", ("", ""), {}),
            ("list_dir", ("",), {}),
            ("copy_path", ("", ""), {}),
            ("move_path", ("", ""), {}),
            ("delete_path", ("",), {}),
            ("search_files", ("", ""), {}),
            ("get_file_info", ("",), {}),
        ]
        with patch("agent_windows.core.filesystem.elevation_hint",
                   return_value="HINT: run elevated"), \
                patch("pathlib.Path.exists", side_effect=PermissionError("denied")), \
                patch("pathlib.Path.resolve", side_effect=PermissionError("denied")):
            for name, args, kwargs in ops:
                fn = getattr(filesystem, name)
                with pytest.raises(PermissionError, match="HINT: run elevated"):
                    fn(*args, **kwargs)
