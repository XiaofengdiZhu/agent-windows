"""File system operations: read, write, list."""

import os
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def read_file(path: str) -> dict[str, Any]:
    """Read a file and return its contents."""
    p = Path(path).expanduser()
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
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"output": f"## File Write\nPath: {p}\nSize: {len(content)} bytes"}


def list_dir(path: str) -> dict[str, Any]:
    """List directory contents."""
    p = Path(path).expanduser()
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
