"""Sync manifest/server version from pyproject.toml + check tool-name drift.

Pure stdlib — does NOT import agent_windows (avoids comtypes/pywin32/Windows
heavy deps; same philosophy as tests/test_manifest.py).

Usage:
    python scripts/sync_manifest.py            # default --check: exit 1 on drift
    python scripts/sync_manifest.py --check    # explicit check
    python scripts/sync_manifest.py --write    # inject version into 4 files
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_TOOL_NAME_RE = re.compile(r'@mcp\.tool\(name="([^"]+)"')
_PYPROJECT_VER_RE = re.compile(r'version\s*=\s*"([^"]+)"')


def read_pyproject_version(root: Path = REPO_ROOT) -> str:
    """version from [project] in pyproject.toml (single source of truth).

    pyproject.toml's [project] section is conventionally first and carries the
    only top-level `version =` assignment; first regex match = that value.
    """
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    m = _PYPROJECT_VER_RE.search(text)
    if not m:
        raise RuntimeError("pyproject.toml has no top-level version")
    return m.group(1)


def read_init_version(root: Path = REPO_ROOT) -> str:
    """__version__ from src/agent_windows/__init__.py (regex, NOT import)."""
    text = (root / "src" / "agent_windows" / "__init__.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise RuntimeError("__init__.py has no __version__ assignment")
    return m.group(1)


def source_tool_names(root: Path = REPO_ROOT) -> set[str]:
    """@mcp.tool(name=...) values across src/agent_windows/mcp/tools/*.py."""
    tools_dir = root / "src" / "agent_windows" / "mcp" / "tools"
    names: set[str] = set()
    for py in sorted(tools_dir.glob("*.py")):
        names.update(_TOOL_NAME_RE.findall(py.read_text(encoding="utf-8")))
    return names


def manifest_tool_names(root: Path = REPO_ROOT) -> set[str]:
    """manifest.json tools[].name."""
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return {t["name"] for t in manifest.get("tools", [])}
