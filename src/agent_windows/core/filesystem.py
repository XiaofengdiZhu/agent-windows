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


def _validate_path(path: str) -> Path:
    """Validate that a file path is not in the blacklist."""
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
    p = _validate_path(path)
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
