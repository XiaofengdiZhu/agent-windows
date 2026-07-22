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
from typing import Any

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

_CHUNK = 64 * 1024  # read granularity for hashing / (upload in Task 3)


def _err(status: int, message: str, **extra: Any) -> JSONResponse:
    payload = {"error": message}
    payload.update(extra)
    return JSONResponse(content=payload, status_code=status)


def _sha256_file(p) -> str:
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


# upload() + register_file_endpoints() added in Task 3.
