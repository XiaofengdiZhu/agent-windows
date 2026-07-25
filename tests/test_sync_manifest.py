"""White-box tests for scripts/sync_manifest.py — tmp_path fake repo, no Windows."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

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


def test_check_no_drift(tmp_path):
    root = _fake_repo(tmp_path)
    ok, diffs = sm.check(root)
    assert ok is True and diffs == []


def test_check_extra_name_drift(tmp_path):
    root = _fake_repo(tmp_path, extra_manifest_names=("ghost",))
    ok, diffs = sm.check(root)
    assert ok is False
    assert any("extra in manifest" in d and "ghost" in d for d in diffs)


def test_check_missing_name_drift(tmp_path):
    # _fake_repo populates source AND manifest from the same tool_names tuple,
    # so to simulate missing-from-manifest drift we add a source-only tool.
    root = _fake_repo(tmp_path)
    tools_dir = root / "src" / "agent_windows" / "mcp" / "tools"
    (tools_dir / "extra.py").write_text(
        '@mcp.tool(name="screenshot", description="d")\ndef screenshot_fn(): pass\n',
        encoding="utf-8")
    ok, diffs = sm.check(root)
    assert ok is False
    assert any("missing in manifest" in d and "screenshot" in d for d in diffs)


def test_check_version_drift_init(tmp_path):
    root = _fake_repo(tmp_path, init_version="9.9.9")
    ok, diffs = sm.check(root)
    assert ok is False
    assert any("version drift" in d and "__init__" in d for d in diffs)


def test_check_version_drift_manifest(tmp_path):
    root = _fake_repo(tmp_path, manifest_version="9.9.9")
    ok, diffs = sm.check(root)
    assert ok is False
    assert any("version drift" in d and "manifest.version" in d for d in diffs)


def test_write_injects_all_four(tmp_path):
    root = _fake_repo(tmp_path, pyproject_version="2.0.0", init_version="0.1.0",
                      manifest_version="0.1.0", server_version="0.1.0")
    written = sm.write(root)
    assert sm.read_init_version(root) == "2.0.0"
    assert json.loads((root / "manifest.json").read_text())["version"] == "2.0.0"
    srv = json.loads((root / "server.json").read_text())
    assert srv["version"] == "2.0.0"
    assert srv["packages"][0]["version"] == "2.0.0"
    assert sorted(written) == ["manifest.json", "server.json",
                               "src/agent_windows/__init__.py"]


def test_write_preserves_init_docstring(tmp_path):
    root = _fake_repo(tmp_path, pyproject_version="3.0.0")
    sm.write(root)
    init_text = (root / "src" / "agent_windows" / "__init__.py").read_text(encoding="utf-8")
    assert init_text.startswith('"""docstring"""')
    assert '__version__ = "3.0.0"' in init_text


def test_write_preserves_manifest_compact_tools(tmp_path):
    # manifest tools[] must stay single-line compact (NOT json.dumps-expanded)
    root = _fake_repo(tmp_path, pyproject_version="3.0.0")
    sm.write(root)
    m_text = (root / "manifest.json").read_text(encoding="utf-8")
    assert '{ "name": "click", "description": "d" }' in m_text


def test_write_atomic_on_missing_init_version(tmp_path):
    root = _fake_repo(tmp_path, pyproject_version="3.0.0", init_has_version=False)
    with pytest.raises(RuntimeError, match="__version__"):
        sm.write(root)
    # no half-write: manifest/server unchanged
    assert json.loads((root / "manifest.json").read_text())["version"] == "0.1.0"
    assert json.loads((root / "server.json").read_text())["version"] == "0.1.0"


def test_write_preserves_manifest_line_endings(tmp_path):
    # manifest.json is LF in the real repo — write() must keep LF, not CRLF.
    root = _fake_repo(tmp_path, pyproject_version="3.0.0")
    m_path = root / "manifest.json"
    m_path.write_bytes(m_path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8"))
    sm.write(root)
    assert b"\r\n" not in m_path.read_bytes()  # still LF, no CRLF introduced
