"""Manifest drift guards: version + tool-list parity via sync_manifest --check.

Delegates the version 4-way + tool-name set comparison to
scripts/sync_manifest.py check() — single source of truth for that logic.
This module keeps only the structural required-fields assertions that
sync_manifest.py does NOT cover. Pure stdlib (json/subprocess/sys/pathlib);
does NOT import the agent_windows package (which pulls in comtypes/pywin32),
keeping it fast and host-independent.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    return json.loads((REPO_ROOT / name).read_text(encoding="utf-8"))


def test_no_manifest_drift():
    """sync_manifest --check passes (version 4-way + tool names aligned).

    Replaces the old inline version + name assertions — the check logic now
    lives in one place (scripts/sync_manifest.py check()).
    """
    r = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "sync_manifest.py"), "--check"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


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
