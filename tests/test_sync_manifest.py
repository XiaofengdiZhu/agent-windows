"""White-box tests for scripts/sync_manifest.py — tmp_path fake repo, no Windows."""
from __future__ import annotations

import sys
from pathlib import Path

# script is not a package — add scripts/ to path and import as module
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import sync_manifest as sm  # noqa: E402


def _fake_repo(tmp_path: Path, *, pyproject_version="0.1.0", init_version="0.1.0",
               manifest_version="0.1.0", server_version="0.1.0",
               tool_names=("click", "type", "ping"), extra_manifest_names=(),
               init_has_version=True) -> Path:
    """Build a minimal fake repo under tmp_path mirroring the real layout."""
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "agent-windows"\nversion = "{pyproject_version}"\n',
        encoding="utf-8")
    pkg = tmp_path / "src" / "agent_windows"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(
        f'"""docstring"""\n\n__version__ = "{init_version}"\n' if init_has_version
        else '"""docstring"""\n',
        encoding="utf-8")
    tools_dir = pkg / "mcp" / "tools"
    tools_dir.mkdir(parents=True)
    src = "".join(
        f'@mcp.tool(name="{n}", description="d")\ndef {n}_fn(): pass\n\n'
        for n in tool_names)
    (tools_dir / "control.py").write_text(src, encoding="utf-8")
    (tmp_path / "manifest.json").write_text(
        '{"version": "%s", "tools": [%s]}' % (
            manifest_version,
            ", ".join('{ "name": "%s", "description": "d" }' % n
                      for n in list(tool_names) + list(extra_manifest_names))),
        encoding="utf-8")
    (tmp_path / "server.json").write_text(
        '{"name": "x", "version": "%s", "packages": [{"version": "%s"}]}' % (
            server_version, server_version),
        encoding="utf-8")
    return tmp_path


def test_read_pyproject_version(tmp_path):
    root = _fake_repo(tmp_path, pyproject_version="1.2.3")
    assert sm.read_pyproject_version(root) == "1.2.3"


def test_source_tool_names_extracts_mcp_registrations(tmp_path):
    root = _fake_repo(tmp_path)
    assert sm.source_tool_names(root) == {"click", "type", "ping"}


def test_manifest_tool_names_reads_manifest(tmp_path):
    root = _fake_repo(tmp_path)
    assert sm.manifest_tool_names(root) == {"click", "type", "ping"}
