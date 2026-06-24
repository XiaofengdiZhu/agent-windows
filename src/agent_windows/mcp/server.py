"""FastMCP server: build the instance, register all tools, and run on a transport.

Transports (mirroring Windows-MCP):
  - stdio              (default; for local clients like Claude Desktop)
  - sse                (HTTP + Server-Sent-Events)
  - streamable-http    (HTTP, the modern MCP transport)

The server does NOT own automation logic — it forwards every tool call to the
existing daemon over the named pipe (see mcp/_call.py). The daemon auto-starts
on first use, exactly as it does for the CLI, so `agent-windows mcp serve`
"just works" with no separate daemon step.

HTTP transports (sse / streamable-http) may require an auth-key (bearer token)
set via `agent-windows mcp auth` or config [mcp] auth_key. When set, every
request must carry `Authorization: Bearer <key>`, enforced by an ASGI
middleware so an unauthenticated bind to a non-loopback address is rejected.
"""
import asyncio
import logging
from typing import Any

from fastmcp import FastMCP

from agent_windows.mcp.tools import register_all

logger = logging.getLogger(__name__)

_mcp: FastMCP | None = None

INSTRUCTIONS = (
    "agent-windows MCP server: drive the Windows desktop — capture "
    "screenshots and the UIA accessibility tree, ground/detect elements by "
    "vision, and automate mouse/keyboard, apps, files, clipboard, processes, "
    "and the registry. Call `snapshot` first to get pixel-exact element "
    "coordinates and @eN labels; use `ground`/`detect` only for custom-drawn "
    "UI the tree can't see. Tools are serialized through a single daemon "
    "(UIA/COM is apartment-bound), so call them sequentially."
)

VALID_TRANSPORTS = ("stdio", "sse", "streamable-http")


class AuthKeyMiddleware:
    """ASGI middleware: reject requests lacking a valid bearer token.

    Only applied to HTTP transports when an auth_key is configured. A request
    with ``Authorization: Bearer <key>`` matching the configured key is passed
    through; everything else gets 401. Mirrors Windows-MCP's AuthKeyMiddleware.
    """

    def __init__(self, app: Any, auth_key: str) -> None:
        self.app = app
        self._expected = auth_key

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "lifespan"):
            await self.app(scope, receive, send)
            return
        # Lifespan startup must pass through unauthenticated (no request headers).
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        if auth == f"Bearer {self._expected}":
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 401,
                    "headers": [[b"content-length", b"0"],
                                [b"www-authenticate", b"Bearer"]]})
        await send({"type": "http.response.body", "body": b"Unauthorized"})


def build_server() -> FastMCP:
    """Create (once) and return the FastMCP instance with all tools registered."""
    global _mcp
    if _mcp is not None:
        return _mcp
    _mcp = FastMCP(name="agent-windows", instructions=INSTRUCTIONS)
    register_all(_mcp)
    return _mcp


def reset_server() -> None:
    """Drop the memoized instance (for tests)."""
    global _mcp
    _mcp = None


def run(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    debug: bool = False,
    auth_key: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
) -> None:
    """Run the MCP server on the given transport.

    For stdio, the server reads tool calls from stdin and writes to stdout
    (the standard local-client transport; auth_key/ssl are ignored). For
    sse/streamable-http it binds an HTTP server on host:port; when auth_key is
    set, a bearer-token middleware gates every request (see AuthKeyMiddleware).
    ssl_certfile/ssl_keyfile enable HTTPS.
    """
    if transport not in VALID_TRANSPORTS:
        raise ValueError(
            f"transport must be one of {VALID_TRANSPORTS}, got {transport!r}"
        )

    if debug:
        logging.basicConfig(level=logging.DEBUG)
        for name in ("fastmcp", "uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.DEBUG)

    # FastMCP/uvicorn want the Windows selector loop on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    if transport == "stdio":
        import os
        os.environ.setdefault("NO_COLOR", "1")  # keep the stdio stream clean

    mcp = build_server()
    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return

    # HTTP transport: assemble middleware + TLS config.
    from starlette.middleware import Middleware
    middleware: list = []
    if auth_key:
        middleware.append(Middleware(AuthKeyMiddleware, auth_key=auth_key))
    uvicorn_config: dict = {}
    if ssl_certfile and ssl_keyfile:
        uvicorn_config["ssl_certfile"] = ssl_certfile
        uvicorn_config["ssl_keyfile"] = ssl_keyfile
    mcp.run(transport=transport, host=host, port=port, show_banner=False,
            middleware=middleware or None, uvicorn_config=uvicorn_config or None)
