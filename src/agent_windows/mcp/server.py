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

# Paths reachable before authentication. /health is a liveness probe; the OAuth
# endpoints must be reachable so a client can discover metadata and start the
# flow unauthenticated (the protected resources /mcp, tools, etc. are still
# gated). Shared by AuthKeyMiddleware, OAuthOnlyMiddleware and
# IPAllowlistMiddleware so the three never disagree about what's public.
_PUBLIC_PATHS = frozenset({
    "/health",
    "/.well-known/oauth-authorization-server",
    "/oauth/register",
    "/oauth/authorize",
    "/oauth/token",
})


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


def compute_allowed_hosts(
    host: str, allow_insecure_remote: bool = False
) -> list[str] | None:
    """Compute the Host-header allowlist for DNS-rebinding protection.

    Returns a list of trusted hosts (for starlette ``TrustedHostMiddleware``)
    keyed off the bind address, or ``None`` when Host validation should be
    skipped. DNS rebinding lets an attacker point a domain they control at the
    victim's loopback/LAN address — requests then carry the attacker's Host,
    bypassing the browser's same-origin policy. Validating the Host header
    against the addresses the server actually bound to closes that hole.

    Branches (mirrors the reference implementation's ``computed_allowed_hosts``):
    - ``allow_insecure_remote`` → ``None`` (user opted out of this protection).
    - loopback bind → ``["localhost", "127.0.0.1", "[::1]"]``.
    - a named host (not a wildcard address) → ``[host]``.
    - ``0.0.0.0``/``::``/``""`` → ``None`` (too broad to name a trusted set).
    """
    if allow_insecure_remote:
        return None
    if is_loopback_host(host):
        return ["localhost", "127.0.0.1", "[::1]"]
    if host not in ("0.0.0.0", "::", ""):
        return [host]
    return None


class AuthKeyMiddleware:
    """ASGI middleware: reject requests lacking a valid bearer token.

    Only applied to HTTP transports when an auth_key is configured. A request
    with ``Authorization: Bearer <key>`` matching the configured key is passed
    through; otherwise, when an ``oauth_validator`` is provided, a valid OAuth
    access token is also accepted as a fallback. Paths in ``_PUBLIC_PATHS``
    (``/health`` and the OAuth endpoints) are always reachable so a client can
    discover/initiate the OAuth flow unauthenticated. Everything else gets 401.
    Mirrors Windows-MCP's AuthKeyMiddleware.
    """

    def __init__(self, app: Any, *, auth_key: str,
                 oauth_validator: Any = None) -> None:
        self.app = app
        self._expected = auth_key
        self._oauth_validator = oauth_validator

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "lifespan"):
            await self.app(scope, receive, send)
            return
        # Lifespan startup must pass through unauthenticated (no request headers).
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        # Public paths (OAuth endpoints + /health) are reachable before auth:
        # a client must be able to discover metadata and start the flow.
        if scope.get("path", "") in _PUBLIC_PATHS:
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
            scope["audit_auth"] = "auth_key"
            await self.app(scope, receive, send)
            return
        if token and self._oauth_validator and self._oauth_validator(token):
            scope["audit_auth"] = "oauth"
            await self.app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 401,
                    "headers": [[b"content-length", b"0"],
                                [b"www-authenticate", b"Bearer"]]})
        await send({"type": "http.response.body", "body": b"Unauthorized"})


class OAuthOnlyMiddleware:
    """ASGI middleware: authenticate via OAuth Bearer tokens only.

    Used when OAuth is configured but no static ``auth_key`` is. A request
    with a valid (non-expired) OAuth access token — as decided by
    ``oauth_validator`` — passes through; everything else gets 401. Paths in
    ``_PUBLIC_PATHS`` are reachable without a token so the OAuth flow can run.
    """

    def __init__(self, app: Any, *, oauth_validator: Any) -> None:
        self.app = app
        self._oauth_validator = oauth_validator

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "lifespan"):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "lifespan":
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in _PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        auth = headers.get("authorization", "")
        token = auth[7:] if auth[:7].lower() == "bearer " else ""
        if token and self._oauth_validator(token):
            scope["audit_auth"] = "oauth"
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
    request whose path is not in ``_PUBLIC_PATHS`` is checked: the client
    address (from the ASGI ``scope['client']`` tuple) must fall within one of
    the configured networks, otherwise a 403 is returned. Public paths
    (``/health`` + the OAuth endpoints) are exempt — health checks must work
    regardless of source, and the OAuth endpoints must be reachable for a
    remote client so OAuth + IP-allowlist can coexist. Implemented as raw ASGI
    (like AuthKeyMiddleware) so it composes without adding a Starlette
    ``BaseHTTPMiddleware`` dependency. Mirrors Windows-MCP's
    IPAllowlistMiddleware.
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
        # Public paths (health probe + OAuth endpoints) are always reachable:
        # health checks must work regardless of source, and OAuth endpoints
        # must be reachable for a remote client so OAuth + IP-allowlist can
        # coexist (the protected resources /mcp, tools, etc. are still gated).
        if scope.get("path", "") in _PUBLIC_PATHS:
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


def apply_tool_filter(
    mcp: FastMCP,
    explicit_tools: list[str] | None,
    exclude_tools: list[str] | None,
) -> None:
    """Remove disabled tools from the MCP registry.

    ``explicit_tools`` (a whitelist) wins over ``exclude_tools``: when set,
    every registered tool NOT in the list is removed. Otherwise ``exclude_tools``
    removes just the named tools. Unknown names are silently ignored (the tool
    simply isn't there to remove) — this keeps typos from crashing startup.

    This is **irreversible for the process**: it mutates the registry in place,
    and ``build_server()`` memoizes the instance, so a later ``build_server()``
    (e.g. a re-serve in the same process) keeps the filtered set. That matches
    the one-shot server-startup use case; tests call ``reset_server()`` between
    cases.

    FastMCP has changed its internal registry across versions: older releases
    expose ``_tool_manager._tools``; 3.x stores components on
    ``_local_provider._components`` keyed ``"tool:<name>@"`` → ``FunctionTool``
    objects. Both shapes are handled so this survives a FastMCP upgrade.
    Ported/adapted from the reference implementation (Windows-MCP).
    """
    tool_mgr = getattr(mcp, "_tool_manager", None)
    tools_dict = getattr(tool_mgr, "_tools", None)
    if tools_dict is None:
        provider = getattr(mcp, "_local_provider", None)
        components = getattr(provider, "_components", {})
        tools_dict = {
            (getattr(v, "name", None) or k.split(":", 1)[1].split("@", 1)[0]): k
            for k, v in components.items()
            if isinstance(k, str) and k.startswith("tool:")
        }

        def _remove(name: str) -> None:
            keys = [
                k
                for k, v in components.items()
                if isinstance(k, str)
                and k.startswith("tool:")
                and (
                    getattr(components[k], "name", None) == name
                    or k.split(":", 1)[1].split("@", 1)[0] == name
                )
            ]
            for k in keys:
                components.pop(k, None)

        registered = set(tools_dict.keys())
    else:

        def _remove(name: str) -> None:
            tools_dict.pop(name, None)

        registered = set(tools_dict.keys())

    if explicit_tools:
        keep = {t for t in explicit_tools if t in registered}
        for name in registered - keep:
            _remove(name)
    elif exclude_tools:
        for name in exclude_tools:
            if name in registered:
                _remove(name)
    logger.debug("Tool filter applied: explicit=%s exclude=%s",
                 explicit_tools, exclude_tools)


def mcp_server_built() -> bool:
    """True if the memoized MCP singleton exists (a server is/has run).

    Distinguishes "no filter configured" from "filter configured but matched
    nothing" for the batch tool's enforcement gate: when a real server is built,
    enforcement is active even if the registry is empty (an over-restrictive
    ``--tools typo`` should fail CLOSED, not disable enforcement). When no
    server is built (unit tests, library use), batch allows all. (review #7)
    """
    return _mcp is not None


def registered_tool_names(mcp: "FastMCP | None" = None) -> set[str]:
    """Return the set of tool names currently in *mcp*'s registry.

    Defaults to the memoized singleton (``_mcp``). After :func:`apply_tool_filter`
    has run, this is exactly the **allowed** set — the tools that survived the
    ``tools``/``exclude_tools`` filter. Used by the ``batch`` tool to enforce the
    same filter on its sub-commands (review #7): a method excluded at the MCP
    discovery layer must not be reachable via ``batch`` either. Handles both
    FastMCP registry shapes, mirroring :func:`apply_tool_filter`.
    """
    if mcp is None:
        mcp = _mcp
    if mcp is None:
        return set()
    tool_mgr = getattr(mcp, "_tool_manager", None)
    tools_dict = getattr(tool_mgr, "_tools", None)
    if tools_dict is not None:
        return set(tools_dict.keys())
    provider = getattr(mcp, "_local_provider", None)
    components = getattr(provider, "_components", {})
    names: set[str] = set()
    for k in components:
        if isinstance(k, str) and k.startswith("tool:"):
            comp = components[k]
            name = getattr(comp, "name", None) or k.split(":", 1)[1].split("@", 1)[0]
            names.add(name)
    return names


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


def maybe_mount_file_endpoints(
    mcp: Any, *, transport: str, file_transfer: bool, max_upload_mb: int,
) -> None:
    """Mount the /files GET/HEAD/PUT HTTP endpoints on the server.

    No-op on the stdio transport (no HTTP listener) and when ``file_transfer``
    is False (the kill-switch). On an HTTP transport with file transfer on,
    delegates to ``mcp.files.register_file_endpoints``. The endpoints ride the
    same uvicorn listener + ASGI middleware chain (bearer auth, IP allowlist,
    TLS, DNS-rebinding) as the /mcp endpoint — ``/files`` is intentionally NOT
    in ``_PUBLIC_PATHS``, so it inherits authentication like any other route.
    """
    if transport == "stdio":
        return
    if not file_transfer:
        return
    from agent_windows.mcp.files import register_file_endpoints
    register_file_endpoints(mcp, max_upload_mb=max_upload_mb)


def run(
    transport: str = "stdio",
    host: str = "127.0.0.1",
    port: int = 8000,
    auth_key: str | None = None,
    ssl_certfile: str | None = None,
    ssl_keyfile: str | None = None,
    ip_allowlist: list | None = None,
    cors_origins: list[str] | None = None,
    tools: list[str] | None = None,
    exclude_tools: list[str] | None = None,
    stateless_http: bool = False,
    file_transfer: bool = True,
    max_upload_mb: int = 512,
    oauth_client_id: str | None = None,
    oauth_client_secret: str | None = None,
    allowed_hosts: list[str] | None = None,
    telemetry: bool = False,
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
    tools (a whitelist) / exclude_tools (a blacklist) trim the registered tools
    at startup (see apply_tool_filter) — applied to all transports. stateless_http
    runs the streamable-http transport without a Mcp-Session-Id and without
    per-connection session state, so a reconnecting client survives a server
    restart and the server scales behind a load balancer; only meaningful for
    streamable-http, ignored otherwise.
    file_transfer (default True) mounts the HTTP /files GET/HEAD/PUT endpoints —
    the host<->Windows file bridge — on sse/streamable-http. False is a kill-switch
    (no route mounted, /files 404s). Ignored for stdio. ``/files`` is NOT in
    _PUBLIC_PATHS, so it inherits bearer/IP/TLS auth like /mcp. See mcp.files.
    max_upload_mb (default 512, 0 = unlimited) bounds PUT body size for /files;
    oversized uploads are rejected with 413. Ignored for stdio.
    oauth_client_id / oauth_client_secret (when both set) enable the OAuth
    2.0 authorization-code + PKCE flow on HTTP transports: the 4 OAuth
    endpoints are mounted and an OAuth token becomes a valid credential
    (see OAuthOnlyMiddleware, or AuthKeyMiddleware's oauth_validator
    fallback when auth_key is also set). Ignored for stdio.
    allowed_hosts (a list of trusted Host-header values) opts in to DNS-rebinding
    protection via starlette TrustedHostMiddleware: each request's Host header is
    checked against the list, mismatches → 400. Note TrustedHostMiddleware also
    validates websocket Host headers (a tighter check than AuthKey/IPAllowlist,
    which pass websocket scopes through) — this closes DNS rebinding on the WS
    path too. ``None``/empty disables it. Typically computed by the CLI from the
    bind address (see compute_allowed_hosts); ignored for stdio.
    telemetry (default False) opts in to local-only tool-call telemetry (TODO #9):
    each tool call's source, name, success/failure and wall-clock duration are
    appended as one line to ``~/.agent-windows/logs/telemetry.log``. Zero network,
    zero new deps. Off by default; the CLI resolves flag/env/config precedence and
    passes the boolean here. When True, every registered tool is wrapped with a
    timing decorator *after* ``apply_tool_filter`` (so filtered tools aren't
    wrapped); applies to all transports, including stdio.
    """
    if transport not in VALID_TRANSPORTS:
        raise ValueError(
            f"transport must be one of {VALID_TRANSPORTS}, got {transport!r}"
        )

    # Debug logging (TODO #21-3): reads the global flag the entry point set
    # via enable_debug(); no-op unless --debug was given. Consolidates what was
    # inline logging.basicConfig here + honors the --no-daemon path too.
    from agent_windows.utils.debug import configure_logging
    configure_logging()

    # FastMCP/uvicorn want the Windows selector loop on Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    if transport == "stdio":
        import os
        os.environ.setdefault("NO_COLOR", "1")  # keep the stdio stream clean

    mcp = build_server()
    if tools or exclude_tools:
        apply_tool_filter(mcp, tools, exclude_tools)
    if telemetry:
        # Wrap every surviving tool with local-only telemetry timing. Imported
        # lazily so the fastmcp-optional CLI path never pays for it; runs after
        # apply_tool_filter so removed tools aren't wrapped. (TODO #9)
        from agent_windows.telemetry import apply_tool_telemetry
        apply_tool_telemetry(mcp, True)
    if transport == "stdio":
        mcp.run(transport="stdio", show_banner=False)
        return

    # HTTP transport: assemble middleware + TLS config.
    from starlette.middleware import Middleware

    # OAuth (HTTP only): mount the 4 endpoints and build a token validator
    # used as an auth fallback. No persistence — the store lives in this
    # process and is lost on restart (clients re-authenticate).
    oauth_validator = None
    if oauth_client_id and oauth_client_secret:
        from agent_windows.mcp.oauth import (
            OAuthStore, build_oauth_routes, validate_oauth_token,
        )
        _oauth_store = OAuthStore()
        scheme = "https" if (ssl_certfile and ssl_keyfile) else "http"
        _issuer = f"{scheme}://{host}:{port}"
        for path, (handler, methods) in build_oauth_routes(
            _oauth_store, _issuer, oauth_client_id, oauth_client_secret,
        ).items():
            mcp.custom_route(path, methods=methods)(handler)
        oauth_validator = lambda tok: validate_oauth_token(_oauth_store, tok)  # noqa: E731

    # /files HTTP endpoints (host<->Windows file bridge). HTTP transport only;
    # they reuse the same listener + auth/IP/TLS middleware chain as /mcp.
    # file_transfer=False is the kill-switch. /files is not in _PUBLIC_PATHS, so
    # it inherits auth. mcp.files is imported lazily here (never on the stdio/CLI
    # cold path).
    maybe_mount_file_endpoints(mcp, transport=transport,
                               file_transfer=file_transfer,
                               max_upload_mb=max_upload_mb)

    middleware: list = []
    # OptionsMiddleware is always present: with an empty allowlist (the default)
    # it returns a bare 200 to preflight with no CORS headers, so Same-Origin
    # Policy blocks cross-origin access. It sits outermost so a preflight never
    # reaches the auth/IP gates (preflight requests carry no Authorization
    # header and would otherwise be 401'd before the browser could read the
    # CORS response).
    middleware.append(Middleware(OptionsMiddleware,
                                 allowed_origins=cors_origins or []))
    # DNS-rebinding protection: validate the Host header against the addresses
    # the server bound to. Sits after OptionsMiddleware (preflight must clear
    # first, since it carries no Authorization header) and before CORSMiddleware
    # (a legitimate cross-origin preflight's Host is always the target server,
    # so it is never wrongly rejected here). A named host bind names a trusted
    # set; a 0.0.0.0/:: bind can't, and the CLI passes None then.
    if allowed_hosts:
        from starlette.middleware.trustedhost import TrustedHostMiddleware
        middleware.append(Middleware(
            TrustedHostMiddleware, allowed_hosts=allowed_hosts))
    if cors_origins:
        from starlette.middleware.cors import CORSMiddleware
        middleware.append(Middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "Authorization", "Mcp-Session-Id"],
            # MCP clients authenticate via the Authorization header, not cookies;
            # credentialed cross-origin requests aren't needed (and enabling them
            # would widen the surface), so credentials stay off.
            allow_credentials=False,
        ))
    if ip_allowlist:
        middleware.append(Middleware(IPAllowlistMiddleware, allowlist=ip_allowlist))
    if auth_key:
        middleware.append(Middleware(
            AuthKeyMiddleware, auth_key=auth_key, oauth_validator=oauth_validator))
    elif oauth_validator:
        middleware.append(Middleware(OAuthOnlyMiddleware, oauth_validator=oauth_validator))
    # Audit caller attribution is read from the LIVE per-request HTTP scope at
    # tool-dispatch time in mcp/_call.py (via FastMCP's get_http_request), NOT
    # via a middleware-set contextvar: the contextvar is copied at the
    # session-creating task spawn and would be stale for later requests on the
    # same stateful streamable-http session. The auth middlewares tag
    # scope["audit_auth"] on their success branches; _resolve_audit_caller
    # reads that tag + scope["client"] per-request. (audit)
    uvicorn_config: dict = {}
    if ssl_certfile and ssl_keyfile:
        uvicorn_config["ssl_certfile"] = ssl_certfile
        uvicorn_config["ssl_keyfile"] = ssl_keyfile
    # The middleware list is always non-empty for HTTP (OptionsMiddleware is
    # always added); stdio ignores the middleware kwarg entirely.
    # stateless_http is forwarded only for HTTP (FastMCP's stdio run doesn't
    # accept the kwarg); it suppresses the Mcp-Session-Id + per-connection
    # session state so reconnecting clients survive a restart.
    mcp.run(transport=transport, host=host, port=port, show_banner=False,
            middleware=middleware, uvicorn_config=uvicorn_config or None,
            stateless_http=stateless_http)
