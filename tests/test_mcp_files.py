"""Tests for the /files HTTP transfer endpoints (handler logic via a bare
Starlette app; no FastMCP/uvicorn)."""
from starlette.applications import Starlette
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


# --- upload (PUT) + register_file_endpoints (Task 3) ---

from functools import partial

from agent_windows.mcp.files import upload


def _upload_app(cap):
    return Starlette(routes=[Route("/files", partial(upload, cap=cap), methods=["PUT"])])


def test_upload_creates_file_and_returns_sha256(tmp_path):
    import hashlib
    dest = tmp_path / "out.bin"
    body = b"x" * 100
    r = TestClient(_upload_app(512)).put(
        f"/files?path={dest}", content=body,
        headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200
    assert dest.read_bytes() == body
    assert r.json()["sha256"] == hashlib.sha256(body).hexdigest()
    assert r.json()["bytes"] == 100


def test_upload_rejects_existing_without_overwrite(tmp_path):
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")
    r = TestClient(_upload_app(512)).put(f"/files?path={dest}", content=b"new")
    assert r.status_code == 409
    assert dest.read_bytes() == b"old"  # untouched


def test_upload_overwrites_with_flag(tmp_path):
    dest = tmp_path / "out.bin"
    dest.write_bytes(b"old")
    r = TestClient(_upload_app(512)).put(f"/files?path={dest}&overwrite=true", content=b"new")
    assert r.status_code == 200
    assert dest.read_bytes() == b"new"


def test_upload_creates_parent_dirs(tmp_path):
    dest = tmp_path / "sub" / "deep" / "out.bin"
    r = TestClient(_upload_app(512)).put(f"/files?path={dest}", content=b"x")
    assert r.status_code == 200
    assert dest.read_bytes() == b"x"


def test_upload_rejects_blacklisted_path(tmp_path):
    blocked = tmp_path / ".ssh" / "k"
    r = TestClient(_upload_app(512)).put(f"/files?path={blocked}", content=b"x")
    assert r.status_code == 403


def test_upload_rejects_oversize_via_content_length(tmp_path):
    # Bytes body -> httpx sends an accurate Content-Length, so the EARLY guard
    # fires before any streaming begins. Provable via the "exceeds" wording
    # (distinct from the mid-stream branch's "exceeded" wording).
    cap = 1  # 1 MiB
    dest = tmp_path / "big.bin"
    body = b"z" * (int(cap * 1024 * 1024) + 10)
    r = TestClient(_upload_app(cap)).put(f"/files?path={dest}", content=body)
    assert r.status_code == 413
    assert "exceeds" in r.json()["error"] and "exceeded" not in r.json()["error"]
    assert not dest.exists()
    assert not any(p.name.startswith(".awpart-") for p in tmp_path.iterdir())


def test_upload_aborts_midstream_over_cap_no_temp(tmp_path):
    # Generator body -> httpx uses chunked transfer-encoding, sending NO
    # Content-Length header. This bypasses the early guard and forces the
    # MID-STREAM path: the first chunk raises _SizeExceeded, the except branch
    # _safe_unlink()s the temp, and the response carries the "exceeded"
    # wording. Proves temp cleanup runs on the streaming-abort path.
    cap = 1  # 1 MiB
    dest = tmp_path / "big.bin"
    cap_bytes = int(cap * 1024 * 1024)

    def body_chunks():
        yield b"z" * (cap_bytes + 10)  # single chunk > cap, no Content-Length

    r = TestClient(_upload_app(cap)).put(f"/files?path={dest}", content=body_chunks())
    assert r.status_code == 413
    assert "exceeded" in r.json()["error"] and "exceeds" not in r.json()["error"]
    assert not dest.exists()
    assert not any(p.name.startswith(".awpart-") for p in tmp_path.iterdir())


def test_register_file_endpoints_mounts_three_routes():
    from agent_windows.mcp.files import register_file_endpoints
    mounted = []

    class FakeMcp:
        def custom_route(self, path, methods):
            def deco(fn):
                mounted.append((path, methods))
                return fn
            return deco

    register_file_endpoints(FakeMcp(), max_upload_mb=16)
    # methods are lists (unhashable) -> join for a deterministic compare.
    paths_methods = sorted(f"{p}:{','.join(m)}" for p, m in mounted)
    assert paths_methods == ["/files:GET", "/files:HEAD", "/files:PUT"]
