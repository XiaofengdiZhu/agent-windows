"""File system operations: read, write, list, copy, move, delete, search, info."""

import fnmatch
import functools
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from agent_windows.utils.win32 import elevation_hint
from agent_windows.utils.formatting import format_size

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def _with_elevation_hint(fn: F) -> F:
    """Re-raise a ``PermissionError`` with an elevation hint appended (TODO #14).

    "Access is denied" is the single most common opaque failure on Windows, and
    the daemon runs non-elevated by default. Appending actionable guidance —
    only when actually non-elevated — keeps the error honest without claiming
    elevation will always fix it. Other exceptions (``FileNotFoundError``,
    ``ValueError`` from validation, ``IsADirectoryError``…) pass through
    unchanged. The original exception is chained via ``raise ... from`` so the
    traceback stays intact.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except PermissionError as exc:
            # elevation_hint() is best-effort (is_elevated guards Win32 faults);
            # guard it anyway so a surprise here can't mask the original error.
            try:
                hint = elevation_hint()
            except Exception:
                logger.debug("elevation_hint() failed; re-raising original", exc_info=True)
                hint = None
            if hint:
                raise PermissionError(f"{exc}\n\nHINT: {hint}") from exc
            raise

    return wrapper  # type: ignore[return-value]

# DoS bounds: cap single-read size and directory-listing length so a multi-GB
# file or a directory with millions of entries can't exhaust daemon memory.
# The IPC message cap (ipc/pipe.py) already truncates the serialized response,
# but read_file/list_dir materialize the full payload in-process first.
# (SEC3-03)
MAX_READ_BYTES = 5 * 1024 * 1024  # 5 MiB
MAX_LIST_ENTRIES = 5000
MAX_SEARCH_RESULTS = 1000

# Path blacklist: match by path component to deny access to sensitive
# directories and files. Each entry is a tuple: (match pattern, description)
#   "part"   — matches any single component in the path (e.g. ".ssh" matches C:\Users\x\.ssh\config)
#   "prefix" — matches a contiguous component prefix (e.g. ("windows", "system32") matches ...\Windows\System32\...)
_BLOCKED_PARTS = frozenset({
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".docker",
    "ntuser.dat",
    "ntuser.ini",
    "sam",
})

_BLOCKED_PART_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("windows", "system32"), "Windows System32 directory"),
    (("windows", "system"), "Windows System directory"),
)

# Credential/secret directories. This is intentionally a SUBSET of
# ``_BLOCKED_PARTS``: ``launch_app`` runs arbitrary binaries, so a
# prompt-injected agent must not execute a payload smuggled into credential
# storage (e.g. ``.ssh/ hook.exe``, ``.aws/credential-helper.exe``). But
# launching legitimate system applications — including ones living under
# System32 — must stay allowed, so the full read/write blacklist does NOT apply
# here. (L-03)
_CREDENTIAL_DIRS = frozenset({
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
})


# Relative paths in fs_* operations resolve against the user's Desktop, so an
# agent can say "report.txt" instead of an absolute path. Win32 SHGetFolderPathW
# is the same call platformdirs.user_desktop_dir() bottoms out at; resolving it
# here avoids adding platformdirs as a dependency. (TODO #4)
def _desktop_dir() -> Path:
    import ctypes

    CSIDL_DESKTOP = 0
    buf = ctypes.create_unicode_buffer(260)
    try:
        if ctypes.windll.shell32.SHGetFolderPathW(0, CSIDL_DESKTOP, 0, 0, buf) == 0:
            return Path(buf.value)
    except Exception:
        logger.debug("SHGetFolderPathW failed; falling back to ~/Desktop", exc_info=True)
    return Path.home() / "Desktop"


def _resolve_user_path(path: str) -> str:
    """Anchor a relative path against the Desktop; pass absolutes through.

    Pure string normalization — NOT a security check. Each operation still runs
    the result through ``_validate_path`` / ``_validate_write_path`` so the
    blacklist and reparse-point defenses apply to the resolved path.
    """
    if os.path.isabs(path):
        return path
    return str(_desktop_dir() / path)


def _as_bool(value: Any) -> bool:
    """Tolerate bool-or-string truthiness for JSON-RPC params (some clients
    send "true"/"false" strings). True bools pass through; the JSON-RPC layer
    normally delivers real bools, so this is defensive. (TODO #4)"""
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _validate_path(path: str) -> Path:
    """Validate that a file path is not in the blacklist."""
    # M-03: resolve() is the reparse-point defense. It follows directory
    # junctions and symlinks and expands 8.3 short names (SSH~1 -> .ssh) and
    # case, so a benign-named link into a credential dir still resolves to the
    # blocked ".ssh" component and is rejected here. Verified on py3.13/Win11;
    # requires-python>=3.13 guarantees this. DO NOT replace with a faster
    # non-resolving check — that reopens the bypass. Regression-locked by
    # TestReparsePointEvasion in tests/test_filesystem.py.
    p = Path(path).expanduser().resolve()
    parts = [part.lower() for part in p.parts]

    # Exact component match (e.g. .ssh, ntuser.dat)
    for part in parts:
        if part in _BLOCKED_PARTS:
            raise ValueError(f"Access denied: path contains restricted segment '{part}'")

    # Contiguous component prefix match (e.g. windows\system32)
    for prefix, desc in _BLOCKED_PART_PREFIXES:
        prefix_len = len(prefix)
        for i in range(len(parts) - prefix_len + 1):
            if tuple(parts[i : i + prefix_len]) == prefix:
                raise ValueError(f"Access denied: path is in {desc}")

    return p


def validate_read_path(path: str) -> Path:
    """Validate a user-supplied read path (e.g. an image passed to a handler).

    Public entry point applying the same blacklist as ``read_file``/``list_dir``,
    so handlers that open arbitrary paths (screenshot ``highlight``/``diff``)
    cannot read past the blacklist. (L-05)
    """
    return _validate_path(path)


def _protected_write_roots() -> tuple[Path, ...]:
    """Directories ``write_file`` must never touch (M-04).

    - The agent's own package directory and (when it is a real directory) its
      parent (source tree under ``src/`` or the installed ``site-packages/``):
      overwriting the agent's source could neuter this very validator on the
      next import.
    - The data directory ``~/.agent-windows`` (mirrors
      :func:`config.get_data_dir`'s location, computed directly to avoid a
      core→utils import edge and the dir-creation side effect): holds
      ``config.toml``, ``daemon.pid``, ``daemon.version``, and ``logs/``.

    Screenshots and prompts are written by their own handlers, not this generic
    writer, so blocking the data dir breaks no legitimate ``write_file`` call.
    """
    import agent_windows

    install = Path(agent_windows.__file__).resolve().parent
    data_dir = (Path.home() / ".agent-windows").resolve()
    roots: list[Path] = [install, data_dir]
    # Also block the install root's parent, but only when it is a real directory
    # — never a bare drive root. A package laid out at e.g. C:\agent_windows
    # would otherwise make install.parent == C:\ and forbid every write on that
    # drive. (M-04-O1)
    parent = install.parent
    if len(parent.parts) > 1:
        roots.append(parent)
    return tuple(roots)


def _validate_write_path(path: str) -> Path:
    """Validate a write target (M-04).

    Applies the base blacklist (:func:`_validate_path`) and additionally refuses
    to write inside the agent's install tree or data directory: a
    prompt-injected ``write_file`` must not be able to clobber the agent's own
    source, config, daemon pid/version, or logs.
    """
    p = _validate_path(path)
    resolved = p.resolve()
    for root in _protected_write_roots():
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        raise ValueError(
            "Access denied: refusing to write inside the agent's install tree "
            "or data directory (source/config protection, M-04)"
        )
    return p


def validate_write_path(path: str) -> Path:
    """Public write-path validator for handlers that write user-supplied output
    paths (e.g. screenshot capture/highlight/diff). Mirrors ``write_file``'s
    protection so a user-directed ``--output`` can't land inside the agent's
    install tree or data directory. (M-04, L-05-W1)
    """
    return _validate_write_path(path)


def validate_executable_path(path: str) -> None:
    """Refuse to launch a binary from a sensitive location (L-03).

    ``launch_app`` runs arbitrary binaries detached; a prompt-injected agent
    must not be coerced into executing a payload smuggled into credential
    storage (``.ssh``/``.aws``/…) or planted inside the agent's own install or
    data tree. Only the credential-dir subset of the blacklist applies —
    launching legitimate system applications (System32, etc.) stays allowed.
    Raises :class:`ValueError` (→ ``INVALID_PARAMS``) on refusal.

    A bare PATH-resolved name (``notepad``, no drive or separator) carries no
    location and is returned unchecked: the OS resolves it via PATH, and
    ``resolve()`` would otherwise anchor it to the daemon's cwd — which can sit
    inside the install tree and falsely deny a legitimate launch. (L-03-O1)
    """
    p = Path(path)
    if not p.is_absolute() and not p.drive and len(p.parts) <= 1:
        return

    # M-03: same reparse-point / 8.3 / case defense as _validate_path's
    # resolve() — a junction or short name into a credential dir resolves to
    # ".ssh" etc. and is caught below. This validator does NOT delegate to
    # _validate_path (it checks only the credential-subset, not the full
    # blacklist), so it normalizes its own path here.
    resolved = p.expanduser().resolve()
    parts = {part.lower() for part in resolved.parts}

    hit = parts & _CREDENTIAL_DIRS
    if hit:
        raise ValueError(
            "Access denied: refusing to launch a binary from a credential "
            f"directory ({next(iter(hit))}) (L-03)"
        )

    # Defense-in-depth: refuse to launch from the agent's own install/data tree
    # even though writes there are already blocked (M-04) — closes a direct
    # launch-of-planted-payload path. ``resolve()`` defeats junction/case/8.3
    # escapes the same way it does for write protection.
    for root in _protected_write_roots():
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        raise ValueError(
            "Access denied: refusing to launch a binary inside the agent's "
            "install tree or data directory (L-03)"
        )


@_with_elevation_hint
def read_file(path: str, offset: int | None = None, limit: int | None = None,
              encoding: str | None = None) -> dict[str, Any]:
    """Read a file and return its contents.

    ``offset`` / ``limit`` (1-based line numbers) select a line range instead
    of the whole file — for large files that would otherwise hit the 5 MiB
    cap. When omitted, the whole file is read (capped at ``MAX_READ_BYTES``).

    ``encoding`` (TODO #20b) overrides the default UTF-8 decode for legacy
    codepage files (e.g. ``gbk``, ``latin-1``). Omit it for UTF-8. A bad
    encoding name raises ``LookupError`` (→ INVALID_PARAMS) before any read.

    Capped at ``MAX_READ_BYTES`` (5 MiB) to bound daemon memory — a larger file
    is truncated with a notice rather than read whole. (SEC3-03)
    """
    # Validate the encoding name up front so a typo surfaces as INVALID_PARAMS
    # (→ ValueError at the router) rather than a partial read. codecs.lookup also
    # normalizes aliases. Re-raise as ValueError because the router maps
    # ValueError → INVALID_PARAMS; LookupError would otherwise fall through to
    # DAEMON_INTERNAL_ERROR with log spam.
    if encoding:
        import codecs
        try:
            codecs.lookup(encoding)
        except LookupError as e:
            raise ValueError(str(e)) from None
    enc = encoding or "utf-8"
    # An explicitly-chosen encoding should fail loudly on a wrong codepage
    # (strict) — returning mojibake with no signal defeats the purpose. The
    # default UTF-8 path keeps errors="replace" for legacy robustness.
    errors = "strict" if encoding else "replace"

    p = _validate_path(_resolve_user_path(path))
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if p.is_dir():
        raise ValueError(f"Path is a directory, not a file: {path}")

    size = p.stat().st_size

    # Line-range mode (TODO #4): read by lines and slice. The cap still applies
    # to the returned content; offset/limit let callers avoid reading a huge
    # file whole by selecting the slice they want.
    if offset is not None or limit is not None:
        try:
            with p.open("r", encoding=enc, errors=errors) as f:
                lines = f.readlines()
        except UnicodeDecodeError:
            return {
                "output": f"## File Read\nPath: {p}\n[Binary file, cannot read by lines]",
                "content": "",
                "size": size,
                "truncated": False,
            }
        total = len(lines)
        start = max(0, (offset or 1) - 1)  # 1-based → 0-based
        end = start + limit if limit else total
        selected = lines[start:end]
        content = "".join(selected)
        if len(content) > MAX_READ_BYTES:
            content = content[:MAX_READ_BYTES]
        note = f"\n\n[Showing lines {start + 1}-{min(end, total)} of {total}]"
        return {
            "output": f"## File Read\nPath: {p}\nSize: {size} bytes\n---\n{content}{note}",
            "content": content,
            "size": size,
            "truncated": False,
            "lines_total": total,
            "lines_shown": f"{start + 1}-{min(end, total)}",
        }

    truncated = size > MAX_READ_BYTES

    try:
        if truncated:
            # Read only the leading slice to avoid loading a multi-GB file whole.
            # With the default errors="replace" decode cannot raise; binary bytes
            # become U+FFFD. With an explicit encoding (errors="strict") a wrong
            # codepage raises UnicodeDecodeError → binary fallback below.
            with p.open("rb") as f:
                raw = f.read(MAX_READ_BYTES)
            content = raw.decode(enc, errors=errors)
        else:
            content = p.read_text(encoding=enc, errors=errors)
    except UnicodeDecodeError:
        # The file isn't decodable in `enc`. Report it as binary using the
        # already-known stat size rather than read_bytes() — that would load
        # the *entire* (potentially multi-GB) file into memory just to count
        # its bytes, defeating MAX_READ_BYTES and risking OOM.
        content = f"[Binary file, {size} bytes]"

    note = f"\n\n[Truncated: file is {size} bytes, showed first {MAX_READ_BYTES}]" if truncated else ""
    return {
        "output": f"## File Read\nPath: {p}\nSize: {size} bytes\n---\n{content}{note}",
        "content": content,
        "size": size,
        "truncated": truncated,
    }


@_with_elevation_hint
def write_file(path: str, content: str, append: bool = False) -> dict[str, Any]:
    """Write content to a file, or append when ``append=True`` (TODO #4)."""
    p = _validate_write_path(_resolve_user_path(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if _as_bool(append) else "w"
    with p.open(mode, encoding="utf-8") as f:
        f.write(content)
    action = "Appended to" if mode == "a" else "Written to"
    return {"output": f"## File {action.title()}\nPath: {p}\nSize: {p.stat().st_size} bytes"}


@_with_elevation_hint
def list_dir(path: str, pattern: str | None = None, recursive: bool = False, show_hidden: bool = False) -> dict[str, Any]:
    """List directory contents.

    ``pattern`` filters entry names via ``fnmatch`` (non-recursive) or the glob
    itself (recursive). ``recursive=True`` walks subdirectories via ``rglob``.
    ``show_hidden`` keeps dotfile entries (skipped by default). (TODO #4)

    Capped at ``MAX_LIST_ENTRIES`` so a directory with millions of entries can't
    exhaust daemon memory. (SEC3-03)
    """
    p = _validate_path(_resolve_user_path(path))
    if not p.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if not p.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    recursive = _as_bool(recursive)
    show_hidden = _as_bool(show_hidden)

    # In recursive mode the pattern is the glob itself (rglob(pattern)); in
    # non-recursive mode it's a post-filter on each name via fnmatch.
    if recursive:
        iterator = p.rglob(pattern or "*")
    else:
        iterator = p.iterdir()

    entries = []
    total = 0
    truncated = False
    for entry in sorted(iterator, key=lambda e: (not e.is_dir(), e.name.lower())):
        if not show_hidden and entry.name.startswith("."):
            continue
        if pattern and not recursive and not fnmatch.fnmatch(entry.name, pattern):
            continue
        total += 1
        if len(entries) >= MAX_LIST_ENTRIES:
            truncated = True
            continue  # keep counting for the total, but stop formatting
        try:
            is_dir = entry.is_dir()
        except OSError:
            is_dir = False
        if is_dir:
            entries.append(f"  {entry.name}/")
        else:
            try:
                size = entry.stat().st_size
            except OSError:
                size = 0
            entries.append(f"  {entry.name} ({format_size(size)})")

    listing = "\n".join(entries) if entries else "(empty)"
    note = f"\n\n[Truncated: directory has {total} entries, showed first {MAX_LIST_ENTRIES}]" if truncated else ""
    header = f"## Directory Listing\nPath: {p}"
    if pattern:
        header += f"\nFilter: {pattern}"
    return {
        "output": f"{header}\n---\n{listing}{note}",
        "count": total,
        "truncated": truncated,
    }


def _check_overwrite_shape(src: Path, dst: Path) -> None:
    """Reject a cross-type overwrite (file↔dir) BEFORE any destructive op.

    Without this, ``move(file, nonempty_dir, overwrite=True)`` would rmtree the
    destination first and THEN fail (NotADirectoryError) — destroying unrelated
    files and leaving nothing usable. (review B3) Raises ValueError on mismatch.
    """
    if dst.is_dir() and not src.is_dir():
        raise ValueError(f"Cannot overwrite a directory with a file: {dst}")
    if dst.is_file() and src.is_dir():
        raise ValueError(f"Cannot overwrite a file with a directory: {dst}")


@_with_elevation_hint
def copy_path(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
    """Copy a file or directory to ``destination`` (TODO #4).

    Source is read-validated, destination write-validated (so a copy can't
    land inside the agent's install/data tree or a blacklisted dir). Refuses
    to overwrite an existing destination unless ``overwrite=True``.
    """
    overwrite = _as_bool(overwrite)
    src = _validate_path(_resolve_user_path(source))
    dst = _validate_write_path(_resolve_user_path(destination))
    if not src.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    if dst.exists() and not overwrite:
        raise ValueError(f"Destination already exists: {destination}. Set overwrite=True to replace.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        if dst.exists() and overwrite:
            _check_overwrite_shape(src, dst)
            shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
        shutil.copy2(src, dst)
        kind = "file"
    elif src.is_dir():
        if dst.exists() and overwrite:
            _check_overwrite_shape(src, dst)
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
        kind = "directory"
    else:
        raise ValueError(f"Unsupported source type: {source}")
    return {"output": f"## File Copy\nCopied {kind}: {src} -> {dst}"}


@_with_elevation_hint
def move_path(source: str, destination: str, overwrite: bool = False) -> dict[str, Any]:
    """Move/rename a file or directory (TODO #4).

    The source is write-validated (not read-validated like copy): moving
    REMOVES the file from the source path, so it's a write to that location —
    the agent's install/data tree and blacklist must protect it, exactly as
    they do for delete_path. (review B2)
    """
    overwrite = _as_bool(overwrite)
    src = _validate_write_path(_resolve_user_path(source))
    dst = _validate_write_path(_resolve_user_path(destination))
    if not src.exists():
        raise FileNotFoundError(f"Source not found: {source}")
    if dst.exists() and not overwrite:
        raise ValueError(f"Destination already exists: {destination}. Set overwrite=True to replace.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and overwrite:
        _check_overwrite_shape(src, dst)
        shutil.rmtree(dst) if dst.is_dir() else dst.unlink()
    shutil.move(src, dst)
    return {"output": f"## File Move\nMoved: {src} -> {dst}"}


@_with_elevation_hint
def delete_path(path: str, recursive: bool = False) -> dict[str, Any]:
    """Delete a file or directory (TODO #4).

    The target is write-validated (treated as a write — so the blacklist AND
    the agent's install/data-tree protection both apply: a prompt-injected
    delete can't remove the agent's own source, config, or blacklisted files).
    A non-empty directory requires ``recursive=True``; otherwise it's refused.
    """
    recursive = _as_bool(recursive)
    p = _validate_write_path(_resolve_user_path(path))
    if not p.exists():
        raise FileNotFoundError(f"Path not found: {path}")
    if p.is_file() or p.is_symlink():
        p.unlink()
        return {"output": f"## File Delete\nDeleted file: {p}"}
    if p.is_dir():
        if not recursive and any(p.iterdir()):
            raise ValueError(f"Directory is not empty: {path}. Set recursive=True to delete non-empty directories.")
        shutil.rmtree(p)
        return {"output": f"## File Delete\nDeleted directory: {p}"}
    raise ValueError(f"Unsupported file type: {path}")


@_with_elevation_hint
def search_files(path: str, pattern: str, recursive: bool = True) -> dict[str, Any]:
    """Find files/dirs matching a glob ``pattern`` under ``path`` (TODO #4).

    ``recursive=True`` (default) walks the whole subtree via ``rglob``;
    ``recursive=False`` checks only the top level via ``glob``. Capped at
    ``MAX_SEARCH_RESULTS`` to bound daemon memory on huge trees.
    """
    recursive = _as_bool(recursive)
    p = _validate_path(_resolve_user_path(path))
    if not p.exists():
        raise FileNotFoundError(f"Search path not found: {path}")
    if not p.is_dir():
        raise ValueError(f"Search path is not a directory: {path}")

    iterator = p.rglob(pattern) if recursive else p.glob(pattern)
    results = []
    count = 0
    truncated = False
    for match in sorted(iterator, key=lambda e: e.name.lower()):
        count += 1
        if len(results) >= MAX_SEARCH_RESULTS:
            truncated = True
            break
        try:
            size = match.stat().st_size if match.is_file() else 0
        except OSError:
            size = 0
        rel = match.relative_to(p)
        tag = "DIR " if match.is_dir() else "FILE"
        results.append(f"  [{tag}] {rel}  ({format_size(size)})" if match.is_file() else f"  [{tag}] {rel}")

    if not results:
        return {"output": f'## File Search\nNo matches for "{pattern}" in {p}', "count": 0}
    listing = "\n".join(results)
    note = f"\n\n[Truncated: {MAX_SEARCH_RESULTS}+ matches]" if truncated else ""
    return {
        "output": f'## File Search\nPattern: "{pattern}" in {p} ({min(count, MAX_SEARCH_RESULTS)} matches)\n{listing}{note}',
        "count": count,
        "truncated": truncated,
    }


@_with_elevation_hint
def get_file_info(path: str) -> dict[str, Any]:
    """Return metadata about a file or directory (TODO #4)."""
    p = _validate_path(_resolve_user_path(path))
    if not p.exists() and not p.is_symlink():
        raise FileNotFoundError(f"Path not found: {path}")
    # lstat (not stat): stat() follows the link target and raises on a dangling
    # symlink, but an "info" tool should report the link itself. lstat stats the
    # link entry, succeeding on broken links. (review M1)
    stat = p.lstat()
    if p.is_dir():
        file_type = "Directory"
    elif p.is_file():
        file_type = "File"
    elif p.is_symlink():
        file_type = "Symlink"
    else:
        file_type = "Other"

    lines = [
        f"## File Info",
        f"Path: {p}",
        f"Type: {file_type}",
        f"Size: {stat.st_size} bytes ({format_size(stat.st_size)})",
        f"Created: {datetime.fromtimestamp(stat.st_ctime).strftime('%Y-%m-%d %H:%M:%S')}",
        f"Modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S')}",
        f"Accessed: {datetime.fromtimestamp(stat.st_atime).strftime('%Y-%m-%d %H:%M:%S')}",
        f"Read-only: {not os.access(p, os.W_OK)}",
    ]
    result: dict[str, Any] = {
        "output": "\n".join(lines),
        "path": str(p),
        "type": file_type,
        "size": stat.st_size,
        "created": stat.st_ctime,
        "modified": stat.st_mtime,
        "accessed": stat.st_atime,
        "read_only": not os.access(p, os.W_OK),
    }
    if p.is_dir():
        try:
            items = list(p.iterdir())
            result["contents_dirs"] = sum(1 for i in items if i.is_dir())
            result["contents_files"] = sum(1 for i in items if i.is_file())
            result["output"] += f"\nContents: {result['contents_files']} files, {result['contents_dirs']} directories"
        except PermissionError:
            pass
    if p.is_file():
        ext = p.suffix or "(none)"
        result["extension"] = ext
        result["output"] += f"\nExtension: {ext}"
    if p.is_symlink():
        target = str(os.readlink(p))
        result["link_target"] = target
        result["output"] += f"\nLink target: {target}"
    return result
