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


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def check(root: Path = REPO_ROOT) -> tuple[bool, list[str]]:
    """Compare version 4-way + tool-name set. Return (ok, human-readable diffs)."""
    diffs: list[str] = []
    pv = read_pyproject_version(root)
    manifest = _load(root / "manifest.json")
    server = _load(root / "server.json")
    for label, v in [
        ("__init__.__version__", read_init_version(root)),
        ("manifest.version", manifest["version"]),
        ("server.version", server["version"]),
        ("server.packages[0].version", server["packages"][0]["version"]),
    ]:
        if v != pv:
            diffs.append(f"version drift: {label}={v!r} != pyproject={pv!r}")
    m_names = manifest_tool_names(root)
    s_names = source_tool_names(root)
    if m_names != s_names:
        diffs.append(
            f"name drift: missing in manifest={sorted(s_names - m_names)}; "
            f"extra in manifest={sorted(m_names - s_names)}")
    return (not diffs, diffs)


def main(argv: list[str] | None = None) -> int:
    """Entry point: --check (default, exit 1 on drift) / --write."""
    p = argparse.ArgumentParser(
        description="Sync manifest version from pyproject + check tool-name drift.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--check", action="store_true",
                   help="fail (exit 1) on drift (default)")
    g.add_argument("--write", action="store_true",
                   help="inject pyproject version into __init__/manifest/server")
    args = p.parse_args(argv)
    if args.write:
        written = write()
        print("wrote: " + ", ".join(written))
        return 0
    ok, diffs = check()
    if ok:
        print("no drift")
        return 0
    print("\n".join(diffs))
    return 1


if __name__ == "__main__":
    sys.exit(main())
