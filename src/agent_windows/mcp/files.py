"""HTTP file-transfer endpoints mounted on the MCP server's HTTP transport.

GET  /files?path=<abs>                  stream a file down (host <- Windows)
HEAD /files?path=<abs>                  headers only (size/sha256/type), no body
PUT  /files?path=<abs>&overwrite=true   stream a file up (host -> Windows)

These ride the SAME uvicorn listener + ASGI middleware chain (bearer auth, IP
allowlist, TLS, DNS-rebinding) as the MCP /mcp endpoint — see
``mcp/server.run()``. They are NOT MCP tools (not callable via JSON-RPC); the
consumer is an HTTP client (curl) on the agent's host. Stdio never mounts
them. Path access reuses ``core.filesystem.validate_read_path`` /
``validate_write_path``, so the blacklist + install-tree protection is
identical to the fs_* tools.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

_CHUNK = 64 * 1024  # read granularity for hashing


def _err(status: int, message: str, **extra: Any) -> JSONResponse:
    payload = {"error": message}
    payload.update(extra)
    return JSONResponse(content=payload, status_code=status)


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


async def download(request: Request) -> Response:
    """GET /files?path=<abs> — stream a file to the client."""
    raw = request.query_params.get("path")
    if not raw:
        return _err(400, "missing required query param 'path'")
    if not Path(raw).is_absolute():
        return _err(400, "path must be absolute (relative paths are not supported)")
    from agent_windows.core.filesystem import validate_read_path
    try:
        target = validate_read_path(raw)
    except ValueError as exc:
        return _err(403, str(exc))
    if target.is_dir():
        return _err(400, "path is a directory; directories are not supported (v1)")
    if not target.exists():
        return _err(404, f"not found: {raw}")
    return FileResponse(str(target), filename=target.name,
                        media_type="application/octet-stream")


async def head(request: Request) -> Response:
    """HEAD /files?path=<abs> — headers (size/sha256/type) only, no body."""
    raw = request.query_params.get("path")
    if not raw:
        return _err(400, "missing required query param 'path'")
    if not Path(raw).is_absolute():
        return _err(400, "path must be absolute (relative paths are not supported)")
    from agent_windows.core.filesystem import validate_read_path
    try:
        target = validate_read_path(raw)
    except ValueError as exc:
        return _err(403, str(exc))
    if target.is_dir():
        return _err(400, "path is a directory; directories are not supported (v1)")
    if not target.exists():
        return _err(404, f"not found: {raw}")
    import asyncio
    size = target.stat().st_size
    digest = await asyncio.to_thread(_sha256_file, target)
    return Response(status_code=200, headers={
        "content-type": "application/octet-stream",
        "content-length": str(size),
        "content-disposition": f'attachment; filename="{target.name}"',
        "x-sha256": digest,
        "x-size": str(size),
        "x-type": "file",
    })


class _SizeExceeded(Exception):
    """Internal: upload body surpassed the configured cap mid-stream."""


def _safe_unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


async def upload(request: Request, *, cap: int | None) -> Response:
    """PUT /files?path=<abs>[&overwrite=true] — stream a file from the client.

    Streams to a same-dir temp file (atomic ``os.replace``), hashing + size-
    counting as it goes. ``cap`` (bytes, None = unlimited) bounds upload size.
    """
    import os
    import secrets
    raw = request.query_params.get("path")
    if not raw:
        return _err(400, "missing required query param 'path'")
    if not Path(raw).is_absolute():
        return _err(400, "path must be absolute (relative paths are not supported)")
    overwrite = str(request.query_params.get("overwrite", "")).strip().lower() in (
        "1", "true", "yes", "on")
    from agent_windows.core.filesystem import validate_write_path
    try:
        target = validate_write_path(raw)
    except ValueError as exc:
        return _err(403, str(exc))
    if target.is_dir():
        return _err(400, "path is a directory; directories are not supported (v1)")
    # Early reject on declared Content-Length (don't read an oversized body).
    if cap is not None:
        cl = request.headers.get("content-length")
        if cl and cl.isascii() and cl.isdigit() and int(cl) > cap:
            return _err(413, f"upload exceeds max_upload_mb cap ({cap} bytes)")
    if target.exists() and not overwrite:
        return _err(409, f"target exists; pass overwrite=true to replace: {raw}")
    tmp = target.parent / f".awpart-{secrets.token_hex(8)}"
    h = hashlib.sha256()
    size = 0
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                if cap is not None and size + len(chunk) > cap:
                    raise _SizeExceeded(cap)
                f.write(chunk)
                h.update(chunk)
                size += len(chunk)
        os.replace(tmp, target)
    except _SizeExceeded:
        _safe_unlink(tmp)
        return _err(413, f"upload exceeded max_upload_mb cap ({cap} bytes)")
    except Exception as exc:  # noqa: BLE001 — cleanup, then honest 500
        _safe_unlink(tmp)
        return _err(500, f"upload failed: {exc}")
    return JSONResponse({"path": str(target), "bytes": size,
                         "sha256": h.hexdigest()}, status_code=200)


def register_file_endpoints(mcp: Any, *, max_upload_mb: int = 512) -> None:
    """Mount GET/HEAD/PUT /files on a FastMCP instance (HTTP transport only).

    Caller (``mcp/server.maybe_mount_file_endpoints``) already gated on
    transport != stdio and ``file_transfer``. ``max_upload_mb`` <= 0 means no
    cap.
    """
    from functools import partial
    cap = (int(max_upload_mb) * 1024 * 1024
           if max_upload_mb and max_upload_mb > 0 else None)
    mcp.custom_route("/files", methods=["GET"])(download)
    mcp.custom_route("/files", methods=["HEAD"])(head)
    mcp.custom_route("/files", methods=["PUT"])(partial(upload, cap=cap))
