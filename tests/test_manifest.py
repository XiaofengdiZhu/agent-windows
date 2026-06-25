"""Manifest drift guards: version single-source-of-truth + tool-list parity.

Mirrors TODO #10's TestDataclassDefaults philosophy — catch drift between
static metadata files and the live code at test time, so a release can never
ship a manifest whose version or tool list is stale. Pure stdlib (json/re/
pathlib); does NOT import the agent_windows package (which pulls in
comtypes/pywin32), keeping it fast and host-independent.
"""
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read_pyproject_version() -> str:
    """version from [project] in pyproject.toml (single source of truth)."""
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml has no top-level version"
    return m.group(1)


def _read_init_version() -> str:
    """__version__ from src/agent_windows/__init__.py.

    Read the file + regex rather than `import agent_windows`: the package
    init pulls in comtypes/pywin32 at import time, which we don't want a
    metadata-drift test to pay for (and it'd need a Windows host).
    """
    text = (REPO_ROOT / "src" / "agent_windows" / "__init__.py").read_text(
        encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    assert m, "__init__.py has no __version__"
    return m.group(1)


def _load(name):
    return json.loads((REPO_ROOT / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def expected_version():
    return _read_pyproject_version()


def test_versions_all_match(expected_version):
    """pyproject == __init__ == manifest.json.version == server.json.version
    == server.json.packages[0].version.

    pyproject.toml is the single source of truth; the other four must track
    it. A release bump that misses one source fails here.
    """
    assert _read_init_version() == expected_version
    manifest = _load("manifest.json")
    server = _load("server.json")
    assert manifest["version"] == expected_version
    assert server["version"] == expected_version
    assert server["packages"][0]["version"] == expected_version


def _source_tool_names() -> set[str]:
    """All @mcp.tool(name=...) values across src/agent_windows/mcp/tools/*.py."""
    tools_dir = REPO_ROOT / "src" / "agent_windows" / "mcp" / "tools"
    names: set[str] = set()
    pattern = re.compile(r'@mcp\.tool\(name="([^"]+)"')
    for py in tools_dir.glob("*.py"):
        names.update(pattern.findall(py.read_text(encoding="utf-8")))
    assert names, "no tools found — regex drifted from registration style?"
    return names


def test_manifest_tools_match_source():
    """manifest.json tools[] names must equal the source @mcp.tool names.

    Adding/renaming/removing a tool without updating manifest.json fails here.
    """
    manifest = _load("manifest.json")
    manifest_names = {t["name"] for t in manifest["tools"]}
    source_names = _source_tool_names()
    assert manifest_names == source_names, (
        f"manifest/tools drift — missing in manifest: "
        f"{source_names - manifest_names}; extra in manifest: "
        f"{manifest_names - source_names}"
    )


def test_manifest_required_fields():
    """manifest.json has the Bundle-spec required top-level keys + launch shape."""
    m = _load("manifest.json")
    for k in ("manifest_version", "name", "version", "description",
              "server", "tools", "compatibility", "license"):
        assert k in m, f"manifest.json missing top-level key: {k}"
    assert m["server"]["mcp_config"]["command"] == "uv"
    assert "serve" in m["server"]["mcp_config"]["args"]
    assert m["compatibility"]["platforms"] == ["win32"]


def test_server_json_required_fields():
    """server.json has the Registry-spec shape (pypi package + uvx runtime)."""
    s = _load("server.json")
    assert s["$schema"].startswith("https://static.modelcontextprotocol.io/")
    assert s["packages"][0]["registryType"] == "pypi"
    assert s["packages"][0]["identifier"] == "agent-windows"
    assert s["packages"][0]["runtimeHint"] == "uvx"
