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


class TestReadFileEncoding:
    """TODO #20b: an explicit encoding overrides UTF-8 for legacy codepage files."""

    def test_encoding_reads_legacy_codepage(self, tmp_path):
        """A GBK-encoded file decodes correctly with encoding='gbk'."""
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "gbk.txt"
        # '中文' in GBK; these bytes are invalid as UTF-8.
        f.write_bytes("中文".encode("gbk"))
        result = read_file(str(f), encoding="gbk")
        assert result["content"] == "中文"

    def test_encoding_alias_normalized(self, tmp_path):
        """A codec alias (latin-1 == iso-8859-1) is accepted via codecs.lookup."""
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "l1.txt"
        f.write_bytes(b"\xe9\xe8\xea")  # è ê in latin-1
        result = read_file(str(f), encoding="latin-1")
        assert result["content"] == "éèê"

    def test_default_is_utf8(self, tmp_path):
        """Omitting encoding reads as UTF-8 (unchanged behavior)."""
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "u8.txt"
        f.write_text("héllo", encoding="utf-8")
        assert read_file(str(f))["content"] == "héllo"

    def test_bad_encoding_raises_valueerror(self, tmp_path):
        """An unknown codec name raises ValueError (→ INVALID_PARAMS at the
        router) before any read — LookupError is re-raised as ValueError."""
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "x.txt"
        f.write_text("ok", encoding="utf-8")
        with pytest.raises(ValueError):
            read_file(str(f), encoding="not-a-real-codec")

    def test_wrong_codepage_falls_back_to_binary(self, tmp_path):
        """An explicit encoding that can't decode the file (errors='strict')
        falls back to the binary notice rather than returning mojibake."""
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "gbk.txt"
        f.write_bytes("中文".encode("gbk"))  # invalid as latin-1? actually decodes; use raw bytes
        # latin-1 can decode any byte (it's a 1:1 byte→codepoint map), so use a
        # codec that genuinely rejects: utf-8 on GBK bytes already tested above
        # via the strict path. Here force a real mismatch with shift_jis on gbk.
        f.write_bytes(b"\xff\xfe\x00")  # invalid for most codecs
        result = read_file(str(f), encoding="shift_jis")
        assert "[Binary file" in result["content"]

    def test_encoding_works_with_line_range(self, tmp_path):
        """encoding applies in line-range (offset/limit) mode too."""
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "gbk.txt"
        f.write_bytes("行一\n行二\n".encode("gbk"))
        result = read_file(str(f), offset=1, limit=1, encoding="gbk")
        assert result["content"] == "行一\n"


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

    def test_list_dir_peak_memory_scales_with_cap_not_tree(self, tmp_path, monkeypatch):
        """A2: the sort no longer materializes the whole tree. The real memory
        bound is that peak scales with the CAP, not with the entry count N. We
        prove it by contrasting two caps over the SAME large tree and checking
        the peak delta is large — a sorted()-based impl (old code) would hold
        ~all N Paths regardless of cap, so the two peaks would be nearly equal
        and this delta assertion would fail."""
        import tracemalloc
        from agent_windows.core import filesystem as fs
        from agent_windows.core.filesystem import list_dir

        # A wide tree: many entries so an unbounded sort would peak high.
        n = 6000
        for i in range(n):
            (tmp_path / f"f{i:05d}.txt").write_text("x", encoding="utf-8")

        def peak_at(cap: int) -> int:
            monkeypatch.setattr(fs, "MAX_LIST_ENTRIES", cap)
            tracemalloc.start()
            r = list_dir(str(tmp_path))
            cur, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert r["count"] == n  # real total preserved at any cap
            return peak

        small_cap_peak = peak_at(20)
        large_cap_peak = peak_at(5000)
        # The heap holds O(cap) Paths. With small cap the peak is dominated by
        # interpreter overhead; with large cap (5000) it carries ~5000 extra
        # Path objects. The delta must be substantial (well over the per-call
        # noise). If the impl regressed to sorted(), BOTH peaks would be ~equal
        # (≈ N Paths) and the delta would be tiny → this assertion fails.
        delta = large_cap_peak - small_cap_peak
        assert delta > 100_000, (
            f"peak did not scale with cap: small={small_cap_peak} "
            f"large={large_cap_peak} delta={delta} — expected the large-cap "
            f"peak to be substantially higher (heap bound depends on cap)"
        )

    def test_list_dir_truncated_output_is_sorted(self, tmp_path, monkeypatch):
        """A2: when truncated, the returned entries are still the globally-sorted
        top cap, not an arbitrary subset (the heap keeps the smallest keys)."""
        from agent_windows.core import filesystem as fs
        from agent_windows.core.filesystem import list_dir
        # Names created out of order; cap below the count.
        for n in ["k", "j", "i", "h", "g", "f", "e", "d", "c", "b", "a"]:
            (tmp_path / f"{n}.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_LIST_ENTRIES", 4)
        r = list_dir(str(tmp_path))
        # Capture in OUTPUT order (no re-sort) — this is what tests the ordering.
        listed = [l.strip().split()[0].rstrip("/")
                  for l in r["output"].splitlines() if l.startswith("  ")]
        # The 4 smallest names, in ascending order: a, b, c, d. If the heap
        # returned them unordered or returned the wrong (largest) set, this fails.
        assert listed == ["a.txt", "b.txt", "c.txt", "d.txt"]


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

    # ── E2 gap-4: UNC and Windows extended-length paths must not bypass the
    # credential-dir check. These shapes have a different .parts structure than
    # drive paths (no drive letter for UNC; a \\?\ prefix for long paths), so
    # the .parts set-intersection that backs the blocklist needs its own guard.
    # The validator is pure Path logic (no win32), so these run on any host;
    # resolve() anchors a non-existent share to the cwd, but the credential
    # substring still lands in .parts and is still caught — locked here as a
    # regression guard against a future rewrite that mishandles these prefixes.

    def test_refuses_unc_path_into_credential_dir(self):
        """A UNC path (\\\\server\\share\\.ssh\\...) must be blocked just like a
        drive path into a credential dir — the \\server prefix has no drive
        letter, but .resolve() still surfaces '.ssh' in .parts."""
        from agent_windows.core.filesystem import validate_executable_path
        with pytest.raises(ValueError, match="credential"):
            validate_executable_path(r"\\server\share\.ssh\payload.exe")

    def test_refuses_long_path_prefix_into_credential_dir(self):
        r"""A Windows extended-length path (\\?\C:\...\.ssh\...) must be
        blocked. The \\?\ prefix disables path normalization, so a naive
        parser could mishandle it; .resolve() strips the prefix and the
        '.ssh' part is still detected."""
        from agent_windows.core.filesystem import validate_executable_path
        with pytest.raises(ValueError, match="credential"):
            validate_executable_path(r"\\?\C:\Users\alice\.ssh\payload.exe")

    def test_allows_unc_path_to_normal_share(self):
        """A UNC path into an ordinary share (no credential dir) stays
        launchable — the blocklist must not over-reach to all UNC paths."""
        from agent_windows.core.filesystem import validate_executable_path
        validate_executable_path(r"\\server\share\app.exe")  # must not raise

    def test_allows_long_path_prefix_to_system32(self):
        r"""A \\?\-prefixed legit system path stays launchable."""
        from agent_windows.core.filesystem import validate_executable_path
        validate_executable_path(r"\\?\C:\Windows\System32\notepad.exe")  # must not raise


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


class TestReadOffsetLimitValidation:
    def test_offset_zero_means_whole_file(self, tmp_path):
        """The documented CLI/MCP convention: offset=0 (and limit=0) means "not
        set" → whole file, not an error. A bare 0 must not be rejected."""
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "x.txt"
        f.write_text("a\nb\nc\n", encoding="utf-8")
        assert read_file(str(f), offset=0)["content"] == "a\nb\nc\n"
        assert read_file(str(f), limit=0)["content"] == "a\nb\nc\n"
        assert read_file(str(f), offset=0, limit=0)["content"] == "a\nb\nc\n"

    def test_offset_must_be_positive(self, tmp_path):
        import pytest
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "x.txt"
        f.write_text("a\n", encoding="utf-8")
        with pytest.raises(ValueError):
            read_file(str(f), offset=-3)
        with pytest.raises(ValueError):
            read_file(str(f), limit=-1)

    def test_offset_must_be_int_not_bool(self, tmp_path):
        # bool is a subclass of int; True must not silently mean offset=1, and
        # False (== 0) must be rejected too — not normalized to "whole file".
        import pytest
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "x.txt"
        f.write_text("a\n", encoding="utf-8")
        with pytest.raises(ValueError):
            read_file(str(f), offset=True)
        with pytest.raises(ValueError):
            read_file(str(f), limit=False)
        with pytest.raises(ValueError):
            read_file(str(f), offset=False)

    def test_limit_negative_rejected(self, tmp_path):
        import pytest
        from agent_windows.core.filesystem import read_file
        f = tmp_path / "x.txt"
        f.write_text("a\n", encoding="utf-8")
        with pytest.raises(ValueError):
            read_file(str(f), limit=-1)

    def test_limit_capped_to_max_lines(self, tmp_path, monkeypatch):
        # A runaway limit must not force islice/readlines to walk a huge file.
        from agent_windows.core import filesystem as fs
        f = tmp_path / "big.txt"
        f.write_text("\n".join(f"l{i}" for i in range(10)) + "\n", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_READ_LINES", 3)
        # limit=1_000_000 collapses to MAX_READ_LINES=3 → only 3 lines.
        r = fs.read_file(str(f), limit=1_000_000)
        assert r["content"].count("\n") == 3

    def test_offset_above_max_is_rejected(self, tmp_path, monkeypatch):
        """F10: a runaway offset must not make islice consume+discard the whole
        file (DoS on the single-threaded daemon). Unlike ``limit`` (which is
        clamped, since shrinking a line count is always safe), ``offset`` selects
        WHERE to read — clamping it would read the wrong window with no signal,
        a data-correctness bug — so it is REJECTED, raising ValueError
        (→ INVALID_PARAMS at the router)."""
        import pytest
        from agent_windows.core import filesystem as fs
        f = tmp_path / "x.txt"
        f.write_text("a\n", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_OFFSET", 3)
        # 1e12 is the runaway case; 4 just exceeds the patched cap of 3.
        for bad in (10**12, 4):
            with pytest.raises(ValueError, match="exceeds maximum"):
                fs.read_file(str(f), offset=bad)

    def test_offset_at_max_is_accepted(self, tmp_path, monkeypatch):
        """The cap is a <= bound, not < : offset == MAX_OFFSET is allowed."""
        from agent_windows.core import filesystem as fs
        f = tmp_path / "x.txt"
        f.write_text("a\nb\n", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_OFFSET", 3)
        # offset == cap is fine (reads an empty window past EOF here, no error).
        r = fs.read_file(str(f), offset=3)
        assert r["content"] == ""


class TestReadOffsetLimitLargeFile:
    def test_large_file_streams_only_window(self, tmp_path, monkeypatch):
        # Simulate a file larger than the read budget without writing 5 MiB:
        # lower MAX_READ_BYTES so a small file takes the streaming branch.
        # Budget must be < file size (15 B) but >= the 5 B window so it isn't
        # byte-truncated; that isolates the streaming path from the byte cap.
        from agent_windows.core import filesystem as fs
        f = tmp_path / "tall.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 10)
        r = fs.read_file(str(f), offset=2, limit=2)
        # Streaming branch: total unknown (window didn't reach EOF), but the
        # requested slice is correct.
        assert r["content"] == "l2\nl3\n"
        assert r["lines_total"] is None

    def test_large_file_window_at_eof_knows_total(self, tmp_path, monkeypatch):
        from agent_windows.core import filesystem as fs
        f = tmp_path / "tall.txt"
        f.write_text("l1\nl2\nl3\n", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 8)  # < file(9), >= window(5)
        # offset=2, limit=5 reads to EOF → total is known (=3).
        r = fs.read_file(str(f), offset=2, limit=5)
        assert r["content"] == "l2\nl3\n"
        assert r["lines_total"] == 3

    def test_large_file_byte_budget_truncates_long_lines(self, tmp_path, monkeypatch):
        from agent_windows.core import filesystem as fs
        f = tmp_path / "wide.txt"
        f.write_text("AAAA" * 100 + "\n", encoding="utf-8")  # one long line
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 16)
        monkeypatch.setattr(fs, "MAX_READ_LINES", 100_000)
        r = fs.read_file(str(f), offset=1, limit=1)
        assert len(r["content"]) <= 16
        # A byte-trimmed window set truncated=True so callers know it was cut.
        assert r["truncated"] is True

    def test_streaming_offset_beyond_eof_unknown_total(self, tmp_path, monkeypatch):
        # Regression: an offset past EOF must NOT fabricate a total. Earlier the
        # heuristic "len(selected) < limit" reported a 5-line file as 98 lines.
        from agent_windows.core import filesystem as fs
        f = tmp_path / "tall.txt"
        f.write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 10)  # < 15 B → streaming
        r = fs.read_file(str(f), offset=99, limit=2)
        assert r["content"] == ""
        assert r["lines_total"] is None  # unknown, NOT a fabricated number
        assert r["truncated"] is False

    def test_streaming_window_reaches_eof_knows_total(self, tmp_path, monkeypatch):
        # limit=None on a file where the window reaches EOF (no byte truncation)
        # must report the exact total — it read to the end.
        from agent_windows.core import filesystem as fs
        f = tmp_path / "tall.txt"
        # A huge first line forces streaming; offset=2 reads the 2 trailing
        # short lines to EOF within the byte budget.
        f.write_text("X" * 6000 + "\nl2\nl3\n", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 100)
        r = fs.read_file(str(f), offset=2, limit=2)
        assert r["content"] == "l2\nl3\n"
        assert r["lines_total"] == 3
        assert r["truncated"] is False

    def test_streaming_does_not_read_whole_file(self, tmp_path, monkeypatch):
        # The OOM fix's core claim: a large file is not fully consumed. Verify
        # by instrumenting iteration so the test fails if the whole file is
        # walked. A "genuinely not fully read" test — the PR's central guarantee.
        import builtins
        from agent_windows.core import filesystem as fs

        f = tmp_path / "huge.txt"
        # 10_000 short lines; budget large enough to NOT byte-truncate, so the
        # only thing stopping the read is the line window.
        f.write_text("\n".join(f"l{i}" for i in range(10_000)) + "\n", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 5 * 1024 * 1024)
        monkeypatch.setattr(fs, "MAX_READ_LINES", 100_000)

        # Wrap builtins.open so the file object counts each line yielded by
        # iteration (islice drives __next__). The real file handle is kept; only
        # __next__ is observed. readlines() (small-file branch) isn't taken here.
        real_open = builtins.open
        counter = {"lines_iterated": 0}

        class CountingFile:
            def __init__(self, fh):
                self._fh = fh
            def __iter__(self):
                return self
            def __next__(self):
                line = self._fh.__next__()
                counter["lines_iterated"] += 1
                return line
            # forward everything else used by read_file / context manager
            def read(self, *a): return self._fh.read(*a)
            def readlines(self, *a): return self._fh.readlines(*a)
            def __enter__(self): return self
            def __exit__(self, *a): self._fh.close()

        def spy_open(path, *a, **k):
            fh = real_open(path, *a, **k)
            return CountingFile(fh) if str(path) == str(f) else fh
        monkeypatch.setattr(builtins, "open", spy_open)

        # Request only 5 lines from a 10_000-line file via streaming.
        r = fs.read_file(str(f), offset=1, limit=5)
        assert r["content"].count("\n") == 5
        # Streaming must stop near the window (≤ window + 1 EOF-probe line),
        # NOT walk all 10_000 lines. The +1 is the EOF probe after a full window.
        assert counter["lines_iterated"] <= 6, (
            f"streaming iterated {counter['lines_iterated']} lines; expected to "
            f"stop near the window (≈5-6), not walk the whole 10_000-line file"
        )

    def test_streaming_byte_budget_honored_for_cjk(self, tmp_path, monkeypatch):
        """G6: the streaming byte budget must be measured in *bytes*, not chars.
        "中" is 3 UTF-8 bytes. Three of them (9 bytes) under a 6-byte budget:
        the old char-counting (len(line)=3 ≤ 6) FAILED to truncate and returned
        9 bytes — violating the "byte-budgeting" docstring claim. The byte-count
        fix must truncate and keep content ≤ MAX_READ_BYTES bytes."""
        from agent_windows.core import filesystem as fs
        f = tmp_path / "cjk.txt"
        f.write_bytes("中中中\n".encode("utf-8"))  # 10 bytes on disk
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 6)  # < 10 → streaming
        r = fs.read_file(str(f), offset=1, limit=1)
        assert r["truncated"] is True
        assert len(r["content"].encode("utf-8")) <= 6
        # 6 bytes slices cleanly on the 3-byte char boundary → "中中", no tail.
        assert r["content"] == "中中"

    def test_streaming_byte_budget_splits_multibyte_char(self, tmp_path, monkeypatch):
        """When the byte budget lands mid-character, the partial slice decodes
        the trailing fragment as U+FFFD (errors='replace'). Documents the
        best-effort partial-fill behavior; content stays ≤ budget + 2 bytes
        (one U+FFFD is 3 UTF-8 bytes)."""
        from agent_windows.core import filesystem as fs
        f = tmp_path / "cjk.txt"
        f.write_bytes("中中中\n".encode("utf-8"))
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 7)  # 7 = 2 chars(6) + 1 byte of 3rd
        r = fs.read_file(str(f), offset=1, limit=1)
        assert r["truncated"] is True
        # Two whole chars + a U+FFFD tail from the split byte.
        assert r["content"] == "中中\ufffd"

    def test_streaming_exact_fit_line_no_partial_fill(self, tmp_path, monkeypatch):
        """Regression (review catch): when an appended line lands EXACTLY on the
        byte budget and a later line overflows, the partial-fill guard
        (have < MAX_READ_BYTES) is False — so content must still be the fully-
        appended buffer, NOT raise UnboundLocalError. The dropped-walrus rewrite
        regressed this; plain ASCII triggers it (no CJK needed)."""
        from agent_windows.core import filesystem as fs
        f = tmp_path / "exact.txt"
        # line1 "abcde\n" = 6 bytes exact-fit; line2 "x\n" overflows.
        f.write_bytes(b"abcde\nx\n")
        monkeypatch.setattr(fs, "MAX_READ_BYTES", 6)
        monkeypatch.setattr(fs, "MAX_READ_LINES", 100_000)
        r = fs.read_file(str(f), offset=1, limit=10)
        assert r["truncated"] is True
        assert r["content"] == "abcde\n"


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

    # ── F2 (round 2): append must be fail-safe on ambiguous truthiness.
    # Some MCP/JSON-RPC clients serialize booleans as 1/0. Under the old
    # ``mode = "a" if _as_bool(append) else "w"``, append=1 silently opened
    # "w" and destroyed the file. Now ambiguous-but-truthy values are refused
    # rather than guessed — both directions (truncate-vs-append) are destructive
    # when wrong, unlike the overwrite/recursive knobs which fail safe by
    # defaulting to off.

    @pytest.mark.parametrize("bad", [1, "yes", "1", "Y"])
    def test_append_ambiguous_truthiness_refused_not_truncated(self, tmp_path, bad):
        """An append value we can't map to a definite bool must raise and leave
        the file untouched — never silently truncate."""
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "log.txt"
        f.write_text("irreplaceable\n", encoding="utf-8")
        with pytest.raises(ValueError, match="append"):
            write_file(str(f), "more", append=bad)
        assert f.read_text(encoding="utf-8") == "irreplaceable\n"

    @pytest.mark.parametrize("falsy", [False, "false", "FALSE", None, 0, ""])
    def test_append_falsy_overwrites(self, tmp_path, falsy):
        """Recognized falsy values (incl. string 'false' and int 0) write."""
        from agent_windows.core.filesystem import write_file
        f = tmp_path / "x.txt"
        write_file(str(f), "new", append=falsy)
        assert f.read_text(encoding="utf-8") == "new"


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

    # ── F4 (round 2): recursive listing must not descend into blacklisted
    # credential dirs. The old rglob walked into .ssh/.aws/..., whose own entry
    # was dotfile-filtered but whose children (id_rsa, config, known_hosts) have
    # ordinary names and leaked through — exposing filenames + sizes that can
    # even hint at key types. The walk now prunes these dirs.

    @pytest.mark.parametrize("cred", [".ssh", ".aws", ".gnupg", ".kube"])
    def test_recursive_listing_prunes_credential_dir(self, tmp_path, cred):
        """A recursive listing must not enumerate the contents of a blacklisted
        credential dir, even with show_hidden=True (the dir itself is pruned,
        not merely hidden)."""
        from agent_windows.core.filesystem import list_dir
        secret = tmp_path / cred
        secret.mkdir()
        (secret / "id_rsa").write_text("PRIVATE", encoding="utf-8")
        (secret / "config").write_text("cfg", encoding="utf-8")
        (tmp_path / "visible.txt").write_text("ok", encoding="utf-8")
        r = list_dir(str(tmp_path), recursive=True, show_hidden=True)
        assert "visible.txt" in r["output"]
        # Neither the dir entry nor any of its children appear.
        assert cred not in r["output"]
        assert "id_rsa" not in r["output"]
        assert "config" not in r["output"]

    def test_recursive_listing_still_lists_normal_subdirs(self, tmp_path):
        """Pruning blacklisted dirs must not over-reach into ordinary dirs —
        a nested normal subdir's contents still appear."""
        from agent_windows.core.filesystem import list_dir
        sub = tmp_path / "projects"
        sub.mkdir()
        (sub / "app.py").write_text("x", encoding="utf-8")
        r = list_dir(str(tmp_path), recursive=True)
        assert "projects" in r["output"]
        assert "app.py" in r["output"]

    def test_nonrecursive_listing_skips_blacklisted_child(self, tmp_path):
        """A blacklisted direct child is skipped even non-recursively (so
        show_hidden=True can't surface the '.ssh' directory entry itself)."""
        from agent_windows.core.filesystem import list_dir
        (tmp_path / ".ssh").mkdir()
        (tmp_path / "readme.txt").write_text("x", encoding="utf-8")
        r = list_dir(str(tmp_path), show_hidden=True)
        assert "readme.txt" in r["output"]
        assert ".ssh" not in r["output"]


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

    def test_search_heap_stops_at_cap(self, tmp_path, monkeypatch):
        """A2: search_files stops walking once the cap fills (keep_total=False),
        not after sorting the whole tree. The count is capped, not the real
        total, and only cap matches are retained."""
        from agent_windows.core import filesystem as fs
        from agent_windows.core.filesystem import search_files
        for i in range(300):
            (tmp_path / f"f{i:03d}.txt").write_text("x", encoding="utf-8")
        monkeypatch.setattr(fs, "MAX_SEARCH_RESULTS", 10)

        retained = {"n": 0}
        real_fn = fs._top_cap_by_key
        def spy(iterator, key, cap, keep_total):
            out, total, trunc = real_fn(iterator, key, cap, keep_total)
            retained["n"] = len(out)
            assert keep_total is False  # search uses early-stop semantics
            return out, total, trunc
        monkeypatch.setattr(fs, "_top_cap_by_key", spy)

        r = search_files(str(tmp_path), "*.txt", recursive=False)
        assert r["truncated"] is True
        assert retained["n"] == 10   # only cap retained, not all 300
        assert r["count"] == 10      # count capped (no exact total promised)

    def test_refuses_blacklisted_path(self):
        """M2: searching inside a blacklisted dir (.ssh) is refused."""
        from agent_windows.core.filesystem import search_files
        with pytest.raises(ValueError, match="Access denied"):
            search_files(r"C:\Users\alice\.ssh", "*")

    def test_refuses_system32(self):
        from agent_windows.core.filesystem import search_files
        with pytest.raises(ValueError, match="Access denied"):
            search_files(r"C:\Windows\System32", "*.dll")

    # ── F4 (round 2): search must prune blacklisted credential dirs too.

    def test_recursive_search_prunes_credential_dir(self, tmp_path):
        """A recursive search must not return matches inside a blacklisted dir,
        even when the pattern would match its files."""
        from agent_windows.core.filesystem import search_files
        secret = tmp_path / ".aws"
        secret.mkdir()
        (secret / "credentials").write_text("SECRET", encoding="utf-8")
        (tmp_path / "credentials").write_text("ok", encoding="utf-8")
        r = search_files(str(tmp_path), "credentials")
        assert r["count"] == 1
        # The top-level match is found; the one inside .aws is pruned (no
        # ".aws" relative path appears in the listing).
        assert ".aws" not in r["output"]
        # The only FILE entry is the top-level one (not .aws\credentials).
        file_lines = [ln for ln in r["output"].splitlines() if "[FILE]" in ln]
        assert len(file_lines) == 1
        assert "credentials" in file_lines[0]

    def test_recursive_search_finds_in_normal_subdir(self, tmp_path):
        """Pruning must not over-reach — matches in ordinary nested dirs found."""
        from agent_windows.core.filesystem import search_files
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("x", encoding="utf-8")
        r = search_files(str(tmp_path), "*.py")
        assert r["count"] == 1
        assert "main.py" in r["output"]


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
