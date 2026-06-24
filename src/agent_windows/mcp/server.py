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
import ipaddress
import logging
import secrets
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


def is_loopback_host(host: str) -> bool:
    """Return True if ``host`` refers only to a loopback interface.

    The footgun guard for bare non-loopback HTTP binds: ``serve`` refuses to
    bind an HTTP transport to a non-loopback address unless an auth_key is set
    or the user explicitly opts in with --allow-insecure-remote. This prevents
    accidental LAN exposure when someone runs ``--host 0.0.0.0`` with no
    auth_key configured — a bare bind there exposes the full MCP toolset
    (screenshots, shell, registry, file I/O) to anyone who can reach the port.
    A hostname that isn't an IP literal (e.g. ``myhost``) is treated as
    non-loopback, matching the reference implementation.
    """
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
        # Compare in constant time: a plain ``==`` short-circuits and leaks the
        # shared-prefix length via response timing. Strip the ``Bearer `` prefix
        # and compare only the token with secrets.compare_digest, matching the
        # reference implementation (Windows-MCP). A wrong scheme or prefix fails
        # the same way a wrong token does — 401.
        token = auth[7:] if auth[:7].lower() == "bearer " else ""
        if token and secrets.compare_digest(token, self._expected):
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 401,
                    "headers": [[b"content-length", b"0"],
                                [b"www-authenticate", b"Bearer"]]})
        await send({"type": "http.response.body", "body": b"Unauthorized"})


def parse_ip_allowlist(raw_entries: list[str]) -> list[ipaddress._BaseNetwork]:
    """Parse a list of IP addresses and CIDR ranges into network objects.

    Accepts individual IPs (e.g. '192.168.1.5') and CIDR ranges
    (e.g. '10.0.0.0/8', '2001:db8::/32'), IPv4 and IPv6. A bare IP becomes a
    /32 (v4) or /128 (v6) network. Raises ``ValueError`` (naming every bad
    entry) if any entry is invalid. Ported from the reference implementation
    (Windows-MCP ``infrastructure/security.py``).
    """
    parsed: list[ipaddress._BaseNetwork] = []
    errors: list[str] = []

    for entry in raw_entries:
        value = entry.strip()
        if not value:
            continue
        try:
            if "/" in value:
                parsed.append(ipaddress.ip_network(value, strict=False))
            else:
                ip = ipaddress.ip_address(value)
                suffix = "/32" if ip.version == 4 else "/128"
                parsed.append(ipaddress.ip_network(f"{ip}{suffix}", strict=False))
        except ValueError as exc:
            errors.append(f"{entry!r}: {exc}")

    if errors:
        raise ValueError("Invalid IP allowlist entries: " + "; ".join(errors))

    return parsed


class IPAllowlistMiddleware:
    """ASGI middleware: restrict inbound HTTP to a set of IP networks (CIDR).

    Applied to HTTP transports when an IP allowlist is configured. Every
    non-``/health`` request is checked: the client address (from the ASGI
    ``scope['client']`` tuple) must fall within one of the configured networks,
    otherwise a 403 is returned. ``/health`` is exempt so health checks work
    regardless of source. Implemented as raw ASGI (like AuthKeyMiddleware) so it
    composes without adding a Starlette ``BaseHTTPMiddleware`` dependency.
    Mirrors Windows-MCP's IPAllowlistMiddleware.
    """

    def __init__(self, app: Any, *, allowlist: list[ipaddress._BaseNetwork]) -> None:
        self.app = app
        self.allowlist = allowlist

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "lifespan"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        # /health is a liveness probe — always reachable so monitors work.
        path = scope.get("path", "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        if client is None:
            await self._forbid(send, "missing client address")
            return
        try:
            client_ip = ipaddress.ip_address(client[0])
        except (ValueError, TypeError):
            await self._forbid(send, f"invalid client address {client[0]!r}")
            return
        if not any(client_ip in net for net in self.allowlist):
            await self._forbid(send, f"{client_ip} is not in the IP allowlist")
            return
        await self.app(scope, receive, send)

    async def _forbid(self, send: Any, reason: str) -> None:
        body = f"Forbidden: {reason}".encode("latin-1")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [[b"content-length", str(len(body)).encode()],
                                [b"content-type", b"text/plain; charset=utf-8"]]})
        await send({"type": "http.response.body", "body": body})


class OptionsMiddleware:
    """ASGI middleware that intercepts OPTIONS preflight requests.

    With an empty allowlist (the default) it returns 200 OK with NO CORS headers,
    so browsers block cross-origin access via Same-Origin Policy — the secure
    default. When an explicit origin allowlist is configured, an OPTIONS request
    carrying a matching ``Origin`` header gets the CORS preflight response
    (allow-origin/methods/headers); a non-matching origin still gets a bare 200
    (no CORS headers), so it's blocked by the browser. Non-OPTIONS requests pass
    through untouched. Mirrors Windows-MCP's OptionsMiddleware.
    """

    def __init__(self, app: Any, *, allowed_origins: list[str] | None = None) -> None:
        self.app = app
        self._allowed: frozenset[str] = frozenset(allowed_origins or [])

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            headers: list[list[bytes]] = [[b"content-length", b"0"]]
            if self._allowed:
                origin = next(
                    (v.decode("latin-1") for k, v in scope.get("headers", [])
                     if k == b"origin"),
                    None,
                )
                if origin and origin in self._allowed:
                    headers += [
                        [b"access-control-allow-origin", origin.encode("latin-1")],
                        [b"access-control-allow-methods", b"GET, POST, OPTIONS"],
                        [b"access-control-allow-headers",
                         b"content-type, authorization, mcp-session-id"],
                        [b"vary", b"Origin"],
                    ]
            await send({"type": "http.response.start", "status": 200,
                        "headers": headers})
            await send({"type": "http.response.body", "body": b""})
            return
        await self.app(scope, receive, send)


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
    ip_allowlist: list | None = None,
    cors_origins: list[str] | None = None,
) -> None:
    """Run the MCP server on the given transport.

    For stdio, the server reads tool calls from stdin and writes to stdout
    (the standard local-client transport; auth_key/ssl are ignored). For
    sse/streamable-http it binds an HTTP server on host:port; when auth_key is
    set, a bearer-token middleware gates every request (see AuthKeyMiddleware).
    ssl_certfile/ssl_keyfile enable HTTPS. ip_allowlist (a list of
    ``ipaddress._BaseNetwork``) further restricts HTTP clients to those
    networks (see IPAllowlistMiddleware); ignored for stdio. cors_origins
    (a list of origin strings) opts in to cross-origin browser access (see
    OptionsMiddleware + CORSMiddleware); empty/None = no CORS headers emitted
    (Same-Origin Policy blocks cross-origin requests). Ignored for stdio.
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
    # OptionsMiddleware is always present: with an empty allowlist (the default)
    # it returns a bare 200 to preflight with no CORS headers, so Same-Origin
    # Policy blocks cross-origin access. It sits outermost so a preflight never
    # reaches the auth/IP gates (preflight requests carry no Authorization
    # header and would otherwise be 401'd before the browser could read the
    # CORS response).
    middleware.append(Middleware(OptionsMiddleware,
                                 allowed_origins=cors_origins or []))
    if cors_origins:
        from starlette.middleware.cors import CORSMiddleware
        middleware.append(Middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "Mcp-Session-Id"],
            allow_credentials=False,
        ))
    if ip_allowlist:
        middleware.append(Middleware(IPAllowlistMiddleware, allowlist=ip_allowlist))
    if auth_key:
        middleware.append(Middleware(AuthKeyMiddleware, auth_key=auth_key))
    uvicorn_config: dict = {}
    if ssl_certfile and ssl_keyfile:
        uvicorn_config["ssl_certfile"] = ssl_certfile
        uvicorn_config["ssl_keyfile"] = ssl_keyfile
    mcp.run(transport=transport, host=host, port=port, show_banner=False,
            middleware=middleware, uvicorn_config=uvicorn_config or None)
