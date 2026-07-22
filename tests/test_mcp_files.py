"""Tests for the /files HTTP transfer endpoints (handler logic via a bare
Starlette app; no FastMCP/uvicorn)."""
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from agent_windows.mcp.files import download, head


def _app(routes):
    return Starlette(routes=routes)


def test_download_streams_file(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"hello-bytes")
    app = _app([Route("/files", download, methods=["GET"])])
    r = TestClient(app).get(f"/files?path={p}")
    assert r.status_code == 200
    assert r.content == b"hello-bytes"
    assert r.headers["content-type"] == "application/octet-stream"


def test_download_missing_path_param(tmp_path):
    app = _app([Route("/files", download, methods=["GET"])])
    assert TestClient(app).get("/files").status_code == 400


def test_download_not_found(tmp_path):
    app = _app([Route("/files", download, methods=["GET"])])
    r = TestClient(app).get(f"/files?path={tmp_path / 'nope.bin'}")
    assert r.status_code == 404


def test_download_directory_rejected(tmp_path):
    app = _app([Route("/files", download, methods=["GET"])])
    r = TestClient(app).get(f"/files?path={tmp_path}")
    assert r.status_code == 400


def test_download_blacklisted_path(tmp_path):
    # .ssh is a blocked component (credential dir) -> validate raises -> 403.
    blocked = tmp_path / ".ssh" / "id_rsa"
    app = _app([Route("/files", download, methods=["GET"])])
    r = TestClient(app).get(f"/files?path={blocked}")
    assert r.status_code == 403


def test_head_returns_sha256_and_size(tmp_path):
    import hashlib
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc")
    app = _app([Route("/files", head, methods=["HEAD"])])
    r = TestClient(app).request("HEAD", f"/files?path={p}")
    assert r.status_code == 200
    assert r.headers["x-sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert r.headers["x-size"] == "3"
    assert r.content == b""
