"""File system operations: read, write, list."""

import os
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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


def read_file(path: str) -> dict[str, Any]:
    """Read a file and return its contents."""
    p = _validate_path(path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if p.is_dir():
        raise ValueError(f"Path is a directory, not a file: {path}")

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except UnicodeDecodeError:
        content = p.read_bytes()
        content = f"[Binary file, {len(content)} bytes]"

    size = p.stat().st_size
    return {
        "output": f"## File Read\nPath: {p}\nSize: {size} bytes\n---\n{content}",
        "content": content,
        "size": size,
    }


def write_file(path: str, content: str) -> dict[str, Any]:
    """Write content to a file."""
    p = _validate_write_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"output": f"## File Write\nPath: {p}\nSize: {len(content)} bytes"}


def list_dir(path: str) -> dict[str, Any]:
    """List directory contents."""
    p = _validate_path(path)
    if not p.exists():
        raise FileNotFoundError(f"Directory not found: {path}")
    if not p.is_dir():
        raise ValueError(f"Path is not a directory: {path}")

    entries = []
    for entry in sorted(p.iterdir()):
        if entry.is_dir():
            entries.append(f"  {entry.name}/")
        else:
            size = entry.stat().st_size
            entries.append(f"  {entry.name} ({size} bytes)")

    listing = "\n".join(entries) if entries else "(empty)"
    return {
        "output": f"## Directory Listing\nPath: {p}\n---\n{listing}",
        "count": len(entries),
    }
